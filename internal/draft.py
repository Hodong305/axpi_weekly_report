"""S6 원고·머리부 초안(설계서 9.6) → review/{issue_week}-draft.yaml. 출처 정보(credits)는 코드가 자료 메타데이터로 만든다."""
import json
import re

import yaml

from .common import evidence_errors, evidence_text, figure_value_errors, short_week, trunc
from .glossary import GLOSS_MAX
from .llm import AutoJudgeFailed

LANG_SUFFIX = {"ko": "", "en": "영문", "ja": "일문", "zh": "중문", "de": "독문", "fr": "불문", "es": "서문",
               "ru": "러문", "it": "이문", "nl": "네문", "pt": "포문"}

FIGURE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["value", "label"],
                 "properties": {"value": {"type": "string", "minLength": 1}, "label": {"type": "string", "minLength": 1}}}

WHY_FIGURE_SCHEMA = {       # 수치 + 왜 중요한지(why는 v4.7 추가, 선택: 이전 응답 호환)
    "type": "object", "additionalProperties": False, "required": ["value", "label"],
    "properties": {**FIGURE_SCHEMA["properties"], "why": {"type": "string"}}}

SEARCH_TERMS_SCHEMA = {"type": "array", "items": {"type": "string"}}     # v4.7 검색어(선택). 개수 한도는 search_terms_max, 근거 블록 일치는 S7 검사(checks.search_term_check)

ARTICLE_SCHEMA = {          # 단신(선택 항목 figure: 오른쪽 수치 배지 하나)
    "type": "object", "additionalProperties": False,
    "required": ["event_id", "title", "title_alternatives", "body", "evidence_ids"],
    "properties": {"event_id": {"type": "string"}, "title": {"type": "string", "minLength": 1},
                   "title_alternatives": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}},
                   "body": {"type": "string", "minLength": 1}, "figure": {"anyOf": [{"type": "null"}, WHY_FIGURE_SCHEMA]},
                   "search_terms": SEARCH_TERMS_SCHEMA,
                   "evidence_ids": {"type": "array", "items": {"type": "string"}}}}

IMAGE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["media_ref", "caption"],
                "properties": {"media_ref": {"type": "string"}, "caption": {"type": "string", "minLength": 1}}}

POINTS_SCHEMA = {          # v4.7 핵심 요점: 무슨 일 · 얼마나 · 왜 중요 · 어디까지(라벨은 코드가 붙인다). why는 나중에 더해 선택
    "type": "object", "additionalProperties": False, "required": ["what", "how_much", "scope"],
    "properties": {k: {"type": "string", "minLength": 1} for k in ("what", "how_much", "why", "scope")}}
POINT_LABELS = (("what", "무슨 일"), ("how_much", "얼마나"), ("why", "왜 중요"), ("scope", "어디까지"))

CORE_FIGURE_SCHEMA = {      # 핵심 수치 카드: why(왜 중요한지)는 v4.7 추가, 선택(이전 응답 호환)
    "type": "object", "additionalProperties": False, "required": ["value", "label"],
    "properties": {**FIGURE_SCHEMA["properties"], "why": {"type": "string"}}}

CORE_ARTICLE_SCHEMA = {     # 핵심: 한 줄 핵심(deck) + 요점(points) + 본문 + MTC AX Insight(watch) + 수치 카드(key_figures 0~3개) + 대표 이미지(image, 후보가 있을 때)
    "type": "object", "additionalProperties": False,
    "required": ["event_id", "title", "title_alternatives", "body", "watch", "key_figures", "evidence_ids"],
    "properties": {**{k: v for k, v in ARTICLE_SCHEMA["properties"].items() if k != "figure"},
                   "deck": {"type": "string", "minLength": 1}, "points": POINTS_SCHEMA,
                   "watch": {"type": "string", "minLength": 1}, "key_figures": {"type": "array", "maxItems": 3, "items": CORE_FIGURE_SCHEMA},
                   "image": {"anyOf": [{"type": "null"}, IMAGE_SCHEMA]}}}
# deck·points는 v4.7에서 추가한 키다. 프롬프트는 항상 쓰라고 하지만 스키마에서는 선택으로 두고, 빠지면 코드가 본문으로 대신한다(fallback_deck).

DECK_MAX, POINT_MAX, WHY_MAX = 120, 180, 80
SEARCH_TERMS_MAX = 3

IMAGE_CAPTION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["caption"],
                        "properties": {"caption": {"type": "string", "minLength": 1}}}

DEFAULT_MEDIA_TYPES = ("company_official", "standards_body", "research_lab")

MORE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ref", "line", "evidence_ids"],
               "properties": {"ref": {"type": "string"}, "line": {"type": "string", "minLength": 1},
                              "evidence_ids": {"type": "array", "items": {"type": "string"}}}}

TOPIC_LABEL = {"T1_CHIPMAKER": "칩메이커", "T2_EQUIPMENT": "장비사", "T3_SOLUTION": "제조 솔루션", "T4_PHYSICAL_AI": "Physical AI",
               "B": "기반기술 전이 사례"}
EVIDENCE_LABEL = {30: "독립 검증·기관 확정", 22: "운영사 발표", 15: "당사자 발표", 8: "언론 보도"}


def image_candidates(ev, records, cfg):
    """대표 이미지 후보(9.8): 사건 자료 중 이미지 정책 대상(source_type이 media_source_types)의 media[]를 doc_id 순·자료 안 순서로 펼친다.
    라벨은 펼친 순서의 M1, M2…(한 자료뿐이면 자료의 이미지 ID와 같다). 이미지 파일 내용은 모델에 주지 않는다."""
    policy = set(cfg.get("media_source_types") or DEFAULT_MEDIA_TYPES)
    out = []
    for d in sorted(ev["doc_ids"]):
        rec = records.get(d)
        if rec is None or rec["source_type"] not in policy:
            continue
        for m in rec.get("media") or []:
            out.append({"label": f"M{len(out) + 1}", "media_ref": f"{d}#{m['id']}", "doc_id": d, "kind": m["kind"], "caption": m["caption"],
                        "alt": m["alt"], "width": m["width"], "height": m["height"], "file": m["file"], "mime": m["mime"]})
    return out


def find_candidate(cands, media_ref):
    return next((c for c in cands if c["media_ref"] == media_ref), None)


def make_tags(ev, slot):
    """꼬리표(9.8, 코드가 만든다): 분야 · 근거 유형. 단신은 분야만. 채점 결과가 없으면 있는 것만(성숙도는 v4.7 개편에서 폐지)."""
    tags = []
    if ev.get("primary_topic") in TOPIC_LABEL:
        tags.append(TOPIC_LABEL[ev["primary_topic"]])
    sc = ev.get("score")
    if slot == "core" and sc:
        t = sc["tiers"]
        for key, table in (("evidence_level", EVIDENCE_LABEL),):
            if t.get(key) in table:
                tags.append(table[t[key]])
    return tags

PUNCH_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["big", "label", "text", "twist", "evidence_id"],
                "properties": {k: {"type": "string"} for k in ("big", "label", "text", "twist", "evidence_id")}}

HEADER_SCHEMA = {       # 후보 3/2/2. 한 방 후보가 없으면 빈 배열
    "type": "object", "additionalProperties": False,
    "required": ["issue_title_candidates", "summary_candidates", "punch_candidates"],
    "properties": {
        "issue_title_candidates": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string"}},
        "summary_candidates": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}},
        "punch_candidates": {"type": "array", "maxItems": 2, "items": PUNCH_SCHEMA}}}

SUMMARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["summary"],
                  "properties": {"summary": {"type": "string", "minLength": 1}}}

GLOSSARY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["term", "meaning", "evidence_ids"],
                   "properties": {"term": {"type": "string"}, "meaning": {"type": "string", "minLength": 1},
                                  "evidence_ids": {"type": "array", "items": {"type": "string"}}}}


# 출처 정보 --------------------------------------------------------------------

def format_credit(rec):
    """설계서 1장 활용 원칙 4: `발행처, 「원제」, YYYY-MM-DD`. 한국어가 아니면 `(영문)` 등, 언론은 `발행처(기자명)`. URL은 쓰지 않는다."""
    pub = rec["publisher"]
    if rec["source_type"] == "press" and rec.get("author"):
        pub = f"{pub}({rec['author']})"
    base = (rec.get("language") or "ko").split("-")[0].lower()
    suffix = LANG_SUFFIX.get(base, base)
    s = f"{pub}, 「{rec['title']}」, {rec['published_date']}"
    return s + (f" ({suffix})" if suffix else "")


def build_credits(evidence_ids, rep_doc_id, records):
    docs = []
    for e in evidence_ids:
        d = e.split("/")[0]
        if d in records and d not in docs:
            docs.append(d)
    return [format_credit(records[d]) for d in (docs or [rep_doc_id])]


# 생성 ------------------------------------------------------------------------

def _article_user(judge, ev, slot, records, cfg, feedback):
    lo, hi = cfg["core_chars"] if slot == "core" else cfg["brief_chars"]
    led = ev["ledger"]
    info = {"event_id": ev["event_id"], "slot": slot, "subject": ev["subject"], "site_task": ev["site_task"],
            "제목_글자수_한도": cfg["article_title_max"], "본문_글자수_범위": [lo, hi],
            "수치_why_글자수_한도": cfg.get("figure_why_chars", WHY_MAX)}
    info["검색어_최대_개수"] = cfg.get("search_terms_max", SEARCH_TERMS_MAX)
    if slot == "core":
        info["한줄핵심_글자수_한도"] = cfg.get("deck_chars", DECK_MAX)
        info["요점_한줄_글자수_한도"] = cfg.get("point_chars", POINT_MAX)
        info["watch_글자수_한도"] = cfg["watch_chars"]
        info["key_figures_최대"] = 3
        if image_candidates(ev, records, cfg):
            info["이미지_설명_글자수_한도"] = cfg["image_caption_chars"]
    if led["verdict"] == "SAME_WITH_CHANGE":
        info["후속"] = {"지난_게재_호": led.get("issue"), "새로_확인된_변화": led.get("changes")}
    parts = ["[작성 지침]\n" + json.dumps(info, ensure_ascii=False)]
    sc = ev.get("score")
    parts.append("[채점 결과]\n" + (json.dumps({k: sc[k] for k in ("claim", "maturity", "fact", "mtc_impact", "action_watch",
                                                                  "unresolved", "evidence")}, ensure_ascii=False)
                                  if sc else "(자동 채점 없음)"))
    parts += [judge.source(records[d]) for d in ev["doc_ids"]]
    cands = image_candidates(ev, records, cfg) if slot == "core" else []
    if cands:        # 이미지 후보 목록(파일 내용은 주지 않는다): 원문 캡션·대체 텍스트와 크기만
        parts.append("[이미지 후보]\n" + json.dumps([{"media_ref": c["media_ref"], "kind": c["kind"], "caption": c["caption"],
                                                      "alt": c["alt"], "size": f"{c['width']}x{c['height']}"} for c in cands], ensure_ascii=False))
    if feedback:
        parts.append(judge.phrase("x_regen") + "\n" + feedback)
    return "\n".join(parts)


def gen_article(judge, ev, slot, records, cfg, feedback=None):
    """기사 하나를 쓴다. 반환: (article dict, 자동 작성 실패 여부)."""
    docs = set(ev["doc_ids"])

    def validate(obj):
        errs = []
        if obj["event_id"] != ev["event_id"]:
            errs.append(f"event_id가 입력과 다름: {obj['event_id']}")
        errs += evidence_errors(obj["evidence_ids"], records, docs)
        if not obj["evidence_ids"]:
            errs.append("근거 ID가 없음")
        if slot == "core":
            errs += key_figure_errors(obj["key_figures"], obj["evidence_ids"], records)
            img = obj.get("image")
            if img:
                if find_candidate(image_candidates(ev, records, cfg), img["media_ref"]) is None:
                    errs.append(f"이미지 후보에 없는 media_ref: {img['media_ref']}")
                if len(img["caption"].strip()) > cfg["image_caption_chars"]:
                    errs.append(f"이미지 설명이 {len(img['caption'].strip())}자로 한도 {cfg['image_caption_chars']}자를 넘음")
        elif obj.get("figure"):
            errs += key_figure_errors([obj["figure"]], obj["evidence_ids"], records)
        return errs
    schema = CORE_ARTICLE_SCHEMA if slot == "core" else ARTICLE_SCHEMA
    try:
        r = judge.ask("s6_article", _article_user(judge, ev, slot, records, cfg, feedback), schema, validate)
        failed = False
    except AutoJudgeFailed:
        sc = ev.get("score")
        ids = [e["id"] for e in sc["evidence"]] if sc else []
        r = {"title": trunc(ev["title_draft"], cfg["article_title_max"]), "title_alternatives": [],
             "body": sc["claim"] if sc else ev["title_draft"], "evidence_ids": ids,
             "watch": (sc["action_watch"] if sc else "") or "후속 자료 확인 필요", "key_figures": []}
        failed = True
    art = {"event_id": ev["event_id"], "slot": slot, "title": r["title"], "title_alternatives": list(r["title_alternatives"]),
           "tags": make_tags(ev, slot), "body": r["body"].strip()}
    if slot == "core":
        art["deck"] = (r.get("deck") or "").strip() or fallback_deck(art["body"], cfg)
        pts = r.get("points")
        art["points"] = {k: pts[k].strip() for k, _ in POINT_LABELS if (pts.get(k) or "").strip()} if pts else None
        art["watch"] = r["watch"].strip()
        art["key_figures"] = [{"value": k["value"].strip(), "label": k["label"].strip(), **({"why": k["why"].strip()} if (k.get("why") or "").strip() else {})}
                              for k in r["key_figures"]]
        img = r.get("image")
        art["image"] = {"media_ref": img["media_ref"], "caption": img["caption"].strip()} if img else None
    elif r.get("figure"):
        art["figure"] = {"value": r["figure"]["value"].strip(), "label": r["figure"]["label"].strip(),
                         **({"why": r["figure"]["why"].strip()} if (r["figure"].get("why") or "").strip() else {})}
    terms = clean_search_terms(r.get("search_terms"), cfg.get("search_terms_max", SEARCH_TERMS_MAX))
    if terms:
        art["search_terms"] = terms
    art["credits"] = build_credits(r["evidence_ids"], ev["rep_doc_id"], records)
    art["evidence_ids"] = list(r["evidence_ids"])
    return art, failed


def clean_search_terms(terms, limit):
    """검색어: 공백 정리, 빈 값·중복(대소문자 무시) 제거, 최대 limit개."""
    out, seen = [], set()
    for t in terms or []:
        t = " ".join(str(t).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:limit]


def fallback_deck(body, cfg=None):
    """한 줄 핵심이 없을 때(이전 버전 응답, 자동 작성 실패): 본문 첫 문장을 한도에 맞춰 자른다."""
    first = re.split(r"(?<=[.!?。])\s+|(?<=다\.)", body.strip(), maxsplit=1)[0].strip()
    return trunc(first, (cfg or {}).get("deck_chars", DECK_MAX))


def key_figure_errors(figs, evidence_ids, records):
    """수치 카드의 값은 인용한 근거 블록에 있는 수치나 말이어야 하고(figure_value_errors) label이 있어야 한다."""
    text = "\n".join(evidence_text(e, records) for e in evidence_ids)
    errs = []
    for k in figs:
        errs += [f"수치 카드 {e}" for e in figure_value_errors(k["value"], text)]
        if not k["label"].strip():
            errs.append(f"수치 카드 '{k['value']}'의 label이 비어 있음")
    return errs


# 더 볼 거리 --------------------------------------------------------------------

PAPER_CUE = re.compile(r"arxiv|\bdoi\b|doi\.org|published\s+(?:in|at|on|as)\b|published\s+['\"‘’“”]|\bjournal\b|conference paper|proceedings|"
                       r"\bieee\b.{0,20}\btrans|논문|학술지|\bpreprint\b", re.I)


def paper_cues(text):
    """연구 논문 단서(arXiv, DOI, 'published …', 저널·학회 논문명)가 있으면 True(9.6)."""
    return bool(PAPER_CUE.search(text or ""))


def more_suffix(rec, text=""):
    """(논문)/(보도) 표시: 논문 단서가 있으면 (논문), 그 밖의 언론 자료는 (보도)."""
    if rec["source_type"] == "academic" or paper_cues(rec["title"] + " " + (text or "")):
        return " (논문)"
    return " (보도)" if rec["source_type"] == "press" else ""


def more_item_for(m, events_by_id):
    """draft.more 항목에서 다시 쓸 입력 항목(더 볼 거리 후보 형식)을 만든다. 사건이면 S4의 대표 주장·근거를 쓴다."""
    ev = events_by_id.get(m["ref"])
    if ev is None:      # 보류 자료: ref가 doc_id
        return {"ref": m["ref"], "topic": m["topic"], "doc_id": m["ref"]}
    sc = ev.get("score")
    return {"ref": m["ref"], "topic": m["topic"], "doc_id": ev["rep_doc_id"], "doc_ids": list(ev["doc_ids"]),
            "claim": sc["claim"] if sc else None, "evidence_ids": [x["id"] for x in sc["evidence"]] if sc else []}


def _more_docs(item):
    return set(item.get("doc_ids") or [item["doc_id"]])


def _more_user(judge, item, records, cfg, feedback):
    rec = records[item["doc_id"]]
    info = {"ref": item["ref"], "분야": TOPIC_LABEL.get(item["topic"], item["topic"]), "한줄_글자수_한도": cfg["more_line_chars"]}
    parts = ["[작성 지침]\n" + json.dumps(info, ensure_ascii=False)]
    if item.get("claim") and item.get("evidence_ids"):
        # 사건 항목: 대표 자료 첫 문단이 아니라 S4의 대표 주장과 근거 블록을 입력으로 쓴다(대표 자료가 사건과 무관할 수 있다)
        parts.append("[채점 결과]\n" + json.dumps({"claim": item["claim"]}, ensure_ascii=False))
        by_doc = {}
        for e in item["evidence_ids"]:
            d, b = e.split("/")
            by_doc.setdefault(d, []).append(b)
        parts += [judge.source(records[d], set(bs)) for d, bs in by_doc.items() if d in records]
    else:
        parts.append(judge.source(rec, set(rec["body"]["lead_ids"])))
    if feedback:
        parts.append(judge.phrase("x_regen") + "\n" + feedback)
    return "\n".join(parts)


def gen_more(judge, item, records, cfg, feedback=None):
    """더 볼 거리 한 줄. 반환: (항목 dict, 자동 작성 실패 여부)"""
    docs = _more_docs(item)

    def validate(obj):
        errs = []
        if obj["ref"] != item["ref"]:
            errs.append(f"ref가 입력과 다름: {obj['ref']}")
        errs += evidence_errors(obj["evidence_ids"], records, docs)
        if not obj["evidence_ids"]:
            errs.append("근거 ID가 없음")
        return errs
    try:
        r = judge.ask("s6_more", _more_user(judge, item, records, cfg, feedback), MORE_SCHEMA, validate)
        failed = False
    except AutoJudgeFailed:
        if item.get("evidence_ids"):
            ids = item["evidence_ids"][:1]
        else:
            lead = records[item["doc_id"]]["body"]["lead_ids"][:1]
            ids = [f"{item['doc_id']}/{lead[0]}"] if lead else []
        r = {"line": trunc(item.get("claim") or records[item["doc_id"]]["title"], cfg["more_line_chars"]), "evidence_ids": ids}
        failed = True
    first = (r["evidence_ids"] or [item["doc_id"] + "/"])[0].split("/")[0]
    rec = records.get(first) or records[item["doc_id"]]
    cue_text = " ".join(evidence_text(e, records) for e in r["evidence_ids"]) + " " + " ".join(
        b["text"] for b in rec["body"]["blocks"] if b["id"] in rec["body"]["lead_ids"])
    return {"ref": item["ref"], "topic": item["topic"], "line": r["line"].strip() + more_suffix(rec, cue_text),
            "credit": format_credit(rec) + (f" 외 {len(docs) - 1}건" if len(docs) > 1 else ""),   # 같은 사건으로 묶인 자료 수
            "evidence_ids": list(r["evidence_ids"])}, failed


# 머리부: 헤드라인·총론·한 방(기사와 더 볼 거리 원고를 모두 쓴 뒤 그 원고만 입력으로 쓴다) ------------------------

PUNCH_LABEL_NUM, PUNCH_LABEL_TEXT = "이번 주 가장 놀라운 숫자", "이번 주 한 방"


def normalize_punch(p):
    """label은 코드가 정한다: big에 숫자가 있으면 '이번 주 가장 놀라운 숫자', 아니면 '이번 주 한 방'."""
    p = {k: p[k].strip() for k in ("big", "text", "twist", "evidence_id")}
    p["label"] = PUNCH_LABEL_NUM if re.search(r"\d", p["big"]) else PUNCH_LABEL_TEXT
    return {k: p[k] for k in ("big", "label", "text", "twist", "evidence_id")}


def _header_user(judge, articles, more, issue_type, counts, cfg, feedback):
    info = {"issue_type": issue_type, "헤드라인_글자수_한도": cfg["issue_title_max"], "총론_글자수_범위": cfg["summary_chars"],
            "한방_글자수_한도(text+twist)": cfg["punch_chars"], "통계": counts}
    parts = ["[작성 지침]\n" + json.dumps(info, ensure_ascii=False)]
    for a in articles:
        parts.append("[기사] " + json.dumps({"slot": a["slot"], "title": a["title"], "deck": a.get("deck"), "body": a["body"], "watch": a.get("watch"),
                                           "key_figures": a.get("key_figures"), "evidence_ids": a["evidence_ids"]}, ensure_ascii=False))
    for m in more:
        parts.append("[더 볼 거리] " + json.dumps({"line": m["line"], "evidence_ids": m["evidence_ids"]}, ensure_ascii=False))
    if feedback:
        parts.append(judge.phrase("x_regen") + "\n" + feedback)
    return "\n".join(parts)


def gen_header(judge, articles, more, records, issue_type, counts, cfg, feedback=None):
    allowed = {e for a in articles for e in a["evidence_ids"]} | {e for m in more for e in m["evidence_ids"]}

    def validate(obj):
        errs = []
        for p in obj["punch_candidates"]:
            if p["evidence_id"] not in allowed or evidence_errors([p["evidence_id"]], records):
                errs.append(f"한 방 근거 ID가 원고의 근거에 없음: {p['evidence_id']}")
            else:
                errs += [f"한 방 big {e}" for e in figure_value_errors(p["big"], evidence_text(p["evidence_id"], records))]
        return errs
    try:
        h = judge.ask("s6_header", _header_user(judge, articles, more, issue_type, counts, cfg, feedback), HEADER_SCHEMA, validate)
        return h, False
    except AutoJudgeFailed:
        first = articles[0]["title"] if articles else "이번 주 소식"
        body = " ".join(a["body"] for a in articles)
        return {"issue_title_candidates": [trunc(first, cfg["issue_title_max"])] * 3,
                "summary_candidates": [trunc(body, cfg["summary_chars"][1])] * 2, "punch_candidates": []}, True


def apply_header(draft, h):
    draft["issue_title_candidates"] = list(h["issue_title_candidates"])
    draft["issue_title"] = h["issue_title_candidates"][0]
    draft["summary_candidates"] = [x.strip() for x in h["summary_candidates"]]
    draft["summary"] = draft["summary_candidates"][0]
    cands = [normalize_punch(p) for p in h["punch_candidates"]]
    draft["punch_candidates"] = cands
    draft["punch"] = cands[0] if cands else None


def published_events(events):
    """원고에 들어갈 사건: 핵심 → 단신 순, 각각 rank 순."""
    return sorted([e for e in events if e["slot"] in ("core", "brief")],
                  key=lambda e: (e["slot"] != "core", e["rank"] or 9999, e["event_id"]))


def status_draft(issue_week, counts, excluded_count=0):
    """현황호(8장): 게재 가능 사건이 없을 때 모델 호출 없이 만드는 머리부. counts: collected, pass_docs, events"""
    if counts["collected"] == 0:
        reason = "이번 주 사외 수집에서 완결된 자료가 없어"
    else:
        reason = f"수집 {counts['collected']}건 중 게재 기준을 충족한 사건이 없어"
    summary = (f"{reason} 이번 호는 현황호로 발행한다. 아래에 네 분야별 조사 결과와 게재 사건이 없는 사유를 정리했다. "
               "다음 주에는 새로 수집한 자료로 다시 판정한다.")
    return {"issue_week": issue_week, "issue_title": "이번 주 현황: 게재 사건 없음",
            "issue_title_candidates": ["이번 주 현황: 게재 사건 없음", "제조 AI 동향 점검: 게재 사건 없음", "이번 주는 현황호로 발행"],
            "summary": summary, "summary_candidates": [summary, "이번 주는 게재할 사건이 없어 조사 현황만 전한다."],
            "punch": None, "punch_candidates": [], "glossary": [], "articles": [], "more": [],
            "excluded_count": excluded_count, "review_log": []}


DRAFT_KEYS = ("issue_week", "issue_title", "issue_title_candidates", "summary", "summary_candidates", "punch", "punch_candidates",
              "glossary", "articles", "more", "excluded_count", "review_log")


def generate_draft(judge, events, records, issue_week, cfg, issue_type, counts, more_items=(), excluded_count=0):
    """반환: (draft, 자동 작성 실패한 event_id 목록)"""
    failed, articles = [], []
    if judge.batching:
        judge.prefetch([("s6_article", _article_user(judge, ev, ev["slot"], records, cfg, None),
                         CORE_ARTICLE_SCHEMA if ev["slot"] == "core" else ARTICLE_SCHEMA) for ev in published_events(events)]
                       + [("s6_more", _more_user(judge, it, records, cfg, None), MORE_SCHEMA) for it in more_items])
    for ev in published_events(events):
        art, bad = gen_article(judge, ev, ev["slot"], records, cfg)
        articles.append(art)
        if bad:
            failed.append(ev["event_id"])
    more = []
    for it in more_items:
        m, bad = gen_more(judge, it, records, cfg)
        more.append(m)
        if bad:
            failed.append(it["ref"])
    by_id = {e["event_id"]: e for e in events}
    h, bad = gen_header(judge, articles, more, records, issue_type, counts, cfg)
    if bad:
        failed.append("머리부")
    draft = {"issue_week": issue_week}
    apply_header(draft, h)
    draft["glossary"] = []
    draft["articles"] = articles
    draft["more"] = more
    draft["excluded_count"] = excluded_count
    draft["review_log"] = []
    ordered = {k: draft[k] for k in DRAFT_KEYS}
    return ordered, failed


# 대표 이미지 설명 다시 쓰기(검토 요청 ②에서 이미지를 바꿨을 때) ----------------------------------------

def gen_image_caption(judge, article, cand, cfg):
    """바꾼 이미지의 설명을 기사 내용과 후보의 원문 캡션·대체 텍스트를 보고 새로 쓴다. 실패하면 None."""
    user = ("[작성 지침]\n" + json.dumps({"설명_글자수_한도": cfg["image_caption_chars"]}, ensure_ascii=False)
            + "\n[기사] " + json.dumps({"title": article["title"], "body": article["body"]}, ensure_ascii=False)
            + "\n[이미지 후보] " + json.dumps({"media_ref": cand["media_ref"], "kind": cand["kind"], "caption": cand["caption"], "alt": cand["alt"]},
                                           ensure_ascii=False))

    def validate(obj):
        n = len(obj["caption"].strip())
        return [f"설명이 {n}자로 한도 {cfg['image_caption_chars']}자를 넘음"] if n > cfg["image_caption_chars"] else []
    try:
        return judge.ask("s6_image_caption", user, IMAGE_CAPTION_SCHEMA, validate)["caption"].strip()
    except AutoJudgeFailed:
        return None


# 용어 풀이 제안 -----------------------------------------------------------------------

def _glossary_user(judge, term, contexts, feedback=None):
    parts = ["[작성 지침]\n" + json.dumps({"term": term, "풀이_글자수_한도": GLOSS_MAX}, ensure_ascii=False)]
    parts += [judge.source(rec, set(ids)) for rec, ids in contexts]
    if feedback:
        parts.append(judge.phrase("x_regen") + "\n" + feedback)
    return "\n".join(parts)


def gen_glossary(judge, term, contexts, records):
    """모르는 약어의 풀이를 원문 근거로 제안한다. contexts: [(레코드, 블록 ID 목록)]. 반환: {term, meaning, source} 또는 None(실패)"""
    allowed = {rec["doc_id"] for rec, _ in contexts}

    def validate(obj):
        errs = []
        if obj["term"] != term:
            errs.append(f"term이 입력과 다름: {obj['term']}")
        if len(obj["meaning"].strip()) > GLOSS_MAX:
            errs.append(f"풀이가 {len(obj['meaning'].strip())}자로 한도 {GLOSS_MAX}자를 넘음")
        errs += evidence_errors(obj["evidence_ids"], records, allowed)
        return errs
    try:
        r = judge.ask("s6_glossary", _glossary_user(judge, term, contexts), GLOSSARY_SCHEMA, validate)
    except AutoJudgeFailed:
        return None
    return {"term": term, "meaning": r["meaning"].strip(), "source": "model"}


# 파일 -----------------------------------------------------------------------

def save_draft(path, draft):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
    tmp.replace(path)


def load_draft(path):
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    d["summary"] = d["summary"].strip()
    d.setdefault("more", [])
    d.setdefault("excluded_count", 0)
    d.setdefault("glossary", [])
    d.setdefault("punch", None)
    d.setdefault("punch_candidates", [])
    return d
