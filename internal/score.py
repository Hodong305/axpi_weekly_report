"""S4 2단계 중요도(설계서 9.4). 모델은 지표별 구간만 고르고, 합계·상한·검증은 코드가 한다."""
import json

from .common import add_flag, evidence_errors
from .llm import AutoJudgeFailed
from .settings import TOPICS

# v4.7 개편: MTC 연관성(적용 영역 30 + 대상 15) 45 · 근거 30 · 구체성 15 · 이번 주 변화 10 = 100.
# 현장 성숙도(단계 구분)와 실행 가능성은 폐지했다. 대상(mtc_target)은 모델이 아니라 코드가 분야·경쟁사 목록으로 매긴다.
METRICS = ["mtc_area", "mtc_target", "evidence_level", "specificity", "weekly_change"]
MODEL_METRICS = ["mtc_area", "evidence_level", "specificity", "weekly_change"]
METRIC_LABEL = {"mtc_area": "MTC 연관성·적용 영역", "mtc_target": "MTC 연관성·대상", "evidence_level": "근거 수준",
                "specificity": "구체성", "weekly_change": "이번 주 변화"}
TIERS = {"mtc_area": [30, 20, 10], "mtc_target": [15, 10, 5], "evidence_level": [30, 22, 15, 8],
         "specificity": [15, 10, 5], "weekly_change": [10, 5, 0]}
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
        "score_tiers": {"type": "object", "additionalProperties": False, "required": MODEL_METRICS,
                        "properties": {m: {"type": "object", "additionalProperties": False, "required": ["tier", "reason"],
                                           "properties": {"tier": {"enum": TIERS[m] + [UNKNOWN]},
                                                          "reason": {"type": "string"}}} for m in MODEL_METRICS}},
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
    for m in MODEL_METRICS:
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


def detect_competitor(ev, records, cfg):
    """경쟁사 사건이면 회사 이름, 아니면 ""(9.5). 주체를 먼저 보고, 없으면 대표 자료 발행처, 그것도 없으면 제목을 본다."""
    from collector.competitors import find_company
    companies = (cfg or {}).get("competitors") or []
    if not companies:
        return ""
    rec = (records or {}).get(ev.get("rep_doc_id")) or {}
    texts = [ev.get("subject")]
    if not (ev.get("subject") or "").strip():
        texts += [rec.get("publisher"), rec.get("title")]
    else:
        texts.append(rec.get("publisher"))
    return find_company(companies, *texts) or ""


def target_tier(topic, competitor):
    """MTC 연관성·대상(코드가 매긴다): 칩메이커 팹(경쟁사 사건 또는 칩메이커 분야) 15, 칩메이커에 공급하는 장비·솔루션 10, 그 밖 5."""
    if competitor or topic == "T1_CHIPMAKER":
        return 15
    if topic in ("T2_EQUIPMENT", "T3_SOLUTION"):
        return 10
    return 5


def mtc_score(tiers):
    """MTC 연관성 합계(적용 영역 + 대상). 미확인이면 None."""
    a, t = tiers.get("mtc_area"), tiers.get("mtc_target")
    return a + t if isinstance(a, int) and isinstance(t, int) else None


def compute_cap(event, recs, tiers, model_cap, topic, mtc_min=30):
    """게재 상한(9.4): 산업 배경, MTC 연관성 미달, 공급사 구상, B 경로 전이 사례는 단신이 상한이다.
    v4.7 개편: 단일 언론·원발표 없는 언론 근거는 상한을 두지 않는다(근거 수준 점수에만 반영).
    모델이 낮춘 상한(brief)은 존중하되 코드가 높이지는 않는다. 반환: (cap, 사유 목록)."""
    reasons = []
    if model_cap == "brief":
        reasons.append("모델 판단")
    if tiers["mtc_area"]["tier"] == 10:
        reasons.append("산업 배경")
    else:
        m = mtc_score({k: v["tier"] for k, v in tiers.items()})
        if m is not None and m < mtc_min:
            reasons.append(f"MTC 연관성 미달({m}<{mtc_min})")
    if topic == "T3_SOLUTION" and tiers["specificity"]["tier"] == 5:
        reasons.append("공급사 구상")
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


def apply_result(event, res, records, fit, cfg=None):
    """검증된 모델 응답을 사건에 반영한다. 대상 점수·합계·상한은 여기서 계산한다."""
    cfg = cfg or {}
    event["competitor"] = detect_competitor(event, records, cfg)
    tiers = dict(res["score_tiers"])
    tiers["mtc_target"] = {"tier": target_tier(res["primary_topic"], event["competitor"]),
                           "reason": "코드: " + (f"경쟁사({event['competitor']})" if event["competitor"] else res["primary_topic"])}
    status = res["status"]
    total = None
    if status == "PASS":
        total = compute_total(tiers)
        if total is None:
            status = "HOLD"
            event["note"] = "총점 보류(미확인 항목)"
            add_flag(event, "근거 미확인")
    recs = [records[d] for d in event["doc_ids"]]
    cap, reasons = compute_cap(event, recs, tiers, res["placement_cap"], res["primary_topic"], cfg.get("core_mtc_min", 30))
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


def score_events(judge, events, records, fit, cfg=None):
    if judge.batching:
        judge.prefetch([("s4_score", _user(judge, ev, fit, records), SCORE_SCHEMA)
                        for ev in events if ev["kind"] == "event" and ev["status"] == "PENDING"])
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
        apply_result(ev, res, records, fit, cfg)
    return events
