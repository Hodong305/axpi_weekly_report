"""S7 원고 자동 검사(설계서 9.7).

휴리스틱 설명(설계서가 방법을 정하지 않은 부분):
- 숫자: 원고의 숫자 토큰(콤마 제거)이 해당 기사가 인용한 근거 블록 텍스트에 있어야 한다. 1~10의 작은 정수는 근거에 영문 숫자 단어
  (one~ten)가 있으면 통과한다. 근거에 있어도 근거 쪽에 한정어(최대·최소·약·up to·at least·about 등)가 붙어 있는데 원고에 없으면 경고한다.
- 기업명: 원고에서 (a) 대문자로 시작하는 라틴 문자 연속어·전부 대문자 3자 이상 약어(일반 약어 목록 제외), (b) 회사형 접미사(전자·하이닉스·
  테크·로보틱스·시스템즈 등)로 끝나는 한글 어절을 기업명 후보로 뽑아 근거 자료(제목·발행처·본문) 어디에도 없으면 경고한다.
- 원문 연속 일치: 공백을 하나로 줄인 뒤 따옴표(“ ” " 「 」) 안을 뺀 원고 문장에서 verbatim_max_run자 창이 근거 자료 본문에 그대로 있으면 위반이다.
"""
import re
import unicodedata

from .common import evidence_errors, evidence_text
from .draft import format_credit
from .review import article_labels

_URL_CHARS = r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]"
_TLD = "com|org|net|io|kr|jp|cn|ai|co|edu|gov|info|biz|tv|de|uk|us|eu|tw|fr"
_URL_RES = [re.compile(rf"https?://{_URL_CHARS}*", re.I), re.compile(rf"\bwww\.{_URL_CHARS}*", re.I),
            re.compile(rf"\b(?:[A-Za-z0-9-]+\.)+(?:{_TLD})\b(?:/{_URL_CHARS}*)?", re.I)]


def strip_urls(text):
    """URL·http·도메인 문자열을 지운다. 반환: (새 텍스트, 지운 문자열 목록)."""
    removed = []
    for rx in _URL_RES:
        removed += rx.findall(text)
        text = rx.sub("", text)
    if re.search(r"https?", text, re.I):
        removed.append("http")
        text = re.sub(r"https?:?/*", "", text, flags=re.I)
    return re.sub(r"[ \t]{2,}", " ", text).strip(), removed


def norm(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip().casefold()


_QUOTE_RES = [re.compile(r"“[^”]*”"), re.compile(r"\"[^\"]*\""), re.compile(r"「[^」]*」"), re.compile(r"‘[^’]*’")]


def verbatim_hit(text, source_norm, run):
    """따옴표 인용을 뺀 text에서 source_norm과 run자 이상 연속 일치하는 구간(없으면 None)."""
    for rx in _QUOTE_RES:
        text = rx.sub("\x00", text)
    for seg in norm(text).split("\x00"):
        seg = seg.strip()
        for i in range(0, max(0, len(seg) - run + 1)):
            if seg[i:i + run] in source_norm:
                return seg[i:i + run]
    return None


# 숫자 ------------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
_EN_NUM = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
_QUAL = {  # 그룹: (숫자 앞에 붙는 말, 뒤에 붙는 말, 원고에 넣을 한글 한정어)
    "max": (["up to", "as much as", "최대", "최고"], ["이하"], "최대"),
    "min": (["at least", "최소"], ["이상"], "최소"),
    "approx": (["about", "approximately", "around", "nearly", "roughly", "약", "대략"], [], "약"),
    "over": (["over", "more than", "초과"], ["초과"], None),
    "under": (["less than", "under"], ["미만"], None),
}


def _numval(tok):
    return tok.replace(",", "")


def _num_re(tok):
    return re.compile(rf"(?<![\d.]){re.escape(_numval(tok))}(?![\d]|\.\d)")


def _num_in(tok, text):
    v = _numval(tok)
    if _num_re(tok).search(text.replace(",", "")):
        return True
    return v.isdigit() and 1 <= int(v) <= 10 and re.search(rf"\b{_EN_NUM[int(v)]}\b", text, re.I) is not None


def _word_before(w, before):
    return re.search(rf"(?<![A-Za-z가-힣]){re.escape(w)}\s*$", before) is not None


def _qual_in_source(tok, text):
    """근거 텍스트에서 숫자 바로 앞·뒤 한정어를 찾는다. 반환: (그룹, 근거 발췌) 또는 None."""
    flat = text.replace(",", "")
    v = _numval(tok)
    for m in _num_re(tok).finditer(flat):
        before = flat[max(0, m.start() - 14):m.start()].lower()
        after = flat[m.end():m.end() + 10]
        unit = after[:1] if after[:1] in ("%", "％") else ""
        for g, (bef, aft, _) in _QUAL.items():
            for w in bef:
                if _word_before(w, before):
                    return g, f"{w} {v}{unit}"
            for w in aft:
                if re.match(rf"\s*[%％]?\s*{w}", after):
                    return g, f"{v}{unit} {w}"
    return None


def _qual_in_draft(group, text, start, end):
    bef, aft, _ = _QUAL[group]
    before = text[max(0, start - 8):start].lower()
    after = text[end:end + 8]
    return any(_word_before(w, before) for w in bef) or any(w in after for w in aft)


def number_warnings(field_text, pairs, target, scope):
    """field_text의 숫자가 근거 블록(pairs=[(근거 ID, 텍스트)])에 있는지, 한정어가 빠지지 않았는지 검사한다."""
    out = []
    joined = "\n".join(t for _, t in pairs)
    first_id = pairs[0][0] if pairs else ""
    for m in _NUM.finditer(field_text):
        tok = m.group()
        e1 = re.compile(r"\S*").match(field_text, m.start()).end()      # 단위가 붙은 어절 끝
        nxt = re.compile(r"\s+\S+").match(field_text, e1)                # 다음 어절 하나까지가 수정안 범위
        old = field_text[m.start():(nxt.end() if nxt else e1)]
        if not _num_in(tok, joined):
            fix = None
            if field_text[m.end():m.end() + 1] in ("%", "％"):
                cands = {x for x in _NUM.findall(joined) if re.search(rf"(?<![\d.]){re.escape(x)}\s*[%％]", joined)}
                if len(cands) == 1 and field_text.count(old) == 1:
                    fix = {"target": target, "old": old, "new": old.replace(tok, next(iter(cands)), 1)}
            out.append({"scope": scope, "quote": old, "kind": "숫자",
                        "message": f"숫자 {tok}이(가) 인용한 근거 블록({first_id or '없음'} 등)에 없음", "fix": fix})
            continue
        q = _qual_in_source(tok, joined)
        if q and not _qual_in_draft(q[0], field_text, m.start(), m.end()):
            word = _QUAL[q[0]][2]
            fix = {"target": target, "old": old, "new": f"{word} {old}"} if word and field_text.count(old) == 1 else None
            eid = next((e for e, t in pairs if _num_re(tok).search(t.replace(",", ""))), first_id)
            out.append({"scope": scope, "quote": old, "kind": "숫자",
                        "message": f"근거 {eid}은(는) \"{q[1]}\" — 한정어 누락", "fix": fix})
    return out


# 기업명 ----------------------------------------------------------------------

_ACRO_STOP = {"AI", "MES", "EUV", "HBM", "DRAM", "NAND", "EDA", "KPI", "CAPEX", "FAB", "MTC", "AX", "PI", "ISO", "SEMI",
              "IOT", "ML", "LLM", "PDF", "ROI", "CEO", "CTO", "GPU", "CPU", "NPU", "API", "SPC", "APC", "FDC", "OEE",
              "WIP", "AMHS", "OHT", "AGV", "AMR", "HOLD", "PASS", "NEW", "SW", "HW", "AOI", "CMP", "CVD", "PVD", "OCR"}
_LATIN = re.compile(r"\b[A-Z][A-Za-z0-9&\-]+(?:\s+[A-Z][A-Za-z0-9&\-]+)*\b")
_KO_SUFFIX = "전자|하이닉스|테크놀로지|테크|로보틱스|시스템즈|머티리얼즈|머티어리얼즈|솔루션즈|디스플레이|엔지니어링|케미칼|하이텍|홀딩스"
_KO_CO = re.compile(rf"[가-힣A-Za-z0-9]{{1,12}}(?:{_KO_SUFFIX})")


def company_candidates(text):
    out = []
    for m in _LATIN.finditer(text):
        tok = m.group()
        words = tok.split()
        if len(words) == 1 and (tok.upper() in _ACRO_STOP or (tok.isupper() and len(tok) < 3)):
            continue
        if len(words) == 1 and not (tok.isupper() or re.search(r"[a-z]", tok[1:])):
            continue
        out.append(tok)
    out += [m.group() for m in _KO_CO.finditer(text)]
    return out


def company_warnings(text, source_norm, scope):
    out = []
    for c in dict.fromkeys(company_candidates(text)):
        n = norm(c)
        if n in source_norm or n.replace(" ", "") in source_norm.replace(" ", ""):
            continue
        out.append({"scope": scope, "quote": c, "kind": "기업명", "message": "근거 자료에 없는 기업명", "fix": None})
    return out


# 점검 본체 -------------------------------------------------------------------

def _source_text(event, records):
    parts = []
    for d in event["doc_ids"]:
        r = records[d]
        parts += [r["title"], r["publisher"]] + [b["text"] for b in r["body"]["blocks"]]
    return norm("\n".join(parts))


def autofix(draft, records):
    """URL 삭제와 출처 정보 형식 보정. 반환: 경고 목록."""
    warns = []

    def clean(text, scope, label):
        new, removed = strip_urls(text)
        if removed:
            warns.append({"scope": scope, "quote": removed[0][:40], "kind": "URL",
                          "message": f"{label}에서 URL·도메인 문자열을 자동 삭제함", "fix": None})
        return new
    labels = {i: lab for lab, i in article_labels(draft).items()}
    for k in ("issue_title", "one_line", "abstract"):
        draft[k] = clean(draft[k], "머리부", k)
    for k in ("issue_title_candidates", "one_line_candidates"):
        draft[k] = [clean(c, "머리부", k) for c in draft[k]]
    if draft.get("hook"):
        draft["hook"]["question"] = clean(draft["hook"]["question"], "머리부", "hook")
        draft["hook"]["answer"] = clean(draft["hook"]["answer"], "머리부", "hook")
    for i, a in enumerate(draft["articles"]):
        a["title"] = clean(a["title"], labels[i], "제목")
        a["body"] = clean(a["body"], labels[i], "본문")
        a["title_alternatives"] = [clean(t, labels[i], "제목 대안") for t in a["title_alternatives"]]
        # 출처 정보: 코드가 자료 메타데이터로 만든 형식으로 항상 되돌린다(자동 보정)
        docs = []
        for e in a["evidence_ids"]:
            d = e.split("/")[0]
            if d in records and d not in docs:
                docs.append(d)
        if docs:
            a["credits"] = [format_credit(records[d]) for d in docs]
    return warns


def length_violations(draft, cfg, issue_type):
    """글자 수 검사. 반환: [(대상, 메시지)] 대상은 ("article", i) 또는 ("header",)"""
    v = []
    amin, amax = cfg["abstract_chars"]
    if len(draft["issue_title"]) > cfg["issue_title_max"]:
        v.append((("header",), f"호 타이틀 {len(draft['issue_title'])}자 > 한도 {cfg['issue_title_max']}자"))
    if len(draft["one_line"]) > cfg["one_line_max"]:
        v.append((("header",), f"한 줄 요약 {len(draft['one_line'])}자 > 한도 {cfg['one_line_max']}자"))
    if issue_type != "현황호" and not amin <= len(draft["abstract"]) <= amax:
        v.append((("header",), f"주간 요약 {len(draft['abstract'])}자, 허용 {amin}~{amax}자"))
    h = draft.get("hook")
    if h and len(h["question"]) + len(h["answer"]) > cfg["hook_chars"]:
        v.append((("header",), f"팹 판단 한 컷 {len(h['question']) + len(h['answer'])}자 > 한도 {cfg['hook_chars']}자"))
    for i, a in enumerate(draft["articles"]):
        if len(a["title"]) > cfg["article_title_max"]:
            v.append((("article", i), f"제목 {len(a['title'])}자 > 한도 {cfg['article_title_max']}자"))
        lo, hi = cfg["core_chars"] if a["slot"] == "core" else cfg["brief_chars"]
        if not lo <= len(a["body"]) <= hi:
            v.append((("article", i), f"본문 {len(a['body'])}자, 허용 {lo}~{hi}자"))
    return v


def body_total(draft):
    h = draft.get("hook")
    return (len(h["question"]) + len(h["answer"]) if h else 0) + sum(len(a["body"]) for a in draft["articles"])


def hard_violations(draft, events_by_id, records, cfg, issue_type):
    """재생성 대상 검사: 글자 수, 근거 ID 실재, 원문 연속 일치. 반환: [(대상, 종류, 메시지)]"""
    v = [(t, "글자 수", m) for t, m in length_violations(draft, cfg, issue_type)]
    run = cfg["verbatim_max_run"]
    all_src = ""
    for i, a in enumerate(draft["articles"]):
        ev = events_by_id[a["event_id"]]
        errs = evidence_errors(a["evidence_ids"], records, set(ev["doc_ids"]))
        if errs or not a["evidence_ids"]:
            v.append((("article", i), "근거 ID", "; ".join(errs) or "근거 ID가 없음"))
        src = _source_text(ev, records)
        all_src += "\n" + src
        for fld in ("title", "body"):
            hit = verbatim_hit(a[fld], src, run)
            if hit:
                v.append((("article", i), "원문 연속 일치", f"{fld}에 원문과 {run}자 이상 같은 구간: \"{hit[:40]}…\""))
                break
    if draft["articles"]:
        text = draft["abstract"] + "\n" + (draft["hook"]["question"] + draft["hook"]["answer"] if draft.get("hook") else "")
        hit = verbatim_hit(text, all_src, run)
        if hit:
            v.append((("header",), "원문 연속 일치", f"머리부에 원문과 {run}자 이상 같은 구간: \"{hit[:40]}…\""))
    h = draft.get("hook")
    if h:
        errs = evidence_errors([h["evidence_id"]], records)
        if errs:
            v.append((("header",), "근거 ID", "; ".join(errs)))
    return v


def soft_warnings(draft, events_by_id, records, cfg, issue_type):
    """경고만 내는 검사: 숫자, 기업명, 게재 상한, 본문 합계."""
    warns = []
    labels = {i: lab for lab, i in article_labels(draft).items()}
    all_src, all_pairs = "", []
    for i, a in enumerate(draft["articles"]):
        ev = events_by_id[a["event_id"]]
        pairs = [(e, evidence_text(e, records)) for e in a["evidence_ids"]]
        all_pairs += pairs
        src = _source_text(ev, records)
        all_src += "\n" + src
        for fld in ("title", "body"):
            warns += number_warnings(a[fld], pairs, ["articles", i, fld], labels[i])
            warns += company_warnings(a[fld], src, labels[i])
        if a["slot"] == "core" and ev["cap"] == "brief" and not ev["cap_override"]:
            warns.append({"scope": labels[i], "quote": a["title"], "kind": "게재 상한",
                          "message": f"게재 상한 위반: 상한 '단신'({' · '.join(ev['cap_reasons'])})인데 핵심으로 편성됨", "fix": None})
    bg = [e for e in events_by_id.values() if e["slot"] in ("core", "brief") and e.get("score")
          and e["score"]["tiers"]["fab_relevance"] == 10]
    if len(bg) > 1:
        warns.append({"scope": "머리부", "quote": "", "kind": "게재 상한", "message": f"게재 상한 위반: 산업 배경 사건이 {len(bg)}건(최대 1건)",
                      "fix": None})
    for fld in ("issue_title", "one_line", "abstract"):
        warns += number_warnings(draft[fld], all_pairs, [fld], "머리부")
        warns += company_warnings(draft[fld], all_src, "머리부")
    if draft.get("hook"):
        for fld in ("question", "answer"):
            warns += company_warnings(draft["hook"][fld], all_src, "머리부")
    total = body_total(draft)
    if total > cfg["issue_body_max"]:
        warns.append({"scope": "머리부", "quote": "", "kind": "글자 수",
                      "message": f"본문 합계 {total:,}자 > 한도 {cfg['issue_body_max']:,}자", "fix": None})
    return warns


def number_ids(warns):
    for i, w in enumerate(warns, 1):
        w["id"] = f"E{i}"
    return warns
