"""구판·신판 수집 결과 비교 도구 시험(tools/compare_runs.py)."""
import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import compare_runs as cr  # noqa: E402

from collector import ref  # noqa: E402

WIN = {"window_start": "2026-11-22T00:00:00+09:00", "window_end_exclusive": "2026-11-29T00:00:00+09:00"}


def rec(url, title, publisher, date, status="complete", codes=(), topics=(), agents=("claude",), with_id=True):
    norm, did = ref.document_key(ref.to_ascii_uri(url))
    return {"doc_id": did, "source_url": norm, "normalized_url": norm, "title": title, "publisher": publisher,
            "published_date": date, "status": status, "reason_codes": list(codes), "topic_hints": list(topics),
            "discovery": [{"route": "llm_search", "agent": a, "source_id": None, "query": "q"} for a in agents],
            "body": {"utf8_bytes": 1000}}


def new_doc():
    return dict(WIN, phase="merged", records=[
        rec("https://a.example.org/news/one", "Lam expands fleet analytics at customer fabs", "Lam", "2026-11-24", topics=["T2_EQUIPMENT"]),
        rec("https://b.example.org/post/two", "Robot patrol moves to routine operation", "Robot Fab", "2026-11-25", topics=["T4_PHYSICAL_AI"]),
        rec("https://c.example.org/x/three", "Paywalled analysis of yield", "C News", "2026-11-26", status="drop", codes=["PAYWALL"]),
        rec("https://d.example.org/four", "Equipment maker reports monitoring results", "Equip", "2026-11-27", status="partial", codes=["BODY_TRUNCATED"]),
        rec("https://e.example.org/only-new", "A brand new article found only now", "E News", "2026-11-23", agents=("gpt", "gemini"),
            topics=["T1_CHIPMAKER"]),
        rec("https://f.example.org/title-match-new", "Completely different slug but same headline words here", "F", "2026-11-24"),
    ])


def old_records():
    return {"records": [rec("https://a.example.org/news/one", "x", "Lam", "2026-11-24")]}      # doc_id 일치용


def test_loader_shapes():
    d = {"records": [{"doc_id": "url1_x", "source_url": "https://a.example.org/1", "title": "t", "publisher": "p", "published_date": "2026-11-24",
                      "status": "complete", "reason_codes": [], "topic_hints": ["T1_CHIPMAKER"], "body": {"utf8_bytes": 5}}]}
    shape, items, _ = cr.extract(d)
    assert shape.startswith("records[]") and items[0]["doc_id"] == "url1_x" and items[0]["bytes"] == 5 and items[0]["topics"] == ["T1_CHIPMAKER"]
    shape, items, _ = cr.extract({"articles": [{"link": "https://a.example.org/2", "headline": "H", "source": {"name": "Src"}, "date": "2026-11-25T10:00:00Z"}]})
    assert shape == "articles[]" and items[0]["title"] == "H" and items[0]["publisher"] == "Src" and items[0]["date"] == "2026-11-25"
    shape, items, _ = cr.extract({"events": [{"primary_topic": "T2", "sources": [{"url": "https://a.example.org/3", "title": "E", "publisher": "P", "date": "2026-11-26"}]}]})
    assert shape == "events[].sources[]" and items[0]["topics"] == ["T2"]
    shape, items, _ = cr.extract([{"url": "https://a.example.org/4", "title": "L"}, {"nothing": 1}])
    assert shape.startswith("객체 목록") and len(items) == 1
    shape, items, w = cr.extract({"meta": {"x": 1}, "weird": {"deep": [{"normalized_url": "https://a.example.org/5", "name": "N"}]}, **{"window_start": "2026-11-22T00:00:00+09:00", "window_end_exclusive": "2026-11-29T00:00:00+09:00"}})
    assert shape.startswith("알 수 없는 구조") and len(items) == 1 and items[0]["title"] == "N" and w == ("2026-11-22", "2026-11-29")
    shape, items, _ = cr.extract({"articles": []})      # 알려진 키가 비어 있으면 재귀 탐색으로
    assert shape.startswith("알 수 없는 구조") and items == []


def test_match_methods_and_one_to_one():
    old = [
        {"doc_id": new_doc()["records"][0]["doc_id"], "url": "https://zzz.example.net/other", "title": None},          # doc_id 일치
        {"doc_id": None, "url": "https://b.example.org/post/two?utm_source=x", "title": "q"},                          # 정규화 일치
        {"doc_id": None, "url": "http://www.c.example.org/x/three/", "title": "q"},                                   # 표기 차이
        {"doc_id": None, "url": "https://nowhere.example.org/a", "title": "Equipment Maker reports monitoring results!"},  # 제목 유사
        {"doc_id": None, "url": "https://nowhere.example.org/b", "title": "Equipment maker reports monitoring results"},   # 이미 쓴 상대 → 매칭 안 됨
        {"doc_id": None, "url": "https://nowhere.example.org/c", "title": "unrelated"},
    ]
    _, new, _ = cr.extract(new_doc())
    pairs = cr.match(old, new)
    assert pairs[0] == (0, "doc_id") and pairs[1] == (1, "URL 정규화 일치") and pairs[2] == (2, "URL 표기 차이")
    assert pairs[3][1] == "제목 유사" and sorted(i for i, _ in pairs.values()).count(3) == 1
    used = [i for i, _ in pairs.values()]
    assert len(set(used)) == len(used) and 4 not in used and 5 not in used     # 각 구판 항목은 한 번만, 같은 제목의 두 번째는 남는다


def write(tmp, name, obj):
    p = tmp / name
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_report_reasons_gz_and_json(tmp_path, capsys):
    old = {"articles": [
        {"url": "https://a.example.org/news/one", "title": "Lam", "publisher": "Lam", "date": "2026-11-24"},                     # 매칭, complete
        {"url": "https://c.example.org/x/three", "title": "Paywalled", "publisher": "C News", "date": "2026-11-26"},             # 매칭, drop
        {"url": "https://d.example.org/four", "title": "Equip", "publisher": "Equip", "date": "2026-11-27"},                    # 매칭, partial
        {"url": "https://old.example.org/a", "title": "Too early story", "publisher": "Old", "date": "2026-11-01"},             # 기간 이전
        {"url": "https://old.example.org/b", "title": "Too late story", "publisher": "Old", "date": "2026-12-05"},              # 기간 이후
        {"url": "https://old.example.org/c", "title": "Never found story", "publisher": "Old2", "date": "2026-11-25"},          # 후보 미발견
        {"url": "https://old.example.org/d", "title": "No date story", "publisher": "Old2"},                                     # 날짜 없음 → 후보 미발견
    ]}
    op = tmp_path / "old.json.gz"
    op.write_bytes(gzip.compress(json.dumps(old).encode()))
    npth = write(tmp_path, "new.json", new_doc())
    out, js = tmp_path / "r.md", tmp_path / "s.json"
    assert cr.main([str(op), str(npth), "--out", str(out), "--json", str(js)]) == 0
    md = out.read_text(encoding="utf-8")
    s = json.loads(js.read_text(encoding="utf-8"))
    assert s["old_total"] == 7 and s["new_total"] == 6 and s["old_shape"] == "articles[]" and s["new_shape"].startswith("records[]")
    assert s["matched"] == {"URL 정규화 일치": 3} and s["only_old"] == 4 and s["matched_not_complete"] == 2 and s["only_new"] == 3
    assert s["only_old_reasons"] == {"new 쪽 drop (PAYWALL)": 1, "new 쪽 partial (BODY_TRUNCATED)": 1, "OUT_OF_WINDOW 가능": 2, "후보 미발견": 2}
    assert "# 수집 결과 비교" in md and "OUT_OF_WINDOW 가능" in md and "후보 미발견" in md and "new 쪽 drop (PAYWALL)" in md
    assert "gemini, gpt" in md and "T1_CHIPMAKER" in md and "기간 이전" in md and "2026-11-22 이상 ~ 2026-11-29 미만" in md
    assert "| 발행처 | 구판 | 신판 |" in md


def test_explicit_window_overrides_and_unknown_old_shape(tmp_path, capsys):
    old = {"x": {"y": [{"link": "https://old.example.org/a", "title": "Story", "date": "2026-11-10"}]}}
    op, npth = write(tmp_path, "o.json", old), write(tmp_path, "n.json", {"records": []})
    md, s = cr.run(str(op), str(npth), "2026-11-09T00:00:00+09:00", "2026-11-16T00:00:00+09:00")
    assert s["old_shape"].startswith("알 수 없는 구조") and s["window"] == ["2026-11-09", "2026-11-16"]
    assert s["only_old_reasons"] == {"후보 미발견": 1}          # 기간 안이므로 OUT_OF_WINDOW가 아님
    md, s = cr.run(str(op), str(npth))                              # 신판에 기간이 없으면 날짜 판정을 생략
    assert s["window"] is None and s["only_old_reasons"] == {"후보 미발견": 1}


def test_unreadable_input_returns_zero(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cr.main([str(bad), str(bad)]) == 0 and "읽을 수 없습니다" in capsys.readouterr().out
