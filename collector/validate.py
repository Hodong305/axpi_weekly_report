"""설계서 7.3 코드 검사(K1~K11). 사외 패키징 직전과 사내 반입 직후에 같은 코드로 실행한다."""
import json

import jsonschema

from .ref import attribute, body_digest, document_key, lead_ids, weekly_window
from .settings import SCHEMA_PATH


def summarize(records):
    st = [r["status"] for r in records]
    return {"records": len(records), "complete": st.count("complete"), "partial": st.count("partial"),
            "drop": st.count("drop"), "paywall": sum("PAYWALL" in r["reason_codes"] for r in records)}


def _attempt_key(a):
    return (a["at"], a["pass"], a["agent"], a["url"], a["observation"])


def _disc_key(d):
    return (d["route"], d["agent"], d["source_id"] or "", d["query"] or "")


def check(doc, lead_min_chars, intake_min=None):
    """반환: (errors, warnings). errors가 비어야 통과."""
    errors, warnings = [], []
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    for e in sorted(v.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        errors.append(f"K1 스키마 {'/'.join(map(str, e.absolute_path))}: {e.message[:200]}")
    if errors:
        return errors, warnings  # 구조가 깨지면 이후 검사는 의미가 없다

    start, end = weekly_window(doc["issue_week"], doc["window_weeks"])
    if doc["window_start"] != start.isoformat() or doc["window_end_exclusive"] != end.isoformat():
        errors.append(f"K2 기간 불일치: 기대 {start.isoformat()} ~ {end.isoformat()}")

    recs = doc["records"]
    ids = [r["doc_id"] for r in recs]
    if ids != sorted(ids):
        errors.append("K11 records가 doc_id 오름차순이 아님")
    if len(ids) != len(set(ids)):
        errors.append("K3 doc_id 중복")

    for i, r in enumerate(recs):
        tag = f"[{i}] {r['doc_id'][:17]}"
        try:
            norm, did = document_key(r["source_url"])
            if norm != r["normalized_url"] or did != r["doc_id"]:
                errors.append(f"K3 {tag} URL·doc_id 재계산 불일치")
        except ValueError as e:
            errors.append(f"K3 {tag} {e}")
        blocks = r["body"]["blocks"]
        p = t = 0
        for b in blocks:
            if b["kind"] == "table":
                t += 1
                exp = f"T{t}"
            else:
                p += 1
                exp = f"P{p}"
            if b["id"] != exp:
                errors.append(f"K4 {tag} 블록 ID 순번 오류: {b['id']} (기대 {exp})")
                break
        if blocks and r["body"]["lead_ids"] != lead_ids(blocks, lead_min_chars):
            errors.append(f"K5 {tag} lead_ids 불일치")
        n, sha = body_digest(blocks)
        if n != r["body"]["utf8_bytes"] or sha != r["body"]["sha256"]:
            errors.append(f"K6 {tag} 바이트 수·해시 재계산 불일치")
        if r["status"] == "complete":
            where = attribute(r["published_at"], r["published_date"], start, end)
            if where != "WINDOW":
                errors.append(f"K7 {tag} complete인데 귀속이 {where}")
        if [_attempt_key(a) for a in r["attempts"]] != sorted({_attempt_key(a) for a in r["attempts"]}):
            errors.append(f"K11 {tag} attempts 정렬·중복 오류")
        if [_disc_key(d) for d in r["discovery"]] != sorted({_disc_key(d) for d in r["discovery"]}):
            errors.append(f"K11 {tag} discovery 정렬·중복 오류")
        if doc["phase"] == "pass2" and not any(a["pass"] == 2 for a in r["attempts"]):
            errors.append(f"K9 {tag} pass2 레코드에 2차 시도 없음")

    if summarize(recs) != doc["summary"]:
        errors.append(f"K8 summary 불일치: 기대 {summarize(recs)}")

    if doc["phase"] == "intake":
        if any(r["status"] != "complete" for r in recs):
            errors.append("K10 intake에 complete 외 레코드 포함")
        rs = doc.get("resurvey")
        if rs and rs["after_count"] != len(recs):
            errors.append("K10 resurvey.after_count ≠ intake records 수")
        if intake_min is not None and len(recs) < intake_min and not rs:
            warnings.append(f"K10 complete {len(recs)}건 < intake_min {intake_min}, 재조사 기록 없음")
    return errors, warnings


def check_file(path, lead_min_chars, intake_min=None):
    import gzip
    raw = gzip.open(path, "rt", encoding="utf-8").read() if str(path).endswith(".gz") else open(path, encoding="utf-8").read()
    return check(json.loads(raw), lead_min_chars, intake_min)

