"""S8 발송본 생성(설계서 9.8): 외부 스크립트·원격 이미지·링크가 없는 정적 HTML과 텍스트본.
HTML은 internal/templates/newsletter.html(표 기반, 인라인 스타일)을 작은 치환기로 채운다. 의존성 없음.
템플릿 문법: {{키}}(HTML 이스케이프 치환), <!--IF 키-->…<!--END 키-->(값이 참일 때), <!--LOOP 키-->…<!--END 키-->(목록 반복)."""
import html
import re
from datetime import date, timedelta
from pathlib import Path

from .checks import strip_urls
from .draft import TOPIC_LABEL

TEMPLATE = Path(__file__).resolve().parent / "templates" / "newsletter.html"
TOPIC_COLOR = {"칩메이커": ("#e0ecff", "#1d4ed8"), "장비사": ("#f3e8ff", "#7e22ce"), "제조 솔루션": ("#ddf4f1", "#0f766e"),
               "Physical AI": ("#fdeee4", "#b4501b"), "기반기술 전이 사례": ("#f1f2f4", "#4b5563")}
MATURITY_COLOR = ("#e7f6ec", "#1a7f37")
EVIDENCE_COLOR = ("#f1f2f4", "#4b5563")
TOPIC_NAME = {"T1_CHIPMAKER": "칩메이커", "T2_EQUIPMENT": "반도체 장비사", "T3_SOLUTION": "장비사 외 제조 솔루션 공급사", "T4_PHYSICAL_AI": "Physical AI"}


def _s(text):
    """URL·도메인 문자열이 남지 않도록 한 번 더 거른다."""
    return strip_urls(str(text))[0]


def period_text(window_start, window_end_exclusive):
    s = date.fromisoformat(window_start[:10])
    e = date.fromisoformat(window_end_exclusive[:10]) - timedelta(days=1)
    return f"{s.isoformat()} ~ {e.isoformat()}"


# 치환기 ------------------------------------------------------------------------

_BLOCK = re.compile(r"<!--(IF|LOOP) (\w+)-->(.*?)<!--END \2-->", re.S)
_VAR = re.compile(r"\{\{(\w+)\}\}")


def fill(template, ctx):
    def block(m):
        kind, name, inner = m.groups()
        v = ctx.get(name)
        if kind == "IF":
            return fill(inner, ctx) if v else ""
        return "".join(fill(inner, {**ctx, **item}) for item in (v or []))
    out = _BLOCK.sub(block, template)
    return _VAR.sub(lambda m: html.escape(_s(ctx.get(m.group(1), "")), quote=True), out)


def verify_static(doc):
    low = doc.lower()
    for bad in ("http", "<a ", "<script", "<img", "<link", "<iframe", "@import", "url("):
        if bad in low:
            raise ValueError(f"발송본에 허용되지 않는 요소가 있습니다: {bad}")


# 내용 -------------------------------------------------------------------------

def _tags(tags, slot):
    out = []
    for i, t in enumerate(tags):
        bg, fg = (TOPIC_COLOR.get(t, EVIDENCE_COLOR) if i == 0 else MATURITY_COLOR if i == 1 else EVIDENCE_COLOR)
        out.append({"text": t, "bg": bg, "fg": fg})
    return out


def _followup(ev):
    led = ev["ledger"] if ev else None
    if led and led["verdict"] == "SAME_WITH_CHANGE" and led.get("issue"):
        return f"{led['issue']} 보도 후속"
    return ""


def more_groups(more):
    groups = []
    for m in more:
        head = TOPIC_LABEL.get(m["topic"], m["topic"] or "기타")
        if not groups or groups[-1]["heading"] != head:
            groups.append({"heading": head, "fg": TOPIC_COLOR.get(head, EVIDENCE_COLOR)[1], "items": []})
        groups[-1]["items"].append({"line": m["line"], "credit": m["credit"]})
    return groups


def build(draft, ctx):
    """ctx: issue_week, period, issue_type, counts{pass_docs,published}, events, status_rows, corrections, note,
    issue_no, n_docs, newsletter_name. 반환: (html, text)"""
    by_id = {e["event_id"]: e for e in ctx["events"]}
    note = ""
    if ctx["issue_type"] == "축약호":
        note = f"축약호: 최종 통과 문서 {ctx['counts']['pass_docs']}건, 게재 사건 {ctx['counts']['published']}건. {ctx['note']}"
    core = [a for a in draft["articles"] if a["slot"] == "core"]
    brief = [a for a in draft["articles"] if a["slot"] == "brief"]
    more = draft.get("more") or []
    hook = draft.get("hook")

    def art(a):
        figs = a.get("key_figures") or []
        w = max(1, 100 // max(1, len(figs)) - 2)
        return {"title": a["title"], "body": a["body"], "watch": a.get("watch") or "", "followup": _followup(by_id.get(a["event_id"])),
                "tags": _tags(a.get("tags") or [], a["slot"]), "credit": " / ".join(a["credits"]),
                "figures": [{"value": k["value"], "label": k["label"], "w": w} for k in figs], "has_figures": bool(figs)}
    core_ctx, brief_ctx = [art(a) for a in core], [art(a) for a in brief]
    groups = more_groups(more)
    status = [{"name": n, "result": r} for n, r in ctx["status_rows"]] if ctx["issue_type"] == "현황호" else []
    corr = [f"{c.get('event_key', '')} 이전 표현 「{c['before']}」 → 수정 표현 「{c['after']}」 ({c.get('date', '')})" for c in ctx.get("corrections") or []]
    t = {"title_tag": f"{draft['issue_title']}", "newsletter_name": ctx["newsletter_name"], "issue_no": ctx["issue_no"],
         "issue_week": ctx["issue_week"], "period": ctx["period"], "n_docs": ctx["n_docs"], "n_pub": ctx["counts"]["published"],
         "issue_title": draft["issue_title"], "one_line": draft["one_line"], "note": note, "abstract": draft["abstract"],
         "hook": bool(hook), "hook_q": hook["question"] if hook else "", "hook_a": hook["answer"] if hook else "",
         "has_status": bool(status), "status_rows": status, "status_note": ctx["note"] if status else "",
         "has_core": bool(core_ctx), "core": core_ctx, "has_brief": bool(brief_ctx), "brief": brief_ctx,
         "has_more": bool(groups), "more": groups, "has_corr": bool(corr), "corrections": [{"text": c} for c in corr],
         "excluded_count": draft.get("excluded_count", 0)}
    out_html = fill(TEMPLATE.read_text(encoding="utf-8"), t)
    verify_static(out_html)
    return out_html, build_text(draft, ctx, t, groups)


def build_text(draft, ctx, t, groups):
    L = [f"{ctx['newsletter_name']} · 제{ctx['issue_no']}호 · {ctx['issue_week']}",
         f"대상 기간 {ctx['period']} · 검토 문서 {ctx['n_docs']}건 · 게재 {ctx['counts']['published']}건", "",
         draft["issue_title"], draft["one_line"], ""]
    if t["note"]:
        L += [t["note"], ""]
    L += ["[이번 주 요약]", draft["abstract"], ""]
    if draft.get("hook"):
        L += ["[팹 판단 한 컷]", f"Q. {draft['hook']['question']}", f"A. {draft['hook']['answer']}", ""]
    if t["has_status"]:
        L.append("[네 분야 조사 결과]")
        L += [f"- {r['name']}: {r['result']}" for r in t["status_rows"]]
        L += [f"0건 사유: {ctx['note']}", ""]

    def article(a):
        fu = a["followup"]
        L.append((f"({fu}) " if fu else "") + a["title"])
        if a["tags"]:
            L.append("  꼬리표: " + " · ".join(x["text"] for x in a["tags"]))
        L.append(a["body"])
        if a["figures"]:
            L.append("  수치: " + " / ".join(f"{k['value']} {k['label']}" for k in a["figures"]))
        if a["watch"]:
            L.append(f"  MTC가 볼 것 · {a['watch']}")
        L.extend([f"  출처 · {a['credit']}", ""])
    if t["has_core"]:
        L.append("[핵심 기사]")
        for a in t["core"]:
            article(a)
    if t["has_brief"]:
        L.append("[단신]")
        for a in t["brief"]:
            article(a)
    if groups:
        L.append("[더 볼 거리]")
        for g in groups:
            L.append(f"<{g['heading']}>")
            for it in g["items"]:
                L += [f"- {it['line']}", f"  {it['credit']}"]
        L.append("")
    if t["has_corr"]:
        L += ["[정정]"] + [c["text"] for c in t["corrections"]] + [""]
    L += ["이 소식지는 공개 자료를 요약·재구성했으며, 사내 AI가 초안을 쓰고 편집자가 검토했습니다. 원문 링크는 사내 보안 정책에 따라 싣지 않습니다.",
          f"이 밖에 검토했으나 싣지 않은 자료는 {t['excluded_count']}건입니다.", "의견·정정 요청은 이 메일에 회신해 주세요. · MTC AX/PI팀"]
    return "\n".join(_s(x) for x in L) + "\n"
