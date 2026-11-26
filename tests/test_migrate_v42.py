"""v4.2 → v4.5 이관 도구 시험(합성 입력)."""
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
    assert obs[0].startswith("[v4.2 이관, 원 수집자 gpt] 조회") and obs[-1].startswith("v4.2 → v4.5 이관") and {a["agent"] for a in r["attempts"]} == {"manual"}
    rs = intake["resurvey"]
    assert rs["after_count"] == 3 and rs["before_count"] == 2 and rs["report"] == "[v4.2] 2건 → 3건"
    assert [t["topic"] for t in rs["topics_checked"]] == ["T1_CHIPMAKER", "T2_EQUIPMENT", "T3_SOLUTION", "T4_PHYSICAL_AI"]
    assert rs["topics_checked"][0]["method"] == "v4.2 기록" and rs["topics_checked"][0]["result"] == "칩"
    assert mg.render_report([old], "2026-W48", merged, intake, rows, [], extra)
