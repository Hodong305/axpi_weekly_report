"""이미 만든 호(review/{week}-draft.yaml·state.json·사외 패키지)를 다른 레이아웃으로 다시 그려 본다(원고·상태는 고치지 않는다).

  python tools/preview_issue.py --issue-week 2026-W48 --theme B7 [--overlay 덮어쓸.yaml] [--out out_internal/2026-W48-preview]

--overlay: 기사별로 덮어쓸 값. 예) {EVT-2026W48-32: {area: maintenance, stage: scaled}, EVT-2026W48-40: {company: Lam Research}}
  area: process·metrology·defect·logistics·maintenance·test·fabwide / stage: plan·pilot·deployed·scaled (internal/render.py AREAS·STAGES)
결과: {out}/{week}-{theme}.html(미리 보기, 이미지 내장)과 .txt, --eml이면 .eml(메일 프로그램으로 열 수 있는 초안, 이미지 cid 내장)
발송본과 같은 마지막 검사(가상·견본 데이터 흔적)를 거친다.
"""
import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from internal import checks, metrics, render, settings as settings_mod  # noqa: E402
from internal.draft import load_draft  # noqa: E402
from internal.intake import load_package  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue-week", required=True)
    ap.add_argument("--theme", default="B7")
    ap.add_argument("--overlay")
    ap.add_argument("--out")
    ap.add_argument("--eml", action="store_true")
    ap.add_argument("--draft", help="원고 대신 쓸 사본(예: 문체만 고친 원고). 생략하면 review/{week}-draft.yaml")
    ap.add_argument("--suffix", default="", help="결과 파일 이름 끝에 붙일 말(예: -humanized)")
    a = ap.parse_args()
    s = settings_mod.load()
    cfg = s.internal
    week = a.issue_week
    d = load_draft(Path(a.draft) if a.draft else s.draft_path(week))
    if a.overlay:
        over = yaml.safe_load(Path(a.overlay).read_text(encoding="utf-8")) or {}
        for art in d["articles"]:
            art.update(over.get(art["event_id"]) or {})
    state = json.loads(s.state_path(week).read_text(encoding="utf-8"))
    pkg = load_package(state["package_dir"], s.lead_min_chars, s.intake_min, week)
    recs = pkg.records

    def load_image(media_ref):
        doc_id, _, mid = media_ref.partition("#")
        rec = recs.get(doc_id)
        m = next((x for x in (rec or {}).get("media") or [] if x["id"] == mid), None)
        data = pkg.image_bytes(m["file"]) if m else None
        return (data, rec["publisher"]) if data else None

    pub = len([e for e in state["events"] if e["slot"] in ("core", "brief")])
    ctx = {"issue_week": week, "period": render.period_text(pkg.doc["window_start"], pkg.doc["window_end_exclusive"]),
           "period_short": render.period_short(pkg.doc["window_start"], pkg.doc["window_end_exclusive"]),
           "issue_type": state["issue_type"], "counts": {"pass_docs": state["counts"]["pass_docs"], "published": pub},
           "events": state["events"], "status_rows": [], "corrections": [], "note": "",
           "newsletter_name": cfg["newsletter_name"], "newsletter_tagline": cfg["newsletter_tagline"], "newsletter_team": cfg["newsletter_team"],
           "theme": a.theme, "issue_no": metrics.issue_number(s.pilot_csv, week), "n_docs": len(recs),
           "competitors": [c["name"] for c in cfg.get("competitors") or []],
           "image_loader": load_image, "image_max_kb": cfg["image_max_kb"], "read_chars_per_min": cfg.get("read_chars_per_min", 500)}
    built = render.build_all(d, ctx)
    left = checks.placeholder_hits(d, built["text"])
    if left:
        raise SystemExit("가상·견본 데이터 흔적이 있어 만들지 않았습니다: " + ", ".join(f"{h['scope']} '{h['quote']}'" for h in left[:10]))
    out = Path(a.out) if a.out else s.out_dir(week).parent / f"{week}-preview"
    out.mkdir(parents=True, exist_ok=True)
    html_path = out / f"{week}-{a.theme}{a.suffix}.html"
    html_path.write_text(built["html_preview"], encoding="utf-8")
    html_path.with_suffix(".txt").write_text(built["text"], encoding="utf-8")
    if a.eml:
        ctx["newsletter_from"] = cfg.get("newsletter_from", "")
        html_path.with_suffix(".eml").write_bytes(render.build_eml(d, ctx, built).as_bytes())
    print(html_path)


if __name__ == "__main__":
    main()
