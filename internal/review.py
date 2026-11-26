"""검토 요청 프로토콜(설계서 9.2): 출력 형식, 응답 문법 해석, 적용.
렌더러는 문자열을 돌려주고 파서는 ReviewError(한 줄 한국어 메시지)를 낸다. 입출력은 pipeline이 맡는다."""
import copy
import re
import unicodedata

from .common import short_week
from .events import ledger_display

BAR = "━━"
SLOT_KO = {"core": "핵심", "brief": "단신", "hold": "보류", "out": "제외"}
KO_SLOT = {"핵심": "core", "단신": "brief", "보류": "hold", "빼기": "out"}


class ReviewError(Exception):
    """해석·검증 실패. 메시지는 한 줄 한국어다."""


def width(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s, w, right=False):
    gap = " " * max(0, w - width(s))
    return gap + s if right else s + gap


def _fmt_tail(s, n):
    s = " ".join((s or "").split())
    out, w = "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in "WF" else 1
        if w + cw > n:
            return out.rstrip() + "…"
        out, w = out + c, w + cw
    return out


# ① 선정 ---------------------------------------------------------------------

def display_lists(events):
    """검토 요청 ①의 번호 체계: 편성표(1..), 대기(H1..), 제외 표본(X1..)."""
    rows = sorted([e for e in events if e["slot"] in ("core", "brief")], key=lambda e: (e["rank"] or 9999, e["event_id"]))
    holds = sorted([e for e in events if e["slot"] == "hold"], key=lambda e: e["event_id"])
    outs = sorted([e for e in events if e["slot"] == "out"], key=lambda e: e["event_id"])
    return rows, holds, outs


def render_table(events):
    rows, _, _ = display_lists(events)
    head = " ".join([pad("#", 3, True), pad("지면", 4), pad("점수", 4, True), pad("분야", 4), pad("가제목", 36), pad("대장", 10), "표시"])
    lines = [head]
    for i, e in enumerate(rows, 1):
        total = e["score"]["total"] if e.get("score") and e["score"]["total"] is not None else "-"
        lines.append(" ".join([pad(str(i), 3, True), pad(SLOT_KO[e["slot"]], 4), pad(str(total), 4, True),
                               pad((e["primary_topic"] or "")[:2], 4), pad(_fmt_tail(e["title_draft"], 36), 36),
                               pad(ledger_display(e), 10), "  ".join(e["flags"])]).rstrip())
    return "\n".join(lines)


def _waiting_line(events):
    _, holds, outs = display_lists(events)
    parts = [f"H{i} 보류({e['note'] or '판단 보류'}) {_fmt_tail(e['title_draft'], 24)}" for i, e in enumerate(holds, 1)]
    parts += [f"X{i} 제외({e['note'] or '제외'}) {_fmt_tail(e['title_draft'], 24)}" for i, e in enumerate(outs, 1)]
    return "  ".join(parts) if parts else "(없음)"


def render_review1(issue_week, counts, events, notice=None):
    """counts: pass_docs, events, candidates, core_eligible"""
    return "\n".join([
        f"{BAR} 검토 요청 ① 선정 · {issue_week} {BAR}",
        *([notice] if notice else []),
        f"통과 문서 {counts['pass_docs']}건 → 사건 {counts['events']}건 → 게재 후보 {counts['candidates']}건"
        f"(핵심 자격 {counts['core_eligible']}건)",
        "",
        "[자동 편성안]",
        render_table(events),
        "[대기] " + _waiting_line(events),
        "",
        "[선택]",
        " 1) 자동 편성안 그대로 승인 (권장)",
        " 2) 바꿔서 승인 — 예: 2) 3=핵심, 4=빼기, 1↔2, H1=단신",
        " 3) 중복 의심 판정 — 예: 3) 3=NEW   또는   3) 3=중복",
        " (2)와 3)은 함께 써도 된다 — 예: 2) 4=빼기 / 3) 3=NEW)",
        "응답:"])


_REF = r"(?:\d+|[HhXx]\d+)"
_SET_RE = re.compile(rf"^({_REF})\s*(?:=|->|→)\s*(핵심|단신|보류|빼기)$")
_SWAP_RE = re.compile(rf"^({_REF})\s*(?:↔|<->|<=>|->|→)\s*({_REF})$")
_LEDGER_RE = re.compile(r"^(\d+)\s*=\s*(NEW|중복)$", re.I)


def parse_review1(text):
    """반환: {"approve": bool, "ops": [("set", ref, 값) | ("swap", a, b) | ("ledger", 번호, "NEW"|"중복")]}"""
    t = (text or "").strip()
    if not t:
        raise ReviewError("응답이 비어 있습니다. 1, 2) …, 3) … 중 하나로 답하세요.")
    if re.fullmatch(r"1\s*\)?", t):
        return {"approve": True, "ops": []}
    ops = []
    for seg in re.split(r"\s*/\s*", t):
        m = re.match(r"^([23])\s*\)\s*(.*)$", seg.strip())
        if not m:
            raise ReviewError(f"해석할 수 없는 응답입니다: '{seg.strip()[:30]}' — 1, 2) …, 3) … 형식으로 답하세요.")
        kind, body = m.group(1), m.group(2).strip()
        items = [i.strip() for i in re.split(r"[,，、]", body) if i.strip()]
        if not items:
            raise ReviewError(f"{kind}) 뒤에 내용이 없습니다.")
        for it in items:
            if kind == "3":
                lm = _LEDGER_RE.match(it)
                if not lm:
                    raise ReviewError(f"해석할 수 없는 항목 '{it}' — 3) 3=NEW 또는 3) 3=중복 형식으로 쓰세요.")
                ops.append(("ledger", lm.group(1), lm.group(2).upper() if lm.group(2).upper() == "NEW" else "중복"))
                continue
            sm = _SET_RE.match(it)
            if sm:
                ref, val = sm.group(1).upper(), sm.group(2)
                if ref[0] in "HX" and val not in ("핵심", "단신"):
                    raise ReviewError(f"'{it}': H·X 항목은 핵심 또는 단신으로만 올릴 수 있습니다.")
                ops.append(("set", ref, val))
                continue
            wm = _SWAP_RE.match(it)
            if wm:
                a, b = wm.group(1).upper(), wm.group(2).upper()
                if not (a.isdigit() and b.isdigit()):
                    raise ReviewError(f"'{it}': 순서 교환은 편성표 번호끼리만 할 수 있습니다.")
                ops.append(("swap", a, b))
                continue
            raise ReviewError(f"해석할 수 없는 항목 '{it}' — 예: 3=핵심, 4=빼기, 1↔2, H1=단신")
    return {"approve": False, "ops": ops}


def render_cap_check(label, reason):
    return "\n".join([f"{BAR} 상한 확인 {BAR} {label}은(는) 게재 상한이 '단신'입니다({reason}).",
                      " 1) 단신으로 유지 (권장)   2) 그래도 핵심으로", "응답:"])


def parse_choice(text, n=2):
    t = (text or "").strip()
    if re.fullmatch(rf"[1-{n}]\s*\)?", t):
        return int(t[0])
    raise ReviewError(f"해석할 수 없는 응답입니다: '{t[:30]}' — 1 또는 {n}로 답하세요.")


def apply_review1(events, ops, cap_answers=None):
    """응답을 편성에 적용한다. 반환: (새 events, 상한 확인이 필요한 항목 목록, 통계).
    번호(1.., H1.., X1..)는 요청을 출력한 시점의 표시 기준이며, 적용 도중 바뀌지 않는다."""
    cap_answers = cap_answers or {}
    evs = copy.deepcopy(events)
    rows, holds, outs = display_lists(evs)
    refs = {str(i): e for i, e in enumerate(rows, 1)}
    refs.update({f"H{i}": e for i, e in enumerate(holds, 1)})
    refs.update({f"X{i}": e for i, e in enumerate(outs, 1)})
    label = {e["event_id"]: (f"{r}번" if r.isdigit() else r) for r, e in refs.items()}
    start = {e["event_id"]: (e["slot"], e["rank"]) for e in evs}
    order = [e["event_id"] for e in rows]
    changed, ledger_changes = set(), 0

    def get(ref):
        if ref not in refs:
            raise ReviewError(f"{ref}번 항목이 없습니다. 표에 있는 번호만 쓰세요.")
        return refs[ref]

    for op in ops:
        if op[0] == "set":
            e = get(op[1])
            new = KO_SLOT[op[2]]
            e["slot"] = new
            if new in ("core", "brief") and e["event_id"] not in order:
                order.append(e["event_id"])
            if new not in ("core", "brief") and e["event_id"] in order:
                order.remove(e["event_id"])
        elif op[0] == "swap":
            a, b = get(op[1]), get(op[2])
            if a["event_id"] not in order or b["event_id"] not in order:
                raise ReviewError(f"{op[1]}↔{op[2]}: 편성표에 없는 항목은 교환할 수 없습니다.")
            i, j = order.index(a["event_id"]), order.index(b["event_id"])
            order[i], order[j] = order[j], order[i]
        else:  # ledger
            e = get(op[1])
            if not op[1].isdigit():
                raise ReviewError(f"{op[1]}: 중복 판정은 편성표 번호에만 쓸 수 있습니다.")
            led = e["ledger"]
            if op[2] == "NEW":
                if led["verdict"] != "NEW":
                    led.update(ref_key=led.get("key"), verdict="NEW", key=None, issue=None, uncertain=False)
                    ledger_changes += 1
                    changed.add(e["event_id"])
                e["flags"] = [f for f in e["flags"] if f != "중복 의심"]
            else:
                if led["verdict"] != "SAME_NO_CHANGE":
                    led["verdict"] = "SAME_NO_CHANGE"
                    ledger_changes += 1
                e.update(slot="out", status="EXCLUDE", note="NO_WEEKLY_CHANGE(편집자 판정)")
                e["flags"] = [f for f in e["flags"] if f != "중복 의심"]
                if e["event_id"] in order:
                    order.remove(e["event_id"])
                changed.add(e["event_id"])
        if op[0] == "set":
            changed.add(e["event_id"])
        elif op[0] == "swap":
            changed.update((a["event_id"], b["event_id"]))

    # 게재 상한 확인(핵심으로 올렸는데 상한이 단신인 사건)
    violations = []
    for e in evs:
        if e["slot"] == "core" and e["cap"] == "brief" and not e["cap_override"]:
            ans = cap_answers.get(e["event_id"])
            if ans == "override":
                e["cap_override"] = True
            elif ans == "keep":
                e["slot"] = "brief"
            else:
                violations.append({"event_id": e["event_id"], "label": label.get(e["event_id"], e["event_id"]),
                                   "reason": " · ".join(e["cap_reasons"]) or "상한"})
    for r, e in enumerate([x for x in (next(y for y in evs if y["event_id"] == i) for i in order)
                           if x["slot"] in ("core", "brief")], 1):
        e["rank"] = r
    for e in evs:
        if e["slot"] not in ("core", "brief"):
            e["rank"] = None
    for e in evs:
        s0, r0 = start[e["event_id"]]
        if e["slot"] != s0 or e["rank"] != r0:
            changed.add(e["event_id"])
    in_before = {i for i, (s, _) in start.items() if s in ("core", "brief")}
    in_after = {e["event_id"] for e in evs if e["slot"] in ("core", "brief")}
    stats = {"changed": len(changed), "published": len(in_after),
             "lifted": len(in_after - in_before), "lowered": len(in_before - in_after),
             "ledger_changes": ledger_changes}
    return evs, violations, stats


# ② 머리부·제목 ---------------------------------------------------------------

def article_labels(draft):
    """기사 라벨: 핵심1.., 단신1.. → articles 인덱스"""
    out, n = {}, {"core": 0, "brief": 0}
    for i, a in enumerate(draft["articles"]):
        n[a["slot"]] += 1
        out[f"{SLOT_KO[a['slot']]}{n[a['slot']]}"] = i
    return out


def over_note(text, limit):
    """글자 수 한도를 넘는 후보에 붙이는 짧은 사유(조용히 빼지 않고 이유와 함께 보인다)."""
    return f" [한도 초과 {len(text)}자>{limit}]" if limit and len(text) > limit else ""


def _cands(prefix, cands, limit=None):
    return " / ".join(f"{prefix}{i} {c}{over_note(c, limit)}" + (" (권장)" if i == 1 else "") for i, c in enumerate(cands, 1))


def new_terms(draft):
    """검토 요청 ②의 [G] 대상: glossary.yaml에 아직 없는 용어(모델 제안·편집자 입력)."""
    return [g for g in draft.get("glossary") or [] if g["source"] != "glossary"]


def image_items(draft, img):
    """[I] 대상: 후보가 있거나 이미지가 있는 핵심 기사. 반환: [(번호, articles 인덱스, 핵심 라벨, 후보 목록)]. img = {articles 인덱스: 후보 목록}"""
    out, k = [], 0
    for i, a in enumerate(draft["articles"]):
        if a["slot"] != "core":
            continue
        k += 1
        cands = (img or {}).get(i) or []
        if cands or a.get("image"):
            out.append((len(out) + 1, i, f"핵심{k}", cands))
    return out


def _image_entry(n, lab, art, cands):
    cur = art.get("image")
    label = next((c["label"] for c in cands if cur and c["media_ref"] == cur["media_ref"]), None)
    head = f"I{n} {lab} · " + (f"{label or '이미지'} {cur['caption']}" if cur else "없음")
    return head + (f" (후보 M1~M{len(cands)})" if cands else "")


def _punch_line(i, p):
    return f"C{i} {p['big']} · {p['text']} / {p['twist']}" + (" (권장)" if i == 1 else "")


def render_review2(issue_week, draft, warnings, cfg=None, img=None):
    cfg = cfg or {}
    from .draft import POINT_LABELS, TOPIC_LABEL
    lines = [f"{BAR} 검토 요청 ② 머리부·제목 · {issue_week} {BAR}",
             "[A 이번 주 헤드라인]  " + _cands("A", draft["issue_title_candidates"], cfg.get("issue_title_max")),
             "[B 한 줄 요약(총론)]  " + _cands("B", draft["summary_candidates"], (cfg.get("summary_chars") or [0, 0])[1]),
             "[C 이번 주 한 방]     " + (" / ".join(_punch_line(i, p) for i, p in enumerate(draft.get("punch_candidates") or [], 1))
                                      or "(없음)")]
    lines.append("[D 기사 제목]   (현재 제목 | 대안 a | 대안 b)")
    for lab, i in article_labels(draft).items():
        a = draft["articles"][i]
        mx = cfg.get("article_title_max")
        lines.append(f"  {lab}  " + " | ".join(t + over_note(t, mx) for t in [a["title"]] + list(a["title_alternatives"][:2])))
        if a["slot"] == "core" and a.get("deck"):       # v4.7 요약층: 바꿀 때는 E(경고 수정안) 또는 draft.yaml 직접 수정
            lines.append(f"      ▶ {a['deck']}" + over_note(a["deck"], cfg.get("deck_chars")))
            for k, lab2 in POINT_LABELS:
                if (a.get("points") or {}).get(k):
                    lines.append(f"      · {lab2}: {a['points'][k]}" + over_note(a["points"][k], cfg.get("point_chars")))
    lines.append("[F 더 볼 거리]   (분야별 한 줄 요약 · 빼려면 F3=빼기)")
    if not draft.get("more"):
        lines.append("  (없음)")
    for j, m in enumerate(draft.get("more") or [], 1):
        lines.append(f"  F{j} {TOPIC_LABEL.get(m['topic'], m['topic'] or '기타')} · {m['line']}")
    items = image_items(draft, img)
    lines.append("[I 대표 이미지]   " + (" / ".join(_image_entry(n, lab, draft["articles"][i], c) for n, i, lab, c in items) or "(없음)"))
    lines.append("[G 새 용어 풀이]")
    news = new_terms(draft)
    if not news:
        lines.append("  (없음)")
    for n, g in enumerate(news, 1):
        mean = g["meaning"] or "(제안 실패 — G%d:\"풀이\"로 직접 입력)" % n
        lines.append(f"  G{n} {g['term']} = {mean} ({'모델 제안' if g['source'] == 'model' else '편집자 입력'})")
    lines.append("[E 자동 검사 경고]")
    if not warnings:
        lines.append("  (없음)")
    for w in warnings:
        s = f"  {w['id']} {w['scope']}" + (f" \"{w['quote']}\"" if w.get("quote") else "") + f" — {w['message']}"
        if w.get("fix"):
            s += f" → 수정안: \"{w['fix']['new']}\""
        lines.append(s)
    lines += ["",
              "[선택] 예: A2 B1 C1 D 핵심1=a E1=수정 G2:\"설비 이상 감지·분류 시스템\"",
              " - A: A1~A3, 또는 A:\"직접 쓴 헤드라인\"",
              " - B: B1~B2, B:\"직접 쓴 총론\", 또는 B:수정 \"수정 지시\"",
              " - C: C1~C2, 또는 C:없음",
              " - G: 고칠 풀이만. 예: G2:\"직접 쓴 풀이\". 생략하면 모델 제안 승인",
              " - D: 바꿀 기사만. 생략하면 현재 제목 유지",
              " - E: 경고마다 수정 또는 유지. 생략하면 수정안 적용",
              " - F: 뺄 항목만. 예: F3=빼기, F7=빼기. 생략하면 모두 싣는다",
              " - I: 바꿀 이미지만. 예: I1=빼기, I2=M3. 생략하면 모델 선택 유지",
              " - 전부 권장안이면: 승인",
              "응답:"]
    return "\n".join(lines)


_Q = r"[\"“”]([^\"“”]*)[\"“”]"
_TOK = [
    ("B_REV", re.compile(r"B\s*[:：]\s*수정\s*" + _Q)),
    ("A_TXT", re.compile(r"A\s*[:：]\s*" + _Q)),
    ("B_TXT", re.compile(r"B\s*[:：]\s*" + _Q)),
    ("C_NONE", re.compile(r"C\s*[:：]\s*없음")),
    ("G", re.compile(r"G(\d+)\s*[:：]\s*" + _Q)),
    ("A", re.compile(r"A([1-9])(?!\w)")),
    ("B", re.compile(r"B([1-9])(?!\w)")),
    ("C", re.compile(r"C([1-9])(?!\w)")),
    ("E", re.compile(r"E(\d+)\s*=\s*(수정|유지)")),
    ("F", re.compile(r"F(\d+)\s*=\s*빼기")),
    ("I_NONE", re.compile(r"I(\d+)\s*=\s*빼기")),
    ("I_SWAP", re.compile(r"I(\d+)\s*=\s*M(\d+)")),
    ("D", re.compile(r"D(?=[\s,]|$)")),
    ("OK", re.compile(r"승인")),
]
_D_ITEM = re.compile(r"(핵심|단신)\s*(\d+)\s*(?:=\s*([abAB])(?![\w])|[:=]\s*" + _Q + ")")


def parse_review2(text):
    """반환: {"A","B","C","D":{라벨:..},"E":{id:..},"F":[번호],"G":{번호:풀이}}.
    A·B는 확정돼야 한다. '승인'은 A·B 권장안(C는 현재 한 방, D는 현재 제목, G는 모델 제안)을 그대로 확정한다.
    B: ("pick", n) | ("text", 문장) | ("revise", 지시), C: ("pick", n) | ("none",) | None(현재 유지)"""
    s = (text or "").strip()
    if not s:
        raise ReviewError("응답이 비어 있습니다. 예: A2 B1 C1, 또는 승인")
    res = {"A": None, "B": None, "C": None, "D": {}, "E": {}, "F": [], "G": {}, "I": {}}
    approve_all = False
    pos, in_d = 0, False
    while pos < len(s):
        m = re.compile(r"[\s,;/，]+").match(s, pos)
        if m:
            pos = m.end()
            continue
        if in_d:
            dm = _D_ITEM.match(s, pos)
            if dm:
                lab = dm.group(1) + dm.group(2)
                res["D"][lab] = ("pick", dm.group(3).lower()) if dm.group(3) else ("text", dm.group(4))
                pos = dm.end()
                continue
            in_d = False
        for name, rx in _TOK:
            tm = rx.match(s, pos)
            if tm:
                break
        else:
            raise ReviewError(f"해석할 수 없는 응답입니다: '{s[pos:pos + 20]}' — 예: A2 B1 C1 D 핵심1=a E1=수정 G2:\"풀이\"")
        pos = tm.end()
        if name == "B_REV":
            res["B"] = ("revise", tm.group(1))
        elif name == "A_TXT":
            res["A"] = ("text", tm.group(1))
        elif name == "B_TXT":
            res["B"] = ("text", tm.group(1))
        elif name == "C_NONE":
            res["C"] = ("none",)
        elif name == "I_NONE":
            res["I"][int(tm.group(1))] = ("none",)
        elif name == "I_SWAP":
            res["I"][int(tm.group(1))] = ("swap", int(tm.group(2)))
        elif name == "G":
            res["G"][int(tm.group(1))] = tm.group(2)
        elif name == "A":
            res["A"] = ("pick", int(tm.group(1)))
        elif name == "B":
            res["B"] = ("pick", int(tm.group(1)))
        elif name == "C":
            res["C"] = ("pick", int(tm.group(1)))
        elif name == "E":
            res["E"][f"E{int(tm.group(1))}"] = tm.group(2)
        elif name == "F":
            n = int(tm.group(1))
            if n not in res["F"]:
                res["F"].append(n)
        elif name == "D":
            in_d = True
        else:
            approve_all = True
    if approve_all:
        res["A"] = res["A"] or ("pick", 1)
        res["B"] = res["B"] or ("pick", 1)
    missing = [k for k in "AB" if res[k] is None]
    if missing:
        raise ReviewError(f"{'·'.join(missing)}를 확정하지 않았습니다. A·B는 반드시 정해야 합니다(전부 권장안이면 '승인').")
    return res


def validate_review2(res, draft, warnings, cfg, img=None):
    """응답이 현재 요청과 맞는지(번호 범위, 글자 수) 검사한다."""
    from .glossary import EDIT_MAX
    smax = cfg["summary_chars"][1]
    for key, cands, mx, name in (("A", draft["issue_title_candidates"], cfg["issue_title_max"], "헤드라인"),
                                 ("B", draft["summary_candidates"], smax, "총론")):
        kind, val = res[key][0], res[key][1] if len(res[key]) > 1 else None
        if kind == "pick" and not 1 <= val <= len(cands):
            raise ReviewError(f"{key}{val}은(는) 없습니다. {key}1~{key}{len(cands)} 중에서 고르세요.")
        if kind == "pick" and len(cands[val - 1]) > mx:
            raise ReviewError(f"{key}{val}은(는) {len(cands[val - 1])}자로 {name} 한도 {mx}자를 넘습니다. 다른 후보나 직접 입력을 쓰세요.")
        if kind == "text" and not 1 <= len(val.strip()) <= mx:
            raise ReviewError(f"{key} 직접 입력({name})은 {mx}자 이내로 쓰세요(현재 {len(val.strip())}자).")
        if kind == "revise" and not val.strip():
            raise ReviewError("B:수정 뒤에 수정 지시를 쓰세요. 예: B:수정 \"결함 얘기를 앞에 두세요\"")
    if res["C"] and res["C"][0] == "pick":
        n = res["C"][1]
        cands = draft.get("punch_candidates") or []
        if not cands:
            raise ReviewError(f"C{n}: 이번 주 한 방 후보가 없습니다. C:없음으로 답하거나 C는 생략하세요.")
        if not 1 <= n <= len(cands):
            raise ReviewError(f"C{n}은(는) 없습니다. C1~C{len(cands)} 중에서 고르세요.")
    labels = article_labels(draft)
    for lab, (kind, val) in res["D"].items():
        if lab not in labels:
            raise ReviewError(f"D {lab}: 해당 기사가 없습니다.")
        a = draft["articles"][labels[lab]]
        if kind == "pick" and ("ab".index(val) >= len(a["title_alternatives"])):
            raise ReviewError(f"D {lab}={val}: 해당 대안이 없습니다.")
        if kind == "pick" and len(a["title_alternatives"]["ab".index(val)]) > cfg["article_title_max"]:
            raise ReviewError(f"D {lab}={val}: 대안이 {len(a['title_alternatives']['ab'.index(val)])}자로 한도 {cfg['article_title_max']}자를 넘습니다.")
        if kind == "text" and not 1 <= len(val.strip()) <= cfg["article_title_max"]:
            raise ReviewError(f"D {lab} 직접 입력은 {cfg['article_title_max']}자 이내로 쓰세요(현재 {len(val.strip())}자).")
    items = {n: c for n, _, _, c in image_items(draft, img)}
    for n, act in res["I"].items():
        if n not in items:
            raise ReviewError(f"I{n}: 해당 대표 이미지 항목이 없습니다.")
        if act[0] == "swap" and not 1 <= act[1] <= len(items[n]):
            raise ReviewError(f"I{n}=M{act[1]}: 후보가 없습니다. M1~M{len(items[n])} 중에서 고르세요." if items[n] else f"I{n}=M{act[1]}: 이 기사에는 이미지 후보가 없습니다.")
    for n in res["F"]:
        if not 1 <= n <= len(draft.get("more") or []):
            raise ReviewError(f"F{n}: 해당 더 볼 거리 항목이 없습니다.")
    for n, text in res["G"].items():
        if not 1 <= n <= len(new_terms(draft)):
            raise ReviewError(f"G{n}: 해당 새 용어가 없습니다.")
        if not 1 <= len(text.strip()) <= EDIT_MAX:
            raise ReviewError(f"G{n} 풀이는 {EDIT_MAX}자 이내로 쓰세요(현재 {len(text.strip())}자).")
    ids = {w["id"] for w in warnings}
    for e in res["E"]:
        if e not in ids:
            raise ReviewError(f"{e}: 해당 경고가 없습니다.")


def apply_fix(draft, fix):
    """경고의 수정안을 원고에 적용한다. 대상 문구가 이미 바뀌었으면 건너뛴다. 반환: 적용 여부."""
    node = draft
    for k in fix["target"][:-1]:
        node = node[k]
    last = fix["target"][-1]
    if fix["old"] in node[last]:
        node[last] = node[last].replace(fix["old"], fix["new"], 1)
        return True
    return False


def apply_review2(draft, res, warnings, img=None):
    """A·B·C·D·E·F·G를 원고에 반영한다(B의 수정 지시는 st['b_revise']로 돌려주고 호출 쪽이 모델로 처리). 반환: 통계 dict."""
    st = {"A_custom": False, "B_custom": False, "b_revise": None, "d_changed": 0, "d_total": len(draft["articles"]),
          "e_fix": 0, "e_keep": 0, "e_total": len(warnings), "d_ids": [], "f_removed": 0, "g_edited": 0, "C_none": False,
          "i_changed": 0, "image_swaps": []}
    for key, field, cands in (("A", "issue_title", "issue_title_candidates"), ("B", "summary", "summary_candidates")):
        kind, val = res[key]
        if kind == "revise":              # 수정 지시: 지금 총론(1순위)을 기준으로 다시 쓴다
            st["b_revise"] = val.strip()
            continue
        chosen = val.strip() if kind == "text" else draft[cands][val - 1]
        st[f"{key}_custom"] = kind == "text"
        draft[cands] = [chosen] + [c for c in draft[cands] if c != chosen]
        draft[field] = chosen
    if res["C"]:
        if res["C"][0] == "none":
            draft["punch"], st["C_none"] = None, True
        else:
            cands = draft["punch_candidates"]
            draft["punch"] = cands[res["C"][1] - 1]
            draft["punch_candidates"] = [draft["punch"]] + [c for c in cands if c is not draft["punch"]]
    labels = article_labels(draft)
    for lab, (kind, val) in res["D"].items():
        a = draft["articles"][labels[lab]]
        new = val.strip() if kind == "text" else a["title_alternatives"]["ab".index(val)]
        if new != a["title"]:
            a["title_alternatives"] = ([a["title"]] + [t for t in a["title_alternatives"] if t != new])[:2]
            a["title"] = new
            st["d_changed"] += 1
            st["d_ids"].append(a["event_id"])
    if res["F"]:        # F: 뺄 항목(요청 시점의 번호 기준)
        gone = set(res["F"])
        draft["more"] = [m for j, m in enumerate(draft.get("more") or [], 1) if j not in gone]
        st["f_removed"] = len(gone)
    for n, i, _, cands in image_items(draft, img):     # I: 이미지를 빼거나 다른 후보로 바꾼다(바꾼 이미지의 설명은 호출 쪽이 새로 쓴다)
        act = res["I"].get(n)
        if act is None:
            continue
        a = draft["articles"][i]
        if act[0] == "none":
            if a.get("image"):
                a["image"] = None
                st["i_changed"] += 1
        else:
            cand = cands[act[1] - 1]
            if not a.get("image") or a["image"]["media_ref"] != cand["media_ref"]:
                a["image"] = {"media_ref": cand["media_ref"], "caption": ""}
                st["image_swaps"].append((i, cand))
                st["i_changed"] += 1
    news = new_terms(draft)
    for n, text in res["G"].items():        # G: 고칠 풀이만. 나머지 모델 제안은 그대로 확정
        news[n - 1]["meaning"], news[n - 1]["source"] = text.strip(), "editor"
        st["g_edited"] += 1
    for w in warnings:
        choice = res["E"].get(w["id"], "수정")    # 생략하면 수정안 적용
        w["decision"] = choice
        if choice == "수정":
            if w.get("fix") and apply_fix(draft, w["fix"]):
                st["e_fix"] += 1
            w["resolved"] = bool(w.get("fix"))
        else:
            st["e_keep"] += 1
            w["resolved"] = False
    return st


def render_b_confirm(summary):
    return "\n".join([f"{BAR} B 재수정 확인 {BAR}"] + ["  " + ln for ln in summary.strip().splitlines()]
                     + [" 1) 승인 (권장)  2) 다시 수정: …", "응답:"])


def parse_b_confirm(text):
    t = (text or "").strip()
    if re.fullmatch(r"1\s*\)?", t):
        return ("approve", None)
    m = re.match(r"^2\s*[\):：]?\s*(.*)$", t, re.S)
    if m:
        if not m.group(1).strip():
            raise ReviewError("2를 고르면 수정 지시를 함께 쓰세요. 예: 2: 단신 묶음 문장을 줄여 주세요")
        return ("again", m.group(1).strip().strip("\"“”"))
    raise ReviewError(f"해석할 수 없는 응답입니다: '{t[:30]}' — 1 또는 2: 수정 지시")


# ③ 발송 확인 -----------------------------------------------------------------

def render_review3(issue_week, s):
    """s: title, summary, summary_len, core, brief, body_len, body_max, sources, warnings"""
    return "\n".join([
        f"{BAR} 발송 확인 · {issue_week} {BAR}",
        f"{s['title']} / {s['summary']} / 총론 {s['summary_len']}자 / 핵심 {s['core']} · 단신 {s['brief']} / "
        f"본문 {s['body_len']:,}자(한도 {s['body_max']:,}) / 출처 {s['sources']}건 / 경고 {s['warnings']}",
        " 1) 발송본 생성·확정 (권장)",
        " 2) 검토 ②로 돌아가기",
        "응답:"])
