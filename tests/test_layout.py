"""풍부한 소식지 레이아웃(설계서 9.5~9.8): 같은 주체 한도, 더 볼 거리, 수치 카드, F 문법, 템플릿 렌더, 호수."""
import copy
import csv
import json

import pytest
import yaml

from internal import checks, draft as draftmod, events as ev_mod, metrics, render, review as rv, select as sel, settings as settings_mod
from internal.llm import AutoJudgeFailed, Judge
from internal.pipeline import Pipeline
from internal.prompts import load_steps
from internal.s7 import run_s7

from test_internal import (CFG, FakeClient, FakeIO, WEEK, articles_ctx, fill, make_package, make_pipeline, mk, pick_doc, sample_draft,
                           sample_records)


# 선정: 같은 주체 한도 ------------------------------------------------------------

def with_subject(ev, subject):
    ev["subject"] = subject
    return ev


def test_same_subject_limit_w48_like():
    evs = [with_subject(mk("a", 88), "삼성반도체"), with_subject(mk("b", 85), "Boston Dynamics"),
           with_subject(mk("c", 78), "SK하이닉스"), with_subject(mk("d", 74), "SK하이닉스"), with_subject(mk("e", 70), "SK하이닉스"),
           with_subject(mk("f", 66), "Intel")]
    sel.select(evs, CFG)
    s = {e["event_id"]: (e["slot"], e["note"]) for e in evs}
    assert [s[k][0] for k in "abcdf"] == ["core", "core", "brief", "brief", "brief"]
    assert s["e"] == ("out", "같은 주체 한도")                # 점수가 가장 낮은 SK하이닉스 단신이 더 볼 거리로
    # 점수가 높은 쪽이 남는다
    evs = [with_subject(mk("a", 88), "A사"), with_subject(mk("b", 85), "B사"), with_subject(mk("x", 70), "S"), with_subject(mk("y", 90, cap="brief"), "S"),
           with_subject(mk("z", 65), "S")]
    sel.select(evs, CFG)
    kept = {e["event_id"] for e in evs if e["slot"] in ("core", "brief")}
    assert {"y", "x"} <= kept and "z" not in kept


def test_same_subject_counts_core_and_brief_together():
    evs = [with_subject(mk("a", 95), "S사"), with_subject(mk("b", 92), "S사"), with_subject(mk("c", 90), "S사"), with_subject(mk("d", 70), "T사")]
    sel.select(evs, CFG)
    s = {e["event_id"]: e["slot"] for e in evs}
    assert s["a"] == s["b"] == "core" and s["c"] == "out" and s["d"] == "brief"


def test_brief_max_and_core_no_cap_config():
    evs = [mk(f"e{i}", 70 - i) for i in range(9)]
    sel.select(evs, CFG)
    assert sum(e["slot"] == "brief" for e in evs) == 4
    evs = [mk(x, 95 - n) for n, x in enumerate("abcde")] + [mk(f"e{i}", 70 - i) for i in range(9)]
    sel.select(evs, CFG)
    assert sum(e["slot"] == "core" for e in evs) == 5 and sum(e["slot"] == "brief" for e in evs) == 4     # 핵심 5건(상한 없음)이어도 단신은 줄지 않는다


# 더 볼 거리 후보 ---------------------------------------------------------------------

def hold_doc(eid, doc_id):
    e = ev_mod.new_event(eid, "hold_doc", [doc_id], doc_id, "발행처", "t", "가", None)
    e.update(status="HOLD", slot="hold")
    return e


def test_more_candidates_rules():
    recs = {r["doc_id"]: r for r in sample_records()}
    ids = sorted(recs)
    def pe(eid, doc, topic, total, slot="out", status="PASS", cap_reasons=()):
        e = mk(eid, total)
        e.update(rep_doc_id=doc, doc_ids=[doc], primary_topic=topic, slot=slot, status=status, cap_reasons=list(cap_reasons))
        return e
    evs = [pe("e1", ids[0], "T2_EQUIPMENT", 70), pe("e2", ids[1], "T1_CHIPMAKER", 55), pe("e3", ids[2], "T1_CHIPMAKER", 62),
           pe("e4", ids[3], "T4_PHYSICAL_AI", 40, cap_reasons=["B 경로 전이 사례"]), pe("e5", ids[4], "T3_SOLUTION", 90, slot="core"),
           pe("x1", ids[5], None, 0, slot="out", status="EXCLUDE")]
    h1, h2, h3 = hold_doc("h1", ids[0]), hold_doc("h2", ids[1]), hold_doc("h3", ids[2])
    h1["rep_doc_id"], h2["rep_doc_id"], h3["rep_doc_id"] = ids[0], ids[1], ids[2]
    fit = {ids[0]: {"stage2": {"fit": {"T1_CHIPMAKER": "1", "T2_EQUIPMENT": "0", "T3_SOLUTION": "0", "T4_PHYSICAL_AI": "0"}}, "route": "REVIEW"},
           ids[1]: {"stage2": {"fit": {t: "U" for t in settings_mod.TOPICS}}, "route": "WATCH_TRANSFER"},
           ids[2]: {"stage2": {"fit": {t: "U" for t in settings_mod.TOPICS}}, "route": "REVIEW"}}
    items, overflow = sel.more_candidates(evs + [h1, h2, h3], fit, recs, CFG)
    refs = [i["ref"] for i in items]
    assert "x1" not in refs and "e5" not in refs and ids[2] not in refs and "h3" not in refs      # 제외·편성·신호 없는 보류는 빠짐
    assert ids[0] in refs and ids[1] in refs                                                      # 보류 자료는 ref가 doc_id
    assert refs.index("e3") < refs.index("e2")                                                    # 같은 분야 안에서 점수 높은 순
    assert [i["topic"] for i in items][-2:] == ["B", "B"] and "e4" in refs                       # B 경로 전이 사례는 마지막 묶음
    items, overflow = sel.more_candidates(evs, fit, recs, dict(CFG, more_max=2))
    assert len(items) == 2 and overflow == 2


# 수치 카드 ---------------------------------------------------------------------------

def test_key_figure_validation():
    recs = {r["doc_id"]: r for r in sample_records()}
    d = [r for r in sample_records() if r["publisher"] == "Lam Research"][0]["doc_id"]
    ev_ids = [f"{d}/P1", f"{d}/P2"]
    ok = [{"value": "30%", "label": "정렬 시간 단축(최대)"}, {"value": "12개", "label": "고객 팹"}]
    assert draftmod.key_figure_errors(ok, ev_ids, recs) == []
    errs = draftmod.key_figure_errors([{"value": "45%", "label": "만든 수치"}], ev_ids, recs)
    assert errs and "근거에 없음" in errs[0]
    assert draftmod.key_figure_errors([{"value": "많음", "label": "숫자 없음"}], ev_ids, recs)
    assert draftmod.key_figure_errors([{"value": "30%", "label": " "}], ev_ids, recs)


def test_core_article_schema_requires_watch_and_figures_and_retries():
    recs = {r["doc_id"]: r for r in sample_records()}
    d = [r for r in sample_records() if r["publisher"] == "Lam Research"][0]["doc_id"]
    ev = ev_mod.new_event("EVT-2026W49-01", "event", [d], d, "Lam Research", "t", "가제목", "T2_EQUIPMENT")
    ev.update(slot="core", score={"tiers": {"field_maturity": 20, "evidence_level": 20}, "claim": "c", "maturity": "m", "fact": [],
                                  "mtc_impact": "", "action_watch": "", "unresolved": [], "evidence": []})
    good = {"event_id": ev["event_id"], "title": "제목", "title_alternatives": ["가", "나"], "body": fill("본문", 450),
            "watch": "확인할 지표", "key_figures": [{"value": "30%", "label": "최대 단축"}], "evidence_ids": [f"{d}/P1"]}
    bad = dict(good, key_figures=[{"value": "77%", "label": "근거에 없음"}])
    seq = iter([bad, good])
    c = FakeClient({"s6_article": lambda u: next(seq)})
    art, failed = draftmod.gen_article(Judge(c), ev, "core", recs, CFG)
    assert not failed and art["key_figures"] == [{"value": "30%", "label": "최대 단축"}] and c.steps() == ["s6_article", "s6_article"]
    assert art["watch"] == "확인할 지표" and art["tags"] == ["장비사", "양산·상시 운영", "운영사 발표"]
    nowatch = {k: v for k, v in good.items() if k != "watch"}
    c = FakeClient({"s6_article": lambda u: nowatch})
    art, failed = draftmod.gen_article(Judge(c), ev, "core", recs, CFG)
    assert failed                                   # 두 번 모두 스키마 위반 → 자동 작성 실패(대체 문구)
    brief = {k: good[k] for k in ("event_id", "title", "title_alternatives", "body", "evidence_ids")}
    art, failed = draftmod.gen_article(Judge(FakeClient({"s6_article": lambda u: brief})), ev, "brief", recs, CFG)
    assert not failed and "watch" not in art and art["tags"] == ["장비사"]


# 더 볼 거리 한 줄 · S7 ------------------------------------------------------------------

def more_draft():
    draft, by_id, recs, d = articles_ctx(fill("정상 본문", 500))
    draft["abstract"] = fill("요약", 260)
    draft["articles"][0].update(watch="후속 지표를 확인", key_figures=[], tags=[], evidence_ids=[f"{d}/P1"])
    ev = next(iter(by_id.values()))
    draft["more"] = [{"ref": ev["event_id"], "topic": "T2_EQUIPMENT", "line": "한 줄 요약이다.", "credit": "c", "evidence_ids": [f"{d}/P1"]}]
    draft["excluded_count"] = 0
    return draft, by_id, recs, d


def test_more_line_suffix_and_credit():
    recs = {r["doc_id"]: r for r in sample_records()}
    press = [r for r in sample_records() if r["source_type"] == "press"][0]
    item = {"ref": press["doc_id"], "topic": "T1_CHIPMAKER", "doc_id": press["doc_id"]}
    c = FakeClient({"s6_more": lambda u: {"ref": press["doc_id"], "line": "한 줄이다.", "evidence_ids": [f"{press['doc_id']}/P1"]}})
    m, bad = draftmod.gen_more(Judge(c), item, recs, CFG)
    assert not bad and m["line"] == "한 줄이다. (보도)" and m["credit"] == draftmod.format_credit(press)
    acad = copy.deepcopy(press)
    acad["source_type"] = "academic"
    assert draftmod.more_suffix(acad) == " (논문)" and draftmod.more_suffix([r for r in sample_records() if r["source_type"] == "company_official"][0]) == ""
    c = FakeClient({"s6_more": lambda u: {"ref": "엉뚱", "line": "x", "evidence_ids": []}})
    m, bad = draftmod.gen_more(Judge(c), item, recs, CFG)
    assert bad and m["evidence_ids"] == [f"{press['doc_id']}/P1"]                    # 실패하면 제목으로 대체


def test_s7_checks_watch_key_figures_and_more():
    draft, by_id, recs, d = more_draft()
    draft["articles"][0]["watch"] = "가" * 170                                          # 한도 160자 초과
    draft["more"][0]["line"] = "나" * 125 + " (보도)"                                    # 한도 120자 초과(접미어 제외, v4.7)
    v = checks.length_violations(draft, CFG, "정규호")
    assert any("MTC가 볼 것" in m for _, m in v) and any(t[0] == "more" for t, _ in v)
    draft["more"][0]["line"] = "나" * 120 + " (보도)"
    assert not any(t[0] == "more" for t, _ in checks.length_violations(draft, CFG, "정규호"))
    # 합계: 핵심 본문 + watch (더 볼 거리는 넣지 않는다)
    draft["articles"][0]["watch"] = "다" * 50
    assert checks.body_total(draft) == 500 + 50
    # URL 삭제
    draft["more"][0]["line"] = "자세히는 https://x.example.org/a 참조"
    draft["articles"][0]["watch"] = "www.example.com 확인"
    w = checks.autofix(draft, recs)
    assert "http" not in draft["more"][0]["line"] and "example" not in draft["articles"][0]["watch"] and any(x["scope"] == "F1" for x in w)
    # 숫자·기업명: 근거에 없는 숫자
    draft["more"][0]["line"] = "처리량이 45% 늘었다"
    draft["articles"][0]["key_figures"] = [{"value": "88%", "label": "Acme Robotics 수치"}]
    ws = checks.soft_warnings(draft, by_id, recs, CFG, "정규호")
    scopes = {(x["scope"], x["kind"]) for x in ws}
    assert ("F1", "숫자") in scopes and ("핵심1", "숫자") in scopes and ("핵심1", "기업명") in scopes
    # 더 볼 거리 근거 ID가 없으면 위반
    draft["more"][0]["evidence_ids"] = [f"{d}/P99"]
    assert any(t == ("more", 0) and k == "근거 ID" for t, k, _ in checks.hard_violations(draft, by_id, recs, CFG, "정규호"))


def test_s7_regenerates_overlong_more_line_once():
    draft, by_id, recs, d = more_draft()
    ev = next(iter(by_id.values()))
    draft["more"][0]["line"] = "나" * 130
    calls = []

    def more(u):
        calls.append(1)
        return {"ref": ev["event_id"], "line": "짧게 다시 쓴 한 줄이다.", "evidence_ids": [f"{d}/P1"]}
    c = FakeClient({"s6_more": more})
    out, warns = run_s7(Judge(c), draft, [ev], recs, CFG, "정규호", {})
    assert len(calls) == 1 and out["more"][0]["line"].startswith("짧게 다시 쓴") and not [w for w in warns if w["scope"] == "F1" and w["kind"] == "글자 수"]


# 검토 요청 ② F -------------------------------------------------------------------------

def draft_with_more():
    d = sample_draft()
    d["more"] = [{"ref": f"r{i}", "topic": t, "line": f"요약 {i}", "credit": "c", "evidence_ids": []}
                 for i, t in enumerate(["T1_CHIPMAKER", "T1_CHIPMAKER", "T3_SOLUTION", "T4_PHYSICAL_AI"], 1)]
    return d


def test_review2_f_grammar_render_and_apply():
    assert rv.parse_review2("승인 F3=빼기")["F"] == [3]
    assert rv.parse_review2("A2 B1 C1 F1=빼기, F4=빼기 E1=수정")["F"] == [1, 4]
    assert rv.parse_review2("승인 F2=빼기 F2=빼기")["F"] == [2]
    assert rv.parse_review2("승인 D 핵심1=a F3=빼기")["D"] == {"핵심1": ("pick", "a")}      # 기존 문법과 함께
    for bad in ("승인 F3=넣기", "승인 F=빼기", "승인 F3"):
        with pytest.raises(rv.ReviewError):
            rv.parse_review2(bad)
    d = draft_with_more()
    text = rv.render_review2(WEEK, d, [], CFG)
    lines = text.splitlines()
    assert any(l.startswith("[F 더 볼 거리]") for l in lines) and "  F1 칩메이커 · 요약 1" in lines and "  F3 제조 솔루션 · 요약 3" in lines
    assert lines.index("[F 더 볼 거리]   (분야별 한 줄 요약 · 빼려면 F3=빼기)") < lines.index("[G 새 용어 풀이]") < lines.index("[E 자동 검사 경고]")
    assert " - F: 뺄 항목만. 예: F3=빼기, F7=빼기. 생략하면 모두 싣는다" in lines
    with pytest.raises(rv.ReviewError, match="F9"):
        rv.validate_review2(rv.parse_review2("승인 F9=빼기"), d, [], CFG)
    res = rv.parse_review2("승인 F1=빼기, F3=빼기")
    rv.validate_review2(res, d, [], CFG)
    st = rv.apply_review2(d, res, [])
    assert [m["line"] for m in d["more"]] == ["요약 2", "요약 4"] and st["f_removed"] == 2
    assert "(없음)" in rv.render_review2(WEEK, dict(d, more=[]), [], CFG)


# 템플릿·텍스트 ---------------------------------------------------------------------------

def full_draft():
    d = sample_draft()
    d["articles"][0].update(tags=["칩메이커", "현장 실증·적용", "운영사 발표"], watch="유효 경보로 이어진 비율",
                            key_figures=[{"value": "99%", "label": "임무 성공률 ≠ 검출률"}, {"value": "875건", "label": "2주 재배치"}],
                            credits=["발행처, 「원제」, 2026-11-24 (영문)"])
    d["articles"][1].update(tags=["Physical AI", "현장 실증·적용", "언론 보도"], watch="확인할 것", key_figures=[], credits=["B 발행처, 「제목」, 2026-11-25"])
    d["articles"][2].update(tags=["제조 솔루션"], credits=["C 발행처, 「단신」, 2026-11-26"])
    d["more"] = [{"ref": "r1", "topic": "T1_CHIPMAKER", "line": "칩메이커 항목 요약 (보도)", "credit": "출처 1", "evidence_ids": []},
                 {"ref": "r2", "topic": "T3_SOLUTION", "line": "솔루션 항목 요약 (논문)", "credit": "출처 2", "evidence_ids": []}]
    d["excluded_count"] = 10
    d["punch"] = {"big": "99%", "label": "이번 주 가장 놀라운 숫자", "text": "로봇개 두 대가 서브팹 점검 9만 건을 해낸 숫자.",
                  "twist": "단, 결함을 찾은 비율이 아니라 임무를 마친 비율이다.", "evidence_id": "x"}
    d["glossary"] = [{"term": "AMHS", "meaning": "웨이퍼를 자동으로 실어 나르는 팹 물류 시스템", "source": "glossary"},
                     {"term": "ADC", "meaning": "자동 결함 분류", "source": "model"}]
    d["articles"][0]["body"] = "AMHS를 넓혔다. AMHS는 다시 나온다. ADC도 쓴다."
    d["articles"][0]["title"] = "AMHS 제목에는 풀이가 붙지 않는다"
    return d


def render_ctx(issue_type="정규호", theme="A"):
    ev = {"event_id": "e1", "ledger": {"verdict": "SAME_WITH_CHANGE", "issue": "2026-W47"}}
    return {"issue_week": WEEK, "period": "2026-11-15 ~ 2026-11-21", "period_short": "11.15 ~ 11.21", "issue_type": issue_type,
            "counts": {"pass_docs": 12, "published": 3},
            "events": [dict(ev, event_id="e1"), dict(ev, event_id="e2", ledger={"verdict": "NEW"})], "status_rows": [], "corrections": [], "note": "",
            "newsletter_name": "FAB4 위클리", "newsletter_tagline": "칩 · 장비 · 솔루션 · 로봇, 네 박자로 읽는 제조 AI",
            "newsletter_team": "삼성전자 DS · 메모리 MTC AX/PI", "theme": theme, "issue_no": 3, "n_docs": 39}


SECTIONS = ("FAB", "제3호 · 2026-W49", "11.15 ~ 11.21", "칩 · 장비 · 솔루션 · 로봇, 네 박자로 읽는 제조 AI", "🏭 칩메이커", "이번 주 헤드라인", "한 줄 요약",
            "이번 주 가장 놀라운 숫자", "99%", "이번 주 핵심", "⚡ 단신", "📚 더 볼 거리", "MTC가 볼 것", "임무 성공률 ≠ 검출률", "875건", "현장 실증·적용",
            "2026-W47 보도 후속", "출처 · 발행처, 「원제」", "칩메이커 항목 요약 (보도)", "솔루션 항목 요약 (논문)", "📖 이번 호 용어 사전",
            "이 밖에 검토했으나 싣지 않은 자료는 10건", "사내 AI가 초안을 쓰고 편집자가 검토", "회신", "검토해 3건을 골랐고", "공개 자료 39건")


@pytest.mark.parametrize("theme", ["A", "B", "C"])
def test_template_has_all_sections_and_is_static_for_each_theme(theme):
    d = full_draft()
    d["articles"][0]["event_id"], d["articles"][1]["event_id"] = "e1", "e2"
    html_doc, text = render.build(d, render_ctx(theme=theme))
    low = html_doc.lower()
    assert "http" not in low and "<a " not in low and "<script" not in low and "<img" not in low and "<link" not in low and "<style" not in low
    for must in SECTIONS:
        assert must in html_doc, must
    assert html_doc.index("이번 주 헤드라인") < html_doc.index("이번 주 핵심") < html_doc.index("⚡ 단신") < html_doc.index("📚 더 볼 거리") < html_doc.index("📖 이번 호 용어 사전") < html_doc.index("이 밖에 검토했으나")
    assert 'width="680"' in html_doc and html_doc.count("<span") >= 7
    th = render.load_themes()[theme]
    assert th["accent"] in html_doc and th["bg"] in html_doc
    assert ("linear-gradient" in html_doc) == th["hero_dark"]             # 그라데이션은 고정색 위에 얹는 어두운 테마(A·C)에만
    if th["hero_dark"]:
        assert f"background:{th['hero']};background-image:linear-gradient" in html_doc
    assert "http" not in text


def test_theme_switch_changes_colors_only():
    d = full_draft()
    d["articles"][0]["event_id"], d["articles"][1]["event_id"] = "e1", "e2"
    out = {t: render.build(copy.deepcopy(d), render_ctx(theme=t))[0] for t in "ABC"}
    assert len({out[t] for t in "ABC"}) == 3
    for t in "ABC":
        stripped = out[t]
        for v in render.load_themes()[t].values():
            pass
    th = render.load_themes()
    assert th["A"]["bg"] in out["A"] and th["B"]["bg"] in out["B"] and th["C"]["bg"] in out["C"] and th["A"]["bg"] not in out["B"]
    with pytest.raises(ValueError, match="테마"):
        render.build(copy.deepcopy(d), render_ctx(theme="Z"))


def test_plain_text_mirrors_sections_and_glossary():
    d = full_draft()
    d["articles"][0]["event_id"], d["articles"][1]["event_id"] = "e1", "e2"
    html_doc, text = render.build(d, render_ctx())
    for must in ("이번 주 헤드라인", "[한 줄 요약]", "[이번 주 가장 놀라운 숫자]", "99% —", "[이번 주 핵심]", "[단신]", "[더 볼 거리]", "<칩메이커>", "<제조 솔루션>",
                 "수치: 99% 임무 성공률 ≠ 검출률", "MTC가 볼 것 · 유효 경보로 이어진 비율", "출처 · 발행처, 「원제」", "[이번 호 용어 사전]", "- AMHS: 웨이퍼를 자동으로",
                 "이 밖에 검토했으나 싣지 않은 자료는 10건", "제3호", "FAB4 위클리"):
        assert must in text, must
    assert "http" not in text


def test_glossary_first_occurrence_only_and_never_in_titles():
    d = full_draft()
    d["articles"][0]["event_id"], d["articles"][1]["event_id"] = "e1", "e2"
    d["articles"][0]["title"] = "AMHS 제목에는 풀이가 붙지 않는다"
    html_doc, text = render.build(d, render_ctx())
    gloss = '<span style="color:#7a8597;font-size:12.5px;">(웨이퍼를 자동으로 실어 나르는 팹 물류 시스템)</span>'
    assert html_doc.count(gloss) == 1                                      # 이번 호 첫 등장 한 곳에만
    assert html_doc.index("AMHS" + gloss) < html_doc.index("AMHS는 다시 나온다")      # 첫 등장 바로 뒤에 붙는다
    assert text.count("(웨이퍼를 자동으로 실어 나르는 팹 물류 시스템)") == 1 and "ADC(자동 결함 분류)도" in text
    assert "AMHS 제목에는 풀이가 붙지 않는다" in html_doc and "AMHS(웨이퍼" not in html_doc.split("이번 주 핵심")[0]      # 헤드라인·총론·제목에는 없다


def test_template_omits_empty_sections_and_scrubs_urls():
    d = full_draft()
    d["more"], d["punch"], d["glossary"] = [], None, []
    d["articles"][1]["key_figures"] = []
    d["issue_title"] = "제목 https://x.example.org/a 끝"
    html_doc, text = render.build(d, render_ctx())
    assert "📚 더 볼 거리" not in html_doc and "가장 놀라운 숫자" not in html_doc and "📖 이번 호 용어 사전" not in html_doc
    assert "x.example.org" not in html_doc and "[더 볼 거리]" not in text and "[이번 호 용어 사전]" not in text
    d["more"] = [{"ref": "r", "topic": "B", "line": "전이 사례 요약", "credit": "c", "evidence_ids": []}]
    assert "기반기술 전이 사례" in render.build(d, render_ctx())[0]
    d["punch"] = {"big": "수 분", "label": "이번 주 한 방", "text": "t", "twist": "w", "evidence_id": "x"}
    h2 = render.build(d, render_ctx())[0]
    assert "💥 이번 주 한 방" in h2


def test_fill_helper_semantics():
    t = "<!--IF a-->[{{x}}]<!--END a--><!--LOOP rows-->({{n}}:{{x}})<!--END rows-->"
    assert render.fill(t, {"a": True, "x": "<b>", "rows": [{"n": 1}, {"n": 2, "x": "q"}]}) == "[&lt;b&gt;](1:&lt;b&gt;)(2:q)"
    assert render.fill(t, {"a": False, "rows": []}) == ""


def test_issue_number_counts_earlier_issues(tmp_path):
    p = tmp_path / "pilot.csv"
    assert metrics.issue_number(p, "2026-W48") == 1
    for w in ("2026-W46", "2026-W47"):
        metrics.upsert_row(p, {"issue_week": w})
    assert metrics.issue_number(p, "2026-W48") == 3 and metrics.issue_number(p, "2026-W47") == 2 and metrics.issue_number(p, "2026-W49") == 3


def test_tags_by_tier():
    ev = {"primary_topic": "T1_CHIPMAKER", "score": {"tiers": {"field_maturity": 15, "evidence_level": 25}}}
    assert draftmod.make_tags(ev, "core") == ["칩메이커", "현장 실증·적용", "독립 검증·기관 확정"]
    ev["score"]["tiers"] = {"field_maturity": 5, "evidence_level": 5}
    assert draftmod.make_tags(ev, "core") == ["칩메이커", "구상·연구", "언론 보도"] and draftmod.make_tags(ev, "brief") == ["칩메이커"]
    assert draftmod.make_tags({"primary_topic": None, "score": None}, "core") == []


# 파이프라인: 더 볼 거리 · 캐시 키 ------------------------------------------------------------

def run_with_more(tmp_path, inputs):
    st = settings_mod.load(data_root=tmp_path)
    st.config["internal"]["brief_max"] = 0        # 점수 60인 사건이 편성에서 빠져 '더 볼 거리'로 간다
    client = FakeClient()
    io = FakeIO(inputs)
    code = Pipeline(st, Judge(client), io).start(make_package(tmp_path, sample_records()))
    return code, client, io


def test_pipeline_more_section_end_to_end(tmp_path):
    code, client, io = run_with_more(tmp_path, ["1", "승인", "1"])
    assert code == 0
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    assert len(d["more"]) == 1 and d["more"][0]["topic"] == "T2_EQUIPMENT" and d["more"][0]["line"].endswith(" (보도)")
    assert d["more"][0]["credit"].startswith("Example News(Lee Reporter), 「Foundry tests") and d["excluded_count"] == 1
    assert "F1 장비사 · 더 볼 거리 한 줄 요약이다. (보도)" in io.text
    html_doc = (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")
    assert "더 볼 거리" in html_doc and "제1호" in html_doc and "자료는 1건" in html_doc and "http" not in html_doc
    txt = (tmp_path / "out_internal" / WEEK / "newsletter.txt").read_text(encoding="utf-8")
    assert "[더 볼 거리]" in txt and "[이번 주 핵심]" in txt
    assert client.steps().count("s6_more") == 1
    pilot = list(csv.DictReader(open(tmp_path / "log" / "pilot.csv", encoding="utf-8")))[0]
    assert pilot["issue_week"] == WEEK


def test_pipeline_f_removal(tmp_path):
    code, client, io = run_with_more(tmp_path, ["1", "승인 F1=빼기", "1"])
    assert code == 0
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    assert d["more"] == []
    assert "더 볼 거리" not in (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))["metrics"]["r2"]["f_removed"] == 1


def test_step_prompts_change_does_not_invalidate_other_steps(tmp_path):
    """캐시 키의 프롬프트 해시는 그 단계 구간의 시스템 프롬프트 해시다. 다른 단계 문구를 바꿔도 S2~S4 응답은 재사용된다."""
    steps = load_steps()
    j1, j2 = Judge(FakeClient(), steps=steps), Judge(FakeClient(), steps={**steps, "s6_article": "바뀐 문구", "s6_more": "바뀐 문구"})
    for s in ("s2_fit", "s2_reclassify", "s3_group", "s3_ledger", "s4_score"):
        assert j1.system_prompt(s) == j2.system_prompt(s)
    assert j1.system_prompt("s6_article") != j2.system_prompt("s6_article")
    pkg = make_package(tmp_path, sample_records())
    st = settings_mod.load(data_root=tmp_path)
    c1 = FakeClient()
    assert Pipeline(st, Judge(c1, steps=steps), FakeIO([])).start(pkg) == 2          # ①에서 중단(S2~S4 응답이 저장됨)
    c2 = FakeClient()
    assert Pipeline(st, Judge(c2, steps={**steps, "s6_article": "바뀐 문구"}), FakeIO([])).start(pkg, restart=True) == 2
    assert c2.calls == []                                                           # S2~S4는 저장된 응답을 그대로 쓴다
