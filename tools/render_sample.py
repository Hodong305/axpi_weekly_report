"""견본 원고로 소식지 발송본을 테마별로 만들어 비교한다(설계서 v4.7 9.8).

  python tools/render_sample.py [--draft docs/samples/sample-draft-v4.7.yaml] [--themes B3 B4 B5] [--out out_internal/samples] [--png]

테마마다 {theme}.html(미리 보기, 맨 위에 '견본' 띠)과 {theme}.txt를 만들고, --png이면 휴대폰 폭(390px) 화면 캡처도 만든다(playwright 필요).
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from internal import render, settings as settings_mod  # noqa: E402

BANNER = ('<div style="background:#fde68a;color:#78350f;font:bold 13px sans-serif;text-align:center;padding:8px;">'
          '견본 · 레이아웃 확인용 가상 내용입니다 (테마 {theme})</div>')


def sample_ctx(theme, cfg):
    return {"issue_week": "2026-W48", "period": "2026-11-15 ~ 2026-11-21", "period_short": "11.15 ~ 11.21", "issue_type": "정규호",
            "counts": {"pass_docs": 14, "published": 6}, "events": [], "status_rows": [], "corrections": [], "note": "",
            "newsletter_name": cfg["newsletter_name"], "newsletter_tagline": cfg["newsletter_tagline"], "newsletter_team": cfg["newsletter_team"],
            "theme": theme, "issue_no": 1, "n_docs": 39, "read_chars_per_min": cfg.get("read_chars_per_min", 500)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", default=str(ROOT / "docs" / "samples" / "sample-draft-v4.7.yaml"))
    ap.add_argument("--themes", nargs="+", default=["B3", "B4", "B5"])
    ap.add_argument("--out", default=str(ROOT / "out_internal" / "samples"))
    ap.add_argument("--png", action="store_true")
    a = ap.parse_args()
    cfg = settings_mod.load().internal
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    pages = []
    for th in a.themes:
        draft = yaml.safe_load(Path(a.draft).read_text(encoding="utf-8"))
        built = render.build_all(draft, sample_ctx(th, cfg))
        doc = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + BANNER.format(theme=th), built["html_preview"], count=1)
        (out / f"{th}.html").write_text(doc, encoding="utf-8")
        (out / f"{th}.txt").write_text(built["text"], encoding="utf-8")
        pages.append(out / f"{th}.html")
        print(out / f"{th}.html")
    if a.png:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 390, "height": 800}, device_scale_factor=2)
            for f in pages:
                pg.goto(f.resolve().as_uri())
                png = f.with_suffix(".png")
                pg.screenshot(path=str(png), full_page=True)
                print(png)
            b.close()


if __name__ == "__main__":
    main()
