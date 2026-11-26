"""v4.2 → v4.6 이관 도구 시험(합성 입력)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import migrate_v42 as mg  # noqa: E402

from collector import ref  # noqa: E402
from collector.settings import load_settings  # noqa: E402
from collector.validate import check  # noqa: E402


def old_rec(url, title, date, publisher="발행처", topics=("equipment",), blocks=None, bad_id=False):
    norm, did = ref.document_key(ref.to_ascii_uri(url))
    blocks = blocks or [{"id": "P1", "kind": "paragraph", "text": "  첫 문단입니다.   " + title + "가" * 120, "page": None},
                        {"id": "P2", "kind": "paragraph", "text": "둘째 문단입니다. " + "나" * 120, "page": None},
                        {"id": "P3", "kind": "table", "text": "a | b\r\n1 | 2", "page": None}]
    return {"doc_id": "url1_" + "0" * 64 if bad_id else did, "source_url": url, "normalized_url": norm, "title": title,
            "publisher": publisher, "published_date": date, "published_at": None, "change_at": None, "change_note": None,
            "event_key": "ev-1", "language": "ko", "track": "A", "topic_hints": list(topics), "source_tier": "primary",
            "rights": {"status": "allowed"}, "body": {"scope": "article", "extent": "full", "scope_note": "원 범위", "blocks": blocks,
                                                     "lead_ids": ["P1"], "utf8_bytes": 1, "sha256": "0" * 64},
            "status": "complete", "reason_codes": [],
            "attempts": [{"collector": "gpt", "at": "2026-11-26T09:00:00+09:00", "url": norm, "observation": "조회", "pass": 1}]}


def test_migrate_rules():
    st = load_settings()
    old = {"schema_version": "4.2", "phase": "intake", "paid_excluded": 0, "records": [
        old_rec("https://a.go.kr/x", "표준 문서", "2026-11-18", publisher="국가기술표준원"),
        old_rec("https://b.go.kr/y", "정부 문서", "2026-11-18", publisher="산업부"),
        old_rec("https://c.europa.eu/z", "EU 문서", "2026-11-19", topics=("chipmaker", "physical_ai")),
        old_rec("https://d.example.org/old", "기간 밖", "2026-11-01"),
        old_rec("https://e.example.org/bad", "id 불일치", "2026-11-18", bad_id=True)],
        "resurvey": {"before_count": 2, "after_count": 3, "topics_checked": {"chipmaker": "칩", "equipment": "장비", "solution": "솔루션", "physical_ai": "로봇"},
                     "report": "2건 → 3건"}}
    merged, intake, rows, aborted, extra = mg.migrate(old, "2026-W48", st, now="2026-11-26T10:00:00+09:00")
    for doc in (merged, intake):
        errs, _ = check(doc, 40, 10)
        assert errs == [], errs
    assert merged["window_start"] == "2026-11-15T00:00:00+09:00" and merged["window_weeks"] == 1
    assert merged["summary"]["records"] == 4 and merged["summary"]["drop"] == 1 and intake["summary"]["complete"] == 3
    assert len(aborted) == 1 and "다름" in aborted[0]["note"]
    by = {r["title"]: r for r in merged["records"]}
    assert by["기간 밖"]["status"] == "drop" and by["기간 밖"]["reason_codes"] == ["OUT_OF_WINDOW"]
    assert "rights" not in by["표준 문서"] and by["표준 문서"]["author"] is None and by["표준 문서"]["event_key"] is None
    assert by["표준 문서"]["source_type"] == "standards_body" and by["정부 문서"]["source_type"] == "other" and by["EU 문서"]["source_type"] == "other"
    assert by["EU 문서"]["topic_hints"] == ["T1_CHIPMAKER", "T4_PHYSICAL_AI"]
    r = by["표준 문서"]
    assert r["discovery"] == [{"route": "llm_search", "agent": "gpt", "source_id": None, "query": None}]
    assert r["body"]["extractor"] == "legacy v4.2 (gpt)" and r["body"]["scope_note"].endswith("; v4.2 이관(원문 대조 미검증)")
    assert r["body"]["blocks"][0]["text"].startswith("첫 문단입니다.") and r["body"]["blocks"][2]["text"] == "a | b\n1 | 2"
    assert [b["id"] for b in r["body"]["blocks"]] == ["P1", "P2", "T1"] and r["body"]["lead_ids"] == ["P1", "P2"]
    obs = [a["observation"] for a in r["attempts"]]
    assert obs[0].startswith("[v4.2 이관, 원 수집자 gpt] 조회") and obs[-1].startswith("v4.2 → v4.6 이관") and {a["agent"] for a in r["attempts"]} == {"manual"}
    rs = intake["resurvey"]
    assert rs["after_count"] == 3 and rs["before_count"] == 2 and rs["report"] == "[v4.2] 2건 → 3건"
    assert [t["topic"] for t in rs["topics_checked"]] == ["T1_CHIPMAKER", "T2_EQUIPMENT", "T3_SOLUTION", "T4_PHYSICAL_AI"]
    assert rs["topics_checked"][0]["method"] == "v4.2 기록" and rs["topics_checked"][0]["result"] == "칩"
    assert mg.render_report([old], "2026-W48", merged, intake, rows, [], extra)


def _doc(records, revision=1):
    return {"schema_version": "4.2", "phase": "intake", "revision": revision, "paid_excluded": 0, "records": records}


def test_multi_input_merge_alias_and_flags():
    st = load_settings()
    a = old_rec("https://x.example.org/p", "같은 문서", "2026-11-18", publisher="P1")
    b = old_rec("https://x.example.org/p", "같은 문서", "2026-11-18", publisher="P1")
    b["attempts"] = [{"collector": "gemini", "at": "2026-11-26T10:00:00+09:00", "url": b["normalized_url"], "observation": "두 번째", "pass": 1}]
    longer = old_rec("https://x.example.org/q", "본문 다름", "2026-11-18", publisher="P1")
    shorter = old_rec("https://x.example.org/q", "본문 다름", "2026-11-18", publisher="P1",
                      blocks=[{"id": "P1", "kind": "paragraph", "text": "짧은 본문 " + "다" * 150, "page": None}])
    # 별칭: 같은 발행처·같은 본문, 한쪽은 쿼리가 붙은 URL / 해시가 다르면 중복 의심
    s1 = old_rec("https://news.syn.example.com/2026-syn", "Syn at SC", "2026-11-16", publisher="Syn")
    s2 = old_rec("https://news.syn.example.com/2026-syn?asPDF=1", "Syn at SC", "2026-11-16", publisher="Syn")
    for r in (s1, s2):
        r["body"]["blocks"] = [{"id": "P1", "kind": "paragraph", "text": "동일 본문입니다. " + "라" * 250, "page": None}]
    d1 = old_rec("https://y.example.org/a", "중복 의심 제목", "2026-11-17", publisher="Q", blocks=[{"id": "P1", "kind": "paragraph", "text": "하나 " + "마" * 250, "page": None}])
    d2 = old_rec("https://y.example.org/a?x=1", "중복 의심 제목", "2026-11-17", publisher="Q", blocks=[{"id": "P1", "kind": "paragraph", "text": "둘 " + "바" * 250, "page": None}])
    pdf = old_rec("https://z.example.org/files/report.pdf", "PDF 요약", "2026-11-18", publisher="Z", blocks=[{"id": "P1", "kind": "paragraph", "text": "요약 " + "사" * 600, "page": None}])
    tp = old_rec("https://third.example.org/n", "Lam Research 소식을 전한 제3자", "2026-11-18", publisher="CASRAI")
    olds = [_doc([a, longer, s1, d1, pdf, tp], 1), _doc([b, shorter, s2, d2], 5)]
    merged, intake, rows, aborted, extra = mg.migrate(olds, "2026-W48", st, now="2026-11-26T10:00:00+09:00")
    for doc in (merged, intake):
        assert check(doc, 40, 10)[0] == []
    urls = [r["source_url"] for r in merged["records"]]
    assert urls.count("https://x.example.org/p") == 1 and len(merged["records"]) == 7      # 같은 doc_id는 하나
    p = next(r for r in merged["records"] if r["source_url"].endswith("/p"))
    assert sorted(x["observation"][:20] for x in p["attempts"] if "두 번째" in x["observation"]) and len(p["attempts"]) == 3
    q = next(r for r in merged["records"] if r["source_url"].endswith("/q"))
    assert q["body"]["blocks"][0]["text"].startswith("첫 문단입니다") and any("본문이 달라" in n for r in rows for n in r["merge_notes"])
    syn = [r for r in merged["records"] if "syn.example.com" in r["source_url"]]
    assert len(syn) == 1 and syn[0]["source_url"] == "https://news.syn.example.com/2026-syn"      # 짧은 URL이 대표
    assert any("asPDF=1" in x["url"] or "별칭 URL:" in x["observation"] for x in syn[0]["attempts"])
    assert len(extra["alias_notes"]) == 1 and extra["suspects"] and "y.example.org/a" in extra["suspects"][0][0] + extra["suspects"][0][1]
    flags = {r["title"]: (b, sr) for r, b, sr in extra["fidelity"]}
    assert flags["PDF 요약"][0] and any("PDF" in x for x in flags["PDF 요약"][0])
    assert any("본문" in x for x in flags["Syn at SC"][0]) or flags["Syn at SC"][0] == []
    assert any("other" in x for x in flags["PDF 요약"][1])
    text = mg.render_report(olds, "2026-W48", merged, intake, rows, [], extra)
    for h in ("## 본문 충실도 점검", "## 별칭 통합(4.4)", "## 중복 의심", "## 출처 확인 필요", "## 입력 병합(doc_id)"):
        assert h in text
