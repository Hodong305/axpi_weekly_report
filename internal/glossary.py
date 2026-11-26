"""약어·용어 풀이(설계서 9.8 '약어 풀이'): glossary.yaml 읽기, 이번 호에 쓴 용어 모으기, 첫 등장에 풀이 붙이기, 모르는 약어 찾기, 새 풀이 추가.
glossary.yaml은 사람이 손으로 고치는 파일이라 주석·순서를 보존하도록 새 용어는 파일 끝(terms 아래)에 줄로 덧붙인다."""
import re
from datetime import date

import yaml

GLOSS_MAX = 25      # 모델이 제안하는 풀이의 글자 수 한도(9.8)
EDIT_MAX = 40       # 편집자가 직접 쓰는 풀이의 한도


def load(path):
    """반환: (terms {용어: 풀이}, common 집합)"""
    d = yaml.safe_load(open(path, encoding="utf-8").read()) or {}
    return {str(k): str(v) for k, v in (d.get("terms") or {}).items()}, set(d.get("common") or [])


def _pattern(term):
    if re.match(r"[A-Za-z0-9]", term[0]):
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])")
    return re.compile(re.escape(term))


def gloss_fields(draft):
    """풀이를 붙이는 자리(제목·헤드라인·총론·한 줄 핵심에는 붙이지 않는다), 문서 순서: 핵심 요점·본문·MTC가 볼 것 → 단신 본문 → 더 볼 거리.
    반환: [(경로, 텍스트)]. 요점은 ("articles", i, "point_what") 등."""
    out = []
    for i, a in enumerate(draft.get("articles") or []):
        for k, v in (a.get("points") or {}).items():
            if v:
                out.append((("articles", i, "point_" + k), v))
        out.append((("articles", i, "body"), a["body"]))
        if a.get("watch"):
            out.append((("articles", i, "watch"), a["watch"]))
    for j, m in enumerate(draft.get("more") or []):
        out.append((("more", j, "line"), m["line"]))
    return out


def annotate(fields, meanings):
    """첫 등장에만 풀이를 붙인다. fields: [(키, 텍스트)], meanings: {용어: 풀이}.
    반환: ({키: [(조각, 풀이 또는 None), ...]}, 쓴 용어 목록(첫 등장 순서)). 풀이 조각은 직전 조각 바로 뒤에 붙는다."""
    pats = {t: _pattern(t) for t in meanings}
    seen, used, out = set(), [], {}
    for key, text in fields:
        hits = []
        for t, p in pats.items():
            if t in seen:
                continue
            m = p.search(text)
            if m:
                hits.append((m.start(), -len(t), m.end(), t))
        hits.sort()
        segs, pos, last_end = [], 0, 0
        for start, _, end, t in hits:
            if start < last_end:            # 앞 용어와 겹치면 건너뜀(긴 용어가 먼저 잡힌다)
                continue
            seen.add(t)
            used.append(t)
            segs.append((text[pos:end], None))
            nxt = text[end:end + 1]
            segs.append(("", meanings[t] if nxt != "(" else None))      # 이미 괄호 설명이 붙어 있으면 풀이를 달지 않는다
            pos, last_end = end, end
        segs.append((text[pos:], None))
        out[key] = segs
    return out, used


def known_meanings(terms):
    return dict(terms)


def build_glossary(draft, terms, previous=()):
    """이번 호에 쓴 용어 목록을 draft['glossary']로 만든다(첫 등장 순서). 풀이는 glossary.yaml → 이전에 확정·제안한 값 순서로 찾는다.
    풀이를 찾지 못한 용어는 넣지 않는다(모르는 약어는 find_unknown이 따로 찾는다)."""
    prev = {g["term"]: g for g in previous}
    meanings = dict(terms)
    for g in prev.values():
        if g["source"] != "glossary" and g.get("meaning"):
            meanings[g["term"]] = g["meaning"]
    _, used = annotate(gloss_fields(draft), meanings)
    out = []
    for t in used:
        src = prev[t]["source"] if t in prev and prev[t]["source"] != "glossary" else "glossary" if t in terms else prev[t]["source"]
        out.append({"term": t, "meaning": meanings[t], "source": src})
    return out


_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9&\-]*[A-Za-z0-9](?![A-Za-z0-9])")


def find_unknown(draft, terms, common, ignore_words=()):
    """풀이가 없는 약어를 찾는다: 대문자 2자 이상(영문 대소문자 섞임 포함). 단어 전체가 대문자이거나 6자 이하여야 하며(긴 고유명사 제외),
    공통어, glossary 용어(와 그 구성 단어), 발행처 이름 단어는 제외한다. 반환: 첫 등장 순서의 용어 목록"""
    words = {w for t in terms for w in re.findall(r"[A-Za-z0-9]+", t)}
    skip = set(common) | set(terms) | words | {w.casefold() for w in ignore_words}
    out = []
    for _, text in gloss_fields(draft):
        for m in _TOKEN.finditer(text):
            tok = m.group()
            caps = sum(c.isupper() for c in tok)
            if caps < 2 or tok in skip or tok.casefold() in skip or tok in out:
                continue
            if not (tok.isupper() or len(tok) <= 6):
                continue
            out.append(tok)
    return out


def append_terms(path, new_terms, issue_week, today=None):
    """glossary.yaml의 terms 끝에 새 용어를 덧붙인다(주석·순서 보존). 이미 있는 용어는 건너뜀. 반환: 추가한 개수"""
    text = open(path, encoding="utf-8").read()
    existing, _ = load(path)
    add = [(t, m) for t, m in new_terms if t not in existing and m]
    if not add:
        return 0
    lines = text.rstrip("\n").split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"terms\s*:", l)), None)
    if start is None:
        lines += ["", "terms:"]
        end = len(lines)
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].strip() and not lines[i].startswith((" ", "\t", "#")):
                end = i
                break
        while end > start + 1 and not lines[end - 1].strip():    # 다음 키 앞의 빈 줄은 뒤에 둔다
            end -= 1
    stamp = (today or date.today()).isoformat()
    block = [f"  # {stamp} {issue_week} 검토 요청 ②에서 확정"]
    for t, m in add:
        key = t if re.fullmatch(r"[A-Za-z0-9가-힣][A-Za-z0-9가-힣 _\-/&.]*", t) and not re.fullmatch(r"(?i)y|n|yes|no|true|false|null|on|off|\d+", t) else yaml.safe_dump(t).strip().split("\n")[0]
        val = m if not re.search(r"[:#]|^[\-\[\]{}&*!|>'\"%@`]", m) else '"' + m.replace("\\", "\\\\").replace('"', '\\"') + '"'
        block.append(f"  {key}: {val}")
    lines[end:end] = block
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return len(add)


def term_contexts(term, draft, events_by_id, records, limit=3):
    """모르는 약어가 나온 원고 칸이 인용한 근거 자료에서 그 약어가 있는 블록을 찾는다. 반환: [(레코드, [블록 ID])]
    인용한 자료에 없으면 입력 전체에서 찾는다."""
    pat = _pattern(term)
    docs = []
    for key, text in gloss_fields(draft):
        if pat.search(text):
            kind, idx, _ = key
            ids = (draft["articles"][idx] if kind == "articles" else draft["more"][idx])["evidence_ids"]
            docs += [e.split("/")[0] for e in ids]
            break
    out = []
    for pool in (list(dict.fromkeys(docs)), sorted(records)):
        for d in pool:
            rec = records.get(d)
            hit = [b["id"] for b in (rec["body"]["blocks"] if rec else []) if pat.search(b["text"])]
            if hit:
                out.append((rec, hit[:2]))
            if len(out) >= limit:
                return out
        if out:
            break
    return out
