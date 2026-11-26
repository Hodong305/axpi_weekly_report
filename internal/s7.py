"""S7 원고 자동 검사 실행(설계서 9.7): 자동 보정 → 재생성 1회 → 남은 문제는 경고(검토 요청 ②의 [E])."""
from . import checks
from .draft import gen_article, gen_header, gen_more, apply_header
from .evidence import prune_invalid_evidence
from .review import article_labels


def run_s7(judge, draft, events, records, cfg, issue_type, counts, failed_gen=()):
    by_id = {e["event_id"]: e for e in events}
    warns = checks.autofix(draft, records)
    for f in failed_gen:
        warns.append({"scope": "머리부" if f == "머리부" else f, "quote": "", "kind": "자동 작성 실패",
                      "message": "자동 작성에 실패해 대체 문구를 넣었습니다. 직접 확인하세요", "fix": None})
    labels = {i: lab for lab, i in article_labels(draft).items()}

    regenerated = False
    vio = checks.hard_violations(draft, by_id, records, cfg, issue_type)
    art_fb, hdr_fb, more_fb = {}, [], {}
    for tgt, kind, msg in vio:
        if tgt[0] == "more":
            more_fb.setdefault(tgt[1], []).append(f"[{kind}] {msg}")
        elif tgt[0] == "article":
            art_fb.setdefault(tgt[1], []).append(f"[{kind}] {msg}")
        else:
            hdr_fb.append(f"[{kind}] {msg}")
    for idx, msgs in art_fb.items():
        a = draft["articles"][idx]
        new, bad = gen_article(judge, by_id[a["event_id"]], a["slot"], records, cfg, "\n".join(msgs))
        if not bad:
            draft["articles"][idx] = new
            regenerated = True
    for j, msgs in more_fb.items():       # 더 볼 거리 한 줄도 한 번 다시 만든다(9.7)
        m = draft["more"][j]
        doc = m["ref"] if m["ref"].startswith("url1_") else by_id[m["ref"]]["rep_doc_id"]
        new, bad = gen_more(judge, {"ref": m["ref"], "topic": m["topic"], "doc_id": doc}, records, cfg, "\n".join(msgs))
        if not bad:
            draft["more"][j] = new
    if (hdr_fb or regenerated) and draft["articles"]:
        fb = list(hdr_fb) + (["기사 본문이 다시 쓰였으니 내용이 어긋나지 않게 머리부를 다시 써라."] if regenerated and not hdr_fb else [])
        h, bad = gen_header(judge, draft["articles"], by_id, records, issue_type, counts, cfg, "\n".join(fb))
        if not bad:
            apply_header(draft, h)

    # 재생성 후에도 남은 문제 → 보정 또는 경고
    for e in prune_invalid_evidence(draft, by_id, records):
        warns.append({"scope": "기사", "quote": e[:40], "kind": "근거 ID",
                      "message": "재생성 후에도 실재하지 않는 근거 ID를 삭제함", "fix": None})
    warns += checks.autofix(draft, records)
    labels = {i: lab for lab, i in article_labels(draft).items()}
    # 글자 수 한도를 넘는 후보·대안은 빼지 않고 남긴다. 검토 요청 ②가 사유와 함께 보여 주고, 고를 수 없게 한다.
    for k in ("issue_title_candidates", "one_line_candidates"):
        mx = cfg["issue_title_max"] if k.startswith("issue") else cfg["one_line_max"]
        ok = [c for c in draft[k] if len(c) <= mx]
        if ok and draft[k][0] not in ok:       # 추천안(첫째)이 한도를 넘으면 한도 안의 후보를 앞에 둔다
            draft[k] = ok + [c for c in draft[k] if c not in ok]
    if draft["issue_title_candidates"][0] != draft["issue_title"]:
        draft["issue_title"] = draft["issue_title_candidates"][0]
    if draft["one_line_candidates"][0] != draft["one_line"]:
        draft["one_line"] = draft["one_line_candidates"][0]
    for tgt, kind, msg in checks.hard_violations(draft, by_id, records, cfg, issue_type):
        scope = labels[tgt[1]] if tgt[0] == "article" else (f"F{tgt[1] + 1}" if tgt[0] == "more" else "머리부")
        warns.append({"scope": scope, "quote": "", "kind": kind, "message": f"재생성 후에도 남음: {msg}", "fix": None})
    warns += checks.soft_warnings(draft, by_id, records, cfg, issue_type)
    return draft, checks.number_ids(warns)
