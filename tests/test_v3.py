"""소식지 v3: 헤드라인·총론·한 방, 용어 풀이(glossary.yaml), 새 문법 G·C, 수치 카드 수정, (논문) 표시, 사건 항목 입력."""
import json
import re
import shutil

import pytest
import yaml

from collector.settings import ROOT
from internal import checks, common, draft as draftmod, glossary as gl, review as rv, select as sel, settings as settings_mod
from internal.llm import Judge
from internal.pipeline import Pipeline
from internal.s7 import run_s7, update_glossary

from test_internal import (CFG, FakeClient, FakeIO, WEEK, articles_ctx, fill, lam_event, make_package, make_pipeline, make_record, pick_doc,
                           sample_draft, sample_records)

TERMS, COMMON = gl.load(ROOT / "glossary.yaml")


# 용어 풀이 ----------------------------------------------------------------------

def test_glossary_file_loads():
    assert TERMS["AMHS"].startswith("웨이퍼") and "AI" in COMMON and "GF" in TERMS and "Physical AI" in TERMS and "서브팹" in TERMS


def gdraft(*texts, more=()):
    return {"articles": [{"slot": "core", "body": t, "watch": ""} for t in texts], "more": [{"line": m} for m in more], "glossary": []}


def test_annotate_first_occurrence_only_and_longest_term():
    d = gdraft("GF가 AMHS를 넓혔다. AMHS는 다시 나온다.", "Physical AI와 FDC. GF는 앞에서 풀었다.", more=["AMHS 더 볼 거리"])
    segs, used = gl.annotate(gl.gloss_fields(d), TERMS)
    assert used == ["GF", "AMHS", "Physical AI", "FDC"]
    first = segs[("articles", 0, "body")]
    assert [m for _, m in first if m] == [TERMS["GF"], TERMS["AMHS"]] and "".join(t for t, _ in first) == "GF가 AMHS를 넓혔다. AMHS는 다시 나온다."
    assert [m for _, m in segs[("articles", 1, "body")] if m] == [TERMS["Physical AI"], TERMS["FDC"]]     # GF·AMHS는 이미 풀었으므로 다시 붙이지 않는다
    assert [m for _, m in segs[("more", 0, "line")] if m] == []
    # 풀이는 용어 조각에 붙는다(괄호 표시는 용어 바로 뒤, 툴팁은 용어 위)
    assert first[0] == ("GF", TERMS["GF"]) and first[1] == ("가 ", None) and first[2] == ("AMHS", TERMS["AMHS"])


def test_annotate_skips_when_parenthesis_follows_and_word_boundaries():
    segs, used = gl.annotate([("k", "FDC(이미 설명) 와 FDCX, ADCS 그리고 SEMICON West 전시")], TERMS)
    assert [m for _, m in segs["k"] if m] == [TERMS["SEMICON West"]]        # 이미 괄호 설명이 있으면 달지 않고, 다른 영문자에 붙은 약어는 무시
    assert "FDC" in used and "ADC" not in used


def test_unknown_acronym_detection():
    d = gdraft("PVTW와 XYZ-9 설비, XQ사와 NVIDIA의 mmWave 그리고 TSMC 공정, DeepSeek와 GlobalFoundries.", "ADC도 AI도 MES도 EUV도 쓴다.")
    found = gl.find_unknown(d, TERMS, COMMON, ignore_words=["NVIDIA", "TSMC"])
    assert "XYZ-9" in found and "XQ" in found                                 # 2자 이상 대문자 약어
    assert not set(found) & {"PVTW", "ADC", "AI", "MES", "EUV", "NVIDIA", "TSMC", "mmWave", "DeepSeek", "GlobalFoundries"}   # glossary·공통어·발행처·긴 고유명사는 제외
    assert gl.find_unknown(gdraft("제목에만 ZZZ가 있다"), TERMS, COMMON) == ["ZZZ"]
    d = {"articles": [{"slot": "core", "title": "ZZZ 제목", "body": "본문", "watch": ""}], "more": []}
    assert gl.find_unknown(d, TERMS, COMMON) == []                              # 제목에는 풀이를 붙이지 않으므로 검사하지 않는다


def test_build_glossary_and_proposals_survive():
    d = gdraft("AMHS와 ZZZ를 쓴다", more=["ADC 한 줄"])
    prev = [{"term": "ZZZ", "meaning": "모델 풀이", "source": "model"}]
    g = gl.build_glossary(d, TERMS, prev)
    assert [(x["term"], x["source"]) for x in g] == [("AMHS", "glossary"), ("ZZZ", "model"), ("ADC", "glossary")]
    g2 = gl.build_glossary(gdraft("AMHS만"), TERMS, g)                           # 쓰지 않게 된 용어는 목록에서 빠진다
    assert [x["term"] for x in g2] == ["AMHS"]


def test_append_terms_preserves_comments_and_is_idempotent(tmp_path):
    p = tmp_path / "glossary.yaml"
    shutil.copyfile(ROOT / "glossary.yaml", p)
    before = p.read_text(encoding="utf-8")
    from datetime import date
    n = gl.append_terms(p, [("ZZZ", "새 용어 풀이"), ("AMHS", "이미 있음"), ("Q: R", "콜론: 있는 풀이"), ("빈", "")], "2026-W49", today=date(2026, 12, 1))
    text = p.read_text(encoding="utf-8")
    assert n == 2 and text.startswith(before.rstrip("\n")) and "# 2026-12-01 2026-W49 검토 요청 ②에서 확정" in text
    assert "  ZZZ: 새 용어 풀이" in text and text.index("  # 제조·공정") < text.index("  ZZZ:")                       # 기존 주석·순서 보존
    terms, common = gl.load(p)
    assert terms["ZZZ"] == "새 용어 풀이" and terms["Q: R"] == "콜론: 있는 풀이" and terms["AMHS"] == TERMS["AMHS"] and common == COMMON
    assert gl.append_terms(p, [("ZZZ", "다시")], "2026-W49") == 0 and p.read_text(encoding="utf-8") == text
    # terms 뒤에 다른 키가 있어도 terms 안에 들어간다
    q = tmp_path / "g2.yaml"
    q.write_text("# 주석\ncommon: [AI]\nterms:\n  A1: 하나\nother: x\n", encoding="utf-8")
    gl.append_terms(q, [("B2", "둘")], "W1")
    assert yaml.safe_load(q.read_text(encoding="utf-8")) == {"common": ["AI"], "terms": {"A1": "하나", "B2": "둘"}, "other": "x"}


def test_s7_proposes_glossary_for_unknown_acronym_and_pipeline_appends(tmp_path):
    """S7이 풀이 없는 약어를 찾아 s6_glossary로 제안(source: model) → [G]에 표시 → 발송 확정 때 glossary.yaml에 추가."""
    st = settings_mod.load(data_root=tmp_path)
    recs = [r for r in sample_records() if r["publisher"] != "Lam Research"]
    recs.append(make_record("https://lam.example.org/news/fleet-ai", "Lam expands fleet analytics", "Lam Research", "2026-11-24", [
        "Lam Research said its fleet analytics are now running in production across customer fabs, using the new QWXZ rig to cut chamber matching time by up to 30% and flag drift before it affects yield.",
        "The company reported the results across 12 customer fabs over 8 weeks of operation, and engineers use the model outputs to schedule chamber maintenance before limits are exceeded."]))
    pkg = make_package(tmp_path, sorted(recs, key=lambda r: r["doc_id"]))

    class C(FakeClient):
        def r_s6_article(self, user):
            out = super().r_s6_article(user)
            if '"slot": "core"' in user and "Lam" in user:
                out["body"] = fill("QWXZ 설비가 챔버 매칭 시간을 최대 30% 줄였다. AMHS 연계도 있다.", 500)
            return out

        def r_s6_glossary(self, user):
            info = json.loads(re.search(r"\[작성 지침\]\n(\{.*\})", user).group(1))
            assert info["term"] == "QWXZ" and "<source" in user
            return {"term": "QWXZ", "meaning": "시험용 가상 약어 설비", "evidence_ids": [f"{pick_doc(user)}/P1"]}
    c = C()
    io = FakeIO(["1", 'A1 B1 G1:"직접 고친 풀이"', "1"])
    assert Pipeline(st, Judge(c), io).start(pkg) == 0
    r2 = next(l for l in io.out if l.startswith("━━ 검토 요청 ② 머리부·제목"))
    assert "  G1 QWXZ = 시험용 가상 약어 설비 (모델 제안)" in r2 and "  G2 " not in r2          # AMHS는 glossary 용어라 [G]에 나오지 않는다
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    by = {g["term"]: g for g in d["glossary"]}
    assert by["QWXZ"] == {"term": "QWXZ", "meaning": "직접 고친 풀이", "source": "editor"} and by["AMHS"]["source"] == "glossary"
    gfile = (tmp_path / "glossary.yaml").read_text(encoding="utf-8")
    assert "  QWXZ: 직접 고친 풀이" in gfile and "검토 요청 ②에서 확정" in gfile
    assert "  # 제조·공정" in gfile and (ROOT / "glossary.yaml").read_text(encoding="utf-8").count("QWXZ") == 0        # 저장소 파일은 건드리지 않았다
    html_doc = (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")
    # 기본 B6는 툴팁: 용어에 title로 풀이를 단다. 용어 사전에도 싣는다
    assert '<span title="직접 고친 풀이" style="border-bottom:1px dotted #8a94a6;cursor:help;">QWXZ</span>' in html_doc
    assert "이번 호 용어 사전" in html_doc and "시험용" not in html_doc
    txt = (tmp_path / "out_internal" / WEEK / "newsletter.txt").read_text(encoding="utf-8")
    assert "QWXZ(직접 고친 풀이)" in txt                    # 텍스트본은 괄호 풀이 그대로


def test_glossary_proposal_failure_is_listed_for_manual_input():
    draft, by_id, recs, d = articles_ctx(fill("QWXZ 설비를 쓴다", 500))
    draft["more"] = []
    c = FakeClient({"s6_glossary": lambda u: {"term": "다른말", "meaning": "x", "evidence_ids": []}})
    warns = []
    failed = update_glossary(Judge(c), draft, by_id, recs, TERMS, COMMON, warns)
    assert failed == ["QWXZ"] and draft["glossary"] == [{"term": "QWXZ", "meaning": "", "source": "model"}]
    assert warns and "[G]에서 직접 입력" in warns[0]["message"]
    text = rv.render_review2(WEEK, draft, [], CFG)
    assert any(l.startswith("  G1 QWXZ = (제안 실패") for l in text.splitlines())
    rv.apply_review2(draft, rv.parse_review2('승인 G1:"직접 입력 풀이"'), [])
    assert draft["glossary"][0] == {"term": "QWXZ", "meaning": "직접 입력 풀이", "source": "editor"}


# 머리부: 헤드라인·총론·한 방 -----------------------------------------------------------

def test_header_input_is_final_drafts_and_punch_validation():
    recs = {r["doc_id"]: r for r in sample_records()}
    d = [r for r in sample_records() if r["publisher"] == "Lam Research"][0]["doc_id"]
    articles = [{"slot": "core", "title": "제목", "body": "본문", "watch": "볼 것", "key_figures": [], "evidence_ids": [f"{d}/P1"]}]
    more = [{"line": "더 볼 거리 줄", "evidence_ids": [f"{d}/P2"]}]
    good = {"issue_title_candidates": ["가", "나", "다"], "summary_candidates": [fill("총론", 180)] * 2,
            "punch_candidates": [{"big": "30%", "label": "아무거나", "text": "t", "twist": "w", "evidence_id": f"{d}/P1"}]}
    bad_ev = dict(good, punch_candidates=[dict(good["punch_candidates"][0], evidence_id=f"{d}/P9")])
    bad_big = dict(good, punch_candidates=[dict(good["punch_candidates"][0], big="77%")])
    seq = iter([bad_ev, bad_big, good])          # 둘 다 틀리면 재시도 → 다시 틀림 → 대체
    c = FakeClient({"s6_header": lambda u: next(seq)})
    h, failed = draftmod.gen_header(Judge(c), articles, more, recs, "정규호", {}, CFG)
    assert failed and h["punch_candidates"] == [] and c.steps() == ["s6_header", "s6_header"]
    user = c.calls[0][1]
    assert '"title": "제목"' in user and "[더 볼 거리]" in user and "더 볼 거리 줄" in user and "총론_글자수_범위" in user
    c = FakeClient({"s6_header": lambda u: good})
    h, failed = draftmod.gen_header(Judge(c), articles, more, recs, "정규호", {}, CFG)
    assert not failed
    dr = {}
    draftmod.apply_header(dr, h)
    assert dr["punch"]["label"] == "이번 주 가장 놀라운 숫자" and dr["issue_title"] == "가" and dr["summary"].startswith("총론") and dr["punch_candidates"] == [dr["punch"]]
    assert draftmod.normalize_punch({"big": "수 분", "label": "x", "text": "t", "twist": "w", "evidence_id": "e"})["label"] == "이번 주 한 방"
    none = {**good, "punch_candidates": []}
    dr = {}
    draftmod.apply_header(dr, none)
    assert dr["punch"] is None and dr["punch_candidates"] == []                      # 후보가 없으면 null


def test_header_checks_summary_range_and_punch_length():
    draft, by_id, recs, d = articles_ctx(fill("정상 본문", 500))
    draft["summary"] = fill("총론", 50)
    draft["punch"] = {"big": "30%", "label": "x", "text": "가" * 200, "twist": "나" * 150, "evidence_id": f"{d}/P1"}
    msgs = [m for _, m in checks.length_violations(draft, CFG, "정규호")]
    assert any("총론 50자" in m for m in msgs) and any("이번 주 한 방" in m and "350자" in m for m in msgs)
    assert not any("총론" in m for _, m in checks.length_violations(dict(draft, summary=fill("총론", 180)), CFG, "정규호"))
    assert not any("총론" in m for _, m in checks.length_violations(dict(draft, summary=fill("총론", 50)), CFG, "현황호"))
    draft["punch"]["big"] = "99%"                                               # 근거에 없는 big → 근거 위반
    assert any(t == ("header",) and k == "근거 ID" for t, k, _ in checks.hard_violations(draft, by_id, recs, CFG, "정규호"))


def test_body_total_counts_punch_core_watch_and_brief():
    draft = {"punch": {"text": "가" * 40, "twist": "나" * 30}, "articles": [{"body": "x" * 250, "watch": "y" * 60}, {"body": "z" * 80}]}
    assert checks.body_total(draft) == 70 + 250 + 60 + 80
    assert checks.body_total({"punch": None, "articles": []}) == 0


# 수치 카드 ---------------------------------------------------------------------------

def test_figure_value_numbers_and_phrases():
    en = "The agent cut investigation from hours to minutes, with 99% mission success."
    ko = "엔지니어가 수 시간 들이던 탐색을 수 분에 끝낸다."
    assert common.figure_value_errors("99%", en) == []
    assert common.figure_value_errors("수 시간 → 수 분", ko) == []
    assert common.figure_value_errors("수시간", ko) == []                           # 공백 차이는 무시
    assert common.figure_value_errors("수 시간 → 수 분", en)                        # 영문 근거에는 없다
    assert common.figure_value_errors("90,000+", "more than 90,000 lots") == []
    assert common.figure_value_errors("45%", en) and common.figure_value_errors("많음", ko) and common.figure_value_errors("", ko)


def test_key_figure_phrase_accepted_in_core_article_and_brief_figure():
    recs = {r["doc_id"]: r for r in sample_records()}
    ko = [r for r in sample_records() if r["language"] == "ko"][0]
    d = ko["doc_id"]
    ev = [f"{d}/P2"]
    assert draftmod.key_figure_errors([{"value": "유효 경보 전환율", "label": "x"}], ev, recs) == []       # 근거에 그대로 있는 말
    assert draftmod.key_figure_errors([{"value": "1개 라인 → 2개 라인", "label": "x"}], [f"{d}/P1"], recs) == []
    assert draftmod.key_figure_errors([{"value": "수 시간", "label": "x"}], ev, recs)
    e = ev_new(d, ko)
    brief = {"event_id": e["event_id"], "title": "t", "title_alternatives": ["a", "b"], "body": fill("본문", 120),
             "figure": {"value": "2개 라인", "label": "운영 범위"}, "evidence_ids": [f"{d}/P1"]}
    art, failed = draftmod.gen_article(Judge(FakeClient({"s6_article": lambda u: brief})), e, "brief", recs, CFG)
    assert not failed and art["figure"] == {"value": "2개 라인", "label": "운영 범위"} and "watch" not in art
    bad = dict(brief, figure={"value": "77개", "label": "x"})
    art, failed = draftmod.gen_article(Judge(FakeClient({"s6_article": lambda u: bad})), e, "brief", recs, CFG)
    assert failed
    art, failed = draftmod.gen_article(Judge(FakeClient({"s6_article": lambda u: dict(brief, figure=None)})), e, "brief", recs, CFG)
    assert not failed and "figure" not in art


def ev_new(doc_id, rec):
    from internal import events as ev_mod
    e = ev_mod.new_event("EVT-2026W49-01", "event", [doc_id], doc_id, rec["publisher"], "t", "가제목", "T4_PHYSICAL_AI")
    e["slot"] = "brief"
    return e


def test_figure_fix_adds_qualifier_and_passes_recheck():
    pairs = [("url1_x/P1", "Spot did more than 90,000 inspections and up to 30% fewer stops and about 5,000 checks.")]
    w = checks.number_warnings("90,000", pairs, ["articles", 0, "key_figures", 0, "value"], "핵심1", figure=True)
    assert len(w) == 1 and w[0]["fix"] == {"target": ["articles", 0, "key_figures", 0, "value"], "old": "90,000", "new": "90,000+"}
    draft = {"articles": [{"key_figures": [{"value": "90,000", "label": "점검"}]}]}
    assert rv.apply_fix(draft, w[0]["fix"]) and draft["articles"][0]["key_figures"][0]["value"] == "90,000+"
    assert checks.number_warnings("90,000+", pairs, ["x"], "핵심1", figure=True) == []                 # 고친 뒤에는 경고가 없다
    assert checks.number_warnings("90,000 이상", pairs, ["x"], "핵심1", figure=True) == []
    w = checks.number_warnings("30%", pairs, ["x"], "핵심1", figure=True)
    assert w[0]["fix"]["new"] == "최대 30%"
    w = checks.number_warnings("5,000", pairs, ["x"], "핵심1", figure=True)
    assert w[0]["fix"]["new"] == "약 5,000"
    # 문장에서는 한정어를 넣는다
    w = checks.number_warnings("점검 90,000건 처리했다", pairs, ["x"], "핵심1")
    assert w[0]["fix"]["new"] == "90,000건 넘게 처리했다"


def test_e_fix_on_key_figure_is_applied_through_review():
    """E=수정을 고르면 수치 카드가 실제로 바뀐다(이전에는 수정안이 없어 아무 일도 일어나지 않았다)."""
    draft, by_id, recs, d = articles_ctx(fill("정상 본문", 500))
    draft["articles"][0]["evidence_ids"] = [f"{d}/P2"]
    draft["articles"][0]["key_figures"] = [{"value": "12", "label": "고객 팹"}]
    recs[d]["body"]["blocks"][1]["text"] = "The rollout covers more than 12 customer fabs over eight weeks."
    warns = [w for w in checks.soft_warnings(draft, by_id, recs, CFG, "정규호") if w["scope"] == "핵심1" and w["kind"] == "숫자"]
    assert len(warns) == 1 and warns[0]["fix"]["new"] == "12+"
    checks.number_ids(warns)
    st = rv.apply_review2(draft, rv.parse_review2("승인 E1=수정"), warns)
    assert draft["articles"][0]["key_figures"][0]["value"] == "12+" and st["e_fix"] == 1


# (논문)/(보도) -------------------------------------------------------------------------

@pytest.mark.parametrize("text,suffix", [
    ("Self-supervised virtual metrology. arXiv:2611.01234 preprint", " (논문)"),
    ("The work was published in IEEE Transactions on Semiconductor Manufacturing.", " (논문)"),
    ("Defect-aware matching, published 'Nature Electronics' this week", " (논문)"),
    ("doi 10.1109/TSM.2026.123 reports", " (논문)"),
    ("Presented as a conference paper at ITC 2026", " (논문)"),
    ("한 연구팀이 논문을 공개했다", " (논문)"),
    ("Fab operator says robots now patrol the sub-fab", " (보도)"),
])
def test_paper_vs_press_suffix(text, suffix):
    rec = {"title": "Some headline", "source_type": "press"}
    assert draftmod.more_suffix(rec, text) == suffix
    assert draftmod.more_suffix({"title": "x", "source_type": "academic"}, "그냥") == " (논문)"
    assert draftmod.more_suffix({"title": "x", "source_type": "company_official"}, "그냥") == ""
    assert draftmod.more_suffix({"title": "Paper on arXiv", "source_type": "company_official"}, "") == " (논문)"


def test_more_line_gets_paper_tag_from_evidence_text():
    recs = copy_recs()
    press = [r for r in recs.values() if r["source_type"] == "press"][0]
    press["body"]["blocks"][0]["text"] = "The study was published in IEEE Transactions on Semiconductor Manufacturing and posted to arXiv."
    item = {"ref": press["doc_id"], "topic": "T1_CHIPMAKER", "doc_id": press["doc_id"]}
    c = FakeClient({"s6_more": lambda u: {"ref": press["doc_id"], "line": "연구 요약이다.", "evidence_ids": [f"{press['doc_id']}/P1"]}})
    m, bad = draftmod.gen_more(Judge(c), item, recs, CFG)
    assert m["line"] == "연구 요약이다. (논문)"


def copy_recs():
    import copy
    return {r["doc_id"]: copy.deepcopy(r) for r in sample_records()}


# 더 볼 거리: 사건 항목은 대표 주장·근거 블록이 입력 --------------------------------------------

def test_more_input_for_events_uses_claim_and_evidence_not_rep_lead():
    recs = copy_recs()
    ids = sorted(recs)
    rep, other = recs[ids[0]], recs[ids[1]]
    rep["body"]["blocks"][0]["text"] = "REP-LEAD 대표 자료의 첫 문단은 사건과 무관한 소개말이다. " * 2
    other["body"]["blocks"][1]["text"] = "EVIDENCE-BLOCK 실제 사건을 입증하는 문단이다. " * 2
    item = {"ref": "EVT-2026W49-05", "topic": "T2_EQUIPMENT", "doc_id": rep["doc_id"], "doc_ids": [rep["doc_id"], other["doc_id"]],
            "claim": "사건의 대표 주장", "evidence_ids": [f"{other['doc_id']}/P2"]}
    user = draftmod._more_user(Judge(FakeClient()), item, recs, CFG, None)
    assert "사건의 대표 주장" in user and "EVIDENCE-BLOCK" in user and "REP-LEAD" not in user
    assert f'<source doc_id="{other["doc_id"]}">' in user and f'<source doc_id="{rep["doc_id"]}">' not in user
    hold = {"ref": rep["doc_id"], "topic": "T1_CHIPMAKER", "doc_id": rep["doc_id"]}               # 보류 자료는 제목·첫 문단
    assert "REP-LEAD" in draftmod._more_user(Judge(FakeClient()), hold, recs, CFG, None)
    # 근거 ID는 사건의 자료 어느 것이든 허용하고, 출처 정보는 첫 근거 자료의 것이다
    c = FakeClient({"s6_more": lambda u: {"ref": item["ref"], "line": "한 줄이다.", "evidence_ids": [f"{other['doc_id']}/P2"]}})
    m, bad = draftmod.gen_more(Judge(c), item, recs, CFG)
    assert not bad and m["credit"] == draftmod.format_credit(other) + " 외 1건"        # 자료가 2건인 사건: 나머지 건수를 붙인다
    c = FakeClient({"s6_more": lambda u: {"ref": item["ref"], "line": "x", "evidence_ids": ["url1_" + "0" * 64 + "/P1"]}})
    m, bad = draftmod.gen_more(Judge(c), item, recs, CFG)
    assert bad and m["evidence_ids"] == [f"{other['doc_id']}/P2"]                                   # 실패하면 S4 근거로 대체


def test_more_candidates_carry_claim_and_evidence():
    recs = {r["doc_id"]: r for r in sample_records()}
    ids = sorted(recs)
    from test_internal import mk
    e = mk("e1", 70)
    e.update(rep_doc_id=ids[0], doc_ids=[ids[0], ids[1]], primary_topic="T2_EQUIPMENT", slot="out", status="PASS")
    e["score"].update(claim="주장", evidence=[{"id": f"{ids[1]}/P1", "type": "실측"}])
    items, _ = sel.more_candidates([e], {}, recs, CFG)
    assert items[0]["claim"] == "주장" and items[0]["evidence_ids"] == [f"{ids[1]}/P1"] and items[0]["doc_ids"] == [ids[0], ids[1]]
    assert draftmod.more_item_for({"ref": "e1", "topic": "T2_EQUIPMENT"}, {"e1": e})["claim"] == "주장"
    assert draftmod.more_item_for({"ref": ids[2], "topic": "T1_CHIPMAKER"}, {"e1": e}) == {"ref": ids[2], "topic": "T1_CHIPMAKER", "doc_id": ids[2]}


# 파이프라인: 테마·머리띠 ---------------------------------------------------------------

@pytest.mark.parametrize("theme", ["A", "B", "C", "B3"])
def test_pipeline_renders_selected_theme(tmp_path, theme):
    st = settings_mod.load(data_root=tmp_path)
    st.config["internal"]["newsletter_theme"] = theme
    name = "MTC AX Tech Brief" if theme == "B3" else "FAB4 위클리"
    st.config["internal"]["newsletter_name"] = name
    io = FakeIO(["1", "승인", "1"])
    assert Pipeline(st, Judge(FakeClient()), io).start(make_package(tmp_path, sample_records())) == 0
    html_doc = (tmp_path / "out_internal" / WEEK / "newsletter.html").read_text(encoding="utf-8")
    from internal import render
    th = render.load_themes()[theme]
    assert th["bg"] in html_doc and "http" not in html_doc.lower() and "<a " not in html_doc.lower()
    assert ("Tech Brief</span>" if theme == "B3" else "FAB<span") in html_doc and "제1호" in html_doc and "공개 자료 6건을 검토해 3건을 골랐고" in html_doc
    txt = (tmp_path / "out_internal" / WEEK / "newsletter.txt").read_text(encoding="utf-8")
    assert "[이번 주 핵심]" in txt and name in txt


def test_config_v3_keys():
    cfg = settings_mod.load().internal
    for gone in ("one_line_max", "abstract_chars", "hook_chars"):
        assert gone not in cfg
    assert cfg["summary_chars"] == [140, 240] and cfg["punch_chars"] == 320 and cfg["article_title_max"] == 50
    assert cfg["newsletter_name"] == "MTC AX Tech Brief" and cfg["newsletter_theme"] == "S6" and "팹 AI 브리프" in cfg["newsletter_tagline"]
    assert cfg["newsletter_team"].endswith("AX/PI팀")
    assert settings_mod.load().config["config_version"] == "11"


def test_step_prompts_carry_v3_rules():
    from internal.prompts import load_steps
    st = load_steps()
    for must in ("전후 대비", "금지", "끝판왕", "수 시간 걸리던 결함 원인 찾기, 삼성 AI 에이전트는 ‘수 분’", "key_figures", "figure"):
        assert must in st["s6_article"], must
    for must in ("대구·대조", "결함은 에이전트가 쫓고, 서브팹은 로봇개가 돈다", "총론", "punch_candidates", "이번 주 가장 놀라운 숫자", "오해하기 쉬운 숫자"):
        assert must in st["s6_header"], must
    assert "25자 이내" in st["s6_glossary"] and "s6_summary" in st and "s6_abstract" not in st
