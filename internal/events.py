"""S3 사건 묶음과 사건 대장 대조(설계서 7.2). 대장 파일은 S8에서만 갱신한다."""
import json
import re
from pathlib import Path

from .common import add_flag, evidence_errors, representative, short_week, trunc, week_tag
from .llm import AutoJudgeFailed
from .settings import TOPICS

LEDGER_FIELDS = ["event_key", "subject", "site_task", "primary_topic", "stage", "claim", "key_facts",
                 "doc_ids", "placement", "issues", "open_questions", "corrections"]
VERDICTS = ["NEW", "SAME_NO_CHANGE", "SAME_WITH_CHANGE"]

GROUP_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["groups"],
    "properties": {"groups": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["doc_ids", "subject", "site_task", "title_draft", "primary_topic"],
        "properties": {"doc_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                       "subject": {"type": "string", "minLength": 1}, "site_task": {"type": "string", "minLength": 1},
                       "title_draft": {"type": "string", "minLength": 1}, "primary_topic": {"enum": TOPICS}}}}}}

LEDGER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "matched_event_key", "uncertain", "reason", "changes", "evidence_ids"],
    "properties": {"verdict": {"enum": VERDICTS}, "matched_event_key": {"type": ["string", "null"]},
                   "uncertain": {"type": "boolean"}, "reason": {"type": "string"},
                   "changes": {"type": "array", "items": {"type": "string"}},
                   "evidence_ids": {"type": "array", "items": {"type": "string"}}}}


# 대장 파일 -----------------------------------------------------------------

def load_ledger(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def save_ledger(path, entries):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps({k: e.get(k) for k in LEDGER_FIELDS}, ensure_ascii=False) + "\n" for e in entries),
                   encoding="utf-8")
    tmp.replace(p)


def _norm(s):
    return re.sub(r"[\W_]+", "", (s or "").casefold())


def ledger_candidates(entries, subject, doc_ids, issue_week, lookback):
    """최근 lookback개 호의 대장 항목 중 subject가 같거나 doc_ids가 겹치는 것(코드, 7.2).
    '최근 호'는 대장에 기록된 호(이번 호 제외) 중 가장 최근 lookback개로 본다."""
    issues = sorted({i for e in entries for i in (e.get("issues") or []) if i != issue_week})
    recent = set(issues[-lookback:]) if lookback else set()
    subj = _norm(subject)
    out = []
    for e in sorted(entries, key=lambda x: x["event_key"]):
        if not recent & set(e.get("issues") or []):
            continue
        if (subj and _norm(e.get("subject")) == subj) or set(e.get("doc_ids") or []) & set(doc_ids):
            out.append(e)
    return out


# 사건 묶음 -----------------------------------------------------------------

def _group_validator(doc_ids):
    expected = set(doc_ids)

    def validate(obj):
        seen, errs = [], []
        for g in obj["groups"]:
            seen += g["doc_ids"]
        unknown = [d for d in seen if d not in expected]
        dup = sorted({d for d in seen if seen.count(d) > 1})
        missing = sorted(expected - set(seen))
        if unknown:
            errs.append(f"입력에 없는 doc_id: {unknown[:3]}")
        if dup:
            errs.append(f"둘 이상의 사건에 들어간 doc_id: {dup[:3]}")
        if missing:
            errs.append(f"어느 사건에도 없는 doc_id: {missing[:3]}")
        return errs
    return validate


def propose_groups(judge, recs, title_max):
    """모델이 묶음을 제안하고 코드가 모든 문서가 정확히 한 묶음에 있는지 검증한다.
    두 번 실패하면 문서마다 개별 사건으로 둔다(가장 보수적인 처리: 서로 다른 사건을 합치지 않는다)."""
    ids = [r["doc_id"] for r in recs]
    if not recs:
        return [], False
    user = "\n".join(judge.source(r, set(r["body"]["lead_ids"])) for r in recs)
    try:
        obj = judge.ask("s3_group", user, GROUP_SCHEMA, _group_validator(ids))
        groups = obj["groups"]
        ok = True
    except AutoJudgeFailed:
        by = {r["doc_id"]: r for r in recs}
        groups = [{"doc_ids": [d], "subject": by[d]["publisher"], "site_task": trunc(by[d]["title"], 40),
                   "title_draft": trunc(by[d]["title"], title_max), "primary_topic": None} for d in ids]
        ok = False
    for g in groups:
        g["title_draft"] = trunc(g["title_draft"], title_max)
    return groups, ok


def new_event(event_id, kind, doc_ids, rep_id, subject, site_task, title_draft, topic):
    return {"event_id": event_id, "kind": kind, "doc_ids": sorted(doc_ids), "rep_doc_id": rep_id, "subject": subject,
            "site_task": site_task, "title_draft": title_draft, "primary_topic": topic, "flags": [], "note": "",
            "status": "PENDING", "score": None, "slot": None, "rank": None, "cap": None, "cap_reasons": [],
            "cap_override": False,
            "ledger": {"verdict": "NEW", "key": None, "issue": None, "uncertain": False, "reason": "",
                       "changes": [], "ref_key": None}}


def build_events(judge, records, fit, issue_week, title_max):
    """ADVANCE 문서를 사건으로 묶고, 판단 보류·제외 문서는 대장에 남길 개별 항목으로 만든다."""
    advanced = [records[d] for d, f in sorted(fit.items()) if f["decision"] == "ADVANCE"]
    groups, ok = propose_groups(judge, advanced, title_max)
    from .common import pub_sort_key
    items = []      # (대표 자료, 종류, 묶음)
    for g in groups:
        recs = [records[d] for d in g["doc_ids"]]
        items.append((representative(recs), "event", g))
    for d, f in sorted(fit.items()):
        if f["decision"] != "ADVANCE":
            items.append((records[d], "hold_doc" if f["decision"] == "HOLD" else "excluded_doc", f))
    # event_key 번호: 사건 대표 자료의 (발행 시각, doc_id) 순(9.10). 보류·제외 사건도 같은 순서로 번호를 받는다.
    items.sort(key=lambda t: (pub_sort_key(t[0]), t[0]["doc_id"]))
    events = []
    for n, (rep, kind, g) in enumerate(items, 1):
        eid = f"EVT-{week_tag(issue_week)}-{n:02d}"
        if kind == "event":
            topic = g["primary_topic"] or next((t for t in TOPICS if fit[rep["doc_id"]]["stage1"]["fit"].get(t) == "2"), TOPICS[0])
            ev = new_event(eid, "event", g["doc_ids"], rep["doc_id"], g["subject"], g["site_task"], g["title_draft"], topic)
            if not ok:
                add_flag(ev, "묶음 자동 판정 실패")
        else:
            held = kind == "hold_doc"
            ev = new_event(eid, kind, [rep["doc_id"]], rep["doc_id"], rep["publisher"], trunc(rep["title"], 40),
                           trunc(rep["title"], title_max), None)
            ev["status"] = "HOLD" if held else "EXCLUDE"
            ev["slot"] = "hold" if held else "out"
            ev["note"] = g.get("note", "")
            for fl in g.get("flags", []):
                add_flag(ev, fl)
        events.append(ev)
    return events


# 대장 대조 -------------------------------------------------------------------

def _ledger_validator(event, cand_keys, records):
    def validate(obj):
        errs = []
        if obj["verdict"] != "NEW" and obj["matched_event_key"] not in cand_keys:
            errs.append(f"matched_event_key가 대장 후보에 없음: {obj['matched_event_key']}")
        errs += evidence_errors(obj["evidence_ids"], records, set(event["doc_ids"]))
        return errs
    return validate


def judge_ledger(judge, event, cands, records):
    """후보 대장 항목과 비교해 판정한다. 결과는 event['ledger']에 기록한다."""
    led = event["ledger"]
    if not cands:
        return
    keys = {c["event_key"] for c in cands}
    by_key = {c["event_key"]: c for c in cands}
    user = ("[이번 주 사건]\n" + json.dumps({"event_id": event["event_id"], "subject": event["subject"],
                                           "site_task": event["site_task"]}, ensure_ascii=False)
            + "\n[대장 후보 항목]\n" + json.dumps([{k: c.get(k) for k in LEDGER_FIELDS} for c in cands], ensure_ascii=False)
            + "\n" + "\n".join(judge.source(_rec(records, d)) for d in event["doc_ids"]))
    try:
        r = judge.ask("s3_ledger", user, LEDGER_SCHEMA, _ledger_validator(event, keys, records))
    except AutoJudgeFailed:
        # 판정 근거 부족 → SAME_WITH_CHANGE + 중복 의심(7.2)
        r = {"verdict": "SAME_WITH_CHANGE", "matched_event_key": cands[0]["event_key"], "uncertain": True,
             "reason": "자동 판정 실패", "changes": [], "evidence_ids": []}
    verdict, key = r["verdict"], r["matched_event_key"]
    if r["uncertain"] and verdict != "NEW":
        verdict = "SAME_WITH_CHANGE"
    elif r["uncertain"] and verdict == "NEW":
        verdict, key = "SAME_WITH_CHANGE", cands[0]["event_key"]
    if verdict != "NEW":
        entry = by_key[key]
        if entry.get("placement") in ("hold", "excluded"):
            # 독자가 본 적 없는 사건 → NEW로 다룬다. 기존 기록은 참고용으로만 남긴다.
            led.update(verdict="NEW", ref_key=key, reason=r["reason"])
            return
        led.update(verdict=verdict, key=key, issue=max(entry["issues"]) if entry.get("issues") else None, uncertain=r["uncertain"],
                   reason=r["reason"], changes=r["changes"], entry=entry)
        if r["uncertain"]:
            add_flag(event, "중복 의심")
    else:
        led.update(reason=r["reason"])


def _rec(records, doc_id):
    return records[doc_id]


def match_ledger(judge, events, records, entries, issue_week, lookback):
    for ev in events:
        if ev["kind"] != "event":
            continue
        cands = ledger_candidates(entries, ev["subject"], ev["doc_ids"], issue_week, lookback)
        judge_ledger(judge, ev, cands, records)
        if ev["ledger"]["verdict"] == "SAME_NO_CHANGE":
            ev.update(status="EXCLUDE", slot="out", note="NO_WEEKLY_CHANGE")


def ledger_label(ev):
    """selection.csv·검토 요청의 대장 표시."""
    led = ev["ledger"]
    if led["verdict"] == "SAME_NO_CHANGE":
        return "중복"
    if "중복 의심" in ev["flags"]:
        return "중복 의심"
    return "후속" if led["verdict"] == "SAME_WITH_CHANGE" else "NEW"


def ledger_display(ev):
    led = ev["ledger"]
    if led["verdict"] == "SAME_WITH_CHANGE" and led.get("issue"):
        return f"후속({short_week(led['issue'])})"
    return "NEW" if led["verdict"] == "NEW" else ("중복" if led["verdict"] == "SAME_NO_CHANGE" else "후속")


# 대장 갱신(S8, 발송 직후에만) ---------------------------------------------------

def update_ledger(entries, events, issue_week, corrections=()):
    """이번 호 결과를 대장에 반영한 새 목록을 반환한다. 같은 호를 다시 반영해도 결과가 같다(멱등).
    - 새 사건(NEW, 판단 보류·제외 자료 포함): 항목 추가(placement: core/brief/hold/excluded)
    - SAME_NO_CHANGE: 근거 doc_id만 추가
    - SAME_WITH_CHANGE: doc_id 추가, 호 목록 갱신, 게재됐으면 stage·claim·key_facts·placement 갱신"""
    entries = [dict(e) for e in entries]
    by_key = {e["event_key"]: e for e in entries}
    for ev in events:
        led, sc = ev["ledger"], ev.get("score")
        published = ev["slot"] in ("core", "brief")
        ent = by_key.get(led.get("key")) if ev["kind"] == "event" and led["verdict"] != "NEW" else None
        if ent is not None:
            ent["doc_ids"] = sorted(set(ent["doc_ids"]) | set(ev["doc_ids"]))
            if led["verdict"] == "SAME_WITH_CHANGE":
                ent["issues"] = sorted(set(ent["issues"]) | {issue_week})
                if published and sc:
                    ent.update(stage=sc["maturity"], claim=sc["claim"], key_facts=sc["fact"], placement=ev["slot"])
            continue
        entry = {"event_key": ev["event_id"], "subject": ev["subject"], "site_task": ev["site_task"],
                 "primary_topic": ev["primary_topic"], "stage": sc["maturity"] if sc else "미확인",
                 "claim": sc["claim"] if sc else ev["title_draft"], "key_facts": list(sc["fact"]) if sc else [],
                 "doc_ids": sorted(ev["doc_ids"]),
                 "placement": ev["slot"] if published else ("hold" if ev["slot"] == "hold" else "excluded"),
                 "issues": [issue_week], "open_questions": list(sc["unresolved"]) if sc else [], "corrections": []}
        if ev["event_id"] in by_key:
            old = by_key[ev["event_id"]]
            entry["corrections"] = old.get("corrections") or []
            entries[entries.index(old)] = entry
        else:
            entries.append(entry)
        by_key[entry["event_key"]] = entry
    for c in corrections:
        ent = by_key.get(c.get("event_key"))
        if ent is not None and c not in (ent.get("corrections") or []):
            ent["corrections"] = list(ent.get("corrections") or []) + [c]
    return entries
