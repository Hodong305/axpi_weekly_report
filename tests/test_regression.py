"""회귀 사례(설계서 11장 5종) 시험. 모델·프롬프트·설정을 바꾸기 전에 실행해 판정이 달라지는지 본다.
실행: python -m pytest -q tests/test_regression.py   (결과 갱신: python tests/regression/run.py --update)"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "regression"))
import harness  # noqa: E402

CASES = harness.load_cases()


@pytest.mark.parametrize("name", sorted(CASES))
def test_regression_matches_stored_result(name):
    assert harness.run_case(name, CASES[name]) == harness.load_expected()[name]


def only_event(name):
    return harness.run_case(name, CASES[name])["events"][0]


def test_regression_key_outcomes():
    e = only_event("1_late_evidence")                  # 후반 문단 근거 → 재분류로 통과, 핵심
    assert (e["status"], e["slot"], e["total"]) == ("PASS", "core", 82)       # 30 + 대상 10 + 22 + 15 + 5
    e = only_event("2_design_manufacturing_mixed")     # 설계·제조 혼재 → 판단 보류
    assert (e["kind"], e["status"], e["slot"], e["total"]) == ("hold_doc", "HOLD", "hold", None)
    e = only_event("3_operating_and_future")           # 운영 중 + 향후 시험: 유형을 따로 기록
    assert e["slot"] == "core" and e["evidence_types"] == ["실측", "계획"]
    r = harness.run_case("4_rerun_no_change", CASES["4_rerun_no_change"])   # 재전재 → 제외, doc_id만 추가
    assert r["events"][0]["slot"] == "out" and r["events"][0]["ledger"] == "SAME_NO_CHANGE"
    assert r["ledger_after"][0]["n_docs"] == 2 and r["ledger_after"][0]["issues"] == ["2026-W47"]
    r = harness.run_case("5_stage_change", CASES["5_stage_change"])         # 단계 변경 → 후속, 대장 갱신
    assert r["events"][0]["ledger"] == "SAME_WITH_CHANGE" and r["ledger_after"][0]["issues"] == ["2026-W47", "2026-W49"]
    assert r["ledger_after"][0]["stage"] == "양산·상시 운영"
