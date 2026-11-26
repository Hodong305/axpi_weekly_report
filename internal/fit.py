"""S2 1단계 적합도(설계서 9.3). 다음 처리는 코드가 정한다."""
from .llm import AutoJudgeFailed
from .settings import TOPICS

FIT_VALUES = ["2", "1", "0", "U"]
EXCLUSION_CODES = ["DESIGN_ONLY", "MARKET_ONLY", "ROBOT_OUTSIDE_FAB", "NO_WEEKLY_CHANGE"]
ROUTES = ["REVIEW", "EXCLUDE_CANDIDATE", "WATCH_TRANSFER"]

FIT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["doc_id", "fit", "reasons", "evidence_ids", "route", "exclusion_code"],
    "properties": {
        "doc_id": {"type": "string"},
        "fit": {"type": "object", "additionalProperties": False, "required": TOPICS,
                "properties": {t: {"enum": FIT_VALUES} for t in TOPICS}},
        "reasons": {"type": "object", "additionalProperties": False, "required": TOPICS,
                    "properties": {t: {"type": "string"} for t in TOPICS}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "route": {"enum": ROUTES},
        "exclusion_code": {"enum": [None] + EXCLUSION_CODES},
    },
}


def decide_stage1(res):
    """1단계 결과의 다음 처리(9.3 표). ADVANCE | RECLASSIFY(본문 전체로 재분류).
    모두 0이어도 제외 사유가 불명확하면 U로 되돌려 RECLASSIFY한다. 제외 후보도 본문으로 한 번 더 확인한다."""
    if "2" in res["fit"].values():
        return "ADVANCE"
    return "RECLASSIFY"


def decide_stage2(res):
    """본문 전체 재분류 결과: ADVANCE | EXCLUDE | HOLD(판단 보류)."""
    vals = list(res["fit"].values())
    if "2" in vals:
        return "ADVANCE"
    if all(v == "0" for v in vals) and res.get("exclusion_code") and all(res["reasons"][t].strip() for t in TOPICS):
        return "EXCLUDE"
    return "HOLD"


def _normalize(obj):
    f = obj.get("fit")
    if isinstance(f, dict):
        obj["fit"] = {k: (str(v) if isinstance(v, int) and not isinstance(v, bool) else v) for k, v in f.items()}
    return obj


def _validator(rec, allowed_blocks, records):
    ids = {f"{rec['doc_id']}/{b}" for b in allowed_blocks}

    def validate(obj):
        _normalize(obj)
        errs = []
        if obj.get("doc_id") != rec["doc_id"]:
            errs.append(f"doc_id가 입력과 다름: {obj.get('doc_id')}")
        bad = [e for e in obj.get("evidence_ids", []) if e not in ids]
        errs += [f"제공되지 않은 근거 ID: {e}" for e in bad]
        if obj.get("fit") and all(v == "U" for v in obj["fit"].values()) and obj.get("evidence_ids"):
            errs.append("U의 근거 ID는 비워 두어야 함")
        return errs
    return validate


def stage1_request(judge, rec):
    return ("s2_fit", judge.source(rec, set(rec["body"]["lead_ids"])), FIT_SCHEMA)


def classify_doc(judge, rec, records):
    """한 문서를 판정해 결과를 {stage1, stage2, decision, flags, ...}로 반환한다."""
    out = {"doc_id": rec["doc_id"], "stage1": None, "stage2": None, "decision": None, "flags": [],
           "exclusion_code": None, "route": None}
    lead = rec["body"]["lead_ids"]
    user = judge.source(rec, set(lead))
    try:
        s1 = judge.ask("s2_fit", user, FIT_SCHEMA, _validator(rec, lead, records))
    except AutoJudgeFailed:
        out.update(decision="HOLD", flags=["자동 판정 실패"], note="자동 판정 실패")
        return out
    _normalize(s1)
    out["stage1"] = s1
    out["route"] = s1["route"]
    if decide_stage1(s1) == "ADVANCE":
        out["decision"] = "ADVANCE"
        return out
    all_ids = [b["id"] for b in rec["body"]["blocks"]]
    try:
        s2 = judge.ask("s2_reclassify", judge.source(rec), FIT_SCHEMA, _validator(rec, all_ids, records))
    except AutoJudgeFailed:
        out.update(decision="HOLD", flags=["자동 판정 실패"], note="자동 판정 실패")
        return out
    _normalize(s2)
    out["stage2"] = s2
    out["route"] = s2["route"]
    out["decision"] = decide_stage2(s2)
    if out["decision"] == "EXCLUDE":
        out["exclusion_code"] = s2["exclusion_code"]
        out["note"] = s2["exclusion_code"]
    elif out["decision"] == "HOLD":
        out["note"] = "분야 판정 불가"
    return out


def run_fit(judge, records):
    if judge.batching:      # 파일 모드: 1단계 요청을 모두 내보내고, 답이 오면 재분류 요청도 한꺼번에 내보낸다
        recs = [records[d] for d in sorted(records)]
        judge.prefetch([stage1_request(judge, r) for r in recs])
        need = []
        for r in recs:
            try:
                s1 = judge.ask("s2_fit", stage1_request(judge, r)[1], FIT_SCHEMA, _validator(r, r["body"]["lead_ids"], records))
            except AutoJudgeFailed:
                continue
            if decide_stage1(_normalize(s1)) != "ADVANCE":
                need.append(("s2_reclassify", judge.source(r), FIT_SCHEMA))
        judge.prefetch(need)
    return {doc_id: classify_doc(judge, rec, records) for doc_id, rec in sorted(records.items())}
