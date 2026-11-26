"""검토 요청·응답 기록(review/{issue_week}-review-log.jsonl). 시범 지표(설계서 11장)와 검토시간 계산에 쓴다."""
import json
from datetime import datetime

from collector.ref import KST


def now_iso():
    return datetime.now(KST).isoformat(timespec="milliseconds")


class ReviewLog:
    def __init__(self, path, clock=now_iso):
        self.path = path
        self.clock = clock

    def write(self, **rec):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": self.clock(), **rec}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read(self):
        if not self.path.exists():
            return []
        return [json.loads(ln) for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def elapsed(self, review):
        """검토 요청 출력 시각(처음)과 마지막 유효 응답 시각의 차이(초). review는 'R1'처럼 접두어로 하위 요청(R1-cap 등)도 포함한다."""
        recs = [r for r in self.read() if r.get("review", "").split("-")[0] == review]
        reqs = [r for r in recs if r["kind"] == "request"]
        ans = [r for r in recs if r["kind"] == "response" and r.get("valid")]
        if not reqs or not ans:
            return 0.0
        t0 = datetime.fromisoformat(reqs[0]["ts"])
        t1 = datetime.fromisoformat(ans[-1]["ts"])
        return max(0.0, (t1 - t0).total_seconds())
