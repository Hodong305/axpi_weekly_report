"""S8 발송본 생성(설계서 9.8): 외부 스크립트·원격 이미지·링크가 없는 정적 HTML과 텍스트본.
HTML은 internal/templates/newsletter.html(표 기반, 인라인 스타일, 최대 폭 680px)을 작은 치환기로 채우고, 색은 templates/themes.yaml의
테마(A|B|C)에서 가져온다. 구조는 하나이고 색만 다르다. 의존성 없음.
템플릿 문법: {{키}}(HTML 이스케이프 치환), {{{키}}}(코드가 만든 HTML 그대로), <!--IF 키-->…<!--END 키-->(값이 참일 때), <!--LOOP 키-->…<!--END 키-->(목록 반복)."""
import base64
import html
import io
import re
from email.message import EmailMessage
from datetime import date, timedelta
from pathlib import Path

import yaml
from PIL import Image

from .checks import strip_urls
from .draft import POINT_LABELS, TOPIC_LABEL
from .glossary import annotate, gloss_fields

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE = TEMPLATE_DIR / "newsletter.html"
LABEL_CODE = {v: k for k, v in {"T1": "칩메이커", "T2": "장비사", "T3": "제조 솔루션", "T4": "Physical AI", "B": "기반기술 전이 사례"}.items()}
TOPIC_CODE = {"T1_CHIPMAKER": "T1", "T2_EQUIPMENT": "T2", "T3_SOLUTION": "T3", "T4_PHYSICAL_AI": "T4", "B": "B"}
TOPIC_ICON = {"T1": "🏭", "T2": "⚙️", "T3": "📊", "T4": "🤖", "B": "🧪"}
TOPIC_NAME = {"T1": "칩메이커", "T2": "장비사", "T3": "제조 솔루션", "T4": "Physical AI", "B": "기반기술 전이 사례"}
GLOSS_STYLE = "color:#7a8597;font-size:12.5px;"


def load_themes():
    return yaml.safe_load((TEMPLATE_DIR / "themes.yaml").read_text(encoding="utf-8"))


def _s(text):
    """URL·도메인 문자열이 남지 않도록 한 번 더 거른다."""
    return strip_urls(str(text))[0]


def period_text(window_start, window_end_exclusive):
    s = date.fromisoformat(window_start[:10])
    e = date.fromisoformat(window_end_exclusive[:10]) - timedelta(days=1)
    return f"{s.isoformat()} ~ {e.isoformat()}"


def period_short(window_start, window_end_exclusive):
    s = date.fromisoformat(window_start[:10])
    e = date.fromisoformat(window_end_exclusive[:10]) - timedelta(days=1)
    return f"{s.month}.{s.day} ~ {e.month}.{e.day}"


# 치환기 ------------------------------------------------------------------------

_BLOCK = re.compile(r"<!--(IF|LOOP) (\w+)-->(.*?)<!--END \2-->", re.S)
_RAW = re.compile(r"\{\{\{(\w+)\}\}\}")
_VAR = re.compile(r"\{\{(\w+)\}\}")


def fill(template, ctx):
    def block(m):
        kind, name, inner = m.groups()
        v = ctx.get(name)
        if kind == "IF":
            return fill(inner, ctx) if v else ""
        return "".join(fill(inner, {**ctx, **item}) for item in (v or []))
    out = _BLOCK.sub(block, template)
    out = _RAW.sub(lambda m: str(ctx.get(m.group(1), "")), out)
    return _VAR.sub(lambda m: html.escape(_s(ctx.get(m.group(1), "")), quote=True), out)


_DATA_URI = re.compile(r"data:image/(?:jpeg|png);base64,[A-Za-z0-9+/=]+")
_IMG = re.compile(r"<img\b[^>]*>", re.I)
_IMG_SRC = re.compile(r'\bsrc="(cid:[^"]+|data:image/(?:jpeg|png);base64,[A-Za-z0-9+/=]+)"')


def verify_static(doc):
    """외부 스크립트·원격 이미지·링크 금지. 이미지는 cid: 또는 data: 주소로 내장된 것만 허용한다(base64 안에는 어떤 글자열이든 우연히 나올 수 있어 검사에서 뺀다)."""
    for tag in _IMG.findall(doc):
        if not _IMG_SRC.search(tag):
            raise ValueError(f"발송본의 이미지는 cid: 또는 data: 로 내장된 것만 허용됩니다: {tag[:60]}")
    low = _DATA_URI.sub("data:", doc).lower()
    low = _IMG.sub("", low)      # <img> 태그는 위에서 검사했다
    for bad in ("http", "<a ", "<script", "<link", "<iframe", "@import", "url("):
        if bad in low:
            raise ValueError(f"발송본에 허용되지 않는 요소가 있습니다: {bad}")


def prepare_image(data, max_kb, width=624):
    """발송본용 이미지: 표시 폭(624px)보다 넓으면 줄이고, 파일 크기가 max_kb 이하가 되도록 품질·크기를 낮춘다. 반환: (바이트, mime, 가로, 세로)"""
    im = Image.open(io.BytesIO(data))
    im.load()
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    if im.mode != "RGB":
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    limit, q = max_kb * 1024, 82
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, optimize=True)
        if buf.tell() <= limit or (q <= 40 and im.width <= 200):
            return buf.getvalue(), "image/jpeg", im.width, im.height
        if q > 40:
            q -= 8
        else:
            im = im.resize((max(1, round(im.width * 0.85)), max(1, round(im.height * 0.85))), Image.LANCZOS)


# 내용 -------------------------------------------------------------------------

B3_NAME = {"T1": "칩메이커", "T2": "장비사", "T3": "제조 솔루션", "T4": "Physical AI", "B": "기반기술"}
B3_NUM = {"T1": "01", "T2": "02", "T3": "03", "T4": "04", "B": "+"}


def number_marker(theme, code):
    """B3 꼭지 표식: 번호 배지(01~04, 기반기술은 +) + 꼭지 이름. 이모지 대신 색 표식을 쓴다."""
    fg, _ = theme["topic"][code]
    return (f'<span style="display:inline-block;background:{fg};color:#ffffff;font-size:11px;font-weight:bold;padding:2px 6px;border-radius:4px;">'
            f'{B3_NUM[code]}</span>&nbsp;<span style="font-size:12px;font-weight:bold;color:{fg};">{B3_NAME[code]}</span>')


def topic_pill(theme, code):
    """v4.7 꼭지 표식: 꼭지 색 바탕의 작은 이름표(이모지·번호 없음)."""
    fg, bg = theme["topic"][code]
    return (f'<span style="display:inline-block;background:{bg};color:{fg};font-size:11.5px;font-weight:bold;padding:2px 8px;'
            f'border-radius:999px;">{B3_NAME[code]}</span>')



def read_minutes(draft, show_body, cpm):
    """읽는 시간(분, 반올림, 최소 1)과 30초 브리핑(헤드라인 + 한 줄 핵심·단신 제목)을 읽는 시간(초, 10초 단위)."""
    arts = draft.get("articles") or []
    full = len(draft.get("issue_title", "")) + len(draft.get("summary", ""))
    p = draft.get("punch")
    full += len(p["text"]) + len(p["twist"]) if p else 0
    for a in arts:
        full += len(a["title"]) + len(a.get("deck") or "") + sum(len(x) for x in (a.get("points") or {}).values())
        full += len(a.get("watch") or "") + (len(a["body"]) if (show_body or a["slot"] != "core") else 0)
    full += sum(len(m["line"]) for m in draft.get("more") or [])
    skim = len(draft.get("issue_title", "")) + sum(len(a.get("deck") or a["title"]) for a in arts)
    return max(1, round(full / cpm)), max(10, round(skim / cpm * 60 / 10) * 10)


def value_size(value, big=23):
    """숫자 칸 글씨 크기: 짧은 수치는 크게, '수 시간 → 수 분' 같은 긴 말은 줄여 옆 칸을 침범하지 않게."""
    n = len(value)
    return big if n <= 7 else big - 5 if n <= 10 else big - 8


def pairs(items):
    """2열 격자용: [{"cells": [a, b]}, ...]. 홀수면 마지막 칸은 빈 칸({"filler": True})."""
    rows = []
    for k in range(0, len(items), 2):
        cells = list(items[k:k + 2])
        if len(cells) == 1:
            cells.append({"filler": True})
        rows.append({"cells": [dict(c, filler=c.get("filler", False)) for c in cells]})
    return rows


def chip(theme, code, size="12px"):
    fg, bg = theme["topic"][code]
    return (f'<span style="display:inline-block;background:{bg};color:{fg};font-weight:bold;padding:3px 10px;border-radius:999px;'
            f'font-size:{size};">{TOPIC_ICON[code]} {TOPIC_NAME[code]}</span>')


def _img_src(im, mode):
    if not im:
        return ""
    if mode == "eml":
        return "cid:" + im["cid"]
    return f"data:{im['mime']};base64," + base64.b64encode(im["data"]).decode("ascii")


def _followup(ev):
    led = ev["ledger"] if ev else None
    if led and led["verdict"] == "SAME_WITH_CHANGE" and led.get("issue"):
        return f"{led['issue']} 보도 후속"
    return ""


def name_html(name, accent, theme=None):
    """이름 꾸밈. 테마에 name_em_words가 있으면 끝 N단어(예: Tech Brief)를 name_em_color로, 없으면 이름의 첫 숫자에 강조색을 입힌다(예: FAB4 위클리)."""
    n = (theme or {}).get("name_em_words")
    if n:
        words = name.split(" ")
        if len(words) > n:
            return html.escape(" ".join(words[:-n])) + f' <span style="color:{theme["name_em_color"]};">' + html.escape(" ".join(words[-n:])) + "</span>"
    m = re.search(r"\d+", name)
    if not m:
        return html.escape(name)
    return html.escape(name[:m.start()]) + f'<span style="color:{accent};">{m.group()}</span>' + html.escape(name[m.end():])


def glossed(draft, top=False):
    """첫 등장에 풀이를 붙인 조각들. 반환: {칸 키: [(조각, 풀이 또는 None)]}  (제목·헤드라인·총론에는 붙이지 않는다)"""
    meanings = {g["term"]: g["meaning"] for g in draft.get("glossary") or [] if g.get("meaning")}
    fields = [(k, _s(t)) for k, t in gloss_fields(draft, top)]
    segs, _ = annotate(fields, meanings)
    return segs


def to_html(segs):
    return "".join(html.escape(t) + (f'<span style="{GLOSS_STYLE}">({html.escape(m)})</span>' if m else "") for t, m in segs)


def to_plain(segs):
    return "".join(t + (f"({m})" if m else "") for t, m in segs)


def build(draft, ctx):
    """미리 보기용 HTML(이미지는 data URI)과 텍스트본. 반환: (html, text)"""
    r = build_all(draft, ctx)
    return r["html_preview"], r["text"]


def build_all(draft, ctx):
    """발송본 전체. 반환: {"html_eml"(이미지는 cid:), "html_preview"(data URI), "text", "images": [(cid, 바이트, mime)]}.
    ctx["image_loader"](media_ref) → (원본 바이트, 발행처) 또는 None. 없으면 이미지 없이 만든다. ctx["image_max_kb"] 기본 300."""
    images, imgs = [], {}
    loader = ctx.get("image_loader")
    for i, a in enumerate(draft["articles"]):
        img = a.get("image")
        got = loader(img["media_ref"]) if (img and loader and a["slot"] == "core") else None
        if got:
            data, mime, w, h = prepare_image(got[0], ctx.get("image_max_kb", 300))
            cid = f"img{len(images) + 1}@newsletter"
            images.append((cid, data, mime))
            imgs[i] = {"cid": cid, "data": data, "mime": mime, "caption": img["caption"], "credit": got[1]}
    out = {"images": images}
    for mode in ("eml", "preview"):
        html_doc, text = _build(draft, ctx, imgs, mode)
        out["html_" + mode] = html_doc
    out["text"] = text
    return out


def _build(draft, ctx, imgs, mode):
    """ctx: issue_week, period, period_short, issue_type, counts{pass_docs,published}, events, status_rows, corrections, note,
    issue_no, n_docs, newsletter_name, newsletter_tagline, newsletter_team, theme. 반환: (html, text)"""
    themes = load_themes()
    key = ctx.get("theme", "A")
    if key not in themes:
        raise ValueError(f"알 수 없는 테마: {key} (A|B|C)")
    th = themes[key]
    b3 = th.get("template") == "newsletter-b3.html"
    show_body = th.get("show_body", True)       # B5(스마트 브리핑)는 핵심 본문을 싣지 않고 요점으로 대신한다
    by_id = {e["event_id"]: e for e in ctx["events"]}
    segs = glossed(draft, th.get("gloss_top", False))
    note = ""
    if ctx["issue_type"] == "축약호":
        note = f"축약호: 최종 통과 문서 {ctx['counts']['pass_docs']}건, 게재 사건 {ctx['counts']['published']}건. {ctx['note']}"
    core = [(i, a) for i, a in enumerate(draft["articles"]) if a["slot"] == "core"]
    brief = [(i, a) for i, a in enumerate(draft["articles"]) if a["slot"] == "brief"]
    more = draft.get("more") or []
    punch = draft.get("punch")

    def code_of(a):
        return LABEL_CODE.get((a.get("tags") or [""])[0], "B")

    def art(i, a):
        code = code_of(a)
        fg, bg = th["topic"][code]
        figs = a.get("key_figures") or []
        w = max(1, 100 // max(1, len(figs)))
        fig = a.get("figure")
        tags = a.get("tags") or []
        pts = a.get("points") or {}
        points = [{"label": lab, "text_html": to_html(segs[("articles", i, "point_" + k)])}
                  for k, lab in POINT_LABELS if pts.get(k) and ("articles", i, "point_" + k) in segs]
        return {"pill_html": topic_pill(th, code), "topic_name": B3_NAME[code], "deck": a.get("deck") or "",
                "points": points, "has_points": bool(points), "show_body": show_body or a["slot"] != "core",
                "fig1_value": figs[0]["value"] if figs else "", "fig1_label": figs[0]["label"] if figs else "",
                "fig_rest": [{"value": k["value"], "label": k["label"]} for k in figs[1:]],
                "marker_html": number_marker(th, code) if b3 else "", "tags_text": " · ".join((a.get("tags") or [])[1:]),
                "inner_pad": "10px 18px 16px 18px" if i in imgs else "16px 18px",
                "title": a["title"], "followup": _followup(by_id.get(a["event_id"])), "topic_chip": chip(th, code), "topic_fg": fg, "topic_bg": bg,
                "tags": [{"text": t} for t in (a.get("tags") or [])[1:]], "body_html": to_html(segs[("articles", i, "body")]),
                "watch_html": to_html(segs[("articles", i, "watch")]) if ("articles", i, "watch") in segs else "",
                "credit": " / ".join(a["credits"]), "has_figures": bool(figs),
                "figures": [{"value": k["value"], "label": k["label"], "why": k.get("why") or "", "w": w, "vsize": value_size(k["value"])} for k in figs],
                "fig_value": fig["value"] if fig else "", "fig_label": fig["label"] if fig else "", "fig_why": (fig or {}).get("why", ""),
                "image_src": _img_src(imgs.get(i), mode), "image_caption": imgs[i]["caption"] if i in imgs else "",
                "image_credit": imgs[i]["credit"] if i in imgs else ""}
    tldr = []
    for n, (i, a) in enumerate(core + brief, 1):
        code = code_of(a)
        line = (a.get("deck") or a["title"]) if a["slot"] == "core" else a["title"]
        key = ("articles", i, "deck")
        line_html = to_html(segs[key]) if (a["slot"] == "core" and key in segs) else html.escape(_s(line))
        tldr.append({"num": f"{n:02d}", "fg": th["topic"][code][0], "pill_html": topic_pill(th, code), "topic_name": B3_NAME[code],
                     "line": line, "line_html": line_html, "is_core": a["slot"] == "core", "is_brief": a["slot"] != "core"})
    mins, secs = read_minutes(draft, show_body, ctx.get("read_chars_per_min", 500))
    core_ctx, brief_ctx = [art(i, a) for i, a in core], [art(i, a) for i, a in brief]
    for n, c in enumerate(core_ctx + brief_ctx, 1):       # 30초 브리핑과 같은 번호(E·S 디자인이 기사 앞에 쓴다)
        c["num"] = f"{n:02d}"
    numbers = []      # v4.7 '숫자로 보는 이번 주': 게재 기사의 수치를 기사 번호와 함께 모은다(핵심 수치 카드 → 단신 수치)
    for c, (i, a) in zip(core_ctx + brief_ctx, core + brief):
        figs = list(a.get("key_figures") or []) + ([a["figure"]] if a.get("figure") else [])
        for k in figs:
            numbers.append({"num": c["num"], "value": k["value"], "label": k["label"], "why": k.get("why") or "", "vsize": value_size(k["value"]),
                            "topic_name": c["topic_name"], "topic_fg": c["topic_fg"], "topic_bg": c["topic_bg"], "article_title": a["title"]})
    summary_html = to_html(segs[("summary",)]) if ("summary",) in segs else html.escape(_s(draft["summary"]))
    first = re.split(r"(?<=다\.)\s", _s(draft["summary"]), maxsplit=1)[0]
    index = [{"name": n, "count": c} for n, c in (("브리핑", len(tldr)), ("숫자로 보는 이번 주", len(numbers)), ("이번 주 핵심", len(core)),
                                                    ("단신", len(brief)), ("더 볼 거리", len(more)), ("용어", len([g for g in draft.get("glossary") or [] if g.get("meaning")])))
             if c]
    groups = []
    for j, m in enumerate(more):
        code = TOPIC_CODE.get(m["topic"], "B")
        head = TOPIC_NAME[code]
        if not groups or groups[-1]["heading"] != head:
            groups.append({"heading": head, "icon": TOPIC_ICON[code], "fg": th["topic"][code][0], "items": [],
                           "marker_html": number_marker(th, code) if b3 else "", "pill_html": topic_pill(th, code)})
        groups[-1]["items"].append({"line_html": to_html(segs[("more", j, "line")]), "credit": m["credit"]})
    status = [{"name": n, "result": r} for n, r in ctx["status_rows"]] if ctx["issue_type"] == "현황호" else []
    corr = [f"{c.get('event_key', '')} 이전 표현 「{c['before']}」 → 수정 표현 「{c['after']}」 ({c.get('date', '')})" for c in ctx.get("corrections") or []]
    gl = [g for g in draft.get("glossary") or [] if g.get("meaning")]
    big = punch["big"] if punch else ""
    t = {**{k: v for k, v in th.items() if k not in ("topic", "name", "hero_dark", "template", "name_em_words", "name_em_color", "show_body",
                                                     "gloss_top", "numbers_panel")},
         "tldr": tldr, "has_tldr": bool(tldr), "read_min": mins, "skim_sec": secs, "skim_min": max(1, round(secs / 60)),
         "numbers": numbers, "has_numbers": bool(numbers), "summary_html": summary_html, "preheader": first,
         "index": index, "n_core": len(core), "n_brief": len(brief),
         "brief_rows": pairs(brief_ctx), "number_rows": pairs(numbers), "show_numbers": th.get("numbers_panel", False),
         "has_image": bool(imgs),
         "hero_dark": th["hero_dark"], "hero_light": not th["hero_dark"],
         "title_tag": f"{ctx['newsletter_name']} {ctx['issue_week']} {draft['issue_title']}", "name": ctx["newsletter_name"],
         "name_html": name_html(ctx["newsletter_name"], th["accent"], th), "tagline": ctx["newsletter_tagline"], "team": ctx["newsletter_team"],
         "issue_no": ctx["issue_no"], "issue_week": ctx["issue_week"], "period_short": ctx["period_short"], "period": ctx["period"],
         "masthead_chips": [{"chip": chip(th, c, "11.5px")} for c in ("T1", "T2", "T3", "T4")],
         "n_docs": ctx["n_docs"], "n_pub": ctx["counts"]["published"], "note": note,
         "issue_title": draft["issue_title"], "summary": draft["summary"],
         "punch": bool(punch), "punch_big_text": big, "punch_big_size": 40 if len(big) <= 6 else 26 if len(big) <= 12 else 20,
         "punch_label_text": punch["label"] if punch else "", "punch_text_body": punch["text"] if punch else "",
         "punch_twist_body": punch["twist"] if punch else "",
         "has_status": bool(status), "status_rows": status, "status_note": ctx["note"] if status else "",
         "has_core": bool(core), "core": core_ctx, "has_brief": bool(brief), "brief": brief_ctx,
         "has_more": bool(groups), "more": groups, "has_gloss": bool(gl), "glossary": [{"term": g["term"], "meaning": g["meaning"]} for g in gl],
         "has_corr": bool(corr), "corrections": [{"text": c} for c in corr], "excluded_count": draft.get("excluded_count", 0)}
    out_html = fill((TEMPLATE_DIR / th.get("template", "newsletter.html")).read_text(encoding="utf-8"), t)
    verify_static(out_html)
    return out_html, build_text(draft, ctx, t, segs, core, brief, groups, gl, note, imgs, b3, show_body)


def build_text(draft, ctx, t, segs, core, brief, groups, gl, note, imgs=None, b3=False, show_body=True):
    imgs = imgs or {}
    L = [f"{ctx['newsletter_name']} · 제{ctx['issue_no']}호 · {ctx['issue_week']}", ctx["newsletter_tagline"],
         f"대상 기간 {ctx['period']}", "01 칩메이커 · 02 장비사 · 03 제조 솔루션 · 04 Physical AI" if b3 else
         "칩메이커 · 장비사 · 제조 솔루션 · Physical AI" if ctx.get("theme") not in ("A", "B", "C") else
         "🏭 칩메이커 · ⚙️ 장비사 · 📊 제조 솔루션 · 🤖 Physical AI", "",
         "[이번 주 헤드라인]", draft["issue_title"], "", "[한 줄 요약]", draft["summary"], ""]
    if t["tldr"]:
        L.append(f"[30초 브리핑] 전체 약 {t['read_min']}분 · 이것만 읽으면 약 {t['skim_sec']}초")
        L += [f"{x['num']} {x['line']}" for x in t["tldr"]] + [""]
    if note:
        L += [note, ""]
    p = draft.get("punch")
    if p:
        L += [f"[{p['label']}]", f"{p['big']} — {p['text']}", p["twist"], ""]
    if t.get("show_numbers") and t["numbers"]:
        L.append("[숫자로 보는 이번 주]")
        L += [f"- {x['value']} {x['label']} (기사 {x['num']})" + (f" — {x['why']}" if x["why"] else "") for x in t["numbers"]] + [""]
    if t["has_status"]:
        L.append("[네 분야 조사 결과]")
        L += [f"- {r['name']}: {r['result']}" for r in t["status_rows"]]
        L += [f"0건 사유: {ctx['note']}", ""]

    def article(i, a, tc):
        fu = tc["followup"]
        L.append((f"({fu}) " if fu else "") + a["title"])
        if a.get("tags"):
            L.append("  꼬리표: " + " · ".join(a["tags"]))
        if i in imgs:
            L.append(f"  [이미지] {imgs[i]['caption']} · 이미지 · {imgs[i]['credit']}")
        if a.get("deck") and a["slot"] == "core":
            L.append(f"  ▶ {a['deck']}")
        for k, lab in POINT_LABELS:
            if ("articles", i, "point_" + k) in segs:
                L.append(f"  · {lab}: {to_plain(segs[('articles', i, 'point_' + k)])}")
        if show_body or a["slot"] != "core":
            L.append(to_plain(segs[("articles", i, "body")]))
        figs = (a.get("key_figures") or []) + ([a["figure"]] if a.get("figure") else [])
        if figs:
            L.append("  수치: " + " / ".join(f"{k['value']} {k['label']}" + (f"(왜 중요: {k['why']})" if k.get("why") else "") for k in figs))
        if ("articles", i, "watch") in segs:
            L.append(f"  MTC가 볼 것 · {to_plain(segs[('articles', i, 'watch')])}")
        L.extend([f"  출처 · {tc['credit']}", ""])
    if t["has_core"]:
        L.append("[이번 주 핵심]")
        for (i, a), tc in zip(core, t["core"]):
            article(i, a, tc)
    if t["has_brief"]:
        L.append("[단신]")
        for (i, a), tc in zip(brief, t["brief"]):
            article(i, a, tc)
    if groups:
        L.append("[더 볼 거리]")
        j = 0
        for g in groups:
            L.append(f"<{g['heading']}>")
            for it in g["items"]:
                L += [f"- {to_plain(segs[('more', j, 'line')])}", f"  {it['credit']}"]
                j += 1
        L.append("")
    if gl:
        L.append("[이번 호 용어 사전]")
        L += [f"- {g['term']}: {g['meaning']}" for g in gl] + [""]
    if t["has_corr"]:
        L += ["[정정]"] + [c["text"] for c in t["corrections"]] + [""]
    L += [f"{ctx['newsletter_name']}는 {ctx['newsletter_team']}이 매주 화요일 보내는 제조 AI 동향 소식지입니다. 이번 호는 공개 자료 {ctx['n_docs']}건을 검토해 "
          f"{ctx['counts']['published']}건을 골랐고, 사내 AI가 초안을 쓰고 편집자가 검토했습니다. 원문 링크는 보안 정책상 싣지 않으니 출처의 발행처·제목으로 검색해 보세요."
          + (" 이미지는 각 발행처의 공개 자료이며 저작권은 발행처에 있습니다." if imgs else ""),
          f"이 밖에 검토했으나 싣지 않은 자료는 {t['excluded_count']}건입니다.", "의견·제보·정정은 이 메일에 회신해 주세요."]
    return "\n".join(_s(x) for x in L) + "\n"


def build_eml(draft, ctx, built):
    """newsletter.eml: multipart/related(HTML + 텍스트 대체본, 이미지는 cid로 내장). 제목 = 소식지 이름 + 헤드라인. 수신자는 비운다(편집자가 정한다)."""
    msg = EmailMessage()
    msg["Subject"] = f"{ctx['newsletter_name']} {draft['issue_title']}"
    if ctx.get("newsletter_from"):
        msg["From"] = ctx["newsletter_from"]
    msg["X-Unsent"] = "1"       # 메일 프로그램이 보내지 않은 초안으로 연다
    msg.set_content(built["text"])
    msg.add_alternative(built["html_eml"], subtype="html")
    html_part = msg.get_payload()[1]
    for cid, data, mime in built["images"]:
        maintype, subtype = mime.split("/")
        html_part.add_related(data, maintype, subtype, cid=f"<{cid}>")
    return msg
