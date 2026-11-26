"""S6 원고·머리부 초안(설계서 9.6) → review/{issue_week}-draft.yaml. 출처 정보(credits)는 코드가 자료 메타데이터로 만든다."""
import json

import yaml

from .common import evidence_errors, short_week, trunc
from .llm import AutoJudgeFailed

LANG_SUFFIX = {"ko": "", "en": "영문", "ja": "일문", "zh": "중문", "de": "독문", "fr": "불문", "es": "서문",
               "ru": "러문", "it": "이문", "nl": "네문", "pt": "포문"}

ARTICLE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["event_id", "title", "title_alternatives", "body", "evidence_ids"],
    "properties": {"event_id": {"type": "string"}, "title": {"type": "string", "minLength": 1},
                   "title_alternatives": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}},
                   "body": {"type": "string", "minLength": 1},
                   "evidence_ids": {"type": "array", "items": {"type": "string"}}}}

HEADER_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["issue_title_candidates", "one_line_candidates", "abstract", "hook"],
    "properties": {
        "issue_title_candidates": {"type": "array", "minItems": 3, "maxItems": 3, "items": {"type": "string"}},
        "one_line_candidates": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "string"}},
        "abstract": {"type": "string", "minLength": 1},
        "hook": {"anyOf": [{"type": "null"}, {
            "type": "object", "additionalProperties": False, "required": ["question", "answer", "evidence_id"],
            "properties": {"question": {"type": "string"}, "answer": {"type": "string"}, "evidence_id": {"type": "string"}}}]}}}

ABSTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["abstract"],
                   "properties": {"abstract": {"type": "string", "minLength": 1}}}


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
            "제목_글자수_한도": cfg["article_title_max"], "본문_글자수_범위": [lo, hi]}
    if led["verdict"] == "SAME_WITH_CHANGE":
        info["후속"] = {"지난_게재_호": led.get("issue"), "새로_확인된_변화": led.get("changes")}
    parts = ["[작성 지침]\n" + json.dumps(info, ensure_ascii=False)]
    sc = ev.get("score")
    parts.append("[채점 결과]\n" + (json.dumps({k: sc[k] for k in ("claim", "maturity", "fact", "mtc_impact", "action_watch",
                                                                  "unresolved", "evidence")}, ensure_ascii=False)
                                  if sc else "(자동 채점 없음)"))
    parts += [judge.source(records[d]) for d in ev["doc_ids"]]
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
        return errs
    try:
        r = judge.ask("s6_article", _article_user(judge, ev, slot, records, cfg, feedback), ARTICLE_SCHEMA, validate)
        failed = False
    except AutoJudgeFailed:
        sc = ev.get("score")
        ids = [e["id"] for e in sc["evidence"]] if sc else []
        r = {"title": trunc(ev["title_draft"], cfg["article_title_max"]), "title_alternatives": [],
             "body": sc["claim"] if sc else ev["title_draft"], "evidence_ids": ids}
        failed = True
    return {"event_id": ev["event_id"], "slot": slot, "title": r["title"],
            "title_alternatives": list(r["title_alternatives"]), "body": r["body"].strip(),
            "credits": build_credits(r["evidence_ids"], ev["rep_doc_id"], records),
            "evidence_ids": list(r["evidence_ids"])}, failed


def _header_user(judge, draft_articles, events_by_id, issue_type, counts, cfg, feedback):
    info = {"issue_type": issue_type, "호_타이틀_한도": cfg["issue_title_max"], "한_줄_요약_한도": cfg["one_line_max"],
            "주간_요약_글자수_범위": cfg["abstract_chars"], "팹_판단_한_컷_한도": cfg["hook_chars"], "통계": counts}
    parts = ["[작성 지침]\n" + json.dumps(info, ensure_ascii=False)]
    for a in draft_articles:
        parts.append("[기사] " + json.dumps({"slot": a["slot"], "title": a["title"], "body": a["body"],
                                           "evidence_ids": a["evidence_ids"]}, ensure_ascii=False))
    if feedback:
        parts.append(judge.phrase("x_regen") + "\n" + feedback)
    return "\n".join(parts)


def gen_header(judge, articles, events_by_id, records, issue_type, counts, cfg, feedback=None):
    allowed = {e for a in articles for e in a["evidence_ids"]}

    def validate(obj):
        h = obj["hook"]
        if h and (h["evidence_id"] not in allowed or evidence_errors([h["evidence_id"]], records)):
            return [f"hook 근거 ID가 기사 근거에 없음: {h['evidence_id']}"]
        return []
    try:
        return judge.ask("s6_header", _header_user(judge, articles, events_by_id, issue_type, counts, cfg, feedback),
                         HEADER_SCHEMA, validate), False
    except AutoJudgeFailed:
        first = articles[0]["title"] if articles else "이번 주 소식"
        return {"issue_title_candidates": [trunc(first, cfg["issue_title_max"])] * 3,
                "one_line_candidates": [trunc(first, cfg["one_line_max"])] * 2,
                "abstract": " ".join(a["body"] for a in articles)[: cfg["abstract_chars"][1]],
                "hook": None}, True


def apply_header(draft, h):
    draft["issue_title_candidates"] = list(h["issue_title_candidates"])
    draft["issue_title"] = h["issue_title_candidates"][0]
    draft["one_line_candidates"] = list(h["one_line_candidates"])
    draft["one_line"] = h["one_line_candidates"][0]
    draft["abstract"] = h["abstract"].strip()
    draft["hook"] = ({"question": h["hook"]["question"], "answer": h["hook"]["answer"],
                      "evidence_id": h["hook"]["evidence_id"]} if h["hook"] else None)


def published_events(events):
    """원고에 들어갈 사건: 핵심 → 단신 순, 각각 rank 순."""
    return sorted([e for e in events if e["slot"] in ("core", "brief")],
                  key=lambda e: (e["slot"] != "core", e["rank"] or 9999, e["event_id"]))


def status_draft(issue_week, counts):
    """현황호(8장): 게재 가능 사건이 없을 때 모델 호출 없이 만드는 머리부. counts: collected, pass_docs, events"""
    if counts["collected"] == 0:
        reason = "이번 주 사외 수집에서 완결된 자료가 없어"
    else:
        reason = f"수집 {counts['collected']}건 중 게재 기준을 충족한 사건이 없어"
    return {"issue_week": issue_week, "issue_title": "이번 주 현황: 게재 사건 없음",
            "issue_title_candidates": ["이번 주 현황: 게재 사건 없음", "제조 AI 동향 점검: 게재 사건 없음", "이번 주는 현황호로 발행"],
            "one_line": "게재 기준을 충족한 사건이 없어 네 분야 조사 결과만 알립니다.",
            "one_line_candidates": ["게재 기준을 충족한 사건이 없어 네 분야 조사 결과만 알립니다.",
                                    "이번 주는 게재할 사건이 없어 조사 현황만 전합니다."],
            "abstract": f"{reason} 이번 호는 현황호로 발행한다. 아래에 네 분야별 조사 결과와 게재 사건이 없는 사유를 정리했다. "
                        "다음 주에는 새로 수집한 자료로 다시 판정한다.",
            "hook": None, "articles": [], "review_log": []}


def generate_draft(judge, events, records, issue_week, cfg, issue_type, counts):
    """반환: (draft, 자동 작성 실패한 event_id 목록)"""
    failed, articles = [], []
    for ev in published_events(events):
        art, bad = gen_article(judge, ev, ev["slot"], records, cfg)
        articles.append(art)
        if bad:
            failed.append(ev["event_id"])
    by_id = {e["event_id"]: e for e in events}
    h, bad = gen_header(judge, articles, by_id, records, issue_type, counts, cfg)
    if bad:
        failed.append("머리부")
    draft = {"issue_week": issue_week}
    apply_header(draft, h)
    draft["articles"] = articles
    draft["review_log"] = []
    ordered = {k: draft[k] for k in ("issue_week", "issue_title", "issue_title_candidates", "one_line",
                                     "one_line_candidates", "abstract", "hook", "articles", "review_log")}
    return ordered, failed


# 파일 -----------------------------------------------------------------------

class _Block(str):
    pass


def _block_repr(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data) + "\n", style="|")


yaml.add_representer(_Block, _block_repr, Dumper=yaml.SafeDumper)


def save_draft(path, draft):
    path.parent.mkdir(parents=True, exist_ok=True)
    d = dict(draft)
    d["abstract"] = _Block(draft["abstract"].strip())
    tmp = path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
    tmp.replace(path)


def load_draft(path):
    d = yaml.safe_load(path.read_text(encoding="utf-8"))
    d["abstract"] = d["abstract"].strip()
    return d
