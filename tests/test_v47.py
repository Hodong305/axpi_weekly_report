"""v4.7 요약층(설계서 9.6·9.8): 한 줄 핵심(deck)·요점 3줄(points)·30초 브리핑·확인 단계 막대·읽는 시간, B4·B5 레이아웃."""
import copy
from pathlib import Path

import yaml

from internal import checks, draft as draftmod, render, review as rv, settings as settings_mod

ROOT = Path(__file__).resolve().parent.parent
CFG = settings_mod.load().internal
SAMPLE = ROOT / "docs" / "samples" / "sample-draft-v4.7.yaml"


def sample():
    return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))


def ctx(theme):
    return {"issue_week": "2026-W48", "period": "2026-11-15 ~ 2026-11-21", "period_short": "11.15 ~ 11.21", "issue_type": "정규호",
            "counts": {"pass_docs": 14, "published": 6}, "events": [], "status_rows": [], "corrections": [], "note": "",
            "newsletter_name": CFG["newsletter_name"], "newsletter_tagline": CFG["newsletter_tagline"], "newsletter_team": CFG["newsletter_team"],
            "theme": theme, "issue_no": 1, "n_docs": 39, "read_chars_per_min": 500}


def test_config_v47_keys():
    assert CFG["newsletter_theme"] == "B4" and CFG["deck_chars"] == 40 and CFG["point_chars"] == 36
    assert CFG["core_chars"] == [150, 220] and CFG["brief_chars"] == [45, 75] and CFG["issue_body_max"] == 1600
    th = render.load_themes()
    assert th["B4"]["template"] == "newsletter-b4.html" and th["B4"]["show_body"] is True
    assert th["B5"]["template"] == "newsletter-b5.html" and th["B5"]["show_body"] is False


def test_sample_draft_passes_length_checks():
    d = sample()
    assert checks.length_violations(d, CFG, "정규호") == []
    assert checks.body_total(d) <= CFG["issue_body_max"]


def test_b4_has_three_layers_and_is_static():
    out = render.build_all(sample(), ctx("B4"))
    h = out["html_preview"]
    render.verify_static(h)
    # ① 30초 브리핑: 핵심은 한 줄 핵심, 단신은 제목, 번호 01~06
    assert "30초 브리핑" in h and "결함 원인 분석을 AI 에이전트가 먼저 맡아" in h and ">06<" in h
    # ② 요약층: 라벨 3개와 한 줄 핵심, 확인 단계 막대(현장 실증·적용 = 3칸)
    for lab in ("무엇이", "얼마나", "어디까지"):
        assert f">{lab}</span>" in h
    assert "●●●</span><span" in h and "현장 실증·적용" in h and "운영사 발표" in h
    # ③ 자세히(본문)와 MTC가 볼 것, 읽는 시간
    assert "자세히" in h and "삼성전자가 웨이퍼 결함의 원인 분석을" in h and "MTC가 볼 것" in h and "전체 약 " in h
    # 약어 풀이는 요점의 첫 등장에 붙는다(본문의 두 번째 등장에는 붙지 않는다)
    assert h.count("(근본 원인 분석)") == 1
    t = out["text"]
    assert t.index("[30초 브리핑]") < t.index("[이번 주 핵심]") and "\n▶ 결함 원인 분석을" in t and "\n· 얼마나: 원인 분석 시간 수 시간 → 수 분" in t and "🏭" not in t


def test_b5_drops_core_body_but_keeps_summary_layer():
    out = render.build_all(sample(), ctx("B5"))
    h, t = out["html_preview"], out["text"]
    render.verify_static(h)
    assert "삼성전자가 웨이퍼 결함의 원인 분석을" not in h and "삼성전자가 웨이퍼 결함의 원인 분석을" not in t   # 핵심 본문 없음
    assert "같은 공정 챔버끼리의 결과 차이를" in h                                                # 단신 본문은 그대로
    assert "그래서 MTC는" in h and "한눈에" in h and "90,000+" in h and ">얼마나</b>" in h


def test_old_themes_still_render_with_v47_draft():
    for th in ("A", "B", "C", "B3"):
        h = render.build_all(sample(), ctx(th))["html_preview"]
        render.verify_static(h)
        assert "삼성전자가 웨이퍼 결함의 원인 분석을" in h


def test_read_minutes_counts_body_only_when_shown():
    d = sample()
    full_b4, skim = render.read_minutes(d, True, 500)
    full_b5, skim5 = render.read_minutes(d, False, 500)
    assert full_b4 >= full_b5 >= 1 and skim == skim5 and skim % 10 == 0 and skim >= 10


def test_stage_meter_levels():
    th = render.load_themes()["B4"]
    assert render.stage_meter(th, "T1", ["칩메이커", "양산·상시 운영"]).count("●") == 4
    m = render.stage_meter(th, "T1", ["칩메이커", "구상·연구"])
    assert ">●</span>" in m and ">●●●</span>" in m
    assert render.stage_meter(th, "T1", ["칩메이커"]) == ""


def test_deck_and_points_length_and_url_checks():
    d = sample()
    a = d["articles"][0]
    a["deck"] = "가" * 45
    a["points"]["scope"] = "나" * 40
    msgs = [m for _, m in checks.length_violations(d, CFG, "정규호")]
    assert any(m.startswith("한 줄 핵심 45자") for m in msgs) and any(m.startswith("요점(scope) 40자") for m in msgs)
    d2 = sample()
    d2["articles"][0]["points"]["what"] = "자세한 내용은 example.com 참고"
    warns = checks.autofix(d2, {})
    assert "example.com" not in d2["articles"][0]["points"]["what"] and any(w["kind"] == "URL" for w in warns)


def test_fallback_deck_when_model_omits_summary_layer():
    body = "삼성전자가 결함 원인 분석 에이전트를 팹에 적용했다. 적용 범위는 비공개다."
    assert draftmod.fallback_deck(body, CFG) == "삼성전자가 결함 원인 분석 에이전트를 팹에 적용했다."
    long = "가" * 80 + "다. 둘째 문장."
    assert len(draftmod.fallback_deck(long, CFG)) == CFG["deck_chars"]


def test_review2_shows_deck_and_points():
    d = sample()
    d["articles"][0]["title_alternatives"] = ["대안 a", "대안 b"]
    text = rv.render_review2("2026-W48", d, [], CFG)
    assert "      ▶ 결함 원인 분석을 AI 에이전트가 먼저 맡아" in text and "      · 어디까지: 운영사 발표" in text


def test_glossary_annotates_points_first():
    d = copy.deepcopy(sample())
    from internal.glossary import gloss_fields
    fields = [k for k, _ in gloss_fields(d)]
    assert fields[:4] == [("articles", 0, "point_what"), ("articles", 0, "point_how_much"), ("articles", 0, "point_scope"), ("articles", 0, "body")]
