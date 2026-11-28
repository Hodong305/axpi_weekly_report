"""PDF 소스: 본문은 MarkItDown(쪽 단위), 그림·차트는 원본 PDF 쪽을 다시 열어 잘라 낸다(media.pdf_candidates)."""
import io

from PIL import Image

from collector import extract, media
from collector.settings import load_settings


def _pdf(pages):
    """pages: [(본문 줄 목록, 막대 개수)] → 최소 PDF 바이트. 막대는 벡터 사각형으로 그린 '차트', 아래에 Figure 캡션."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines, bars in pages:
        ops = ["BT /F1 11 Tf 72 740 Td 14 TL"] + [f"({t}) Tj T*" for t in lines] + ["ET"]
        if bars:
            ops.append("0.2 0.4 0.8 rg")
            for i in range(bars):
                ops.append(f"{100 + i * 40} 400 28 {40 + i * 25} re f")
            ops.append(f"BT /F1 9 Tf 100 380 Td (Chart Title Axis) Tj ET")
            ops.append(f"BT /F1 10 Tf 100 360 Td (Figure 1. Fab yield by line.) Tj ET")
        stream = "\n".join(ops).encode()
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {len(objs)} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out, offs = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for n, o in enumerate(objs, 1):
        offs.append(out.tell())
        body = o if isinstance(o, bytes) else o.encode()
        out.write(f"{n} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offs:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer << /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


RAW = _pdf([(["Samsung expands AI fab automation across lines.", "", "The pilot cut defects by 12 percent."], 0),
            (["Second page text about predictive maintenance."], 9)])


def test_pdf_body_uses_markitdown_with_pages():
    x = extract.extract_pdf(RAW)
    assert x["extractor"].startswith("markitdown ")
    assert x["scope"] == "pdf" and x["meta"]["pages"] == 2
    texts = {b["page"]: " ".join(t["text"] for t in x["blocks"] if t["page"] == b["page"]) for b in x["blocks"]}
    assert "AI fab automation" in texts[1] and "predictive maintenance" in texts[2]
    assert [b["id"] for b in x["blocks"]][0] == "P1"


def test_pdf_falls_back_to_pdfplumber(monkeypatch):
    def boom(raw):
        raise RuntimeError("no markitdown")
    monkeypatch.setattr(extract, "_markitdown_pages", boom)
    x = extract.extract_pdf(RAW)
    assert x["extractor"].startswith("pdfplumber ")
    assert any("MarkItDown 변환 실패" in n for n in x["notes"])
    assert any("predictive maintenance" in b["text"] for b in x["blocks"])


def test_markdown_table_becomes_table_block():
    md = "# Results\n\nIntro line.\n\n| Line | Yield |\n|---|---|\n| A | 91% |\n\nAfter table."
    b = extract._markdown_blocks(md, 3)
    assert [x["kind"] for x in b] == ["heading", "paragraph", "table", "paragraph"]
    assert b[2]["text"] == "Line | Yield\nA | 91%" and all(x["page"] == 3 for x in b)


def test_pdf_chart_is_cropped_from_its_page_with_caption():
    cfg = load_settings().c
    cands = media.pdf_candidates(RAW, "https://example.com/r.pdf", cfg)
    assert len(cands) == 1
    c = cands[0]
    assert c["url"] == "https://example.com/r.pdf#page=2" and c["kind"] == "figure"
    assert c["caption"].startswith("Figure 1. Fab yield")
    im = Image.open(io.BytesIO(c["data"]))
    assert im.width > im.height           # 쪽 전체가 아니라 차트 영역만
    res = media.process_image(c["data"], cfg)
    assert not isinstance(res, str) and res[1] == "image/png"


def test_media_pages_override_renders_whole_page_first():
    cfg = load_settings().c
    cands = media.pdf_candidates(RAW, "https://example.com/r.pdf", cfg, pages=[1])
    assert [c["url"][-7:] for c in cands] == ["#page=1", "#page=2"]
    im = Image.open(io.BytesIO(cands[0]["data"]))
    assert abs(im.width / im.height - 612 / 792) < 0.02


def test_download_uses_prerendered_pdf_data(tmp_path):
    cfg = load_settings().c
    cands = media.pdf_candidates(RAW, "https://example.com/r.pdf", cfg)

    class NoFetch:
        def get(self, url):
            raise AssertionError("PDF 그림은 다시 내려받지 않는다")
    items, fails = media.download(NoFetch(), cands, "url1_" + "a" * 64, tmp_path / "media", cfg)
    assert not fails and items[0]["file"].startswith("media/url1_aaaaaaa-M1.") and items[0]["url"].endswith("#page=2")
