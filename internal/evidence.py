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
    for m in draft.get("more") or []:
        docs = {m["ref"]} if m["ref"].startswith("url1_") else set(events_by_id.get(m["ref"], {}).get("doc_ids", []))
        keep = [e for e in m["evidence_ids"] if not evidence_errors([e], records, docs)]
        removed += [e for e in m["evidence_ids"] if e not in keep]
        m["evidence_ids"] = keep
    return removed


def prune_invalid_images(draft, events_by_id, records, cfg):
    """재생성 후에도 후보에 없거나 정책 대상이 아닌 대표 이미지는 뺀다(검사는 경고로 남는다). 뺀 media_ref 목록을 반환."""
    from .draft import find_candidate, image_candidates
    dropped = []
    for a in draft["articles"]:
        img = a.get("image")
        if img and find_candidate(image_candidates(events_by_id[a["event_id"]], records, cfg), img["media_ref"]) is None:
            dropped.append(img["media_ref"])
            a["image"] = None
    return dropped
