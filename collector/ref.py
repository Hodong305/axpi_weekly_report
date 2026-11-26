"""설계서 6장 기준 구현. 설계서의 코드 블록과 이 파일은 항상 같은 내용을 유지한다."""
import hashlib
import unicodedata
import re
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, urlsplit, urlunsplit

KST = timezone(timedelta(hours=9))


# 6.1 URL과 자료 ID ---------------------------------------------------------

def to_ascii_uri(url):
    # 한글 경로·IDN 호스트를 ASCII URI로 바꾼다. 이미 인코딩된 %XX는 유지한다.
    u = urlsplit(url.strip())
    host = u.hostname or ""
    if not host.isascii():
        host = host.encode("idna").decode("ascii")
    if ":" in host:
        host = "[" + host + "]"
    netloc = host + (":" + str(u.port) if u.port is not None else "")
    safe = "/:@!$&'()*+,;=-._~%"
    return urlunsplit((u.scheme, netloc, quote(u.path, safe=safe),
                       quote(u.query, safe=safe + "?"), quote(u.fragment, safe=safe + "?")))


def document_key(source_url):
    # 입력은 to_ascii_uri를 거친 절대 ASCII URI.
    if not source_url.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in source_url):
        raise ValueError("절대 URI를 확인하세요")
    u = urlsplit(source_url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username is not None or u.password is not None:
        raise ValueError("HTTP(S) 원문 URL이 필요합니다")
    host = u.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if u.port is not None and (u.scheme, u.port) not in (("http", 80), ("https", 443)):
        host += ":" + str(u.port)
    keep = []
    for pair in u.query.split("&") if u.query else []:
        key = pair.partition("=")[0].lower()
        if not key.startswith("utm_") and key not in {"gclid", "fbclid"}:
            keep.append(pair)
    normalized = urlunsplit((u.scheme, host, u.path or "/", "&".join(keep), ""))
    return normalized, "url1_" + hashlib.sha256(normalized.encode("ascii")).hexdigest()


# 6.2 수집 기간과 귀속 -------------------------------------------------------

def weekly_window(issue_week, weeks=1):
    year, week = int(issue_week[:4]), int(issue_week[6:])
    monday = date.fromisocalendar(year, week, 1)
    end = datetime.combine(monday - timedelta(days=1), time(0), KST)
    start = end - timedelta(days=7 * weeks)
    return start, end


def attribute(published_at, published_date, start, end):
    # 반환: WINDOW / FUTURE / OLD / UNKNOWN
    if published_at:
        t = datetime.fromisoformat(published_at).astimezone(KST)
        s, e = start, end
    elif published_date:
        t = date.fromisoformat(published_date)
        s, e = start.date(), end.date()
    else:
        return "UNKNOWN"
    if t >= e:
        return "FUTURE"
    if t >= s:
        return "WINDOW"
    return "OLD"


# 6.3 블록 정규화, 본문 해시, 첫 문단 ----------------------------------------

def normalize_text(text, keep_newlines=False):
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    if keep_newlines:  # 표: 행 구분 줄바꿈은 유지하고 각 행 안의 공백만 정리
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.split("\n")]
        return "\n".join(ln for ln in lines if ln)
    return re.sub(r"\s+", " ", text).strip()


def body_digest(blocks):
    if not blocks:
        return 0, None
    data = "\n\n".join(b["text"] for b in blocks).encode("utf-8")
    return len(data), hashlib.sha256(data).hexdigest()


def lead_ids(blocks, lead_min_chars):
    paras = [b for b in blocks if b["kind"] == "paragraph"]
    substantive = [b for b in paras if len(b["text"]) >= lead_min_chars] or paras
    return [b["id"] for b in substantive[:2]]


def assign_ids(blocks):
    # 표가 아닌 블록은 P1부터, 표는 T1부터 문서 순서대로 따로 번호를 매긴다.
    p = t = 0
    for b in blocks:
        if b["kind"] == "table":
            t += 1
            b["id"] = f"T{t}"
        else:
            p += 1
            b["id"] = f"P{p}"
    return blocks
