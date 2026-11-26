"""D1 레지스트리 순회: sources.yaml의 발행처 목록에서 후보 URL을 모은다."""
import re
from datetime import timedelta
from urllib.parse import urljoin

from lxml import etree, html as lxml_html

from .extract import decode_html, parse_when

CRAWL_METHODS = {"rss", "sitemap", "list_html"}


def url_allowed(src, url):
    inc = src.get("include_url_patterns") or []
    exc = src.get("exclude_url_patterns") or []
    if inc and not any(re.search(p, url) for p in inc):
        return False
    return not any(re.search(p, url) for p in exc)


def _in_scan_range(date_str, start, end, margin_days):
    """피드 날짜는 힌트다. 여유일을 두고 명백히 범위 밖인 항목만 건너뛴다. 날짜가 없으면 후보로 둔다."""
    if not date_str:
        return True
    d = date_str[:10]
    return (start - timedelta(days=margin_days)).date().isoformat() <= d < (end + timedelta(days=margin_days)).date().isoformat()


def _parse_rss(raw):
    import feedparser
    feed = feedparser.parse(raw)
    out = []
    for e in feed.entries:
        link = e.get("link")
        if not link:
            continue
        when = e.get("published") or e.get("updated") or e.get("dc_date")
        at, d = parse_when(when) if when else (None, None)
        out.append({"url": link.strip(), "date_hint": d, "at_hint": at})
    return out, bool(feed.bozo) and not feed.entries


def _parse_sitemap(raw, fetcher, depth=0):
    out = []
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return out, True
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    if root.tag.endswith("sitemapindex") and depth == 0:
        for loc in root.findall("s:sitemap/s:loc", ns)[-3:]:  # 최근 하위 사이트맵 3개만
            sub = fetcher.get(loc.text.strip())
            if sub and sub.ok:
                items, _ = _parse_sitemap(sub.content, fetcher, depth + 1)
                out.extend(items)
        return out, False
    for u in root.findall("s:url", ns):
        loc = u.find("s:loc", ns)
        lm = u.find("s:lastmod", ns)
        if loc is None or not (loc.text or "").strip():
            continue
        at, d = parse_when(lm.text) if lm is not None and lm.text else (None, None)
        out.append({"url": loc.text.strip(), "date_hint": d, "at_hint": at})
    return out, False


def _parse_list(raw, content_type, base, selector):
    doc = lxml_html.fromstring(decode_html(raw, content_type))
    from lxml.cssselect import CSSSelector
    out = []
    for a in CSSSelector(selector)(doc):
        href = a.get("href")
        if href and not href.startswith(("#", "javascript:", "mailto:")):
            out.append({"url": urljoin(base, href.strip()), "date_hint": None, "at_hint": None})
    return out, False


def crawl_source(src, fetcher, start, end, margin_days, max_items):
    """반환: (후보 목록, 로그 dict)."""
    log = {"id": src["id"], "method": src["method"], "status": None, "items": 0, "kept": 0, "note": ""}
    if src["method"] not in CRAWL_METHODS:
        log["status"] = "skip"
        return [], log
    url = src.get("feed_url") or src.get("list_url")
    f = fetcher.get(url)
    log["status"] = f.status if f else None
    if not f or not f.ok:
        log["note"] = (f.error if f and f.error else f"HTTP {f.status if f else '-'}")
        return [], log
    if src["method"] == "rss":
        items, broken = _parse_rss(f.content)
    elif src["method"] == "sitemap":
        items, broken = _parse_sitemap(f.content, fetcher)
    else:
        if not src.get("list_selector"):
            log["note"] = "list_selector 없음"
            return [], log
        items, broken = _parse_list(f.content, f.content_type, f.final_url, src["list_selector"])
    if broken:
        log["note"] = "형식 오류"
    log["items"] = len(items)
    seen, kept = set(), []
    for it in items:
        if it["url"] in seen or not url_allowed(src, it["url"]):
            continue
        if not _in_scan_range(it["date_hint"], start, end, margin_days):
            continue
        seen.add(it["url"])
        kept.append({
            "url": it["url"], "date_hint": it["date_hint"], "at_hint": it["at_hint"],
            "discovery": {"route": "registry", "agent": "script", "source_id": src["id"], "query": None},
            "topic_hints": list(src.get("topics", [])), "track": src.get("track", "A"),
        })
        if len(kept) >= max_items:
            log["note"] = (log["note"] + f" 상한 {max_items}건 도달").strip()
            break
    log["kept"] = len(kept)
    return kept, log
