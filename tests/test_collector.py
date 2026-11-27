"""오프라인 통합 시험: 로컬 HTTP 서버로 피드·기사·PDF를 흉내 내고 수집 전 과정을 확인한다.
실행: python -m pytest -q tests"""
import copy
import gzip
import json
import shutil
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from collector import ref
from collector.pipeline import package, run
from collector.settings import ROOT, load_settings
from collector.validate import check

WEEK = "2026-W49"   # 기간 2026-11-22 ~ 2026-11-29 (KST)
LONG_EN = ("Lam Research said its fleet analytics are now running in production across customer fabs, "
           "cutting chamber matching time for etch tools and flagging drift before it affects yield. ")
LONG_KO = ("예시반도체는 서브팹 순찰 자율이동로봇의 운영 범위를 1개 라인에서 2개 라인으로 확대했다고 밝혔다. "
           "로봇은 배관 누설과 펌프 이상음을 감지하면 보전 담당자에게 점검 요청을 자동으로 등록한다. ")


def article(title, date_published=None, body="", lang="en", canonical=None, extra_head="", paid=False):
    ld = {"@context": "https://schema.org", "@type": "NewsArticle", "headline": title}
    if date_published:
        ld["datePublished"] = date_published
    if paid:
        ld["isAccessibleForFree"] = False
    can = f'<link rel="canonical" href="{canonical}"/>' if canonical else ""
    return (f'<html lang="{lang}"><head><meta charset="utf-8"><title>{title} | Example</title>{can}{extra_head}'
            f'<meta property="og:site_name" content="Example"/>'
            f'<script type="application/ld+json">{json.dumps(ld)}</script></head>'
            f'<body><nav>Home</nav><article><h1>{title}</h1>{body}</article><footer>c</footer></body></html>')


def paras(text, n=3):
    return "".join(f"<p>{text} Paragraph {i + 1}.</p>" for i in range(n))


def make_pdf(text_lines):
    """reportlab 없이 텍스트 PDF를 만든다."""
    content = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(f"({t}) Tj T*" for t in text_lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            "<< /Title (Fab AI Whitepaper) /CreationDate (D:20261125090000+09'00') >>"]
    out, offs = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for o in offs:
        out += f"{o:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


@pytest.fixture()
def site(tmp_path):
    www = tmp_path / "www"
    (www / "news").mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(www)))
    base = f"http://127.0.0.1:{server.server_address[1]}"
    pages = {
        "news/a1": article("Lam expands fleet AI", "2026-11-24T09:00:00-08:00", paras(LONG_EN), canonical=f"{base}/news/a1"),
        "news/a1-alias": article("Lam expands fleet AI", "2026-11-24T09:00:00-08:00", paras(LONG_EN), canonical=f"{base}/news/a1"),
        "news/ko": article("예시반도체 순찰 로봇 확대", "2026-11-26", paras(LONG_KO), lang="ko"),
        "news/login": article("Members story", "2026-11-25", "<p>짧은 소개만 보입니다.</p><p>로그인 후 전체 기사를 볼 수 있습니다.</p>"),
        "news/old": article("Old story", "2026-11-01T10:00:00+09:00", paras(LONG_EN)),
        "news/future": article("Next week story", "2026-11-29T10:00:00+09:00", paras(LONG_EN)),
        "news/nodate": article("Undated story", None, paras(LONG_EN)),
        "news/paid": article("Paid story", "2026-11-24", paras(LONG_EN), paid=True),
    }
    for k, v in pages.items():
        (www / k).write_text(v, encoding="utf-8")
    (www / "doc.pdf").write_bytes(make_pdf([
        "Fab AI whitepaper describing virtual metrology deployed on etch tools in a production fab.",
        "The deployment covers two lines and reports results measured over eight weeks of operation.",
        "Engineers used the model outputs to schedule chamber maintenance before drift exceeded limits.",
        "This page is part of an offline test fixture for the collector."]))
    rss = ("<?xml version='1.0'?><rss version='2.0'><channel><title>Example</title>"
           f"<item><title>a1</title><link>{base}/news/a1</link><pubDate>Tue, 24 Nov 2026 17:00:00 +0000</pubDate></item>"
           f"<item><title>old</title><link>{base}/news/old-feed</link><pubDate>Sun, 01 Nov 2026 01:00:00 +0000</pubDate></item>"
           f"<item><title>login</title><link>{base}/news/login</link><pubDate>Wed, 25 Nov 2026 01:00:00 +0000</pubDate></item>"
           "</channel></rss>")
    (www / "feed.xml").write_text(rss, encoding="utf-8")
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield base, tmp_path
    server.shutdown()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/news/err500"):
            self.send_response(500); self.end_headers(); return
        p = self.path.split("?", 1)[0]
        f = Path(self.directory) / p.lstrip("/")
        if f.is_file() and not f.suffix:
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        return super().do_GET()


def make_settings(tmp, base):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["paths"] = {"work_dir": "work", "out_dir": "out"}
    cfg["collection"].update(per_host_delay_sec=0, fetch_retries=1, retry_backoff_sec=0, browser_fallback=False)
    src = {"registry_version": "test", "sources": [{
        "id": "example-feed", "publisher": "Example Newsroom", "domains": ["127.0.0.1"], "topics": ["T2_EQUIPMENT"],
        "track": "A", "language": "en", "source_type": "company_official", "source_tier": "primary",
        "method": "rss", "feed_url": f"{base}/feed.xml", "verified": True}]}
    (tmp / "config.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp / "sources.yaml").write_text(yaml.safe_dump(src, allow_unicode=True), encoding="utf-8")
    shutil.copy(ROOT / "queries.yaml", tmp / "queries.yaml")
    shutil.copytree(ROOT / "prompts", tmp / "prompts")
    return load_settings(tmp)


def llm_files(st, base, agent, fence=False):
    d = st.candidates_dir(WEEK)
    d.mkdir(parents=True, exist_ok=True)
    items = [{"url": f"{base}/news/a1?utm_source=llm", "topic_hints": ["T2"], "track": "A", "found_via": "fleet AI"},
             {"url": f"{base}/news/a1-alias", "topic_hints": ["T2"], "track": "A", "found_via": "alias"},
             {"url": f"{base}/news/ko", "topic_hints": ["T4"], "found_via": "순찰 로봇"},
             {"url": f"{base}/news/old"}, {"url": f"{base}/news/future"}, {"url": f"{base}/news/nodate"},
             {"url": f"{base}/news/paid"}, {"url": f"{base}/doc.pdf", "topic_hints": ["T3"]},
             {"url": f"{base}/news/err500"}, {"url": "not a url"}]
    body = json.dumps({"candidates": items}, ensure_ascii=False)
    if fence:  # 모델별 출력 습관: 앞 설명 + 코드 펜스
        body = "다음은 결과입니다.\n```json\n" + body + "\n```\n"
    (d / f"{agent}-T2.json").write_text(body, encoding="utf-8")


def recs(doc):
    return {r["source_url"].split("/", 3)[-1]: r for r in doc["records"]}


def test_reference_vectors():
    assert ref.document_key(ref.to_ascii_uri("HTTPS://EXAMPLE.ORG:443/news?utm_source=a&id=7#top")) == (
        "https://example.org/news?id=7", "url1_e9c75cf2494b1a86e31e072943071761f56805175a3985878dbe401890893717")
    s, e = ref.weekly_window("2026-W49")
    assert (s.isoformat(), e.isoformat()) == ("2026-11-22T00:00:00+09:00", "2026-11-29T00:00:00+09:00")
    s2, _ = ref.weekly_window("2026-W49", weeks=2)
    assert s2.isoformat() == "2026-11-15T00:00:00+09:00"
    assert ref.attribute(None, "2026-11-29", s, e) == "FUTURE"
    assert ref.attribute("2026-11-28T15:30:00Z", None, s, e) == "FUTURE"
    assert ref.attribute(None, "2026-11-22", s, e) == "WINDOW"


def test_full_run(site):
    base, tmp = site
    st = make_settings(tmp, base)
    llm_files(st, base, "claude")
    docs, errors = run(st, WEEK, force=True, log=lambda *a: None)
    assert not any(v[0] for v in errors.values()), errors
    m = recs(docs["merged"])
    assert m["news/a1"]["status"] == "complete"
    # 피드·utm 붙은 URL·canonical 별칭이 한 레코드로 합쳐진다
    assert sum(1 for k in m if k.startswith("news/a1")) == 1
    assert {d["agent"] for d in m["news/a1"]["discovery"]} == {"script", "claude"}
    assert m["news/a1"]["published_date"] == "2026-11-24"   # 발행처 현지 날짜
    assert m["news/ko"]["status"] == "complete" and m["news/ko"]["language"] == "ko"
    assert m["doc.pdf"]["status"] == "complete" and m["doc.pdf"]["body"]["scope"] == "pdf"
    assert m["news/login"]["status"] == "partial" and "ACCESS_BLOCKED" in m["news/login"]["reason_codes"]
    assert m["news/old"]["reason_codes"] == ["OUT_OF_WINDOW"]
    assert m["news/paid"]["reason_codes"] == ["PAYWALL"]
    assert m["news/nodate"]["reason_codes"] == ["DATE_UNCERTAIN"]
    assert m["news/err500"]["reason_codes"] == ["FETCH_FAILED"]
    assert "news/future" not in m          # 다음 주 대상
    assert docs["intake"]["summary"]["complete"] == 3
    assert all(r["status"] == "complete" for r in docs["intake"]["records"])

    # 수동 보완 후 재실행 → r2
    man = st.manual_dir(WEEK)
    login_id = m["news/login"]["doc_id"]
    (man / f"{login_id}.html").write_text(article("Members story", "2026-11-25", paras(LONG_EN)), encoding="utf-8")
    (man / "overrides.yaml").write_text(yaml.safe_dump({m["news/nodate"]["doc_id"]: {
        "published_date": "2026-11-23", "note": "원문 하단 날짜"}}), encoding="utf-8")
    docs2, errors2 = run(st, WEEK, force=True, log=lambda *a: None)
    assert not any(v[0] for v in errors2.values()), errors2
    m2 = recs(docs2["merged"])
    assert docs2["merged"]["revision"] == 2
    assert m2["news/login"]["status"] == "complete"
    assert any(a["agent"] == "manual" for a in m2["news/login"]["attempts"])
    assert m2["news/nodate"]["status"] == "complete"
    assert m2["news/a1"]["body"]["sha256"] == m["news/a1"]["body"]["sha256"]
    files, warns = package(st, WEEK)
    intake = json.loads(gzip.open(files[0], "rt", encoding="utf-8").read())
    errs, _ = check(intake, st.c["extraction"]["lead_min_chars"], st.c["thresholds"]["intake_min"])
    assert not errs and intake["summary"]["complete"] == 5
    assert any("intake_min" in w for w in warns)   # 5건 < 10건 → 재조사 경고


def _strip_volatile(doc):
    d = copy.deepcopy(doc)
    d.pop("generated_at")
    for r in d["records"]:
        r.pop("attempts")
        r.pop("discovery")
    return d


def test_same_result_regardless_of_llm(site):
    """같은 URL을 어느 LLM이 어떤 형식으로 냈든 discovery 외의 결과가 같아야 한다."""
    base, tmp = site
    out = {}
    for agent, fence in (("claude", False), ("gpt", True), ("gemini", True)):
        root = tmp / agent
        root.mkdir()
        st = make_settings(root, base)
        llm_files(st, base, agent, fence)
        docs, errors = run(st, WEEK, force=True, log=lambda *a: None, skip_registry=True)
        assert not any(v[0] for v in errors.values())
        out[agent] = _strip_volatile(docs["merged"])
    assert out["claude"] == out["gpt"] == out["gemini"]


def test_two_week_window(site):
    base, tmp = site
    st = make_settings(tmp, base)
    llm_files(st, base, "claude")
    docs, _ = run(st, WEEK, weeks=2, force=True, log=lambda *a: None, skip_registry=True)
    assert docs["merged"]["window_start"] == "2026-11-15T00:00:00+09:00"
    assert recs(docs["merged"])["news/old"]["reason_codes"] == ["OUT_OF_WINDOW"]   # 11-01은 2주 밖


def test_design_doc_code_matches_reference():
    """설계서 6장의 코드 블록이 collector/ref.py와 글자 그대로 같아야 한다."""
    import re
    doc = (ROOT / "docs" / "MTC-AX-Unified-Design-v4.7.md").read_text(encoding="utf-8")
    src = (ROOT / "collector" / "ref.py").read_text(encoding="utf-8")
    blocks = re.findall(r"~~~python\n(.*?)~~~", doc, re.S)
    assert len(blocks) == 3
    for block in blocks:
        for func in re.split(r"\n(?=def )", block.strip()):
            assert func.strip() in src, func.splitlines()[0]


def test_placeholder_dates_are_rejected_and_fall_through():
    """설계서 4.4: 1970-01-01 같은 사이트 기본값은 버리고 다음 출처로. 남는 출처가 없으면 DATE_UNCERTAIN(수동 보완), OUT_OF_WINDOW가 아니다."""
    from datetime import date
    from collector.extract import html_metadata, plausible_date
    assert not plausible_date("1970-01-01") and not plausible_date("1900-01-01") and not plausible_date("0001-01-01")
    assert plausible_date("2026-11-17", today=date(2026, 11, 27)) and not plausible_date("2026-12-31", today=date(2026, 11, 27))
    html = ('<html lang="en"><head><title>T</title><script type="application/ld+json">'
            '{"@type":"NewsArticle","headline":"T","datePublished":"1970-01-01T08:00:00+08:00"}</script>'
            '<meta property="article:published_time" content="2026-11-17T09:00:00+08:00"></head><body><p>x</p></body></html>')
    m = html_metadata(html, "https://pr.example.com/english/news/3355")
    assert m["published_date"] == "2026-11-17" and m["date_source"] == "article:published_time"
    assert m["date_rejected"] == ["json-ld datePublished=1970-01-01"]
    only_bad = html.replace('<meta property="article:published_time" content="2026-11-17T09:00:00+08:00">', "")
    m2 = html_metadata(only_bad, "https://pr.example.com/english/news/3355")
    assert m2["published_date"] is None and m2["date_rejected"]
