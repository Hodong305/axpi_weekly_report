"""v4.2 수집 결과(intake)를 v4.5 형식으로 이관한다. 입력 파일의 내용은 데이터로만 다룬다.
  python tools/migrate_v42.py OLD.json [OLD2.json ...] --issue-week 2026-W48 [--root .]
여러 파일은 doc_id로 합친다(같은 doc_id는 하나만 두고 attempts를 합치며, 본문이 다르면 긴 쪽을 쓴다). 같은 발행처·같은 본문 해시는 별칭 통합(설계서 4.4).
out/{issue_week}/에 merged·intake·report(r1)를 써서 `python -m collector package`가 그대로 동작하게 한다.
원문을 다시 가져오지 않는다: 본문은 v4.2 수집기(LLM) 산출물이며 원문 바이트와 대조되지 않았다."""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.fetch import now_kst  # noqa: E402
from collector.ref import (assign_ids, attribute, body_digest, document_key, lead_ids, normalize_text, to_ascii_uri,  # noqa: E402
                           weekly_window)
from collector.settings import TOPIC_CODES, file_name, load_settings  # noqa: E402
from collector.validate import check, summarize  # noqa: E402

TOPIC_MAP = {"chipmaker": "T1_CHIPMAKER", "equipment": "T2_EQUIPMENT", "solution": "T3_SOLUTION", "physical_ai": "T4_PHYSICAL_AI"}
AGENTS = {"script", "claude", "gpt", "gemini", "manual"}
KINDS = {"paragraph", "heading", "caption", "note", "table"}
SCOPES = {"article", "pdf", "video_description", "transcript", "unknown"}
NOTE = "v4.2 이관(원문 대조 미검증)"
MIGRATION_OBS = "v4.2 → v4.5 이관. 본문은 v4.2 수집기(LLM) 산출물이며 원문 바이트 대조 미검증"


def guess_source_type(settings, url, publisher):
    """반환: (source_type, 근거 설명). sources.yaml의 발행처가 우선이고, 없으면 호스트 휴리스틱."""
    src = settings.source_for_url(url)
    if src:
        return src["source_type"], f"sources.yaml({src['id']})"
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(".go.kr") or host == "go.kr":
        if "표준" in (publisher or ""):
            return "standards_body", "휴리스틱: *.go.kr + 발행처에 '표준' 포함"
        return "other", "휴리스틱: *.go.kr(정부), 발행처에 '표준' 없음"
    if host.endswith(".europa.eu") or host == "europa.eu":
        return "other", "휴리스틱: EU 기관"
    return "other", "휴리스틱: 해당 규칙 없음"


def migrate_blocks(old_blocks, lead_min):
    blocks = []
    for b in old_blocks:
        kind = b.get("kind") if b.get("kind") in KINDS else "paragraph"
        text = normalize_text(str(b.get("text", "")), keep_newlines=(kind == "table"))
        if text:
            page = b.get("page")
            blocks.append({"id": "", "kind": kind, "text": text, "page": page if isinstance(page, int) and page >= 1 else None})
    assign_ids(blocks)
    n, sha = body_digest(blocks)
    return blocks, lead_ids(blocks, lead_min), n, sha


def migrate_record(settings, r, start, end, now):
    """반환: (새 레코드 또는 None, 보고 행 dict)"""
    row = {"old_doc_id": r.get("doc_id"), "title": r.get("title"), "note": ""}
    try:
        src = to_ascii_uri(r["source_url"])
        norm, did = document_key(src)
    except (ValueError, KeyError, UnicodeError) as e:
        row.update(result="중단", note=f"URL 재계산 실패: {e}")
        return None, row
    row["doc_id_equal"] = (did == r.get("doc_id") and norm == r.get("normalized_url"))
    row["new_doc_id"] = did
    if not row["doc_id_equal"]:
        row.update(result="중단", note="doc_id·normalized_url 재계산 값이 구판과 다름 — 레코드를 이관하지 않음")
        return None, row
    lead_min = settings.c["extraction"]["lead_min_chars"]
    min_chars = settings.c["extraction"]["min_body_chars"]
    ob = r.get("body", {})
    blocks, leads, nbytes, sha = migrate_blocks(ob.get("blocks", []), lead_min)
    agents = sorted({a.get("collector") if a.get("collector") in AGENTS else "manual" for a in r.get("attempts", [])}) or ["manual"]
    main_agent = agents[0]
    stype, why = guess_source_type(settings, norm, r.get("publisher"))
    scope = ob.get("scope") if ob.get("scope") in SCOPES else "unknown"
    note = ob.get("scope_note")
    attempts = [{"pass": a.get("pass") if a.get("pass") in (1, 2) else 1, "agent": "manual", "at": a.get("at") or now,
                 "url": a.get("url") or norm, "http_status": None,
                 "observation": f"[v4.2 이관, 원 수집자 {a.get('collector')}] {a.get('observation') or '(기록 없음)'}"}
                for a in r.get("attempts", [])]
    attempts.append({"pass": 1, "agent": "manual", "at": now, "url": norm, "http_status": None, "observation": MIGRATION_OBS})
    uniq = {(a["at"], a["pass"], a["agent"], a["url"], a["observation"]): a for a in attempts}
    attempts = [uniq[k] for k in sorted(uniq)]
    topics = list(dict.fromkeys(TOPIC_MAP.get(t, t) for t in r.get("topic_hints", []) if TOPIC_MAP.get(t, t) in TOPIC_CODES))
    rec = {"doc_id": did, "source_url": src, "normalized_url": norm, "title": r.get("title"), "publisher": r.get("publisher"),
           "author": None, "language": r.get("language"), "source_type": stype, "published_date": r.get("published_date"),
           "published_at": r.get("published_at"), "change_at": r.get("change_at"), "change_note": r.get("change_note"),
           "discovery": sorted([{"route": "llm_search", "agent": a, "source_id": None, "query": None} for a in agents],
                               key=lambda d: d["agent"]),
           "event_key": None, "track": r.get("track") if r.get("track") in ("A", "B") else "A", "topic_hints": topics,
           "source_tier": r.get("source_tier") if r.get("source_tier") in ("primary", "named_secondary", "unverified") else "unverified",
           "body": {"scope": scope, "extent": "full" if blocks else "none",
                    "scope_note": f"{note}; {NOTE}" if note else NOTE, "extractor": f"legacy v4.2 ({main_agent})",
                    "blocks": blocks, "lead_ids": leads, "utf8_bytes": nbytes, "sha256": sha},
           "status": "complete", "reason_codes": [], "attempts": attempts}
    where = attribute(rec["published_at"], rec["published_date"], start, end)
    chars = sum(len(b["text"]) for b in blocks)
    if where == "OLD":
        rec.update(status="drop", reason_codes=["OUT_OF_WINDOW"])
    elif where == "FUTURE":
        row.update(result="제외", note="기간 이후 자료(FUTURE): 레코드를 만들지 않음")
        return None, row
    elif not blocks:
        rec.update(status="partial", reason_codes=["BODY_MISSING"])
    elif not (rec["title"] and rec["publisher"] and rec["language"]):
        rec.update(status="partial", reason_codes=["BODY_MISSING"])
    elif where == "UNKNOWN":
        rec.update(status="partial", reason_codes=["DATE_UNCERTAIN"])
    elif chars < min_chars:
        rec.update(status="partial", reason_codes=["BODY_TRUNCATED"])
    elif not leads:
        rec.update(status="partial", reason_codes=["BODY_MISSING"])
    if rec["status"] != "complete" and not blocks:
        rec["body"]["extent"] = "none"
    elif rec["status"] == "partial":
        rec["body"]["extent"] = "partial"
    row.update(result=rec["status"] + (f" {rec['reason_codes']}" if rec["reason_codes"] else ""), source_type=stype, source_type_why=why,
               old_leads=ob.get("lead_ids"), new_leads=leads, old_sha=ob.get("sha256"), new_sha=sha,
               hash_changed=ob.get("sha256") != sha, old_event_key=r.get("event_key"), old_blocks=len(ob.get("blocks", [])),
               new_blocks=len(blocks), old_date=r.get("published_date"))
    return rec, row


def body_len(r):
    return sum(len(b.get("text", "")) for b in r.get("body", {}).get("blocks", []))


def merge_inputs(olds):
    """같은 doc_id는 하나로: 본문이 더 긴 쪽을 쓰고 attempts를 합친다. 반환: (레코드 목록, doc_id별 병합 메모)"""
    by, notes = {}, {}
    for fi, old in enumerate(olds):
        for r in old.get("records", []):
            did = r.get("doc_id")
            if did not in by:
                by[did] = dict(r, attempts=list(r.get("attempts", [])))
                notes[did] = [f"입력 {fi + 1}"]
                continue
            cur = by[did]
            notes[did].append(f"입력 {fi + 1}")
            if (cur.get("body", {}).get("sha256") != r.get("body", {}).get("sha256")):
                keep_new = body_len(r) > body_len(cur)
                notes[did].append(f"본문이 달라 {'입력 ' + str(fi + 1) if keep_new else '먼저 읽은 쪽'}의 더 긴 본문을 씀"
                                  f"({body_len(cur)}자 vs {body_len(r)}자)")
                if keep_new:
                    atts = cur["attempts"]
                    cur = by[did] = dict(r, attempts=list(r.get("attempts", [])))
                    cur["attempts"] = atts + cur["attempts"]
                    continue
            cur["attempts"] = cur["attempts"] + [a for a in r.get("attempts", []) if a not in cur["attempts"]]
    return list(by.values()), notes


def _rep_key(rec):
    return (not rec["normalized_url"].startswith("https://"), len(rec["normalized_url"]), rec["normalized_url"])


def merge_aliases(recs, rows_by_id):
    """별칭 통합(4.4): 같은 발행처·같은 본문 sha256 → 대표 하나(https 우선 → 짧은 URL → 사전순). 나머지 조회 이력은 observation 앞에
    `별칭 URL:`을 붙여 대표에 남긴다. 반환: (레코드, 통합 메모 목록, 중복 의심 목록)"""
    groups = {}
    for r in recs:
        if r["body"]["sha256"]:
            groups.setdefault((r["publisher"], r["body"]["sha256"]), []).append(r)
    drop, notes = set(), []
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=_rep_key)
        rep, rest = g[0], g[1:]
        for o in rest:
            drop.add(o["doc_id"])
            rep["discovery"] = sorted({json.dumps(d, sort_keys=True): d for d in rep["discovery"] + o["discovery"]}.values(),
                                      key=lambda d: (d["route"], d["agent"], d["source_id"] or "", d["query"] or ""))
            rep["attempts"] += [dict(a, observation="별칭 URL: " + a["observation"]) for a in o["attempts"]]
            notes.append(f"{rep['source_url']} ← 별칭 {o['source_url']} (같은 발행처·본문 해시 {rep['body']['sha256'][:8]})")
        uniq = {(a["at"], a["pass"], a["agent"], a["url"], a["observation"]): a for a in rep["attempts"]}
        rep["attempts"] = [uniq[k] for k in sorted(uniq)]
    kept = [r for r in recs if r["doc_id"] not in drop]
    # 중복 의심: 같은 발행처인데 본문 해시가 다르고, 제목이 같거나 URL이 쿼리·끝 슬래시만 다른 경우(통합하지 않고 보고만)
    sus = []
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            if a["publisher"] != b["publisher"] or a["body"]["sha256"] == b["body"]["sha256"]:
                continue
            same_url = a["normalized_url"].split("?")[0].rstrip("/") == b["normalized_url"].split("?")[0].rstrip("/")
            if same_url or (a["title"] and a["title"] == b["title"]):
                sus.append((a["source_url"], b["source_url"]))
    return kept, notes, sus


def fidelity_flags(settings, rec):
    """반환: (본문 충실도 사유 목록, 출처 확인 사유 목록)"""
    chars = sum(len(b["text"]) for b in rec["body"]["blocks"])
    url = rec["source_url"]
    path = urlsplit(url).path.lower()
    pdf_like = path.endswith(".pdf") or rec["body"]["scope"] == "pdf" or "/attach/down" in path or "aspdf" in url.lower()
    body, src = [], []
    if chars < 700:
        body.append(f"본문 {chars}자 (<700)")
    if pdf_like and chars < 2000 and not body:
        body.append(f"PDF·내려받기 자료인데 본문 {chars}자")
    elif pdf_like and body:
        body.append("PDF·내려받기 자료")
    if rec["source_type"] in ("press", "other"):
        src.append(f"source_type {rec['source_type']}")
    reg = settings.source_for_url(url)
    if reg and reg["publisher"] != rec["publisher"]:
        src.append(f"레지스트리 발행처 '{reg['publisher']}'와 다름")
    title = (rec["title"] or "").casefold()
    for s in settings.sources:
        name = s.get("publisher") or ""
        if len(name) >= 4 and name.casefold() in title and name != rec["publisher"] and (not reg or reg["id"] != s["id"]):
            src.append(f"제목에 다른 발행처 '{name}'가 나옴 — 제3자 사이트 게재 가능성")
            break
    return body, src


def migrate(olds, issue_week, settings, now=None):
    """olds: v4.2 문서 하나 또는 목록. 반환: (merged, intake, rows, aborted, extra) — extra는 병합·별칭·충실도 점검 결과"""
    olds = olds if isinstance(olds, list) else [olds]
    now = now or now_kst()
    start, end = weekly_window(issue_week, 1)
    merged_old, merge_notes = merge_inputs(olds)
    recs, rows, aborted = [], [], []
    for r in merged_old:
        rec, row = migrate_record(settings, r, start, end, now)
        row["merge_notes"] = merge_notes.get(r.get("doc_id"), [])
        rows.append(row)
        if rec:
            recs.append(rec)
        else:
            aborted.append(row)
    recs, alias_notes, suspects = merge_aliases(recs, None)
    alive = {r["doc_id"] for r in recs}
    for row in rows:
        if row.get("new_doc_id") and row["new_doc_id"] not in alive and row["result"] != "중단":
            row["result"] = "별칭 통합(대표에 합침)"
    flags = {r["doc_id"]: fidelity_flags(settings, r) for r in recs if r["status"] == "complete"}
    extra = {"alias_notes": alias_notes, "suspects": suspects, "fidelity": [(r, *flags[r["doc_id"]]) for r in recs if r["doc_id"] in flags],
             "n_inputs": len(olds)}
    recs.sort(key=lambda r: r["doc_id"])
    cfg = settings.c
    gen = {"name": "migrate_v42", "version": "0.1.0", "registry_version": str(settings.source_meta.get("registry_version", "unknown")),
           "config_version": str(cfg.get("config_version", "unknown"))}
    base = {"schema_version": "4.5", "revision": 1, "issue_week": issue_week, "window_weeks": 1,
            "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(), "generated_at": now, "generator": gen}
    merged = dict(base, phase="merged", summary=summarize(recs), records=recs)
    intake_recs = [r for r in recs if r["status"] == "complete"]
    intake = dict(base, phase="intake", summary=summarize(intake_recs), records=intake_recs)
    with_rs = [o for o in olds if o.get("resurvey")]
    rs = max(with_rs, key=lambda o: (o.get("revision") or 0))["resurvey"] if with_rs else None     # revision이 가장 높은 입력의 기록
    if rs:
        after = len(intake_recs)
        added = max(0, rs.get("after_count", 0) - rs.get("before_count", 0))
        tc = rs.get("topics_checked", {})
        topics = [{"topic": TOPIC_MAP[k], "method": "v4.2 기록", "result": str(tc.get(k) or "미실행")} for k in TOPIC_MAP]
        res = {"before_count": max(0, after - added), "after_count": after, "topics_checked": topics,
               "report": "[v4.2] " + str(rs.get("report") or "(기록 없음)")}
        merged["resurvey"], intake["resurvey"] = res, dict(res)
    return merged, intake, rows, aborted, extra


def _tbl(rows, head):
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]


def render_report(olds, issue_week, merged, intake, rows, errs, extra=None):
    extra = extra or {"alias_notes": [], "suspects": [], "fidelity": []}
    olds = olds if isinstance(olds, list) else [olds]
    old = olds[0]
    s = intake["summary"]
    L = [f"# {issue_week} v4.2 → v4.5 이관 리포트", "",
         *[f"- 원본 {i}: schema {o.get('schema_version')} / phase {o.get('phase')} / revision {o.get('revision')} / 구 기간 {o.get('window_start')} ~ {o.get('window_end_exclusive')} / 레코드 {len(o.get('records', []))}건"
           for i, o in enumerate(olds, 1)],
         f"- 새 기간: {merged['window_start']} ~ {merged['window_end_exclusive']} (`weekly_window`로 다시 계산, window_weeks 1)",
         f"- 이관 결과: merged {merged['summary']['records']}건 / intake(complete) {s['complete']}건"
         + (f" / 구 `paid_excluded` {sum(o.get('paid_excluded') or 0 for o in olds)}건(레코드 없음)" if any(o.get("paid_excluded") for o in olds) else ""),
         "- **주의**: 본문은 v4.2 수집기(LLM) 산출물이며 원문 바이트와 대조하지 않았다. `rights`는 버렸고 `author`는 null이다.",
         "", "## summary", "", f"records {s['records']} · complete {s['complete']} · partial {s['partial']} · drop {s['drop']} · paywall {s['paywall']}", ""]
    L += ["## 레코드별 이관 결과", "", "| # | 구 doc_id | doc_id 일치 | 결과 | lead_ids | 본문 해시 | source_type | 구 event_key |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        L.append(f"| {i} | {str(r.get('old_doc_id'))[:17]}… | {'같음' if r.get('doc_id_equal') else '다름'} | {r['result']} "
                 f"| {r.get('old_leads')} → {r.get('new_leads')} | {'바뀜' if r.get('hash_changed') else '같음'} "
                 f"| {r.get('source_type', '-')} | {r.get('old_event_key') or '-'} |")
    merged_rows = [r for r in rows if len(r.get("merge_notes", [])) > 1]
    L += ["", "## 입력 병합(doc_id)", ""] + ([f"- {r['title']}: {', '.join(r['merge_notes'])}" for r in merged_rows] or ["(여러 파일에 걸친 레코드 없음)"])
    L += ["", "## 별칭 통합(4.4)", ""] + ([f"- {n}" for n in extra["alias_notes"]] or ["(없음)"])
    L += ["", "## 중복 의심", "", "같은 발행처인데 본문 해시가 달라 통합하지 않은 쌍(제목이 같거나 URL이 쿼리·끝 슬래시만 다름):"]
    L += [f"- {a}  ↔  {b}" for a, b in extra["suspects"]] or ["(없음)"]
    L += ["", "## 본문 충실도 점검", "",
          "본문이 700자 미만이거나 PDF·내려받기 자료인데 짧은 레코드. 요약문일 수 있으나 원문을 다시 가져올 수 없어 complete로 유지했다.", ""]
    fid = [(r, b, sr) for r, b, sr in extra["fidelity"] if b]
    L += _tbl([(r["publisher"], r["title"][:50], "; ".join(b)) for r, b, _ in fid], ["발행처", "제목", "사유"]) if fid else ["(없음)"]
    L += ["", "## 출처 확인 필요", "", "source_type이 press/other이거나, 발행처가 레지스트리와 다르거나, 제목이 다른 발행처를 가리키는 레코드:", ""]
    sc = [(r, b, sr) for r, b, sr in extra["fidelity"] if sr]
    L += _tbl([(r["publisher"], r["title"][:50], "; ".join(sr)) for r, _, sr in sc], ["발행처", "제목", "사유"]) if sc else ["(없음)"]
    L += ["", "## 제목", ""] + [f"{i}. {r['title']} ({r.get('old_date')}) — 블록 {r.get('old_blocks')}→{r.get('new_blocks')}" for i, r in enumerate(rows, 1)]
    bad = [r for r in rows if r["result"] in ("중단", "제외") or r["result"].startswith(("drop", "partial"))]
    L += ["", "## 이관되지 않았거나 intake에서 빠진 레코드", ""]
    L += [f"- {r['title']}: {r['result']} {r['note']}" for r in bad] or ["(없음)"]
    L += ["", "## source_type 결정 근거", "", "`sources.yaml`의 발행처(`Settings.source_for_url`)가 있으면 그 값을 쓰고, 없으면 호스트로 추정했다.",
          "추정 규칙: `*.go.kr`이면서 발행처에 '표준'이 있으면 standards_body, 그 밖의 정부(`*.go.kr`)·EU(`*.europa.eu`)·해당 없음은 other.", ""]
    for i, r in enumerate(rows, 1):
        if "source_type" in r:
            L.append(f"- {i}. {r['source_type']} ← {r['source_type_why']}")
    L += ["", "## 변환 규칙", "",
          "- 주제 코드: chipmaker→T1_CHIPMAKER, equipment→T2_EQUIPMENT, solution→T3_SOLUTION, physical_ai→T4_PHYSICAL_AI. `event_key`는 null(구 값은 위 표에만 남김).",
          "- discovery: route llm_search, agent는 구 attempts의 `collector`, source_id·query는 null. attempts: agent manual, 관찰 기록에 `[v4.2 이관, 원 수집자 …]` 접두와 이관 기록 1건 추가.",
          "- 본문: 블록 텍스트를 `normalize_text`로 정규화, ID 재부여, lead_ids·해시 재계산, extractor `legacy v4.2 (…)`.",
          "- 상태: 기간 이전은 drop/OUT_OF_WINDOW(intake 제외), 메타데이터·본문 길이 검사에 걸리면 partial. 원문은 다시 조회하지 않았다.",
          "- resurvey: topics_checked를 목록으로 바꾸고 method `v4.2 기록`. before_count는 구 기록의 증가분(after−before)을 유지해 intake 건수에서 역산했다."]
    L += ["", "## 코드 검사(7.3)", "", "intake·merged 모두 통과" if not errs else "\n".join(f"- {e}" for e in errs)]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description="v4.2 수집 결과를 v4.5로 이관")
    ap.add_argument("old", nargs="+", help="v4.2 수집 결과 파일(여러 개면 doc_id로 합침)")
    ap.add_argument("--issue-week", required=True)
    ap.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    settings = load_settings(a.root)
    olds = [json.loads(Path(f).read_text(encoding="utf-8")) for f in a.old]
    merged, intake, rows, aborted, extra = migrate(olds, a.issue_week, settings)
    lead = settings.c["extraction"]["lead_min_chars"]
    errs = []
    for name, doc in (("merged", merged), ("intake", intake)):
        e, w = check(doc, lead, settings.c["thresholds"]["intake_min"])
        errs += [f"{name}: {x}" for x in e]
    out = settings.out_dir(a.issue_week)
    out.mkdir(parents=True, exist_ok=True)
    report = render_report(olds, a.issue_week, merged, intake, rows, errs, extra)
    (out / file_name(a.issue_week, "report", 1, "md")).write_text(report, encoding="utf-8")
    if errs:
        print("코드 검사 실패 — 이관하지 않았습니다:\n" + "\n".join(errs[:20]))
        return 1
    for kind, doc in (("merged", merged), ("intake", intake)):
        (out / file_name(a.issue_week, kind, 1)).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"이관 완료: merged {merged['summary']['records']}건, intake {intake['summary']['complete']}건 → {out}")
    for r in rows:
        print(f"  {r['result']:<12} doc_id {'=' if r.get('doc_id_equal') else '≠'} {r.get('source_type', '-'):<15} {str(r['title'])[:50]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
