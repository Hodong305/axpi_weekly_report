"""회귀 사례 실행기: cases.json의 고정 입력과 정해진 모델 응답으로 파이프라인을 돌려 결과를 요약한다.
쓰임: tests/test_regression.py(시험), tests/regression/run.py(결과 비교·갱신)."""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import test_internal as ti                      # noqa: E402  (가짜 입출력·패키지 생성기 재사용)
from internal import events as ev_mod           # noqa: E402
from internal import settings as settings_mod   # noqa: E402
from internal.llm import Judge                  # noqa: E402
from internal.pipeline import Pipeline          # noqa: E402

TOPIC = {"T1": "T1_CHIPMAKER", "T2": "T2_EQUIPMENT", "T3": "T3_SOLUTION", "T4": "T4_PHYSICAL_AI"}
WEEK = ti.WEEK


def load_cases():
    return json.loads((HERE / "cases.json").read_text(encoding="utf-8"))["cases"]


def load_expected():
    return json.loads((HERE / "expected.json").read_text(encoding="utf-8"))


class CannedClient(ti.FakeClient):
    """사례별로 정해 둔 응답만 돌려주는 가짜 모델. 문서는 제목으로 찾는다."""

    def __init__(self, spec, records):
        super().__init__()
        self.spec = spec
        self.records = records

    def _key(self, user):
        d = ti.doc_ids_in(user)[0]
        for doc in self.spec["docs"]:
            if self.records_by_key[doc["key"]] == d:
                return d, doc["key"]
        raise KeyError(d)

    def _fit_resp(self, user, stage):
        d, key = self._key(user)
        s = self.spec["llm"][key][stage]
        fit = {TOPIC[k]: v for k, v in s["fit"].items()}
        direct = [t for t, v in fit.items() if v == "2"]
        ev = [f"{d}/{s['evidence']}"] if direct and "evidence" in s else ([f"{d}/P1"] if direct else [])
        return {"doc_id": d, "fit": fit, "reasons": {t: "사유" for t in fit}, "evidence_ids": ev, "route": "REVIEW",
                "exclusion_code": s.get("exclusion_code")}

    def r_s2_fit(self, user):
        return self._fit_resp(user, "stage1")

    def r_s2_reclassify(self, user):
        return self._fit_resp(user, "stage2")

    def r_s3_group(self, user):
        groups = []
        for d in ti.doc_ids_in(user):
            rec = self.records[d]
            groups.append({"doc_ids": [d], "subject": rec["publisher"], "site_task": "현장 / 과업", "title_draft": rec["title"][:30],
                           "primary_topic": "T2_EQUIPMENT"})
        return {"groups": groups}

    def r_s3_ledger(self, user):
        d, key = self._key(user)
        s = self.spec["llm"][key]["ledger"]
        return {"verdict": s["verdict"], "matched_event_key": s["matched_event_key"], "uncertain": False, "reason": "고정 응답",
                "changes": s.get("changes", []), "evidence_ids": []}

    def r_s4_score(self, user):
        import re
        eid = re.search(r'"event_id": "(EVT-[^"]+)"', user).group(1)
        d, key = self._key(user)
        s = self.spec["llm"][key]["score"]
        from internal.score import METRICS
        return {"event_id": eid, "status": "PASS", "primary_topic": "T2_EQUIPMENT", "claim": "대표 주장 한 문장",
                "evidence_ids": [{"id": f"{d}/{b}", "type": t} for b, t in zip(s["evidence"], s["types"])],
                "maturity": s["maturity"], "score_tiers": {m: {"tier": t, "reason": "사유"} for m, t in zip(METRICS, s["tiers"])},
                "placement_cap": "core", "fact": ["사실"], "mtc_impact": "영향", "action_watch": "지표", "unresolved": []}


def run_case(name, spec):
    """사례 하나를 실행해 결과 요약(dict)을 반환한다."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        recs = sorted([ti.make_record(d["url"], d["title"], d["publisher"], d["date"], d["paras"], source_type=d["source_type"],
                                      tier=d["tier"]) for d in spec["docs"]], key=lambda r: r["doc_id"])
        by_title = {r["title"]: r["doc_id"] for r in recs}
        st = settings_mod.load(data_root=tmp)
        if spec["ledger"]:
            led = []
            for e in spec["ledger"]:
                e = dict(e)
                led.append(e)
            ev_mod.save_ledger(st.ledger_path, led)
        client = CannedClient(spec, {r["doc_id"]: r for r in recs})
        client.records_by_key = {d["key"]: by_title[d["title"]] for d in spec["docs"]}
        pkg = ti.make_package(tmp, recs)
        io = ti.FakeIO(["1", "승인", "1"])
        code = Pipeline(st, Judge(client), io).start(pkg)
        state = json.loads((tmp / "review" / f"{WEEK}-state.json").read_text(encoding="utf-8"))
        events = [{"kind": e["kind"], "status": e["status"], "slot": e["slot"], "rank": e["rank"],
                   "total": (e["score"] or {}).get("total") if e.get("score") else None, "cap": e["cap"],
                   "ledger": e["ledger"]["verdict"], "flags": e["flags"],
                   "evidence_types": [x["type"] for x in e["score"]["evidence"]] if e.get("score") else []} for e in state["events"]]
        ledger = [{"event_key": e["event_key"], "placement": e["placement"], "issues": e["issues"], "stage": e["stage"],
                   "n_docs": len(e["doc_ids"])} for e in ev_mod.load_ledger(st.ledger_path)]
        return {"exit": code, "issue_type": state["issue_type"], "events": events, "ledger_after": ledger}


def run_all():
    return {name: run_case(name, spec) for name, spec in load_cases().items()}
