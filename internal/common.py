"""여러 단계가 함께 쓰는 작은 도구."""
import re
from datetime import datetime

from collector.ref import KST

from .settings import TOPICS

EVIDENCE_RE = re.compile(r"^(url1_[0-9a-f]{64})/([PT][1-9][0-9]*)$")


def topic_short(code):
    return (code or "")[:2]


def block_index(rec):
    return {b["id"]: b for b in rec["body"]["blocks"]}


def evidence_errors(ids, records, allowed_doc_ids=None):
    """근거 ID(doc_id/P3, doc_id/T1)가 실재하는지 검사한다. 오류 문자열 목록을 반환."""
    errs = []
    for eid in ids:
        m = EVIDENCE_RE.match(eid or "")
        if not m:
            errs.append(f"근거 ID 형식 오류: {eid}")
            continue
        doc_id, bid = m.groups()
        if allowed_doc_ids is not None and doc_id not in allowed_doc_ids:
            errs.append(f"이 사건의 자료가 아닌 근거 ID: {eid}")
        elif doc_id not in records or bid not in block_index(records[doc_id]):
            errs.append(f"존재하지 않는 근거 ID: {eid}")
    return errs


def evidence_text(eid, records):
    m = EVIDENCE_RE.match(eid or "")
    if not m or m.group(1) not in records:
        return ""
    b = block_index(records[m.group(1)]).get(m.group(2))
    return b["text"] if b else ""


def pub_sort_key(rec):
    """발행 시각 비교용 문자열(KST). 날짜만 있으면 자정으로 본다."""
    if rec.get("published_at"):
        return datetime.fromisoformat(rec["published_at"]).astimezone(KST).isoformat()
    return (rec.get("published_date") or "9999-12-31") + "T00:00:00+09:00"


def representative(recs):
    """대표 자료: primary → 공식 발표(company_official) → 이른 발행 순(7.2). 마지막은 doc_id로 고정."""
    return min(recs, key=lambda r: (r["source_tier"] != "primary", r["source_type"] != "company_official",
                                    pub_sort_key(r), r["doc_id"]))


def week_tag(issue_week):
    """2026-W49 -> 2026W49"""
    return issue_week.replace("-", "")


def short_week(issue_week):
    """2026-W47 -> W47"""
    return issue_week.split("-")[-1]


def add_flag(obj, flag):
    if flag not in obj.setdefault("flags", []):
        obj["flags"].append(flag)


def trunc(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"
