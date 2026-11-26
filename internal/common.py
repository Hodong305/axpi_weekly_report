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


# 숫자·수치 값 검사 도구(S7과 S6 검증이 함께 쓴다) -------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EN_NUM = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}


def _numval(tok):
    return tok.replace(",", "")


def _num_re(tok):
    return re.compile(rf"(?<![\d.]){re.escape(_numval(tok))}(?![\d]|\.\d)")


def _num_in(tok, text):
    v = _numval(tok)
    if _num_re(tok).search(text.replace(",", "")):
        return True
    return v.isdigit() and 1 <= int(v) <= 10 and re.search(rf"\b{_EN_NUM[int(v)]}\b", text, re.I) is not None


def figure_value_errors(value, text):
    """수치 카드·한 방의 값 검사: 숫자는 근거 텍스트에 있어야 하고, 짧은 말('수 시간')은 근거에 그대로 있어야 한다.
    '수 시간 → 수 분', '99%', '90,000+'처럼 →·/·≠·+로 이어 붙인 값은 조각마다 본다(한 글자 조각은 단위로 보고 건너뜀)."""
    errs = []
    compact = re.sub(r"\s+", "", text)
    for part in re.split(r"[→/≠·+~]|->", value):
        part = part.strip()
        if len(part) <= 1:
            continue
        nums = _NUM.findall(part)
        if nums:
            errs += [f"'{value}'의 {n}이(가) 근거에 없음" for n in nums if not _num_in(n, text)]
        elif re.sub(r"\s+", "", part) not in compact:
            errs.append(f"'{value}'의 '{part}'이(가) 근거에 그대로 나오지 않음")
    if not any(len(p.strip()) > 1 for p in re.split(r"[→/≠·+~]|->", value)) and not _NUM.search(value):
        errs.append(f"'{value}'에 수치나 근거에 있는 말이 없음")
    return errs
