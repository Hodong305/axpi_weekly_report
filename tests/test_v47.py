"""v4.7 요약층과 작성 수준(설계서 9.6·9.8): 한 줄 핵심(deck)·요점 4개(points)·숫자의 의미(why)·브리핑·숫자로 보는 이번 주·MTC AX Insight·중언부언 지양,
B6(브리핑 리포트)·S4(매거진) 후보 레이아웃과 이전 시안(B4·B5·E·S·S2·S3)."""
import copy
import re
from pathlib import Path

import yaml

from internal import checks, draft as draftmod, render, review as rv, settings as settings_mod
from internal.glossary import gloss_fields

ROOT = Path(__file__).resolve().parent.parent
CFG = settings_mod.load().internal
SAMPLE = ROOT / "docs" / "samples" / "sample-draft-v4.7.yaml"
CORE_BODY = "지금까지 결함 원인 분석은 엔지니어가"


def sample():
    return yaml.safe_load(SAMPLE.read_text(encoding="utf-8"))


def ctx(theme):
    return {"issue_week": "2026-W48", "period": "2026-11-15 ~ 2026-11-21", "period_short": "11.15 ~ 11.21", "issue_type": "정규호",
            "counts": {"pass_docs": 14, "published": 8}, "events": [], "status_rows": [], "corrections": [], "note": "",
            "newsletter_name": CFG["newsletter_name"], "newsletter_tagline": CFG["newsletter_tagline"], "newsletter_team": CFG["newsletter_team"],
            "theme": theme, "issue_no": 1, "n_docs": 39, "read_chars_per_min": 500}


def build(theme, d=None):
    return render.build_all(d or sample(), ctx(theme))


# 설정·견본 ---------------------------------------------------------------------------

def test_config_v47_keys():
    assert CFG["newsletter_theme"] == "B6"
    assert (CFG["deck_chars"], CFG["point_chars"], CFG["figure_why_chars"], CFG["watch_chars"]) == (120, 180, 80, 160)
    assert CFG["core_chars"] == [400, 640] and CFG["brief_chars"] == [90, 150] and CFG["summary_chars"] == [140, 240]
    assert CFG["more_line_chars"] == 120 and CFG["punch_chars"] == 320 and CFG["issue_body_max"] == 9000 and CFG["article_title_max"] == 50
    th = render.load_themes()
    for t in ("B6", "S4"):
        assert th[t]["gloss_top"] is True and th[t]["numbers_panel"] is True and th[t]["show_body"] is True
    assert th["B5"]["show_body"] is False


def test_sample_draft_passes_length_checks_and_uses_four_full_sentence_points():
    d = sample()
    assert checks.length_violations(d, CFG, "정규호") == []
    assert checks.body_total(d) <= CFG["issue_body_max"]
    for a in d["articles"]:
        if a["slot"] == "core":
            assert list(a["points"]) == ["what", "how_much", "why", "scope"]
            for v in a["points"].values():
                assert v.rstrip().endswith("다.") and len(v) >= 60        # 명사형 조각이 아닌 완결 문장
            assert all(k.get("why") for k in a["key_figures"])
        else:
            assert len(re.findall(r"다\.", a["body"])) >= 2            # 단신은 2문장(무슨 일 + 왜 볼 만한지)


# 후보 레이아웃 B6 · S4 ----------------------------------------------------------------

def test_b6_sections_in_reading_order_and_static():
    out = build("B6")
    h, t = out["html_preview"], out["text"]
    render.verify_static(h)
    order = ["이번 주 헤드라인", "한 줄 요약", "이번 주 브리핑", "이번 주 가장 놀라운 숫자", "숫자로 보는 이번 주", "이번 주 핵심",
             ">단신<", "더 볼 거리", "이번 호 용어 사전"]
    pos = [h.index(order[0])]
    for x in order[1:]:
        pos.append(h.index(x, pos[-1] + 1))
    assert pos == sorted(pos)
    for lab in ("무슨 일", "얼마나", "왜 중요", "어디까지"):
        assert f">{lab}</span>" in h
    assert CORE_BODY in h and "MTC AX Insight" in h and "●" not in h
    # 중언부언 지양: 구역 건수 목록·섹션 부연 설명·성숙도 꼬리표·꼬리말·팀 이름 중복 줄이 없다
    for gone in ("이번 호 구성", "무엇을 잰 숫자이고 왜 중요한지", "원문을 보지 않아도", "바쁘면 여기까지만", "싣지는 않았지만",
                 "현장 실증·적용 · 운영사 발표", "이 메일을 받는 이유", "만드는 방식", "링크가 없는 이유", "주간 소식지</td>"):
        assert gone not in h, gone
    assert "💡 MTC AX Insight" in h and "MTC가 볼 것" not in h and "이 메일을 받는 이유" not in t and "회신해 주세요" not in t
    # 받은편지함 미리 보기 글(숨김)은 총론 첫 문장
    assert 'display:none;max-height:0' in h and "이번 주에는 경쟁 칩메이커 두 곳이" in h
    # 텍스트본도 같은 순서, 숫자 구역 포함
    assert t.index("[30초 브리핑]") < t.index("[숫자로 보는 이번 주]") < t.index("[이번 주 핵심]")
    assert "- 99% 임무 성공률(≠ 이상 검출률) (기사 02) — 로봇이 멈추지 않고" in t


def test_b6_briefs_are_two_column_grid():
    h = build("B6")["html_preview"]
    assert h.count('<td width="50%" valign="top" style="padding:6px;">') == 4     # 단신 4건 → 2행 × 2칸


def test_s4_number_tiles_and_core_cards():
    h = build("S4")["html_preview"]
    render.verify_static(h)
    assert h.count("border-top:4px solid #ffd60a") == 7                          # 숫자 타일 7개(핵심 6 + 단신 1)
    assert "기사 02 · Physical AI" in h and ">왜 중요</span>" in h and CORE_BODY in h
    assert "모았습니다" not in h and "MTC AX Insight →" in h
    for gone in ("이번 호 ▸", "주간 소식지</div>", "현장 실증·적용", "무엇을 잰 숫자이고", "이 메일을 받는 이유"):
        assert gone not in h, gone
    assert h.count(CFG["newsletter_tagline"]) == 1                                   # 부제는 한 번만


def test_jargon_is_explained_at_first_appearance_at_the_top():
    """B6·S4: '서브팹'은 맨 위 한 줄 요약에서 처음 풀이하고, 아래에서는 되풀이하지 않는다. 제목에는 쉬운 수식어가 있다."""
    for th in ("B6", "S4"):
        h = build(th)["html_preview"]
        first = h.index("(클린룸 아래층의 배관·전력·가스 설비 공간)")
        assert first < h.index("이번 주 브리핑") and h.count("(클린룸 아래층의 배관·전력·가스 설비 공간)") == 1
    assert "클린룸 아래 설비층 ‘서브팹’" in sample()["articles"][1]["title"]
    # 이전 시안(B4)은 맨 위에서 풀이하지 않고 본문 첫 등장에서 풀이한다
    h4 = build("B4")["html_preview"]
    assert h4.index("(클린룸 아래층의") > h4.index("이번 주 핵심")


def test_long_values_get_smaller_type():
    assert render.value_size("99%") == 23 and render.value_size("90,000+") == 23 and render.value_size("수 시간 → 수 분") < 23


# 이전 시안 ---------------------------------------------------------------------------

def test_previous_drafts_still_render():
    for th in ("A", "B", "C", "B3", "B4", "B5", "E", "S", "S2", "S3"):
        h = build(th)["html_preview"]
        render.verify_static(h)
        assert "결함은 에이전트가 쫓고" in h
    assert CORE_BODY not in build("B5")["html_preview"]                             # B5는 핵심 본문을 싣지 않는다


# 검사·작성 --------------------------------------------------------------------------

def test_deck_points_and_figure_why_length_and_url_checks():
    d = sample()
    a = d["articles"][0]
    a["deck"] = "가" * 125
    a["points"]["why"] = "나" * 185
    a["key_figures"][0]["why"] = "다" * 85
    msgs = [m for _, m in checks.length_violations(d, CFG, "정규호")]
    assert any(m.startswith("한 줄 핵심 125자") for m in msgs) and any(m.startswith("요점(why) 185자") for m in msgs)
    assert any("의미 85자" in m for m in msgs)
    d2 = sample()
    d2["articles"][0]["points"]["what"] = "자세한 내용은 example.com 참고"
    d2["articles"][4]["figure"]["why"] = "example.com 참고"
    warns = checks.autofix(d2, {})
    assert "example.com" not in d2["articles"][0]["points"]["what"] + d2["articles"][4]["figure"]["why"]
    assert len([w for w in warns if w["kind"] == "URL"]) == 2


def test_points_without_why_still_work():
    d = sample()
    del d["articles"][0]["points"]["why"]
    h = build("B6", d)["html_preview"]
    assert h.count(">왜 중요</span>") == 3                                            # 나머지 핵심 세 건에만


def test_fallback_deck_when_model_omits_summary_layer():
    body = "삼성전자가 결함 원인 분석 에이전트를 팹에 적용했다. 적용 범위는 비공개다."
    assert draftmod.fallback_deck(body, CFG) == "삼성전자가 결함 원인 분석 에이전트를 팹에 적용했다."
    assert len(draftmod.fallback_deck("가" * 150 + "다. 둘째 문장.", CFG)) == CFG["deck_chars"]


def test_review2_shows_deck_and_points():
    d = sample()
    d["articles"][0]["title_alternatives"] = ["대안 a", "대안 b"]
    text = rv.render_review2("2026-W48", d, [], CFG)
    assert "      ▶ 삼성전자가 결함 원인 분석 AI 에이전트(Defect Agent)에" in text and "      · 왜 중요: 결함 원인 분석은" in text and "      · 어디까지: 회사 발표 기준이다." in text


def test_gloss_field_order():
    d = copy.deepcopy(sample())
    plain = [k for k, _ in gloss_fields(d)]
    assert plain[:5] == [("articles", 0, "point_what"), ("articles", 0, "point_how_much"), ("articles", 0, "point_why"),
                         ("articles", 0, "point_scope"), ("articles", 0, "body")]
    top = [k for k, _ in gloss_fields(d, top=True)]
    assert top[:3] == [("summary",), ("articles", 0, "deck"), ("articles", 1, "deck")]


def test_prompt_requires_fuller_writing():
    steps = (ROOT / "prompts" / "internal-steps.md").read_text(encoding="utf-8")
    for must in ("points{what, how_much, why, scope}", "why(왜 중요)", "4~6문장", "단신은 2문장", "쉬운 수식어", "key_figures[{value, label, why}]"):
        assert must in steps, must


def test_insight_prompt_and_doc_definition():
    steps = (ROOT / "prompts" / "internal-steps.md").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "MTC-AX-Unified-Design-v4.7.md").read_text(encoding="utf-8")
    assert "MTC AX Insight" in steps and "생각해 볼 화두" in steps and "의미 있게 볼 지점" in steps
    assert "MTC AX Insight(v4.7)" in doc and "중언부언 지양" in doc and "MTC가 볼 것" not in steps
