"""레코드 생성과 상태 판정 (설계서 4.4, 5장)."""
from urllib.parse import urlsplit

from .extract import date_from_url, extract_html, extract_pdf, parse_when, plausible_date
from .ref import attribute, body_digest, document_key, lead_ids, to_ascii_uri
from .settings import TOPIC_CODES

RECOVERY_ORDER = ["ACCESS_BLOCKED", "FETCH_FAILED", "BODY_MISSING", "BODY_TRUNCATED", "TABLE_MISSING",
                  "DATE_UNCERTAIN", "MERGE_CONFLICT"]
TERMINAL_ORDER = ["PAYWALL", "OUT_OF_WINDOW", "UNTRUSTED_SOURCE"]


def doc_key(url):
    return document_key(to_ascii_uri(url))


def _strip_www(h):
    h = (h or "").lower()
    return h[4:] if h.startswith("www.") else h


def choose_source_url(final_url, canonical):
    """canonical이 같은 호스트(www 차이 무시)이고 경로가 '/'가 아니면 canonical, 아니면 최종 URL."""
    if canonical:
        try:
            c, f = urlsplit(to_ascii_uri(canonical)), urlsplit(to_ascii_uri(final_url))
            if c.scheme in ("http", "https") and _strip_www(c.hostname) == _strip_www(f.hostname) and c.path not in ("", "/"):
                return to_ascii_uri(canonical)
        except ValueError:
            pass
    return to_ascii_uri(final_url)


def _has_marker(text, markers):
    t = (text or "").lower()
    return any(m.lower() in t for m in markers or [])


def _sorted_codes(codes):
    order = RECOVERY_ORDER + TERMINAL_ORDER
    return sorted(set(codes), key=order.index)


def _attempt(f, pass_no, observation, url=None):
    return {"pass": pass_no, "agent": f.agent if f else "script", "at": f.at, "url": url or f.requested,
            "http_status": f.status, "observation": observation}


def empty_body(scope="unknown", note=None):
    return {"scope": scope, "extent": "none", "scope_note": note, "extractor": None, "blocks": [],
            "lead_ids": [], "utf8_bytes": 0, "sha256": None}


def build_record(group, fetched, pass_no, settings, window, override=None, manual_file=None):
    """group: 같은 doc_id 후보 묶음 {url, discovery[], topic_hints, track, date_hint, at_hint}.
    fetched: Fetched. 반환: (record, 귀속 결과)."""
    cfg = settings.c["extraction"]
    override = override or {}
    start, end = window
    codes, notes = [], []
    meta, blocks, extractor, scope = {}, [], None, "unknown"

    fail = fetched.failure_code() if fetched else "FETCH_FAILED"
    if fail is None:
        if fetched.is_pdf:
            x = extract_pdf(fetched.content)
            if x["meta"].get("missing_pages"):
                codes.append("BODY_MISSING")
        else:
            x = extract_html(fetched.content, fetched.content_type, fetched.final_url)
        meta, blocks, extractor, scope = x["meta"], x["blocks"], x["extractor"], x["scope"]
        notes += x.get("notes", [])
    else:
        codes.append(fail)

    final_url = fetched.final_url if fetched and fetched.final_url else group["url"]
    if manual_file:
        final_url = group["url"]
    source_url = choose_source_url(final_url, meta.get("canonical"))
    normalized, did = document_key(source_url)
    host = urlsplit(source_url).hostname
    # 발행처는 대표 URL로만 정한다(어느 경로·모델이 찾았는지와 무관하게 같은 값)
    src = settings.source_for_url(source_url)

    # 메타데이터 (4.4 우선순위)
    published_at, published_date, date_src = meta.get("published_at"), meta.get("published_date"), meta.get("date_source")
    rejected = list(meta.get("date_rejected") or [])      # 말이 안 되는 날짜(1970-01-01 등)는 버리고 다음 출처로(4.4)
    if not published_date and (group.get("date_hint") or group.get("at_hint")):
        if plausible_date(group.get("date_hint") or (group.get("at_hint") or "")[:10]):
            published_at, published_date, date_src = group.get("at_hint"), group.get("date_hint"), "feed pubDate"
        else:
            rejected.append(f"feed pubDate={group.get('date_hint') or group.get('at_hint')}")
    if not published_date:
        d = date_from_url(source_url)
        if d and plausible_date(d):
            published_date, date_src = d, "URL 경로"
        elif d:
            rejected.append(f"URL 경로={d}")
    if not published_date and meta.get("pdf_creation_date"):
        if plausible_date(meta["pdf_creation_date"]):
            published_date, date_src = meta["pdf_creation_date"], "PDF CreationDate"
        else:
            rejected.append(f"PDF CreationDate={meta['pdf_creation_date']}")
    if override.get("published_date"):
        published_at, published_date = None, str(override["published_date"])
        date_src = f"수동 확인({override.get('note', '근거 미기재')})"
        if override.get("published_at"):
            published_at = str(override["published_at"])

    title = meta.get("title") or next((b["text"] for b in blocks if b["kind"] == "heading"), None)
    publisher = (src or {}).get("publisher") or meta.get("site_name") or host
    language = meta.get("language") or (src or {}).get("language")
    if language:
        parts = language.replace("_", "-").split("-")
        language = "-".join([parts[0].lower()] + parts[1:])

    # 판정
    paywall = bool(meta.get("paywalled")) or bool(override.get("paywall"))
    body_chars = sum(len(b["text"]) for b in blocks)
    if fail is None:
        if not blocks:
            codes.append("BODY_MISSING")
        if _has_marker(meta.get("page_title"), cfg["block_page_markers"]):
            codes.append("ACCESS_BLOCKED")
        short = blocks and body_chars < cfg["min_body_chars"] and not override.get("accept_short")
        if short:
            codes.append("BODY_TRUNCATED")
            if _has_marker(meta.get("page_text"), cfg["login_markers"]):
                codes.append("ACCESS_BLOCKED")
        if meta.get("rel_next"):
            codes.append("BODY_TRUNCATED")
        if blocks and _has_marker(blocks[-1]["text"][-200:], cfg["truncation_markers"]) and not override.get("accept_short"):
            codes.append("BODY_TRUNCATED")
        if meta.get("has_main_table") and not any(b["kind"] == "table" for b in blocks):
            codes.append("TABLE_MISSING")
        if not title or not publisher or not language:
            codes.append("BODY_MISSING")
            notes.append("필수 메타데이터 결손: " + ", ".join(k for k, v in (("title", title), ("publisher", publisher), ("language", language)) if not v))

    where = attribute(published_at, published_date, start, end)
    if fail is None and where == "UNKNOWN":
        codes.append("DATE_UNCERTAIN")

    terminal = []
    if settings.blocked(host):
        terminal.append("UNTRUSTED_SOURCE")
    if paywall:
        terminal.append("PAYWALL")
    if where == "OLD":
        terminal.append("OUT_OF_WINDOW")

    if terminal:
        status, reason = "drop", _sorted_codes(terminal)
    elif codes:
        status, reason = "partial", _sorted_codes(codes)
    else:
        status, reason = "complete", []

    n, sha = body_digest(blocks)
    extent = "none" if not blocks else ("full" if status == "complete" else "partial")
    if blocks:
        scope_note = f"블록 {len(blocks)}개(표 {sum(b['kind'] == 'table' for b in blocks)}개)"
        if meta.get("pages"):
            scope_note += f", PDF {meta['pages']}쪽"
        if notes:
            scope_note += "; " + "; ".join(notes)
        body = {"scope": scope, "extent": extent, "scope_note": scope_note, "extractor": extractor,
                "blocks": blocks, "lead_ids": lead_ids(blocks, cfg["lead_min_chars"]), "utf8_bytes": n, "sha256": sha}
    else:
        body = empty_body(scope, "; ".join(notes) or None)

    # 시도 이력
    obs = []
    if fetched:
        obs.append(f"HTTP {fetched.status}" if fetched.status else f"조회 실패: {fetched.error}")
        if fetched.final_url and fetched.final_url != fetched.requested:
            obs.append(f"최종 URL {fetched.final_url}")
    if manual_file:
        obs.append(f"수동 저장 파일 {manual_file}")
    if blocks:
        obs.append(f"본문 {body_chars}자, 블록 {len(blocks)}개")
    if date_src:
        obs.append(f"발행일 출처: {date_src}")
    if rejected:
        obs.append("버린 발행일(기본값·불가능한 날짜): " + ", ".join(rejected))
    obs.append(f"귀속 {where}")
    if source_url != to_ascii_uri(group["url"]):
        obs.append(f"대표 URL {source_url} (후보 URL {group['url']})")
    if reason:
        obs.append("판정 " + "/".join(reason))
    attempts = [_attempt(fetched, pass_no, "; ".join(obs))] if fetched else []

    topics = [t for t in TOPIC_CODES if t in set(group.get("topic_hints", [])) | set((src or {}).get("topics", []))]
    track = "A" if any(c == "A" for c in group.get("tracks", [group.get("track", "A")])) else "B"
    if src and src.get("track"):
        track = src["track"]

    rec = {
        "doc_id": did, "source_url": source_url, "normalized_url": normalized,
        "title": title, "publisher": publisher, "author": meta.get("author") or None, "language": language,
        "source_type": (src or {}).get("source_type", "other"),
        "published_date": published_date, "published_at": published_at,
        "change_at": meta.get("modified_at") if meta.get("modified_at") and meta.get("modified_at") != published_at else None,
        "change_note": None,
        "discovery": sorted_discovery(group["discovery"]),
        "event_key": None, "track": track, "topic_hints": topics,
        "source_tier": (src or {}).get("source_tier", "unverified"),
        "body": body, "media": [], "status": status, "reason_codes": reason, "attempts": attempts,
    }
    if rec["change_at"]:
        rec["change_note"] = "발행처 표기 수정 시각(dateModified)"
    return rec, where


def sorted_discovery(items):
    seen = {}
    for d in items:
        k = (d["route"], d["agent"], d["source_id"] or "", d["query"] or "")
        seen[k] = {"route": d["route"], "agent": d["agent"], "source_id": d["source_id"], "query": d["query"]}
    return [seen[k] for k in sorted(seen)]


def sort_attempts(attempts):
    seen = {}
    for a in attempts:
        k = (a["at"], a["pass"], a["agent"], a["url"], a["observation"])
        seen[k] = a
    return [seen[k] for k in sorted(seen, key=lambda k: (k[0], k[1], k[2], k[3], k[4]))]


def failed_record(group, pass_no, error_obs, at):
    """URL 자체가 잘못돼 조회조차 못 한 후보. doc_id는 후보 URL로 계산."""
    url = to_ascii_uri(group["url"])
    normalized, did = document_key(url)
    return {
        "doc_id": did, "source_url": url, "normalized_url": normalized, "title": None, "publisher": None,
        "author": None, "language": None, "source_type": "other", "published_date": None, "published_at": None,
        "change_at": None, "change_note": None, "discovery": sorted_discovery(group["discovery"]),
        "event_key": None, "track": group.get("track", "A"),
        "topic_hints": [t for t in TOPIC_CODES if t in group.get("topic_hints", [])], "source_tier": "unverified",
        "body": empty_body(), "media": [], "status": "partial", "reason_codes": ["FETCH_FAILED"],
        "attempts": [{"pass": pass_no, "agent": "script", "at": at, "url": url, "http_status": None, "observation": error_obs}],
    }
