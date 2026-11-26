"""원고의 근거 ID 정리."""
from .common import evidence_errors


def prune_invalid_evidence(draft, events_by_id, records):
    """재생성 후에도 실재하지 않는 근거 ID는 원고에서 뺀다(검사는 경고로 남는다). 제거한 ID 목록을 반환."""
    removed = []
    for a in draft["articles"]:
        docs = set(events_by_id[a["event_id"]]["doc_ids"])
        keep = [e for e in a["evidence_ids"] if not evidence_errors([e], records, docs)]
        removed += [e for e in a["evidence_ids"] if e not in keep]
        a["evidence_ids"] = keep
    return removed
