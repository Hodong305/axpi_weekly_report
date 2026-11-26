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
        f = fetcher.get(cand["url"])
        if f is None or not f.ok:
            failures.append((cand["url"], getattr(f, "status", None), "내려받기 실패"))
            continue
        if "svg" in f.content_type.lower():
            failures.append((cand["url"], f.status, "SVG는 쓰지 않음"))
            continue
        res = process_image(f.content, cfg)
        if isinstance(res, str):
            failures.append((cand["url"], f.status, res))
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
