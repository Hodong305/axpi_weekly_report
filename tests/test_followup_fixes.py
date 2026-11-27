"""W48 시험 운영에서 나온 수정 5건의 시험: NO_AI_OPS, X 표시 사유, 숫자 한정어, 제목 대안 표시, --answer 이어 답하기."""
import json

import pytest

from internal import checks, fit as fit_mod, review as rv, select as sel, settings as settings_mod
from internal.cli import build_parser
from internal.llm import Judge
from internal.pipeline import ConsoleIO, Pipeline
from internal.s7 import run_s7

from test_internal import (CFG, FakeClient, WEEK, articles_ctx, fill, make_package, mk, sample_draft, sample_records, slots)


# 1. NO_AI_OPS -----------------------------------------------------------------

def _fit_resp(fit, code, evidence=(), reasons="사유"):
    return lambda user: {"doc_id": __import__("re").search(r'<source doc_id="(url1_[0-9a-f]{64})"', user).group(1),
                         "fit": {t: v for t, v in zip(settings_mod.TOPICS, fit)}, "reasons": {t: reasons for t in settings_mod.TOPICS},
                         "evidence_ids": list(evidence), "route": "REVIEW", "exclusion_code": code}


def test_no_ai_ops_after_full_body_becomes_exclude():
    rec = sample_records()[0]
    recs = {rec["doc_id"]: rec}
    c = FakeClient({"s2_fit": _fit_resp("UUUU", None), "s2_reclassify": _fit_resp("0000", "NO_AI_OPS")})
    out = fit_mod.classify_doc(Judge(c), rec, recs)
    assert out["decision"] == "EXCLUDE" and out["exclusion_code"] == "NO_AI_OPS" and out["note"] == "NO_AI_OPS"
    from internal import events as ev_mod
    evs = ev_mod.build_events(Judge(FakeClient()), recs, {rec["doc_id"]: out}, WEEK, 30)
    assert evs[0]["slot"] == "out" and evs[0]["status"] == "EXCLUDE" and evs[0]["note"] == "NO_AI_OPS"
    assert "X1 제외(NO_AI_OPS)" in rv.render_review1(WEEK, {"pass_docs": 0, "events": 0, "candidates": 0, "core_eligible": 0}, evs)
    # 코드가 없는 모두 0은 여전히 판단 보류
    c = FakeClient({"s2_fit": _fit_resp("UUUU", None), "s2_reclassify": _fit_resp("0000", None)})
    assert fit_mod.classify_doc(Judge(c), rec, recs)["decision"] == "HOLD"
    assert "NO_AI_OPS" in fit_mod.FIT_SCHEMA["properties"]["exclusion_code"]["enum"]


def test_no_ai_ops_is_not_allowed_at_stage1():
    rec = sample_records()[0]
    recs = {rec["doc_id"]: rec}
    c = FakeClient({"s2_fit": _fit_resp("0000", "NO_AI_OPS"), "s2_reclassify": _fit_resp("0000", "NO_AI_OPS")})
    out = fit_mod.classify_doc(Judge(c), rec, recs)       # 1단계 응답은 검증에서 거부되어 두 번 실패 → 자동 판정 실패
    assert out["decision"] == "HOLD" and "자동 판정 실패" in out["flags"] and c.steps() == ["s2_fit", "s2_fit"]
    from internal.prompts import load_steps
    assert "NO_AI_OPS" in load_steps()["s2_reclassify"] and "NO_AI_OPS" not in load_steps()["s2_fit"]


# 2. X 표시 사유 ----------------------------------------------------------------

def test_out_note_background_only_when_quota_used():
    evs = [mk("a", 88), mk("b", 85), mk("d", 70, fab=10, cap="brief"), mk("e", 68, fab=10, cap="brief")]
    sel.select(evs, CFG)
    notes = {e["event_id"]: e["note"] for e in evs}
    assert notes["e"] == "편성 한도 초과(산업 배경은 1건)" and slots(evs)["d"][0] == "brief"
    evs = [mk("a", 88), mk("b", 85), mk("c", 70), mk("d", 66), mk("e", 64), mk("f", 62, fab=10, cap="brief")]
    sel.select(evs, dict(CFG, brief_max=1))         # 핵심 4건(c·d는 최소 채우기), 단신 1건(e)
    s = {e["event_id"]: (e["slot"], e["note"]) for e in evs}
    assert s["e"][0] == "brief" and s["f"] == ("out", "편성 한도 초과")            # 산업 배경 한도가 아니라 일반 단신 한도 때문


# 3. 숫자 한정어 ----------------------------------------------------------------

def warn(draft_text, evidence):
    return [w for w in checks.number_warnings(draft_text, [("url1_x/P1", evidence)], ["x"], "핵심1") if "한정어" in w["message"]]


@pytest.mark.parametrize("text,ev", [
    ("올해 90,000건 넘게 처리했다", "processed more than 90,000 lots"),
    ("90,000건 이상 처리했다", "processed more than 90,000 lots"),
    ("90,000건 초과 처리했다", "over 90,000 lots"),
    ("90,000건 넘는 규모", "at least 90,000 lots"),
    ("90,000건 넘어 처리했다", "more than 90,000 lots"),
    ("최대 30% 줄였다", "reduced by up to 30%"),
    ("30%까지 줄였다", "reduced by up to 30%"),
    ("30% 이하로 줄였다", "as much as 30%"),
    ("약 5,000건을 처리했다", "about 5,000 units"),
    ("5,000건 가량 처리했다", "approximately 5,000 units"),
    ("5,000건 정도 처리했다", "around 5,000 units"),
    ("5,000건 안팎이다", "roughly 5,000 units"),
    ("5,000여 건이다", "nearly 5,000 units"),
    ("거의 5,000건이다", "nearly 5,000 units"),
    ("5,000건 가까이 된다", "nearly 5,000 units"),
])
def test_korean_qualifiers_accepted(text, ev):
    assert warn(text, ev) == []


def test_missing_qualifier_still_warns():
    w = warn("90,000건 처리했다", "processed more than 90,000 lots")
    assert w and "한정어 누락" in w[0]["message"]
    w = warn("30% 단축했다", "reduced by up to 30%")
    assert w and w[0]["fix"]["new"] == "최대 30% 단축했다"
    assert warn("5,000여 건", "up to 5,000 units")                    # '여'는 약(approx)이지 최대가 아님


# 4. 제목 대안 -------------------------------------------------------------------

LONG_A = "삼성 Defect Agent, 혐의 공정 탐색 수 시간에서 수 분으로 줄였다…팹 여러 곳으로 넓힌다"        # 한도(50자) 초과
LONG_B = "Intel Foundry, AI 보조 공정 최적화 사례를 학회에서 처음으로 공개했다…다음은 양산 라인"   # 한도(50자) 초과


def test_overlong_alternatives_are_kept_and_shown_with_reason():
    assert len(LONG_A) > 36 and len(LONG_B) > 36
    draft, by_id, recs, d = articles_ctx(fill("정상 본문", 500))
    draft["summary"] = draft["summary_candidates"][0] = fill("총론", 180)
    draft["articles"][0]["title"] = "Lam 제목"
    draft["articles"][0]["title_alternatives"] = [LONG_A, LONG_B]
    draft["articles"][0]["evidence_ids"] = [f"{d}/P1"]
    out, warns = run_s7(Judge(FakeClient()), draft, list(by_id.values()), recs, CFG, "정규호", {})
    assert out["articles"][0]["title_alternatives"] == [LONG_A, LONG_B]       # 조용히 빼지 않는다
    text = rv.render_review2(WEEK, out, [], CFG)
    assert f"{LONG_A} [한도 초과 {len(LONG_A)}자>50]" in text and f"{LONG_B} [한도 초과 {len(LONG_B)}자>50]" in text
    with pytest.raises(rv.ReviewError, match="한도"):
        rv.validate_review2(rv.parse_review2("승인 D 핵심1=a"), out, [], CFG)
    rv.validate_review2(rv.parse_review2("승인"), out, [], CFG)


def test_valid_alternatives_always_show():
    d = sample_draft()
    d["articles"][0]["title_alternatives"] = ["삼성 Defect Agent, 혐의 공정 탐색", "Intel Foundry, AI 보조 공정 최적화"]
    text = rv.render_review2(WEEK, d, [], CFG)
    assert "핵심 제목1 | 삼성 Defect Agent, 혐의 공정 탐색 | Intel Foundry, AI 보조 공정 최적화" in text and "한도 초과" not in text


def test_overlong_candidate_cannot_be_picked_and_first_overlong_is_demoted():
    d = sample_draft()
    d["issue_title_candidates"] = ["가" * 31, "정상 타이틀", "다른 타이틀"]
    assert "[한도 초과 31자>30]" in rv.render_review2(WEEK, d, [], CFG)
    with pytest.raises(rv.ReviewError, match="A1"):
        rv.validate_review2(rv.parse_review2("A1 B1"), d, [], CFG)


# 5. --answer 반복·이어 답하기 -------------------------------------------------------

def noninteractive(tmp, client, answers=()):
    return Pipeline(settings_mod.load(data_root=tmp), Judge(client), ConsoleIO(list(answers), interactive=False))


def state_of(tmp):
    return json.loads((tmp / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))


def test_answer_option_is_repeatable():
    a = build_parser().parse_args(["resume", "--answer", "2) 3=핵심", "--answer", "2"])
    assert a.answer == ["2) 3=핵심", "2"]


def test_nested_cap_check_answered_in_one_run(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    assert noninteractive(tmp_path, client).start(pkg) == 4
    capsys.readouterr()
    assert noninteractive(tmp_path, client, ["2) 3=핵심", "2"]).resume() == 4         # ① 응답 + 상한 확인 답을 한 번에
    out = capsys.readouterr().out
    assert "━━ 상한 확인" in out and "━━ 검토 요청 ② 머리부·제목" in out
    st = state_of(tmp_path)
    assert st["pending_review"] is None and sorted(e["slot"] for e in st["events"] if e["slot"] == "core") == ["core"] * 3


def test_pending_cap_check_survives_resume(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    noninteractive(tmp_path, client).start(pkg)
    capsys.readouterr()
    assert noninteractive(tmp_path, client, ["2) 3=핵심"]).resume() == 4               # 상한 확인에서 멈춤
    out = capsys.readouterr().out
    assert "━━ 상한 확인" in out
    pend = state_of(tmp_path)["pending_review"]
    assert pend["review"] == "R1" and pend["text"] == "2) 3=핵심" and state_of(tmp_path)["stage"] == "R1"
    assert noninteractive(tmp_path, client, ["1"]).resume() == 4                       # 질문에 이어서 답(단신 유지)
    out = capsys.readouterr().out
    assert "━━ 검토 요청 ① 선정" not in out and "━━ 검토 요청 ② 머리부·제목" in out    # ①을 다시 묻지 않는다
    st = state_of(tmp_path)
    assert st["pending_review"] is None and sum(e["slot"] == "core" for e in st["events"]) == 2


def test_pending_b_confirm_survives_resume(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    noninteractive(tmp_path, client).start(pkg)
    assert noninteractive(tmp_path, client, ["1"]).resume() == 4                       # ② 요청까지
    capsys.readouterr()
    assert noninteractive(tmp_path, client, ['A2 B:수정 "더 짧게"']).resume() == 4         # B 재수정 확인에서 멈춤
    assert "━━ B 재수정 확인" in capsys.readouterr().out
    assert state_of(tmp_path)["pending_review"]["review"] == "R2"
    assert noninteractive(tmp_path, client, ["2: 한 번 더"]).resume() == 4               # 다시 수정 → 새 확인 질문
    assert "━━ B 재수정 확인" in capsys.readouterr().out
    assert noninteractive(tmp_path, client, ["1"]).resume() == 4                         # 승인 → 발송 확인으로
    out = capsys.readouterr().out
    assert "━━ 검토 요청 ② 머리부·제목" not in out and "━━ 발송 확인" in out
    st = state_of(tmp_path)
    assert st["pending_review"] is None and st["metrics"]["r2"]["b_revisions"] == 2 and st["metrics"]["r2"]["A_custom"] is False
    import yaml
    d = yaml.safe_load((tmp_path / "review" / f"{WEEK}-draft.yaml").read_text(encoding="utf-8"))
    assert d["issue_title"] == "현장 적용 범위 확대" and d["summary"].startswith("수정된 총론이다.")


def test_b_confirm_answers_in_one_run(tmp_path, capsys):
    pkg = make_package(tmp_path, sample_records())
    client = FakeClient()
    noninteractive(tmp_path, client).start(pkg)
    noninteractive(tmp_path, client, ["1"]).resume()
    capsys.readouterr()
    assert noninteractive(tmp_path, client, ['승인 B:수정 "짧게"', "1"]).resume() == 4         # ② 응답 + B 확인을 한 번에 → 발송 확인에서 멈춤
    assert "━━ 발송 확인" in capsys.readouterr().out


# 영문 근거 ↔ 한국어 원고: 단위만 다른 같은 숫자, 표기만 다른 같은 회사(v4.7 개편, W48 오탐) -----------------

def test_number_with_korean_unit_matches_english_source():
    from internal import checks
    pairs = [("d/P1", "Spot robots completed more than 90,000 inspections across the sub-fab.")]
    w = checks.number_warnings("서브팹 점검 9만 건을 넘게 했다", pairs, ["x"], "01")
    assert w == []                                                     # 9만 = 90,000, 한정어(넘게)도 있음
    w = checks.number_warnings("서브팹 점검 9만 건을 했다", pairs, ["x"], "01")
    assert len(w) == 1 and "한정어 누락" in w[0]["message"]              # 값은 같지만 'more than'이 빠짐
    pairs = [("d/P2", "The company will invest $1.2 billion in the fab.")]
    assert checks.number_warnings("12억 달러를 투자한다", pairs, ["x"], "01") == []
    assert len(checks.number_warnings("13억 달러를 투자한다", pairs, ["x"], "01")) == 1     # 값이 다르면 여전히 경고
    pairs = [("d/P3", "about 90 thousand wafers")]
    assert checks.number_warnings("약 90,000장", pairs, ["x"], "01") == []


def test_company_alias_matches_english_source():
    from internal import checks
    src = checks.norm("Samsung Electronics said its Defect Agent cut analysis time. SK hynix also ...")
    al = checks.alias_groups({"competitors": [{"name": "SK hynix", "aliases": ["SK하이닉스"]}]})
    assert checks.company_warnings("삼성전자와 SK하이닉스가 결함 분석에 AI를 쓴다", src, "01", al) == []
    assert [w["quote"] for w in checks.company_warnings("삼성전자가 발표했다", src, "01")] == ["삼성전자"]   # 별칭이 없으면 예전처럼 경고
    assert [w["quote"] for w in checks.company_warnings("LG전자가 발표했다", src, "01", al)] == ["LG전자"]   # 근거에 없는 회사는 경고
