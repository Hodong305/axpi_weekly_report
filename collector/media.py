"""이미지 후보 추출·내려받기(설계서 4.4 '이미지 후보'). 공식 발표 발행처의 complete 레코드만 대상이다.
후보: og:image/twitter:image 1개 + 본문 영역(article·main)의 <img> 중 figcaption이나 alt가 있는 것. 로고·아이콘·SVG·추적 픽셀·너무 작은 것은 뺀다.
받은 이미지는 긴 변이 media_max_px를 넘으면 줄이고 JPEG(품질 82)로 저장한다(PNG 도식은 PNG 유지)."""
import hashlib
import io
import re
import zipfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

from .ref import normalize_text

SKIP_WORDS = re.compile(r"logo|icon|avatar|sprite|favicon|spacer|pixel|tracking", re.I)


def _text(el):
    return normalize_text(el.get_text(" ")) if el is not None else ""


def extract_candidates(content, base_url, cfg):
    """HTML 바이트에서 이미지 후보를 뽑는다(내려받기 전). 반환: [{kind, url, caption, alt}] (og 먼저, 문서 순서, 최대 media_max_per_doc개)"""
    c = cfg["collection"]
    soup = BeautifulSoup(content, "lxml")
    out, seen = [], set()

    def add(kind, src, caption, alt, el=None):
        if not src or src.startswith("data:"):
            return
        url = urljoin(base_url, src.strip())
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.path.lower().endswith(".svg") or url in seen:
            return
        name = Path(parts.path).name
        marks = " ".join([name] + (el.get("class", []) if el is not None else []) + ([el.get("id", "")] if el is not None else []))
        if SKIP_WORDS.search(marks):
            return
        if el is not None:      # 태그에 크기가 적혀 있으면 미리 걸러 낸다
            for attr in ("width", "height"):
                v = re.match(r"\d+", str(el.get(attr, "")))
                if v and int(v.group()) < c["media_min_px"]:
                    return
        seen.add(url)
        out.append({"kind": kind, "url": url, "caption": caption or None, "alt": alt or None})

    og = soup.find("meta", attrs={"property": "og:image"}) or soup.find("meta", attrs={"name": "twitter:image"}) \
        or soup.find("meta", attrs={"property": "twitter:image"})
    if og:
        add("og_image", og.get("content"), None, None)
    area = soup.find("article") or soup.find("main")
    for img in (area.find_all("img") if area else []):
        fig = img.find_parent("figure")
        cap = _text(fig.find("figcaption")) if fig is not None and fig.find("figcaption") else ""
        alt = normalize_text(img.get("alt") or "")
        if not (cap or alt):
            continue
        src = img.get("src") or img.get("data-src") or (img.get("srcset") or "").split(",")[0].split(" ")[0]
        add("figure", src, cap, alt, img)
    return out[: c["media_max_per_doc"]]


# PDF 그림 ---------------------------------------------------------------
# MarkItDown은 PDF를 글자로만 옮겨 그림·차트가 빠진다. 그래서 그림은 원본 PDF의 해당 쪽을 다시 열어
# 그림 영역만 이미지로 잘라 낸다(삽입된 사진 + 벡터로 그린 차트·도식). 후보 url은 `PDF주소#page=N`.

CAPTION_RE = re.compile(r"^\s*[\[<(]?\s*(Figure|Fig\.?|Chart|Exhibit|Graph|Diagram|그림|도표|차트|자료)\s*[\dⅠ-Ⅻ]+", re.I)  # 번호가 붙어야 캡션
PDF_PAD = 8           # 잘라 낼 때 둘레 여백(pt): 축 이름·범례가 잘리지 않게
PDF_GAP = 18          # 이 거리(pt) 안의 그래픽 요소는 한 그림으로 묶는다
PDF_MIN_OBJS = 6      # 벡터 요소가 이보다 적은 묶음은 그림이 아니다(밑줄·표 테두리)
PDF_MIN_PT = 110      # 잘라 낸 영역의 가로·세로가 이보다 작으면 뺀다(아이콘·로고)


def _boxes_overlap(a, b, gap):
    return not (a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1])


def _union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _merge(clusters):
    """겹치거나 PDF_GAP 안으로 붙은 묶음([box, n_vector, n_image])을 하나로 합친다(제자리)."""
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _boxes_overlap(clusters[i][0], clusters[j][0], PDF_GAP):
                    a, b = clusters[i], clusters.pop(j)
                    a[0], a[1], a[2] = _union(a[0], b[0]), a[1] + b[1], a[2] + b[2]
                    merged = True
                    break
            if merged:
                break


def _page_regions(page):
    """한 쪽에서 그림 영역(x0, top, x1, bottom, 종류)을 찾는다. 종류: photo(삽입 이미지만) / chart(벡터 포함)."""
    pw, ph = float(page.width), float(page.height)
    items = []      # (box, is_vector)
    for im in page.images:
        box = (float(im["x0"]), float(im["top"]), float(im["x1"]), float(im["bottom"]))
        if (box[2] - box[0]) * (box[3] - box[1]) < 0.95 * pw * ph:      # 쪽 전체 배경은 뺀다
            items.append((box, False))
    for o in list(page.curves) + list(page.rects):
        w, h = float(o["x1"]) - float(o["x0"]), float(o["bottom"]) - float(o["top"])
        if w < 1.5 or h < 1.5 or w * h > 0.6 * pw * ph:      # 선처럼 얇은 것·쪽 배경 상자
            continue
        items.append(((float(o["x0"]), float(o["top"]), float(o["x1"]), float(o["bottom"])), True))
    clusters = []   # [box, n_vector, n_image]
    for box, vec in sorted(items, key=lambda t: (t[0][1], t[0][0])):
        for c in clusters:
            if _boxes_overlap(c[0], box, PDF_GAP):
                c[0] = _union(c[0], box)
                c[1 if vec else 2] += 1
                break
        else:
            clusters.append([box, int(vec), int(not vec)])
    _merge(clusters)    # 묶음끼리도 겹치면 다시 합친다
    clusters = [c for c in clusters if c[2] or c[1] >= PDF_MIN_OBJS]
    # 축 이름·범례·제목 같은 그림 안팎의 글자 줄을 영역에 넣는다. 본문 문단(쪽 폭의 절반을 넘는 긴 줄)과 캡션 줄은 넣지 않는다
    try:
        lines = page.extract_text_lines(return_chars=False)
    except Exception:
        lines = []
    for c in clusters:
        for _ in range(3):
            grown = c[0]
            for ln in lines:
                lb = (float(ln["x0"]), float(ln["top"]), float(ln["x1"]), float(ln["bottom"]))
                if (lb[2] - lb[0]) > 0.5 * pw or CAPTION_RE.match(ln["text"]):
                    continue
                if _boxes_overlap(grown, lb, 10):
                    grown = _union(grown, lb)
            if grown == c[0]:
                break
            c[0] = grown
    _merge(clusters)
    out = []
    for box, nvec, nimg in clusters:
        x0, t, x1, b = max(0, box[0] - PDF_PAD), max(0, box[1] - PDF_PAD), min(pw, box[2] + PDF_PAD), min(ph, box[3] + PDF_PAD)
        if x1 - x0 < PDF_MIN_PT or b - t < PDF_MIN_PT:
            continue
        out.append((x0, t, x1, b, "chart" if nvec else "photo"))
    return out


def _caption_near(page, box):
    """그림 바로 아래(없으면 바로 위) 줄이 'Figure 1 …', '그림 2 …'처럼 시작하면 그 문장을 캡션으로 쓴다.
    반환: (캡션, 잘라 낼 영역). 캡션 줄이 여백에 걸리면 영역을 캡션 앞에서 끊는다(그림 이미지에 캡션 글자가 끼지 않게)."""
    try:
        lines = page.extract_text_lines(return_chars=False)
    except Exception:
        return None, box
    below = [ln for ln in lines if box[3] - PDF_PAD - 6 <= float(ln["top"]) <= box[3] + 40]
    above = [ln for ln in lines if box[1] - 40 <= float(ln["bottom"]) <= box[1] + PDF_PAD + 6]
    below, above = sorted(below, key=lambda l: l["top"]), sorted(above, key=lambda l: -l["top"])
    for group in (below, above):
        for i, ln in enumerate(group):
            if CAPTION_RE.match(ln["text"]):
                text = ln["text"]
                if group is below:      # 캡션이 두 줄 이상이면 바로 다음 줄까지 붙인다
                    text += " " + " ".join(l["text"] for l in group[i + 1:i + 2])
                    box = box[:3] + (min(box[3], float(ln["top"]) - 1),) + box[4:]
                else:
                    box = box[:1] + (max(box[1], float(ln["bottom"]) + 1),) + box[2:]
                return normalize_text(text)[:300] or None, box
    return None, box


def _render(page, box, cfg, photo):
    """쪽의 box 영역만 이미지로 그린다(긴 변이 media_max_px 정도가 되도록 해상도를 고른다). 차트는 PNG, 사진은 JPEG 원본."""
    long_pt = max(box[2] - box[0], box[3] - box[1])
    dpi = max(72, min(300, int(cfg["collection"]["media_max_px"] * 72 / long_pt)))
    im = page.crop(box[:4], strict=False).to_image(resolution=dpi).original.convert("RGB")
    buf = io.BytesIO()
    if photo:
        im.save(buf, "JPEG", quality=92)
    else:
        im.save(buf, "PNG")
    return buf.getvalue()


def pdf_candidates(content, base_url, cfg, pages=None):
    """PDF 바이트에서 그림 후보를 만든다. 반환: [{kind, url, caption, alt, data}] (data = 이미 그린 이미지 바이트).
    pages(쪽 번호 목록)가 있으면 그 쪽 전체를 먼저 후보로 넣는다(overrides.yaml의 media_pages: 자동 검출이 놓친 그림)."""
    import pdfplumber
    c = cfg["collection"]
    url = base_url.split("#", 1)[0]
    forced, found = [], []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for n in pages or []:
            if 1 <= int(n) <= len(pdf.pages):
                page = pdf.pages[int(n) - 1]
                box = (0, 0, float(page.width), float(page.height), "chart")
                forced.append({"kind": "figure", "url": f"{url}#page={int(n)}", "caption": None, "alt": None,
                               "data": _render(page, box, cfg, False), "page": int(n)})
        for n, page in enumerate(pdf.pages, start=1):
            if len(found) >= 4 * c["media_max_per_doc"]:
                break
            for box in _page_regions(page):
                cap, box = _caption_near(page, box)
                found.append((n, page, box, cap))
        # 캡션이 붙은 그림을 먼저 고르고, 문서 순서대로 놓는다
        picked = sorted(found, key=lambda t: (t[3] is None, t[0], t[2][1]))[: max(0, c["media_max_per_doc"] - len(forced))]
        picked.sort(key=lambda t: (t[0], t[2][1]))
        for n, page, box, cap in picked:
            forced.append({"kind": "figure", "url": f"{url}#page={n}", "caption": cap, "alt": None,
                           "data": _render(page, box, cfg, box[4] == "photo"), "page": n})
    return forced[: c["media_max_per_doc"]]


def process_image(data, cfg):
    """내려받은 바이트를 검사·변환한다. 반환: (바이트, mime, 가로, 세로) 또는 사유 문자열(제외)."""
    c = cfg["collection"]
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        return "이미지로 읽을 수 없음"
    w, h = im.size
    if min(w, h) < c["media_min_px"]:
        return f"너무 작음({w}x{h} < {c['media_min_px']}px)"
    keep_png = im.format == "PNG"
    long_side = max(w, h)
    if long_side > c["media_max_px"]:
        k = c["media_max_px"] / long_side
        im = im.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
    buf = io.BytesIO()
    if keep_png:
        im.save(buf, "PNG", optimize=True)
        mime = "image/png"
    else:
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        im.save(buf, "JPEG", quality=82, optimize=True)
        mime = "image/jpeg"
    return buf.getvalue(), mime, im.size[0], im.size[1]


def download(fetcher, cands, doc_id, media_dir, cfg):
    """후보를 내려받아 media_dir에 저장한다. 반환: (media 항목 목록, 실패 관찰 목록[(url, http_status, 사유)])"""
    media_dir = Path(media_dir)
    media_dir.mkdir(parents=True, exist_ok=True)
    items, failures = [], []
    for cand in cands:
        if cand.get("data") is not None:        # PDF 쪽에서 이미 그려 둔 그림(pdf_candidates)
            raw, status = cand["data"], 200
        else:
            f = fetcher.get(cand["url"])
            if f is None or not f.ok:
                failures.append((cand["url"], getattr(f, "status", None), "내려받기 실패"))
                continue
            if "svg" in f.content_type.lower():
                failures.append((cand["url"], f.status, "SVG는 쓰지 않음"))
                continue
            raw, status = f.content, f.status
        res = process_image(raw, cfg)
        if isinstance(res, str):
            failures.append((cand["url"], status, res))
            continue
        data, mime, w, h = res
        n = len(items) + 1
        name = f"media/{doc_id[:12]}-M{n}.{'png' if mime == 'image/png' else 'jpg'}"
        (media_dir.parent / name).write_bytes(data)
        items.append({"id": f"M{n}", "kind": cand["kind"], "url": cand["url"], "caption": cand["caption"], "alt": cand["alt"],
                      "file": name, "mime": mime, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "width": w, "height": h})
    return items, failures


def write_zip(path, files):
    """이미지 묶음: files = [(압축 안 이름, 바이트)]. 같은 입력이면 같은 바이트가 되도록 이름순·고정 시각으로 쓴다."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        for name, data in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, data)
