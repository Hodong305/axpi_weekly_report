"""S4 2단계 중요도(설계서 9.4). 모델은 지표별 구간만 고르고, 합계·상한·검증은 코드가 한다."""
import json

from .common import add_flag, evidence_errors
from .llm import AutoJudgeFailed
from .settings import TOPICS

METRICS = ["fab_relevance", "evidence_level", "field_maturity", "weekly_change", "actionability"]
METRIC_LABEL = {"fab_relevance": "FAB 업무 관련성", "evidence_level": "근거 수준", "field_maturity": "현장 성숙도",
                "weekly_change": "이번 주 변화", "actionability": "실행 가능성"}
TIERS = {"fab_relevance": [30, 20, 10], "evidence_level": [25, 20, 15, 5], "field_maturity": [20, 15, 10, 5],
         "weekly_change": [15, 10, 5], "actionability": [10, 5, 0]}
UNKNOWN = "미확인"
CLAIM_TYPES = ["실측", "규모", "목표", "계획", "기관 결정"]
SCORE_FIELDS = ["event_id", "status", "primary_topic", "claim", "evidence_ids", "maturity", "score_tiers",
                "placement_cap", "fact", "mtc_impact", "action_watch", "unresolved"]

SCORE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": SCORE_FIELDS,
    "properties": {
        "event_id": {"type": "string"},
        "status": {"enum": ["PASS", "HOLD", "EXCLUDE"]},
        "primary_topic": {"enum": TOPICS},
        "claim": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "type"],
            "properties": {"id": {"type": "string"}, "type": {"enum": CLAIM_TYPES}}}},
        "maturity": {"type": "string"},
        "score_tiers": {"type": "object", "additionalProperties": False, "required": METRICS,
                        "properties": {m: {"type": "object", "additionalProperties": False, "required": ["tier", "reason"],
                                           "properties": {"tier": {"enum": TIERS[m] + [UNKNOWN]},
                                                          "reason": {"type": "string"}}} for m in METRICS}},
        "placement_cap": {"enum": ["core", "brief"]},
        "fact": {"type": "array", "items": {"type": "string"}},
        "mtc_impact": {"type": "string"},
        "action_watch": {"type": "string"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
    },
}


def tier_errors(tiers):
    """구간 허용값 검사. 허용값이 아니면 오류 목록을 반환한다."""
    errs = []
    for m in METRICS:
        t = tiers[m]["tier"]
        if isinstance(t, bool) or (t != UNKNOWN and t not in TIERS[m]):
            errs.append(f"{METRIC_LABEL[m]} 구간 {t!r}은(는) 허용값 {TIERS[m]} 밖")
    return errs


def compute_total(tiers):
    """합계. 미확인이 하나라도 있으면 None(총점 보류)."""
    vals = [tiers[m]["tier"] for m in METRICS]
    if any(v == UNKNOWN for v in vals):
        return None
    return sum(vals)


def compute_cap(event, recs, tiers, model_cap, topic):
    """게재 상한(9.4): 공급사 구상, 단일 언론 근거, 산업 배경, B 경로 전이 사례는 단신이 상한이다.
    모델이 낮춘 상한(brief)은 존중하되 코드가 높이지는 않는다. 반환: (cap, 사유 목록)."""
    reasons = []
    if model_cap == "brief":
        reasons.append("모델 판단")
    if tiers["fab_relevance"]["tier"] == 10:
        reasons.append("산업 배경")
    if topic == "T3_SOLUTION" and tiers["field_maturity"]["tier"] == 5:
        reasons.append("공급사 구상")
    originals = [r for r in recs if r["source_type"] in ("company_official", "standards_body", "academic", "research_lab")]
    if not originals and any(r["source_type"] == "press" for r in recs):
        reasons.append("단일 언론 근거" if len(recs) == 1 else "원발표 없는 언론 근거")
    elif not originals:
        reasons.append("당사자 원문 미확인")
    if recs and all(r["track"] == "B" for r in recs):
        reasons.append("B 경로 전이 사례")
    return ("brief" if reasons else "core"), reasons


def _validator(event, records):
    docs = set(event["doc_ids"])

    def validate(obj):
        errs = []
        if obj["event_id"] != event["event_id"]:
            errs.append(f"event_id가 입력과 다름: {obj['event_id']}")
        errs += evidence_errors([e["id"] for e in obj["evidence_ids"]], records, docs)
        if obj["status"] == "PASS":
            errs += tier_errors(obj["score_tiers"])
            if not obj["evidence_ids"]:
                errs.append("PASS인데 근거 ID가 없음")
            if not obj["claim"].strip():
                errs.append("PASS인데 대표 주장이 비어 있음")
        return errs
    return validate


def _user(judge, event, fit, records):
    led = event["ledger"]
    parts = ["[사건]\n" + json.dumps({"event_id": event["event_id"], "subject": event["subject"],
                                      "site_task": event["site_task"]}, ensure_ascii=False)]
    parts.append("[1단계 결과]\n" + json.dumps({d: (fit[d].get("stage2") or fit[d].get("stage1")) for d in event["doc_ids"]},
                                              ensure_ascii=False))
    parts.append("[사건 대장 대조 결과]\n" + json.dumps({"verdict": led["verdict"], "matched_event_key": led["key"],
                                                    "changes": led["changes"]}, ensure_ascii=False))
    if led["verdict"] == "SAME_WITH_CHANGE":
        parts.append("[해당 대장 항목]\n" + json.dumps(led.get("entry"), ensure_ascii=False))
    parts += [judge.source(records[d]) for d in event["doc_ids"]]
    return "\n".join(parts)


def apply_result(event, res, records, fit):
    """검증된 모델 응답을 사건에 반영한다. 합계·상한은 여기서 계산한다."""
    tiers = res["score_tiers"]
    status = res["status"]
    total = None
    if status == "PASS":
        total = compute_total(tiers)
        if total is None:
            status = "HOLD"
            event["note"] = "총점 보류(미확인 항목)"
            add_flag(event, "근거 미확인")
    recs = [records[d] for d in event["doc_ids"]]
    cap, reasons = compute_cap(event, recs, tiers, res["placement_cap"], res["primary_topic"])
    event["primary_topic"] = res["primary_topic"]
    event["status"] = status
    event["cap"], event["cap_reasons"] = cap, reasons
    event["score"] = {"total": total if status == "PASS" else None,
                      "tiers": {m: tiers[m]["tier"] for m in METRICS},
                      "tier_reasons": {m: tiers[m]["reason"] for m in METRICS},
                      "claim": res["claim"], "evidence": res["evidence_ids"], "maturity": res["maturity"],
                      "fact": res["fact"], "mtc_impact": res["mtc_impact"], "action_watch": res["action_watch"],
                      "unresolved": res["unresolved"]}
    if status == "HOLD" and not event["note"]:
        event["note"] = "근거 미확인·출처 충돌" if res["unresolved"] else "보류"
    if status == "EXCLUDE":
        event["note"] = "2단계 제외"


def score_events(judge, events, records, fit):
    """PASS 후보 사건마다 2단계를 호출한다. 두 번 실패하면 '자동 판정 실패'로 대기 목록에 올린다."""
    for ev in events:
        if ev["kind"] != "event" or ev["status"] != "PENDING":
            continue
        try:
            res = judge.ask("s4_score", _user(judge, ev, fit, records), SCORE_SCHEMA, _validator(ev, records))
        except AutoJudgeFailed as e:
            ev.update(status="HOLD", note="자동 판정 실패")
            add_flag(ev, "자동 판정 실패")
            ev["score"] = None
            continue
        apply_result(ev, res, records, fit)
    return events
