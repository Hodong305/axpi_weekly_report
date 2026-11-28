"""원문 바이트 → 메타데이터와 본문 블록 (설계서 4.4)."""
import io
import json
import re
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from importlib.metadata import version as pkg_version
from urllib.parse import urljoin, urlsplit

from bs4 import UnicodeDammit
from lxml import etree, html as lxml_html

from .ref import assign_ids, normalize_text

ARTICLE_TYPES = {"newsarticle", "article", "blogposting", "report", "pressrelease", "techarticle",
                 "scholarlyarticle", "analysisnewsarticle", "reportagenewsarticle", "webpage", "videoobject"}


def _ver(pkg):
    try:
        return pkg_version(pkg)
    except Exception:
        return "unknown"


# 날짜 --------------------------------------------------------------------

DATE_FLOOR = date(1995, 1, 1)      # 이보다 이른 발행일은 사이트의 기본값·자리 표시로 본다(1970-01-01 등)
DATE_FUTURE_DAYS = 2                # 수집일보다 이 날수를 넘어 미래인 발행일도 믿지 않는다


def plausible_date(d, today=None):
    """발행일(YYYY-MM-DD)이 말이 되는지. 1995-01-01 이전(1970-01-01 같은 기본값)이나 수집일 +2일 이후면 False.
    False인 값은 버리고 다음 날짜 출처로 넘어간다(설계서 4.4). 남는 출처가 없으면 DATE_UNCERTAIN → 수동 보완 큐."""
    if not d:
        return False
    try:
        dd = date.fromisoformat(str(d)[:10])
    except ValueError:
        return False
    today = today or date.today()
    return DATE_FLOOR <= dd <= today + timedelta(days=DATE_FUTURE_DAYS)


def parse_when(value):
    """문자열 → (published_at 또는 None, published_date 또는 None).
    오프셋이 있는 시각만 published_at으로 쓴다. 시각만 있고 오프셋이 없으면 날짜만 쓴다."""
    if not value or not isinstance(value, str):
        return None, None
    v = value.strip()
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            return dt.replace(microsecond=0).isoformat(), dt.date().isoformat()
        return None, dt.date().isoformat()
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(v)  # RFC 822 (피드)
        if dt.tzinfo is not None:
            return dt.replace(microsecond=0).isoformat(), dt.date().isoformat()
        return None, dt.date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", v)
    if m:
        try:
            return None, date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return None, None
    return None, None


def date_from_url(url):
    m = re.search(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|$|[-_])", urlsplit(url).path)
    if not m:
        return None
    try:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        return None


# JSON-LD ------------------------------------------------------------------

def _flatten(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _flatten(x)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            yield from _flatten(obj["@graph"])
        yield obj


def _types(o):
    t = o.get("@type", [])
    t = t if isinstance(t, list) else [t]
    return {str(x).lower() for x in t}


def jsonld_article(doc):
    best = None
    for node in doc.xpath('//script[@type="application/ld+json"]'):
        raw = (node.text or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for o in _flatten(data):
            ts = _types(o)
            if ts & ARTICLE_TYPES:
                # WebPage보다 기사 유형을 우선한다
                if best is None or ("webpage" in _types(best) and "webpage" not in ts):
                    best = o
    return best or {}


def _author_names(a):
    out = []
    for x in a if isinstance(a, list) else [a]:
        if isinstance(x, dict) and x.get("name"):
            out.append(str(x["name"]).strip())
        elif isinstance(x, str) and x.strip():
            out.append(x.strip())
    return ", ".join(dict.fromkeys(out)) or None


def _meta(doc, *names):
    for n in names:
        for attr in ("property", "name", "itemprop"):
            v = doc.xpath(f'//meta[@{attr}="{n}"]/@content')
            if v and v[0].strip():
                return v[0].strip()
    return None


# HTML ---------------------------------------------------------------------

def decode_html(raw, content_type=""):
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    encs = [m[1]] if m else []
    return UnicodeDammit(raw, encs, is_html=True).unicode_markup or ""


def html_metadata(text, final_url):
    doc = lxml_html.fromstring(text)
    ld = jsonld_article(doc)
    m = {}
    canon = doc.xpath('//link[translate(@rel,"CANONICAL","canonical")="canonical"]/@href')
    m["canonical"] = urljoin(final_url, canon[0].strip()) if canon and canon[0].strip() else None
    m["rel_next"] = bool(doc.xpath('//link[translate(@rel,"NEXT","next")="next"]/@href'))
    lang = doc.xpath("/html/@lang")
    m["language"] = lang[0].strip() if lang and lang[0].strip() else None

    pub_src = [("json-ld datePublished", ld.get("datePublished") or ld.get("uploadDate")),
               ("article:published_time", _meta(doc, "article:published_time", "datePublished")),
               ("time[datetime]", (doc.xpath("//time/@datetime") or [None])[0])]
    m["published_at"] = m["published_date"] = m["date_source"] = None
    m["date_rejected"] = []
    for src, val in pub_src:
        at, d = parse_when(val)
        if d and not plausible_date(d):
            m["date_rejected"].append(f"{src}={d}")       # 사이트 기본값(1970-01-01 등)은 버리고 다음 출처로
            continue
        if d:
            m["published_at"], m["published_date"], m["date_source"] = at, d, src
            break
    m["modified_at"], _ = parse_when(ld.get("dateModified") or _meta(doc, "article:modified_time"))

    site = _meta(doc, "og:site_name")
    m["site_name"] = site
    title = ld.get("headline") if isinstance(ld.get("headline"), str) else None
    title = title or _meta(doc, "og:title")
    if not title:
        t = doc.xpath("//title/text()")
        title = t[0] if t else None
        if title and site:
            for sep in (" | ", " - ", " – ", " — ", " :: "):
                if title.endswith(sep + site):
                    title = title[: -len(sep + site)]
    m["title"] = normalize_text(title) if title else None
    m["author"] = _author_names(ld.get("author")) if ld.get("author") else _meta(doc, "author")
    free = ld.get("isAccessibleForFree")
    m["paywalled"] = free is False or str(free).lower() == "false"
    page_title = (doc.xpath("//title/text()") or [""])[0]
    m["page_title"] = normalize_text(page_title)
    body = doc.xpath("//body")
    m["page_text"] = normalize_text(body[0].text_content()) if body else ""
    m["has_main_table"] = bool(doc.xpath("//article//table | //main//table"))
    m["is_video"] = "videoobject" in _types(ld) if ld else False
    return m


def _cell_text(el):
    return normalize_text(" ".join(el.itertext()))


def html_blocks(text):
    import trafilatura
    xml = trafilatura.extract(text, output_format="xml", include_tables=True, include_comments=False,
                              favor_recall=True, include_images=False, include_formatting=False,
                              deduplicate=False)
    if not xml:
        return []
    root = etree.fromstring(xml.encode("utf-8"))
    main = root.find("main")
    if main is None:
        return []
    blocks = []

    def add(kind, t, keep=False):
        t = normalize_text(t, keep_newlines=keep)
        if t:
            blocks.append({"id": "", "kind": kind, "text": t, "page": None})

    def walk(el):
        tag = el.tag
        if tag == "head":
            add("heading", " ".join(el.itertext()))
        elif tag in ("p", "quote"):
            if tag == "quote" and el.find("p") is not None:
                for c in el:
                    walk(c)
            else:
                add("paragraph", " ".join(el.itertext()))
        elif tag == "list":
            for item in el.findall("item"):
                add("paragraph", " ".join(item.itertext()))
        elif tag == "table":
            rows = []
            for row in el.iter("row"):
                cells = [_cell_text(c) for c in row.findall("cell")]
                if any(cells):
                    rows.append(" | ".join(cells))
            add("table", "\n".join(rows), keep=True)
        elif tag in ("div", "section"):
            for c in el:
                walk(c)
        else:
            add("paragraph", " ".join(el.itertext()))

    for child in main:
        walk(child)
    return assign_ids(blocks)


def extract_html(raw, content_type, final_url):
    text = decode_html(raw, content_type)
    if not text.strip():
        return {"meta": {}, "blocks": [], "extractor": f"trafilatura {_ver('trafilatura')}", "scope": "article"}
    meta = html_metadata(text, final_url)
    blocks = html_blocks(text)
    return {"meta": meta, "blocks": blocks, "extractor": f"trafilatura {_ver('trafilatura')}",
            "scope": "video_description" if meta.get("is_video") else "article"}


# PDF ----------------------------------------------------------------------

_END = re.compile(r"[.!?。！？다요]\)?[\"'”’]?$")


def _pdf_paragraphs(page_text):
    lines = [ln.strip() for ln in page_text.split("\n")]
    if any(not ln for ln in lines):  # 빈 줄로 문단이 나뉘어 있으면 그대로 따른다
        chunks, cur = [], []
        for ln in lines:
            if ln:
                cur.append(ln)
            elif cur:
                chunks.append(" ".join(cur)); cur = []
        if cur:
            chunks.append(" ".join(cur))
        return chunks
    lines = [ln for ln in lines if ln]
    if not lines:
        return []
    width = max(len(ln) for ln in lines)
    chunks, cur = [], []
    for ln in lines:
        cur.append(ln)
        if _END.search(ln) and len(ln) < 0.8 * width:
            chunks.append(" ".join(cur)); cur = []
    if cur:
        chunks.append(" ".join(cur))
    return chunks


_MD_TABLE_SEP = re.compile(r"^\|?[\s:|-]+\|?$")


def _markitdown_pages(raw):
    """PDF를 한 쪽씩 떼어 MarkItDown으로 변환한다(쪽 번호를 블록에 남기기 위해). 반환: [(쪽, 마크다운)]
    MarkItDown은 글자·표만 옮기고 그림·차트는 버린다. 그림은 media.pdf_candidates가 원본 PDF 쪽에서 따로 잘라 낸다."""
    import pypdfium2 as pdfium
    from markitdown import MarkItDown, StreamInfo
    md = MarkItDown(enable_plugins=False)
    info = StreamInfo(extension=".pdf", mimetype="application/pdf")
    src = pdfium.PdfDocument(raw)
    out = []
    try:
        for i in range(len(src)):
            one = pdfium.PdfDocument.new()
            one.import_pages(src, [i])
            buf = io.BytesIO()
            one.save(buf)
            one.close()
            out.append((i + 1, md.convert_stream(io.BytesIO(buf.getvalue()), stream_info=info).markdown or ""))
    finally:
        src.close()
    return out


def _markdown_blocks(text, page):
    """한 쪽 분량 마크다운 → 블록. 표(| 줄)는 table, # 줄은 heading, 나머지는 문단으로 나눈다."""
    blocks, prose = [], []

    def flush_prose():
        if prose:
            for para in _pdf_paragraphs("\n".join(prose)):
                t = normalize_text(para)
                if t:
                    blocks.append({"id": "", "kind": "paragraph", "text": t, "page": page})
            prose.clear()

    rows = []

    def flush_table():
        if rows:
            t = normalize_text("\n".join(rows), keep_newlines=True)
            if t:
                blocks.append({"id": "", "kind": "table", "text": t, "page": page})
            rows.clear()

    for ln in text.replace("\x0c", "\n").split("\n"):
        s = ln.strip()
        if s.startswith("|"):
            flush_prose()
            if not _MD_TABLE_SEP.match(s):
                cells = [normalize_text(c) for c in s.strip("|").split("|")]
                if any(cells):
                    rows.append(" | ".join(cells))
            continue
        flush_table()
        if s.startswith("#"):
            flush_prose()
            t = normalize_text(s.lstrip("#"))
            if t:
                blocks.append({"id": "", "kind": "heading", "text": t, "page": page})
            continue
        prose.append(s)
    flush_table()
    flush_prose()
    return blocks


def extract_pdf(raw):
    """PDF 본문: MarkItDown(쪽 단위)으로 글자·표를 옮긴다. MarkItDown이 없거나 실패하면 pdfplumber로 대신하고 notes에 남긴다.
    메타데이터(제목·작성자·CreationDate·쪽 수)는 pdfplumber로 읽는다."""
    import pdfplumber
    blocks, notes, meta = [], [], {}
    missing_pages = []
    try:
        md_pages = _markitdown_pages(raw)
        extractor = f"markitdown {_ver('markitdown')}"
    except Exception as e:      # 설치 안 됨·변환 실패
        md_pages = None
        extractor = f"pdfplumber {_ver('pdfplumber')}"
        notes.append(f"MarkItDown 변환 실패, pdfplumber로 대신함: {type(e).__name__}: {str(e)[:120]}")
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        info = pdf.metadata or {}
        meta["title"] = normalize_text(info.get("Title", "")) or None
        meta["author"] = normalize_text(info.get("Author", "")) or None
        cd = info.get("CreationDate") or ""
        m = re.match(r"D:(\d{4})(\d{2})(\d{2})", cd)
        meta["pdf_creation_date"] = f"{m[1]}-{m[2]}-{m[3]}" if m else None
        meta["pages"] = len(pdf.pages)
        if md_pages is not None:
            for i, text in md_pages:
                got = _markdown_blocks(text, i)
                if not got:
                    missing_pages.append(i)
                blocks += got
        else:
            for i, page in enumerate(pdf.pages, start=1):
                txt = page.extract_text() or ""
                if not txt.strip():
                    missing_pages.append(i)
                    continue
                for para in _pdf_paragraphs(txt):
                    t = normalize_text(para)
                    if t:
                        blocks.append({"id": "", "kind": "paragraph", "text": t, "page": i})
                for tbl in page.extract_tables() or []:
                    rows = [" | ".join(normalize_text(c or "") for c in row) for row in tbl if any(c for c in row)]
                    t = normalize_text("\n".join(rows), keep_newlines=True)
                    if t:
                        blocks.append({"id": "", "kind": "table", "text": t, "page": i})
    if missing_pages:
        notes.append(f"텍스트 없는 페이지(스캔본 추정): {missing_pages}")
    meta["missing_pages"] = missing_pages
    if not meta.get("title") and blocks:
        meta["title"] = blocks[0]["text"][:200]
    return {"meta": meta, "blocks": assign_ids(blocks), "extractor": extractor, "scope": "pdf", "notes": notes}
