"""S8 발송본 생성(설계서 9.8): 외부 스크립트·원격 이미지·링크가 없는 정적 HTML과 텍스트본. 스타일은 인라인 CSS만 쓴다."""
import html
from datetime import date, timedelta

from .checks import strip_urls
from .draft import published_events
from .settings import TOPICS

TOPIC_LABEL = {"T1_CHIPMAKER": "① 칩메이커", "T2_EQUIPMENT": "② 반도체 장비사", "T3_SOLUTION": "③ 장비사 외 제조 솔루션 공급사",
               "T4_PHYSICAL_AI": "④ Physical AI"}

FONT = "'Malgun Gothic','Apple SD Gothic Neo','Noto Sans KR',Arial,sans-serif"


def _s(text):
    """URL·도메인 문자열이 남지 않도록 한 번 더 거른다."""
    return strip_urls(str(text))[0]


def _e(text):
    return html.escape(_s(text), quote=True)


def period_text(window_start, window_end_exclusive):
    s = date.fromisoformat(window_start[:10])
    e = date.fromisoformat(window_end_exclusive[:10]) - timedelta(days=1)
    return f"{s.isoformat()} ~ {e.isoformat()}"


def _numbered_sources(draft):
    """출처 정보 목록(번호, 링크 없음). 같은 출처는 한 번만 싣는다."""
    items, index = [], {}
    for a in draft["articles"]:
        for c in a["credits"]:
            c = _s(c)
            if c not in index:
                items.append(c)
                index[c] = len(items)
    return items, index


def _followup(ev):
    led = ev["ledger"]
    if led["verdict"] == "SAME_WITH_CHANGE" and led.get("issue"):
        return f"{led['issue']} 보도 후속"
    return None


def build(draft, ctx):
    """ctx: issue_week, period, issue_type, counts{pass_docs,published}, events, status_rows, corrections, note
    반환: (html, text)"""
    by_id = {e["event_id"]: e for e in ctx["events"]}
    sources, index = _numbered_sources(draft)
    core = [a for a in draft["articles"] if a["slot"] == "core"]
    brief = [a for a in draft["articles"] if a["slot"] == "brief"]
    note = None
    if ctx["issue_type"] == "축약호":
        note = (f"축약호: 최종 통과 문서 {ctx['counts']['pass_docs']}건, 게재 사건 {ctx['counts']['published']}건. {ctx['note']}")
    h = []
    t = []
    w = h.append
    w('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
      '<meta name="viewport" content="width=device-width, initial-scale=1">'
      f"<title>{_e(draft['issue_title'])}</title></head>")
    w('<body style="margin:0;padding:0;background:#f3f4f6;">')
    w(f'<div style="max-width:680px;margin:0 auto;padding:28px 24px;background:#ffffff;font-family:{FONT};'
      'color:#1f2933;line-height:1.75;font-size:15px;">')
    w(f'<div style="font-size:12px;color:#6b7280;">{_e(ctx["issue_week"])} · 대상 기간 {_e(ctx["period"])}</div>')
    w(f'<h1 style="font-size:24px;line-height:1.35;margin:8px 0 6px 0;color:#111827;">{_e(draft["issue_title"])}</h1>')
    w(f'<p style="font-size:16px;margin:0 0 16px 0;color:#374151;">{_e(draft["one_line"])}</p>')
    t += [f"{draft['issue_title']}", f"{ctx['issue_week']} · 대상 기간 {ctx['period']}", "", draft["one_line"], ""]
    if note:
        w(f'<p style="font-size:13px;color:#92400e;background:#fef3c7;padding:8px 12px;margin:0 0 16px 0;">{_e(note)}</p>')
        t += [note, ""]
    w('<h2 style="font-size:16px;margin:20px 0 6px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">이번 주 요약</h2>')
    w(f'<p style="margin:0 0 12px 0;">{_e(draft["abstract"])}</p>')
    t += ["[이번 주 요약]", draft["abstract"], ""]
    hook = draft.get("hook")
    if hook:
        w('<div style="background:#eef2ff;padding:12px 14px;margin:12px 0;">'
          f'<div style="font-weight:bold;">팹 판단 한 컷 · {_e(hook["question"])}</div>'
          f'<div style="margin-top:4px;">{_e(hook["answer"])}</div></div>')
        t += ["[팹 판단 한 컷]", f"Q. {hook['question']}", f"A. {hook['answer']}", ""]

    def article(a, n):
        ev = by_id.get(a["event_id"])
        fu = _followup(ev) if ev else None
        refs = ", ".join(str(index[_s(c)]) for c in a["credits"])
        w('<div style="margin:14px 0 18px 0;">')
        if fu:
            w(f'<div style="font-size:12px;color:#2563eb;">{_e(fu)}</div>')
        w(f'<h3 style="font-size:17px;margin:2px 0 6px 0;color:#111827;">{_e(a["title"])}</h3>')
        w(f'<p style="margin:0 0 6px 0;">{_e(a["body"])}</p>')
        w(f'<div style="font-size:12px;color:#6b7280;">출처 {_e(refs)}</div></div>')
        t.extend([(f"({fu}) " if fu else "") + a["title"], a["body"], f"출처 {refs}", ""])

    if core:
        w('<h2 style="font-size:16px;margin:24px 0 4px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">핵심 기사</h2>')
        t.append("[핵심 기사]")
        for i, a in enumerate(core, 1):
            article(a, i)
    if brief:
        w('<h2 style="font-size:16px;margin:24px 0 4px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">단신</h2>')
        t.append("[단신]")
        for i, a in enumerate(brief, 1):
            article(a, i)
    if ctx["issue_type"] == "현황호":
        w('<h2 style="font-size:16px;margin:24px 0 4px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">네 분야 조사 결과</h2>')
        w('<table style="border-collapse:collapse;width:100%;font-size:14px;">')
        t.append("[네 분야 조사 결과]")
        for row in ctx["status_rows"]:
            w(f'<tr><td style="border:1px solid #e5e7eb;padding:6px 8px;width:40%;">{_e(row[0])}</td>'
              f'<td style="border:1px solid #e5e7eb;padding:6px 8px;">{_e(row[1])}</td></tr>')
            t.append(f"- {row[0]}: {row[1]}")
        w("</table>")
        w(f'<p style="margin:10px 0;">0건 사유: {_e(ctx["note"])}</p>')
        t += ["", f"0건 사유: {ctx['note']}", ""]
    w('<h2 style="font-size:16px;margin:24px 0 4px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">출처 정보</h2>')
    w('<ol style="margin:6px 0;padding-left:22px;font-size:13px;color:#4b5563;">')
    t.append("[출처 정보]")
    for i, s in enumerate(sources, 1):
        w(f"<li>{_e(s)}</li>")
        t.append(f"{i}. {s}")
    w("</ol>")
    if not sources:
        t.append("(없음)")
    if ctx.get("corrections"):
        w('<h2 style="font-size:16px;margin:24px 0 4px 0;color:#111827;border-bottom:1px solid #e5e7eb;padding-bottom:4px;">정정</h2>')
        t += ["", "[정정]"]
        for c in ctx["corrections"]:
            line = f"{c.get('event_key', '')} 이전 표현 「{c['before']}」 → 수정 표현 「{c['after']}」 ({c.get('date', '')})"
            w(f'<p style="margin:4px 0;font-size:13px;">{_e(line)}</p>')
            t.append(line)
    w("</div></body></html>")
    out_html, out_text = "\n".join(h) + "\n", "\n".join(_s(x) for x in t) + "\n"
    verify_static(out_html)
    return out_html, out_text


def verify_static(doc):
    low = doc.lower()
    for bad in ("http", "<a ", "<script", "<img", "<link", "<iframe", "@import", "url("):
        if bad in low:
            raise ValueError(f"발송본에 허용되지 않는 요소가 있습니다: {bad}")
