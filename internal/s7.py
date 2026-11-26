"""S7 원고 자동 검사 실행(설계서 9.7): 자동 보정 → 재생성 1회 → 남은 문제는 경고(검토 요청 ②의 [E]). 약어 풀이 용어 모으기와 제안(G)도 여기서 한다."""
from . import checks, glossary
from .draft import apply_header, gen_article, gen_glossary, gen_header, gen_more, more_item_for, _glossary_user, GLOSSARY_SCHEMA
from .evidence import prune_invalid_evidence, prune_invalid_images
from .review import article_labels


def update_glossary(judge, draft, by_id, records, terms, common, warns=None):
    """이번 호에 쓴 용어를 draft['glossary']로 다시 모은다. glossary.yaml에 없는 약어는 모델이 원문 근거로 풀이를 제안한다(source: model).
    풀이를 만들지 못한 약어는 풀이 없이 목록에 남아 검토 요청 ②의 [G]에서 직접 입력할 수 있다."""
    previous = [g for g in draft.get("glossary") or [] if g["source"] != "glossary"]
    have = {g["term"] for g in previous}
    ignore = {w for r in records.values() for w in (r["publisher"] or "").replace("·", " ").split()}
    unknown = [t for t in glossary.find_unknown(draft, terms, common, ignore) if t not in have]
    reqs = {t: glossary.term_contexts(t, draft, by_id, records) for t in unknown}
    if judge.batching:
        judge.prefetch([("s6_glossary", _glossary_user(judge, t, c), GLOSSARY_SCHEMA) for t, c in reqs.items() if c])
    failed = []
    for t in unknown:
        r = gen_glossary(judge, t, reqs[t], records) if reqs[t] else None
        if r:
            previous.append(r)
        else:
            previous.append({"term": t, "meaning": "", "source": "model"})
            failed.append(t)
    entries = glossary.build_glossary(draft, terms, previous)
    present = {g["term"] for g in entries}
    for g in previous:      # 풀이가 비어 있는 약어도 원고에 아직 있으면 목록에 남긴다
        if g["term"] not in present and not g["meaning"] and any(glossary._pattern(g["term"]).search(x) for _, x in glossary.gloss_fields(draft, top=True)):
            entries.append(g)
    draft["glossary"] = entries
    if warns is not None:
        for t in failed:
            warns.append({"scope": "머리부", "quote": t, "kind": "용어", "message": f"약어 {t}의 풀이를 자동으로 만들지 못함 — [G]에서 직접 입력", "fix": None})
    return failed


def run_s7(judge, draft, events, records, cfg, issue_type, counts, failed_gen=(), glossary_data=None):
    by_id = {e["event_id"]: e for e in events}
    warns = checks.autofix(draft, records)
    for f in failed_gen:
        warns.append({"scope": "머리부" if f == "머리부" else f, "quote": "", "kind": "자동 작성 실패",
                      "message": "자동 작성에 실패해 대체 문구를 넣었습니다. 직접 확인하세요", "fix": None})

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
        new, bad = gen_more(judge, more_item_for(m, by_id), records, cfg, "\n".join(msgs))
        if not bad:
            draft["more"][j] = new
            regenerated = True
    if (hdr_fb or regenerated) and draft["articles"]:
        fb = list(hdr_fb) + (["기사·더 볼 거리 원고가 다시 쓰였으니 내용이 어긋나지 않게 머리부를 다시 써라."] if regenerated and not hdr_fb else [])
        h, bad = gen_header(judge, draft["articles"], draft.get("more") or [], records, issue_type, counts, cfg, "\n".join(fb))
        if not bad:
            apply_header(draft, h)

    # 재생성 후에도 남은 문제 → 보정 또는 경고
    for e in prune_invalid_evidence(draft, by_id, records):
        warns.append({"scope": "기사", "quote": e[:40], "kind": "근거 ID",
                      "message": "재생성 후에도 실재하지 않는 근거 ID를 삭제함", "fix": None})
    for ref in prune_invalid_images(draft, by_id, records, cfg):
        warns.append({"scope": "기사", "quote": ref[:40], "kind": "이미지", "message": "재생성 후에도 후보에 없는 대표 이미지를 뺐습니다", "fix": None})
    warns += checks.autofix(draft, records)
    labels = {i: lab for lab, i in article_labels(draft).items()}
    # 글자 수 한도를 넘는 후보·대안은 빼지 않고 남긴다. 검토 요청 ②가 사유와 함께 보여 주고, 고를 수 없게 한다.
    ok = [c for c in draft["issue_title_candidates"] if len(c) <= cfg["issue_title_max"]]
    if ok and draft["issue_title_candidates"][0] not in ok:       # 추천안(첫째)이 한도를 넘으면 한도 안의 후보를 앞에 둔다
        draft["issue_title_candidates"] = ok + [c for c in draft["issue_title_candidates"] if c not in ok]
    draft["issue_title"] = draft["issue_title_candidates"][0]
    draft["summary"] = draft["summary_candidates"][0] if draft["summary_candidates"] else draft["summary"]
    for tgt, kind, msg in checks.hard_violations(draft, by_id, records, cfg, issue_type):
        scope = labels[tgt[1]] if tgt[0] == "article" else (f"F{tgt[1] + 1}" if tgt[0] == "more" else "머리부")
        warns.append({"scope": scope, "quote": "", "kind": kind, "message": f"재생성 후에도 남음: {msg}", "fix": None})
    warns += checks.soft_warnings(draft, by_id, records, cfg, issue_type)
    if glossary_data is not None:
        update_glossary(judge, draft, by_id, records, *glossary_data, warns=warns)
    return draft, checks.number_ids(warns)
