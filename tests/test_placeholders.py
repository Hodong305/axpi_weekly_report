"""v4.7 가상·견본 데이터 차단(설계서 9.7·9.2 ③·9.8): 견본 표시·가상 인물·가려 쓴 회사 이름·자리 표시 글자."""
import copy
from pathlib import Path

import pytest
import yaml

from internal import checks, review as rv, settings as settings_mod

ROOT = Path(__file__).resolve().parent.parent
CFG = settings_mod.load().internal


def sample():
    return yaml.safe_load((ROOT / "docs" / "samples" / "sample-draft-v4.7.yaml").read_text(encoding="utf-8"))


def clean_draft():
    """견본에서 가상 표시를 모두 실제처럼 바꾼 원고(차단되지 않아야 한다)."""
    d = sample()
    d["glossary"] = [g for g in d["glossary"]]
    keep = d["articles"][:2]
    for a in keep:
        a["credits"] = [c.replace("(견본) ", "") for c in a["credits"]]
    d["articles"] = keep
    d["more"] = []
    d["summary"] = d["summary"]
    return d


def test_sample_is_flagged_everywhere_it_should_be():
    hits = checks.placeholder_hits(sample())
    quotes = {h["quote"] for h in hits}
    assert {"견본", "홍길동", "장비사 A", "솔루션 기업 B", "칩메이커 C", "로봇 기업 D"} <= quotes
    kinds = {h["kind"] for h in hits}
    assert {"견본 표시", "가상 인물", "가려 쓴 회사 이름"} <= kinds


@pytest.mark.parametrize("text", ["가상 계측을 식각 라인 전체로", "샘플링 검사 비율", "회사 AI 전략", "Intel Foundry A16 공정",
                                  "SK hynix 이천 M15X", "예시반도체 서브팹", "OOD 검출 모델", "A/B 시험"])
def test_real_manufacturing_terms_are_not_flagged(text):
    assert checks.placeholder_hits({"issue_title": text, "summary": text, "articles": [], "more": []}) == []


@pytest.mark.parametrize("text,kind", [("(견본) 보도", "견본 표시"), ("레이아웃 확인용 내용", "가상 데이터 표시"), ("가상 회사 사례", "가상 데이터 표시"),
                                       ("전자신문(홍길동)", "가상 인물"), ("lorem ipsum", "자리 표시 글자"), ("수율 XXX% 개선", "자리 표시 글자"),
                                       ("메모리 경쟁사 K가", "가려 쓴 회사 이름"), ("장비사 F가", "가려 쓴 회사 이름"), ("TODO 채우기", "자리 표시 글자")])
def test_each_pattern(text, kind):
    hits = checks.placeholder_hits({"issue_title": "", "summary": text, "articles": [], "more": []})
    assert hits and hits[0]["kind"] == kind


def test_clean_draft_passes_and_extra_text_is_checked():
    d = clean_draft()
    assert checks.placeholder_hits(d) == []
    assert checks.placeholder_hits(d, "꼬리말 견본") and checks.placeholder_hits(d, "꼬리말 견본")[0]["scope"] == "발송본"


def test_s7_hard_violation_targets_model_written_fields():
    d = clean_draft()
    d["articles"][0]["points"]["what"] = "장비사 A가 결함 분석을 자동화했다."
    d["summary"] = d["summary"] + " 홍길동"
    v = [(t, k) for t, k, _ in checks.hard_violations(d, {a["event_id"]: {"event_id": a["event_id"], "doc_ids": []} for a in d["articles"]},
                                                       {}, CFG, "정규호") if k == "가상 데이터"]
    assert (("article", 0), "가상 데이터") in v and (("header",), "가상 데이터") in v


def test_review3_blocks_send_while_placeholders_remain():
    s = {"title": "t", "summary": "s", "summary_len": 1, "core": 1, "brief": 0, "body_len": 1, "body_max": 9, "sources": 1, "warnings": 0}
    ph = [{"scope": "핵심1 출처", "quote": "견본", "kind": "견본 표시"}]
    text = rv.render_review3("2026-W48", dict(s, placeholders=ph))
    assert "[발송 불가]" in text and " 1) 발송본" not in text and "핵심1 출처 \"견본\"" in text
    with pytest.raises(rv.ReviewError):
        rv.parse_review3("1", ph)
    assert rv.parse_review3("2", ph) == 2
    ok = rv.render_review3("2026-W48", s)
    assert " 1) 발송본 생성·확정 (권장)" in ok and rv.parse_review3("1", []) == 1


def test_prompt_forbids_placeholders():
    steps = (ROOT / "prompts" / "internal-steps.md").read_text(encoding="utf-8")
    assert "가려 쓰거나" in steps and "홍길동" in steps
