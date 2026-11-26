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

from .common import evidence_errors, evidence_text, figure_value_errors
from .draft import find_candidate, format_credit, image_candidates
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

from .common import _EN_NUM, _NUM, _num_in, _num_re, _numval  # noqa: E402,F401  (숫자 검사 도구는 common에 있다)

# 한정어 그룹: 근거(영문·한글)에서 찾는 말(앞·뒤)과, 원고에서 같은 뜻으로 인정하는 말(앞·뒤), 수정안에 넣을 한글 한정어.
# 원고의 한정어는 숫자 앞뒤 어느 쪽에 있어도 된다(한국어는 "90,000건 넘게"처럼 뒤에 붙는 경우가 많다).
_OVER_OK = (["more than", "over", "at least", "최소", "초과"], ["넘게", "넘는", "넘어", "이상", "초과", "+"])
_QUAL = {  # 그룹: (근거 앞, 근거 뒤, 원고 앞, 원고 뒤, 수정안 한정어)
    "max": (["up to", "as much as", "최대", "최고"], ["이하", "까지"],
            ["up to", "as much as", "최대", "최고"], ["까지", "이하"], "최대"),
    "min": (["at least", "최소"], ["이상"], *_OVER_OK, "최소"),
    "over": (["over", "more than", "greater than", "초과"], ["초과"], *_OVER_OK, None),
    "approx": (["about", "approximately", "around", "nearly", "roughly", "약", "대략"], ["가량", "정도", "안팎"],
               ["about", "approximately", "around", "nearly", "roughly", "약", "대략", "거의"], ["가량", "정도", "안팎", "거의", "가까이"], "약"),
    "under": (["less than", "under"], ["미만"], ["less than", "under", "미만"], ["미만", "이하"], None),
}
_AFTER_YEO = re.compile(r"\s*[%％]?\s*여(?![가-힣])")     # "30여 건"의 '여'(한 글자라 따로 검사)


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
        for g, (bef, aft, *_rest) in _QUAL.items():
            for w in bef:
                if _word_before(w, before):
                    return g, f"{w} {v}{unit}"
            for w in aft:
                if re.match(rf"\s*[%％]?\s*{w}", after):
                    return g, f"{v}{unit} {w}"
    return None


def _qual_in_draft(group, text, start, end):
    """원고의 숫자 앞 8자·뒤 10자에 같은 뜻의 한정어가 있는지 본다."""
    _, _, bef, aft, _ = _QUAL[group]
    before = text[max(0, start - 8):start].lower()
    after = text[end:end + 10]
    if any(_word_before(w, before) for w in bef) or any(w in after for w in aft):
        return True
    return group == "approx" and bool(_AFTER_YEO.match(after))


def _fix_text(group, old, figure):
    """한정어를 넣은 수정안. 수치 카드는 '90,000+'처럼 짧게 고치고, 문장은 한정어를 앞뒤에 넣는다. 정할 수 없으면 None."""
    if figure:
        return {"max": "최대 " + old, "min": old + "+", "over": old + "+", "approx": "약 " + old, "under": old + " 미만"}.get(group)
    if group in ("max", "min", "approx"):
        return f"{_QUAL[group][4]} {old}"
    if group == "over":
        first, _, rest = old.partition(" ")
        return f"{first} 넘게" + (f" {rest}" if rest else "")
    return None


def number_warnings(field_text, pairs, target, scope, figure=False):
    """field_text의 숫자가 근거 블록(pairs=[(근거 ID, 텍스트)])에 있는지, 한정어가 빠지지 않았는지 검사한다."""
    out = []
    joined = "\n".join(t for _, t in pairs)
    first_id = pairs[0][0] if pairs else ""
    for m in _NUM.finditer(field_text):
        tok = m.group()
        e1 = re.compile(r"\S*").match(field_text, m.start()).end()      # 단위가 붙은 어절 끝
        nxt = re.compile(r"\s+\S+").match(field_text, e1)                # 다음 어절 하나까지가 수정안 범위
        old = field_text[m.start():(e1 if figure else (nxt.end() if nxt else e1))]
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
            new = _fix_text(q[0], old, figure)
            fix = {"target": target, "old": old, "new": new} if new and field_text.count(old) == 1 else None
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


SUFFIXES = (" (논문)", " (보도)")


def strip_suffix(line):
    for x in SUFFIXES:
        if line.endswith(x):
            return line[:-len(x)]
    return line


def more_docs(item, events_by_id):
    """더 볼 거리 항목이 가리키는 자료 doc_id 집합(ref가 doc_id이면 그 자료, 사건이면 사건의 자료)."""
    if item["ref"].startswith("url1_"):
        return {item["ref"]}
    ev = events_by_id.get(item["ref"])
    return set(ev["doc_ids"]) if ev else set()


def more_source(item, events_by_id, records):
    ev = events_by_id.get(item["ref"])
    return _source_text(ev, records) if ev else norm("\n".join(
        [records[item["ref"]]["title"]] + [b["text"] for b in records[item["ref"]]["body"]["blocks"]]) if item["ref"] in records else "")


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
    for k in ("issue_title", "summary"):
        draft[k] = clean(draft[k], "머리부", k)
    for k in ("issue_title_candidates", "summary_candidates"):
        draft[k] = [clean(c, "머리부", k) for c in draft.get(k) or []]
    for p in [draft.get("punch")] + list(draft.get("punch_candidates") or []):
        if p:
            for k in ("big", "text", "twist"):
                p[k] = clean(p[k], "머리부", f"한 방 {k}")
    for i, a in enumerate(draft["articles"]):
        a["title"] = clean(a["title"], labels[i], "제목")
        a["body"] = clean(a["body"], labels[i], "본문")
        if a.get("watch"):
            a["watch"] = clean(a["watch"], labels[i], "MTC가 볼 것")
        if a.get("deck"):
            a["deck"] = clean(a["deck"], labels[i], "한 줄 핵심")
        for pk in list(a.get("points") or {}):
            a["points"][pk] = clean(a["points"][pk], labels[i], "요점")
        for k in list(a.get("key_figures") or []) + ([a["figure"]] if a.get("figure") else []):
            k["value"], k["label"] = clean(k["value"], labels[i], "수치 카드"), clean(k["label"], labels[i], "수치 카드")
        a["title_alternatives"] = [clean(t, labels[i], "제목 대안") for t in a["title_alternatives"]]
        if a.get("image"):
            a["image"]["caption"] = clean(a["image"]["caption"], labels[i], "이미지 설명")
        # 출처 정보: 코드가 자료 메타데이터로 만든 형식으로 항상 되돌린다(자동 보정)
        docs = []
        for e in a["evidence_ids"]:
            d = e.split("/")[0]
            if d in records and d not in docs:
                docs.append(d)
        if docs:
            a["credits"] = [format_credit(records[d]) for d in docs]
    for j, m in enumerate(draft.get("more") or [], 1):
        m["line"] = clean(m["line"], f"F{j}", "더 볼 거리")
    return warns


def length_violations(draft, cfg, issue_type):
    """글자 수 검사. 반환: [(대상, 메시지)] 대상은 ("article", i), ("more", j) 또는 ("header",)"""
    v = []
    smin, smax = cfg["summary_chars"]
    if len(draft["issue_title"]) > cfg["issue_title_max"]:
        v.append((("header",), f"헤드라인 {len(draft['issue_title'])}자 > 한도 {cfg['issue_title_max']}자"))
    if issue_type != "현황호" and not smin <= len(draft["summary"]) <= smax:
        v.append((("header",), f"총론 {len(draft['summary'])}자, 허용 {smin}~{smax}자"))
    p = draft.get("punch")
    if p and len(p["text"]) + len(p["twist"]) > cfg["punch_chars"]:
        v.append((("header",), f"이번 주 한 방 {len(p['text']) + len(p['twist'])}자 > 한도 {cfg['punch_chars']}자(big 제외)"))
    for i, a in enumerate(draft["articles"]):
        if len(a["title"]) > cfg["article_title_max"]:
            v.append((("article", i), f"제목 {len(a['title'])}자 > 한도 {cfg['article_title_max']}자"))
        lo, hi = cfg["core_chars"] if a["slot"] == "core" else cfg["brief_chars"]
        if not lo <= len(a["body"]) <= hi:
            v.append((("article", i), f"본문 {len(a['body'])}자, 허용 {lo}~{hi}자"))
        if a.get("image") and len(a["image"]["caption"]) > cfg["image_caption_chars"]:
            v.append((("article", i), f"이미지 설명 {len(a['image']['caption'])}자 > 한도 {cfg['image_caption_chars']}자"))
        if a["slot"] == "core" and len(a.get("watch") or "") > cfg["watch_chars"]:
            v.append((("article", i), f"MTC가 볼 것 {len(a['watch'])}자 > 한도 {cfg['watch_chars']}자"))
        dmax, pmax = cfg.get("deck_chars", 40), cfg.get("point_chars", 36)
        if a["slot"] == "core" and len(a.get("deck") or "") > dmax:
            v.append((("article", i), f"한 줄 핵심 {len(a['deck'])}자 > 한도 {dmax}자"))
        for pk, pt in (a.get("points") or {}).items():
            if len(pt) > pmax:
                v.append((("article", i), f"요점({pk}) {len(pt)}자 > 한도 {pmax}자"))
    for j, m in enumerate(draft.get("more") or []):
        n = len(strip_suffix(m["line"]))
        if n > cfg["more_line_chars"]:
            v.append((("more", j), f"더 볼 거리 한 줄 {n}자 > 한도 {cfg['more_line_chars']}자"))
    return v


def body_total(draft):
    """9.8의 4~6: 이번 주 한 방(text+twist) + 핵심 한 줄 핵심·요점·본문·MTC가 볼 것 + 단신 본문(더 볼 거리·꼬리말은 넣지 않는다)"""
    p = draft.get("punch")
    return ((len(p["text"]) + len(p["twist"]) if p else 0)
            + sum(len(a["body"]) + len(a.get("watch") or "") + len(a.get("deck") or "") + sum(len(x) for x in (a.get("points") or {}).values())
                  for a in draft["articles"]))


def article_fields(a):
    """검사 대상 글 칸: [(경로 꼬리, 텍스트)]. 요점은 ("points", 키)."""
    out = [((f,), a[f]) for f in ("title", "deck", "body", "watch") if a.get(f)]
    out += [(("points", k), v) for k, v in (a.get("points") or {}).items() if v]
    return out


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
        img = a.get("image")
        if img:      # 후보 실재·정책 대상 여부, 설명이 원문 캡션을 옮기지 않았는지(활용 원칙 3)
            cand = find_candidate(image_candidates(ev, records, cfg), img["media_ref"])
            if cand is None:
                v.append((("article", i), "이미지", f"후보에 없거나 이미지 정책 대상이 아닌 media_ref: {img['media_ref']}"))
            else:
                hit = verbatim_hit(img["caption"], norm(" ".join(x for x in (cand["caption"], cand["alt"]) if x)), run)
                if hit:
                    v.append((("article", i), "원문 연속 일치", f"이미지 설명이 원문 캡션과 {run}자 이상 같음: \"{hit[:40]}…\""))
        for path, text in article_fields(a):
            hit = verbatim_hit(text, src, run)
            if hit:
                v.append((("article", i), "원문 연속 일치", f"{'.'.join(path)}에 원문과 {run}자 이상 같은 구간: \"{hit[:40]}…\""))
                break
    for j, m in enumerate(draft.get("more") or []):
        errs = evidence_errors(m["evidence_ids"], records, more_docs(m, events_by_id))
        if errs or not m["evidence_ids"]:
            v.append((("more", j), "근거 ID", "; ".join(errs) or "근거 ID가 없음"))
        hit = verbatim_hit(strip_suffix(m["line"]), more_source(m, events_by_id, records), run)
        if hit:
            v.append((("more", j), "원문 연속 일치", f"한 줄 요약이 원문과 {run}자 이상 같음: \"{hit[:40]}…\""))
    if draft["articles"]:
        p = draft.get("punch")
        text = draft["summary"] + "\n" + (p["text"] + " " + p["twist"] if p else "")
        hit = verbatim_hit(text, all_src, run)
        if hit:
            v.append((("header",), "원문 연속 일치", f"머리부에 원문과 {run}자 이상 같은 구간: \"{hit[:40]}…\""))
    p = draft.get("punch")
    if p:
        errs = evidence_errors([p["evidence_id"]], records)
        if not errs:
            errs = [f"한 방 big {e}" for e in figure_value_errors(p["big"], evidence_text(p["evidence_id"], records))]
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
        for path, text in article_fields(a):
            warns += number_warnings(text, pairs, ["articles", i, *path], labels[i])
            warns += company_warnings(text, src, labels[i])
        for k_i, k in enumerate(a.get("key_figures") or []):
            warns += number_warnings(k["value"], pairs, ["articles", i, "key_figures", k_i, "value"], labels[i], figure=True)
            warns += company_warnings(k["label"], src, labels[i])
        if a.get("figure"):
            warns += number_warnings(a["figure"]["value"], pairs, ["articles", i, "figure", "value"], labels[i], figure=True)
        if a["slot"] == "core" and ev["cap"] == "brief" and not ev["cap_override"]:
            warns.append({"scope": labels[i], "quote": a["title"], "kind": "게재 상한",
                          "message": f"게재 상한 위반: 상한 '단신'({' · '.join(ev['cap_reasons'])})인데 핵심으로 편성됨", "fix": None})
    bg = [e for e in events_by_id.values() if e["slot"] in ("core", "brief") and e.get("score")
          and e["score"]["tiers"]["fab_relevance"] == 10]
    if len(bg) > 1:
        warns.append({"scope": "머리부", "quote": "", "kind": "게재 상한", "message": f"게재 상한 위반: 산업 배경 사건이 {len(bg)}건(최대 1건)",
                      "fix": None})
    for j, m in enumerate(draft.get("more") or []):       # 총론·한 방은 더 볼 거리 근거도 인용할 수 있다
        all_pairs += [(e, evidence_text(e, records)) for e in m["evidence_ids"]]
        all_src += "\n" + more_source(m, events_by_id, records)
    for fld in ("issue_title", "summary"):
        warns += number_warnings(draft[fld], all_pairs, [fld], "머리부")
        warns += company_warnings(draft[fld], all_src, "머리부")
    p = draft.get("punch")
    if p:
        for fld in ("text", "twist"):
            warns += number_warnings(p[fld], all_pairs, ["punch", fld], "머리부")
            warns += company_warnings(p[fld], all_src, "머리부")
    for j, m in enumerate(draft.get("more") or []):
        pairs = [(e, evidence_text(e, records)) for e in m["evidence_ids"]]
        warns += number_warnings(m["line"], pairs, ["more", j, "line"], f"F{j + 1}")
        warns += company_warnings(m["line"], more_source(m, events_by_id, records), f"F{j + 1}")
    total = body_total(draft)
    if total > cfg["issue_body_max"]:
        warns.append({"scope": "머리부", "quote": "", "kind": "글자 수",
                      "message": f"본문 합계 {total:,}자 > 한도 {cfg['issue_body_max']:,}자", "fix": None})
    return warns


def number_ids(warns):
    for i, w in enumerate(warns, 1):
        w["id"] = f"E{i}"
    return warns
