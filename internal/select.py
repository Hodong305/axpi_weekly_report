"""S5 자동 편성(설계서 9.5) → review/{issue_week}-selection.csv."""
import csv

from .common import add_flag, evidence_text, trunc
from .events import _norm, ledger_label
from .score import METRICS

CSV_COLUMNS = ["rank", "slot", "event_id", "total", "placement_cap", "title_draft", "claim", "stage", "evidence",
               "ledger", "flags", "note"]


def _tie_key(ev):
    t = ev["score"]["tiers"]
    return (ev["score"]["total"], t["fab_relevance"], t["evidence_level"], t["weekly_change"])


def sort_candidates(events):
    """총점 → FAB 관련성 → 근거 수준 → 이번 주 변화 순(내림차순). 같으면 event_id 순으로 고정한다."""
    return sorted(events, key=lambda e: tuple(-v for v in _tie_key(e)) + (e["event_id"],))


def select(events, cfg):
    """사건의 slot·rank·flags를 정한다(제자리 수정). 반환: 요약 dict."""
    core_min, core3_min, brief_min = cfg["core_min_score"], cfg["core3_min_score"], cfg["brief_min_score"]
    for ev in events:
        if ev["status"] == "HOLD":
            ev["slot"], ev["rank"] = "hold", None
        elif ev["status"] == "EXCLUDE":
            ev["slot"], ev["rank"] = "out", None
    cands = [e for e in events if e["status"] == "PASS" and e["score"]["total"] is not None]
    for e in cands:
        if e["score"]["total"] < brief_min:
            e["slot"], e["rank"], e["note"] = "out", None, f"점수 미달({e['score']['total']}<{brief_min})"
    cands = sort_candidates([e for e in cands if e["score"]["total"] >= brief_min])

    # 핵심: 게재 상한이 단신인 사건은 점수와 관계없이 뺀다. core_min 이상 상위 2건, 3위도 core3_min 이상이면 3건.
    # 같은 주체는 핵심+단신을 합쳐 same_subject_max건까지이며, 넘는 사건은 점수가 낮은 쪽부터 빠진다(점수순으로 채우므로 자연히 낮은 쪽이 빠짐).
    limit = cfg.get("same_subject_max") or 99
    count, subj_skipped = {}, set()

    def room(e):
        k = _norm(e["subject"]) or e["event_id"]
        if count.get(k, 0) >= limit:
            subj_skipped.add(e["event_id"])
            return False
        return True

    def take(e):
        k = _norm(e["subject"]) or e["event_id"]
        count[k] = count.get(k, 0) + 1

    pool = [e for e in cands if e["cap"] != "brief" and e["score"]["total"] >= core_min]
    core = []
    for e in pool:
        if not room(e):
            continue
        if len(core) < 2 or (len(core) == 2 and e["score"]["total"] >= core3_min):
            core.append(e)
            take(e)
        else:
            break
    # 단신: 남은 brief_min 이상 중 최대 brief_max건(핵심 3건이면 brief_max-1건). 산업 배경은 최대 1건.
    max_brief = cfg["brief_max"] if len(core) < 3 else cfg["brief_max"] - 1
    brief, background, bg_skipped = [], 0, set()
    for e in cands:
        if e in core:
            continue
        if len(brief) >= max_brief:
            break
        if not room(e):
            continue
        if e["score"]["tiers"]["fab_relevance"] == 10:
            if background >= 1:
                bg_skipped.add(e["event_id"])      # 산업 배경 1건 한도에 걸려 건너뜀
                continue
            background += 1
        brief.append(e)
        take(e)
    chosen = core + brief
    for e in cands:
        if e not in chosen:
            e["slot"], e["rank"] = "out", None
            e["note"] = ("같은 주체 한도" if e["event_id"] in subj_skipped else
                         "편성 한도 초과(산업 배경은 1건)" if e["event_id"] in bg_skipped else "편성 한도 초과")
    for i, e in enumerate(core, 1):
        e["slot"], e["rank"] = "core", i
    for i, e in enumerate(brief, len(core) + 1):
        e["slot"], e["rank"] = "brief", i
    # 동점: 비교 키가 모두 같은 사건이 편성 대상에 있으면 표시한다(편집자가 MTC 과제와의 연관성으로 정한다).
    for e in cands:
        same = [o for o in cands if _tie_key(o) == _tie_key(e)]
        if len(same) > 1 and any(o in chosen for o in same):
            add_flag(e, "동점")
    return {"candidates": len(chosen), "core": len(core), "brief": len(brief)}


def core_eligible(events, cfg):
    """핵심 자격: core_min_score 이상이면서 게재 상한이 단신이 아닌 사건(8장 분모 정의)."""
    return [e for e in events if e["status"] == "PASS" and e["score"] and e["score"]["total"] is not None
            and e["score"]["total"] >= cfg["core_min_score"] and e["cap"] != "brief" and e["slot"] in ("core", "brief")]


def issue_type(events, cfg):
    """8장: 정규호 / 축약호 / 현황호. 게재 가능 사건은 편성(핵심+단신)된 사건이다."""
    published = [e for e in events if e["slot"] in ("core", "brief")]
    if not published:
        return "현황호"
    if len(published) >= 3 and core_eligible(events, cfg):
        return "정규호"
    return "축약호"


def evidence_excerpt(ev, records):
    sc = ev.get("score")
    if sc and sc["evidence"]:
        text = evidence_text(sc["evidence"][0]["id"], records)
        if text:
            return trunc(text, 200)
    rec = records[ev["rep_doc_id"]]
    lead = rec["body"]["lead_ids"]
    blocks = {b["id"]: b["text"] for b in rec["body"]["blocks"]}
    return trunc(blocks.get(lead[0], rec["title"]) if lead else rec["title"], 200)


def ordered(events):
    """slot 순서(core, brief, hold, out)와 rank로 정렬한 사건 목록."""
    order = {"core": 0, "brief": 1, "hold": 2, "out": 3}
    return sorted(events, key=lambda e: (order.get(e["slot"], 4), e["rank"] or 9999, e["event_id"]))


def write_csv(path, events, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for e in ordered(events):
            sc = e.get("score")
            w.writerow({"rank": e["rank"] or "", "slot": e["slot"], "event_id": e["event_id"],
                        "total": sc["total"] if sc and sc["total"] is not None else "",
                        "placement_cap": e["cap"] or "", "title_draft": e["title_draft"],
                        "claim": sc["claim"] if sc else "", "stage": sc["maturity"] if sc else "",
                        "evidence": evidence_excerpt(e, records), "ledger": ledger_label(e) if e["kind"] == "event" else "NEW",
                        "flags": ";".join(e["flags"]), "note": e["note"]})


TOPIC_ORDER = ["T1_CHIPMAKER", "T2_EQUIPMENT", "T3_SOLUTION", "T4_PHYSICAL_AI", "B"]


def more_candidates(events, fit, records, cfg):
    """'더 볼 거리' 후보(9.6): 편성되지 않은 PASS 사건(점수 무관)과, 본문 재분류에서 한 분야 이상 1을 받았거나 WATCH_TRANSFER인 보류 자료.
    제외(EXCLUDE) 자료는 넣지 않는다. 순서: 분야(T1→T4, 그다음 B 경로 전이 사례) 안에서 점수 높은 순, 점수가 없으면 발행일 최신 순.
    반환: (항목 목록, more_max를 넘어 빠진 건수). 항목: {ref, topic, doc_id, title, event_id, score}"""
    out = []
    for e in events:
        rec = records[e["rep_doc_id"]]
        if e["kind"] == "event" and e["status"] == "PASS" and e["slot"] == "out":
            transfer = "B 경로 전이 사례" in e.get("cap_reasons", [])
            out.append((e, rec, e["primary_topic"], transfer, e["score"]["total"] if e.get("score") else None))
        elif e["kind"] == "hold_doc" and e["slot"] == "hold":
            f = fit.get(e["rep_doc_id"], {})
            st2 = f.get("stage2") or f.get("stage1") or {}
            signal = "1" in (st2.get("fit") or {}).values() or f.get("route") == "WATCH_TRANSFER"
            if signal:
                one = next((t for t, v in (st2.get("fit") or {}).items() if v == "1"), None)
                out.append((e, rec, one, f.get("route") == "WATCH_TRANSFER", None))
    def key(t):
        e, rec, topic, transfer, score = t
        grp = TOPIC_ORDER.index("B") if transfer else (TOPIC_ORDER.index(topic) if topic in TOPIC_ORDER else len(TOPIC_ORDER))
        date = rec.get("published_date") or ""
        # 점수가 있는 항목이 먼저(높은 순), 없으면 발행일 최신 순
        return (grp, 0 if score is not None else 1, -(score or 0), "".join(chr(255 - ord(c)) for c in date), e["event_id"])
    out.sort(key=key)
    overflow = max(0, len(out) - cfg["more_max"])
    items = [{"ref": rec["doc_id"] if e["kind"] == "hold_doc" else e["event_id"], "topic": "B" if transfer else (topic or ""), "doc_id": rec["doc_id"], "title": rec["title"],
              "event_id": e["event_id"], "score": score} for e, rec, topic, transfer, score in out[:cfg["more_max"]]]
    return items, overflow
