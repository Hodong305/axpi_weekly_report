"""이전 수집 결과(구버전·형식 불명)와 이번 수집기 결과를 비교하는 도구.
  python tools/compare_runs.py OLD.json NEW.json [--out report.md] [--json summary.json]
                               [--window-start ISO --window-end ISO]
구조를 모르는 파일도 읽을 수 있도록, 알려진 형태를 자동 감지하고 아니면 URL 필드가 있는 객체를 재귀로 찾는다. 항상 종료 코드 0."""
import argparse
import difflib
import gzip
import json
import re
import sys
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from collector.ref import document_key, to_ascii_uri  # noqa: E402

URL_FIELDS = ["source_url", "url", "link", "normalized_url", "href"]
TITLE_THRESHOLD = 0.85
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


# 읽기 ------------------------------------------------------------------------

def read_json(path):
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _text(v):
    if isinstance(v, dict):
        v = _first(v, ["name", "title", "publisher", "id"])
    return str(v).strip() if v not in (None, "") else None


def _topics(*vals):
    out = []
    for v in vals:
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            out += [str(x) for x in v if x]
    return list(dict.fromkeys(out))


def to_item(d, inherit=None):
    """객체 하나를 공통 항목으로. URL도 doc_id도 없으면 None."""
    inherit = inherit or {}
    url = _first(d, URL_FIELDS)
    body = d.get("body") if isinstance(d.get("body"), dict) else {}
    ds = _first(d, ["published_date", "date", "published_at", "published", "pub_date", "publishedAt"])
    m = DATE_RE.search(str(ds)) if ds else None
    item = {"doc_id": d.get("doc_id") if isinstance(d.get("doc_id"), str) else None,
            "url": url if isinstance(url, str) else None,
            "title": _text(_first(d, ["title", "headline", "name"])),
            "publisher": _text(_first(d, ["publisher", "source", "outlet", "site"])),
            "date": m.group() if m else None,
            "status": d.get("status") if isinstance(d.get("status"), str) else None,
            "reason_codes": d.get("reason_codes") if isinstance(d.get("reason_codes"), list) else [],
            "topics": _topics(d.get("topic_hints"), d.get("topics"), d.get("topic"), d.get("category"),
                              inherit.get("topics")),
            "agents": sorted({x.get("agent") for x in d.get("discovery", []) if isinstance(x, dict) and x.get("agent")})
            if isinstance(d.get("discovery"), list) else [],
            "bytes": body.get("utf8_bytes")}
    return item if (item["url"] or item["doc_id"]) else None


def _urlish_dicts(node, out, inherit=None):
    if isinstance(node, dict):
        if any(isinstance(node.get(k), str) and node.get(k) for k in URL_FIELDS):
            out.append((node, inherit))
        for v in node.values():
            _urlish_dicts(v, out, inherit)
    elif isinstance(node, list):
        for v in node:
            _urlish_dicts(v, out, inherit)


def extract(doc):
    """반환: (모양 설명, 항목 목록, 문서에 적힌 기간 또는 None)"""
    window = None
    if isinstance(doc, dict) and doc.get("window_start") and doc.get("window_end_exclusive"):
        window = (str(doc["window_start"])[:10], str(doc["window_end_exclusive"])[:10])
    shape, raw = None, []
    if isinstance(doc, dict) and isinstance(doc.get("records"), list):
        shape, raw = "records[] (v4.x 수집 결과)", [(r, None) for r in doc["records"] if isinstance(r, dict)]
    elif isinstance(doc, dict) and isinstance(doc.get("articles"), list):
        shape, raw = "articles[]", [(r, None) for r in doc["articles"] if isinstance(r, dict)]
    elif isinstance(doc, dict) and isinstance(doc.get("events"), list):
        for ev in doc["events"]:
            if isinstance(ev, dict) and isinstance(ev.get("sources"), list):
                inh = {"topics": _topics(ev.get("primary_topic"), ev.get("topic"), ev.get("category"))}
                raw += [(s, inh) for s in ev["sources"] if isinstance(s, dict)]
        if raw:
            shape = "events[].sources[]"
    elif isinstance(doc, list) and doc and all(isinstance(x, dict) for x in doc):
        shape, raw = "객체 목록(bare list)", [(r, None) for r in doc]
    items = [i for i in (to_item(d, inh) for d, inh in raw) if i]
    if not items:
        found = []
        _urlish_dicts(doc, found)
        items = [i for i in (to_item(d, inh) for d, inh in found) if i]
        shape = "알 수 없는 구조(URL 필드가 있는 객체를 재귀로 탐색)"
    return shape, items, window


# 매칭 ------------------------------------------------------------------------

def exact_key(url):
    try:
        return document_key(to_ascii_uri(url))[0]
    except (ValueError, UnicodeError):
        return None


def loose_key(url):
    """표기 차이 허용 키: scheme·www.·끝 슬래시·fragment 무시, 호스트 소문자."""
    try:
        u = urlsplit(to_ascii_uri(url))
    except (ValueError, UnicodeError):
        u = urlsplit(url)
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    path = u.path.rstrip("/")
    return f"{host}{path}" + (f"?{u.query}" if u.query else "")


def norm_title(t):
    t = unicodedata.normalize("NFKC", t or "").casefold()
    return re.sub(r"[\W_]+", "", t)


def match(old, new):
    """반환: {new 인덱스: (old 인덱스, 방법)}. 각 항목은 한 번만 매칭된다."""
    pairs, used_old = {}, set()

    def by_key(method, keyfn):
        idx = {}
        for i, o in enumerate(old):
            k = keyfn(o)
            if k and i not in used_old:
                idx.setdefault(k, []).append(i)
        for j, n in enumerate(new):
            if j in pairs:
                continue
            k = keyfn(n)
            cand = [i for i in idx.get(k, []) if i not in used_old] if k else []
            if cand:
                pairs[j] = (cand[0], method)
                used_old.add(cand[0])
    by_key("doc_id", lambda x: x["doc_id"])
    by_key("URL 정규화 일치", lambda x: exact_key(x["url"]) if x["url"] else None)
    by_key("URL 표기 차이", lambda x: loose_key(x["url"]) if x["url"] else None)
    cands = []
    for j, n in enumerate(new):
        nt = norm_title(n["title"])
        if j in pairs or not nt:
            continue
        for i, o in enumerate(old):
            ot = norm_title(o["title"])
            if i in used_old or not ot:
                continue
            r = difflib.SequenceMatcher(None, nt, ot).ratio()
            if r >= TITLE_THRESHOLD:
                cands.append((r, j, i))
    for r, j, i in sorted(cands, key=lambda c: (-c[0], c[1], c[2])):
        if j not in pairs and i not in used_old:
            pairs[j] = (i, "제목 유사")
            used_old.add(i)
    return pairs


# 보고서 ----------------------------------------------------------------------

def where(d, window):
    if not d or not window:
        return "알 수 없음"
    return "기간 이전" if d < window[0] else ("기간 이후" if d >= window[1] else "기간 안")


def analyze(old, new, window):
    pairs = match(old, new)
    matched_old = {i for i, _ in pairs.values()}
    methods = Counter(m for _, m in pairs.values())
    only_old = []
    for i, o in enumerate(old):
        if i in matched_old:
            n = new[next(j for j, (oi, _) in pairs.items() if oi == i)]
            if n["status"] in (None, "complete"):
                continue
            guess = f"new 쪽 {n['status']}" + (f" ({', '.join(n['reason_codes'])})" if n["reason_codes"] else "")
            only_old.append((o, guess, "매칭됨·complete 아님"))
        else:
            w = where(o["date"], window)
            guess = "OUT_OF_WINDOW 가능" if w in ("기간 이전", "기간 이후") else "후보 미발견"
            only_old.append((o, guess, "매칭 안 됨"))
    only_new = [n for j, n in enumerate(new) if j not in pairs]
    status = Counter()
    for j, (i, m) in pairs.items():
        n = new[j]
        status[(n["status"] or "상태 없음") + (f" [{', '.join(n['reason_codes'])}]" if n["reason_codes"] else "")] += 1
    return {"pairs": pairs, "methods": methods, "only_old": only_old, "only_new": only_new, "status": status}


def _count(items, key):
    c = Counter()
    for it in items:
        vals = key(it) or ["(없음)"]
        c.update(vals)
    return c


def _table(rows, head):
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in rows]
    return out


def side_table(title, old, new, key):
    a, b = _count(old, key), _count(new, key)
    keys = sorted(set(a) | set(b), key=lambda k: (-(a[k] + b[k]), k))
    return [f"### {title}", ""] + _table([(k, a[k], b[k]) for k in keys], [title.split(" ")[-1], "구판", "신판"]) + [""]


def report(old_path, new_path, old_shape, new_shape, old, new, window, res):
    L = [f"# 수집 결과 비교", "",
         f"- 구판: `{old_path}` — 형태 {old_shape}, 항목 {len(old)}건",
         f"- 신판: `{new_path}` — 형태 {new_shape}, 항목 {len(new)}건",
         f"- 기준 기간: " + (f"{window[0]} 이상 ~ {window[1]} 미만" if window else "알 수 없음(날짜 판정 생략)"), "",
         "## 요약", ""]
    m = res["methods"]
    L += _table([("구판 전체", len(old)), ("신판 전체", len(new)), ("doc_id 일치", m["doc_id"]),
                 ("URL 정규화 일치", m["URL 정규화 일치"]), ("URL 표기 차이", m["URL 표기 차이"]), ("제목 유사(≥0.85)", m["제목 유사"]),
                 ("매칭 합계", len(res["pairs"])),
                 ("구판에만 있음(매칭 안 됨)", sum(1 for x in res["only_old"] if x[2] == "매칭 안 됨")),
                 ("매칭됐으나 신판에서 complete 아님", sum(1 for x in res["only_old"] if x[2] != "매칭 안 됨")),
                 ("신판에만 있음", len(res["only_new"]))], ["항목", "건수"]) + [""]
    L += ["## 매칭된 항목의 신판 상태", "", "intake에 없는 이유를 설명한다(complete가 아니면 사내로 넘어가지 않는다).", ""]
    L += _table(sorted(res["status"].items(), key=lambda kv: -kv[1]), ["상태 [사유 코드]", "건수"]) if res["status"] else ["(매칭 없음)"]
    L += ["", "## 구판에만 있는 항목과 추정 사유", ""]
    if res["only_old"]:
        L += _table([(o["date"] or "-", o["publisher"] or "-", (o["title"] or o["url"] or "-")[:70], kind, g)
                     for o, g, kind in res["only_old"]], ["날짜", "발행처", "제목", "구분", "추정 사유"])
    else:
        L.append("(없음)")
    L += ["", "## 신판에만 있는 항목", ""]
    if res["only_new"]:
        L += _table([(n["date"] or "-", n["publisher"] or "-", (n["title"] or n["url"] or "-")[:70], n["status"] or "-",
                      ", ".join(n["agents"]) or "-", ", ".join(n["topics"]) or "-") for n in res["only_new"]],
                    ["날짜", "발행처", "제목", "상태", "발견 주체", "분야 힌트"])
    else:
        L.append("(없음)")
    L += ["", "## 분야별·발행처별 건수", ""]
    L += side_table("분야", old, new, lambda i: i["topics"])
    L += side_table("발행처", old, new, lambda i: [i["publisher"]] if i["publisher"] else [])
    L += ["### 날짜 분포(기간 대비)", ""]
    co, cn = Counter(where(i["date"], window) for i in old), Counter(where(i["date"], window) for i in new)
    L += _table([(k, co[k], cn[k]) for k in ["기간 이전", "기간 안", "기간 이후", "알 수 없음"]], ["위치", "구판", "신판"])
    return "\n".join(L) + "\n"


def run(old_path, new_path, window_start=None, window_end=None):
    od, nd = read_json(old_path), read_json(new_path)
    old_shape, old, ow = extract(od)
    new_shape, new, nw = extract(nd)
    window = (window_start[:10], window_end[:10]) if window_start and window_end else (nw or ow)
    res = analyze(old, new, window)
    md = report(old_path, new_path, old_shape, new_shape, old, new, window, res)
    summary = {"old_shape": old_shape, "new_shape": new_shape, "old_total": len(old), "new_total": len(new),
               "window": list(window) if window else None, "matched": dict(res["methods"]), "matched_total": len(res["pairs"]),
               "only_old": sum(1 for x in res["only_old"] if x[2] == "매칭 안 됨"),
               "matched_not_complete": sum(1 for x in res["only_old"] if x[2] != "매칭 안 됨"),
               "only_new": len(res["only_new"]), "new_status": dict(res["status"]),
               "only_old_reasons": dict(Counter(g for _, g, _ in res["only_old"]))}
    return md, summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="이전 수집 결과와 새 수집기 결과 비교")
    ap.add_argument("old")
    ap.add_argument("new")
    ap.add_argument("--out", help="보고서(Markdown) 저장 경로. 없으면 화면에 출력")
    ap.add_argument("--json", help="기계가 읽는 요약 JSON 저장 경로")
    ap.add_argument("--window-start", help="기준 기간 시작(ISO). 없으면 신판 파일의 값")
    ap.add_argument("--window-end", help="기준 기간 끝(미포함, ISO)")
    a = ap.parse_args(argv)
    try:
        md, summary = run(a.old, a.new, a.window_start, a.window_end)
    except (OSError, ValueError) as e:
        print(f"파일을 읽을 수 없습니다: {e}")
        return 0
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"보고서: {a.out}")
    else:
        print(md)
    if a.json:
        Path(a.json).write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"요약 JSON: {a.json}")
    print(f"구판 {summary['old_total']}건({summary['old_shape']}) / 신판 {summary['new_total']}건({summary['new_shape']}) / "
          f"매칭 {summary['matched_total']}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
