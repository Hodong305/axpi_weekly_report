"""경쟁 칩메이커 우선(설계서 v4.7 1장, 4.5, 9.5~9.7): 탐색 프롬프트, coverage, 재조사 순서, 리포트 표, 경쟁사 우선 편성, 검색어."""
import copy
import csv
import json
import re
import shutil
from datetime import date
from pathlib import Path

import yaml

from collector import candidates as cand, report
from collector.competitors import complete_counts, find_company
from collector.settings import load_settings
from internal import checks, draft as draftmod, render, select as sel, settings as settings_mod
from internal.prompts import load_steps

from test_internal import CFG, mk, sample_records
from test_v47 import build, ctx, sample

ROOT = Path(__file__).resolve().parent.parent
ST = load_settings(ROOT)
COMPANIES = ST.queries["competitors"]
START, END = date(2026, 11, 22), date(2026, 11, 29)
ONLY_MFG = "경쟁사 소식이라도 설계·개발이 아니라 제조 현장(팹 운영)의 AI·자동화에 관한 것만 찾는다."


def prompt(topic, **kw):
    return cand.render_prompt(ST, topic, START, END, **kw)


# 사외: 탐색 프롬프트 ----------------------------------------------------------------

def test_every_prompt_has_top_goal_and_manufacturing_only_sentence():
    names = [c["name"] for c in COMPANIES]
    assert len(names) == 15 and "SK hynix" in names and "Rapidus" in names
    for t in ("T1", "T2", "T3", "T4", "B"):
        for resurvey in (False, True):
            p = prompt(t, resurvey=resurvey)
            assert p.startswith("[최우선 목표]") and ONLY_MFG in p and "설계용 AI·EDA" in p
            assert all(n in p.split("\n\n")[0] for n in names)
            assert "{{" not in p
    for t in ("T2", "T3", "T4", "B"):
        assert "[경쟁사별 확인 목록]" not in prompt(t) and "coverage" not in prompt(t)


def test_t1_prompt_lists_every_competitor_with_checks_and_coverage_format():
    p = prompt("T1")
    block = p[p.index("[경쟁사별 확인 목록]"):p.index("[검색어 묶음]")]
    assert ONLY_MFG in block
    for c in COMPANIES:
        assert f". {c['name']}\n" in block
        assert all(ch in block for ch in c["channels"]) and all(q in block for q in c["queries"])
    for mark in "①②③④⑤":
        assert mark in block
    assert "넓게 반환" in block and '"coverage"' in p and "found" in p


def test_competitor_queries_are_manufacturing_only():
    mfg = ("fab", "yield", "defect", "manufactur", "maintenance", "factory", "automation", "inspection", "수율", "제조", "팹", "스마트 팩토리",
           "良率", "智慧製造", "智能制造", "晶圓廠", "晶圆厂", "歩留まり", "製造", "工場", "自動化", "缺陷", "缺陷", "異常検知")
    bad = ("design", "EDA", "설계", "roadmap", "launch", "earnings", "stock", "HBM4", "신제품")
    for c in COMPANIES:
        assert 3 <= len(c["queries"]) <= 5 and c["aliases"] and c["channels"]
        for q in c["queries"]:
            assert any(m.lower() in q.lower() for m in mfg), (c["name"], q)
            assert not any(b.lower() in q.lower() for b in bad), (c["name"], q)
    t1 = ST.queries["topics"]["T1"]
    assert "설계·개발 소식" in t1["scope"] and "R&D" in t1["scope"]
    assert any("경쟁" in q for q in t1["resurvey_queries"])


def test_coverage_is_read_per_file_and_bad_entries_ignored(tmp_path):
    body = {"candidates": [{"url": "https://example.com/a"}],
            "coverage": [{"company": "SK hynix", "checked": ["뉴스룸", "DigiTimes"], "found": 2}, {"company": ""}, "x",
                         {"company": "TSMC", "checked": "없음", "found": "많음"}]}
    (tmp_path / "claude-T1.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "gpt-T2.json").write_text(json.dumps({"candidates": []}), encoding="utf-8")
    cands, logs = cand.load_candidate_files(tmp_path)
    assert len(cands) == 1
    by = {l["file"]: l for l in logs}
    assert by["claude-T1.json"]["coverage"] == [{"company": "SK hynix", "checked": ["뉴스룸", "DigiTimes"], "found": 2},
                                                 {"company": "TSMC", "checked": [], "found": None}]
    assert by["gpt-T2.json"]["coverage"] == []


def rec(publisher, title, url, status="complete"):
    return {"status": status, "publisher": publisher, "title": title, "source_url": url}


def test_alias_matching_is_case_insensitive_and_word_bounded():
    assert find_company(COMPANIES, "sk hynix newsroom") == "SK hynix" and find_company(COMPANIES, "SK하이닉스 뉴스룸") == "SK hynix"
    assert find_company(COMPANIES, "台積電 智慧製造") == "TSMC" and find_company(COMPANIES, "Umcoin news") is None
    counts = complete_counts(COMPANIES, [rec("TSMC", "x", "https://pr.tsmc.com/a"), rec("Example", "Micron fab AI", "https://e.com/a"),
                                         rec("Example", "y", "https://news.skhynix.com/a"), rec("SK hynix", "z", "https://x.com", "partial")])
    assert counts["TSMC"] == 1 and counts["Micron"] == 1 and counts["SK hynix"] == 0


def test_report_table_marks_zero_complete_and_unchecked():
    recs = [rec("TSMC", "TSMC fab AI", "https://pr.tsmc.com/a")]
    logs = [{"file": "claude-T1.json", "agent": "claude", "topic": "T1", "coverage": [
        {"company": "tsmc", "checked": ["뉴스룸"], "found": 3}, {"company": "Micron", "checked": [], "found": 0}]}]
    text = "\n".join(report.coverage_table(ST, recs, logs))
    assert text.startswith("## 경쟁사 탐색 범위")
    line = {l.split("|")[1].strip(): l for l in text.splitlines() if l.startswith("| ") and "---" not in l}
    assert "확인 · 후보 3" in line["TSMC"] and "재조사 대상" not in line["TSMC"]
    assert "미확인" in line["Micron"] and "재조사 대상" in line["Micron"]
    assert "재조사 대상" in line["SK hynix"] and "- 재조사 대상:" in text and "SK hynix" in text.split("- 재조사 대상:")[1]
    # coverage가 전혀 없으면 확인 여부는 판단하지 않고 complete 0인 회사만 표시한다
    text2 = "\n".join(report.coverage_table(ST, recs, []))
    assert "coverage가 없어" in text2
    line2 = {l.split("|")[1].strip(): l for l in text2.splitlines() if l.startswith("| ") and "---" not in l}
    assert "재조사 대상" not in line2["TSMC"] and "재조사 대상" in line2["Micron"]


def test_resurvey_t1_lists_zero_complete_competitors_first(tmp_path):
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    for f in ("config.yaml", "sources.yaml", "queries.yaml"):
        shutil.copy(ROOT / f, tmp_path / f)
    st = load_settings(tmp_path)
    assert cand.zero_complete_companies(st, "2026-W49") is None          # 결과가 없으면 전체 목록 그대로
    out = st.out_dir("2026-W49")
    out.mkdir(parents=True)
    done = [rec("TSMC", "TSMC fab AI", "https://pr.tsmc.com/a"), rec("Example", "Micron fab", "https://e.com/m")]
    (out / "mtc-ax-2026W49-merged-r1.json").write_text(json.dumps({"records": done}), encoding="utf-8")
    zero = cand.zero_complete_companies(st, "2026-W49")
    assert "TSMC" not in zero and "Micron" not in zero and "SK hynix" in zero and len(zero) == 13
    p = cand.render_prompt(st, "T1", START, END, True, zero)
    block = p[p.index("[경쟁사별 확인 목록]"):p.index("[검색어 묶음]")]
    order = [m for m in re.findall(r"^\d+\. (.+?)(?: ★재조사 우선)?$", block, re.M)]
    assert order[:13] == zero and order[13:] == ["Micron", "TSMC"] and "★재조사 우선" in block
    assert "complete가 0건이던 회사" in block and ONLY_MFG in p


def test_combine_footer_keeps_coverage_inside_t1(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "tools"))
    import combine_prompts as cp
    assert "coverage" in cp.FOOTER and '"T1"' in cp.FOOTER
    monkeypatch.setattr(cp, "ROOT", tmp_path)
    ans = tmp_path / "a.txt"
    ans.write_text(json.dumps({"agent": "ChatGPT", "T1": {"candidates": [{"url": "https://e.com/a"}],
                                                          "coverage": [{"company": "TSMC", "checked": ["뉴스룸"], "found": 1}]}}), encoding="utf-8")
    cp.split("2026-W01", [str(ans)], None, False)
    saved = json.loads((tmp_path / "work/2026-W01/candidates/gpt-T1.json").read_text(encoding="utf-8"))
    assert saved["coverage"][0]["company"] == "TSMC"
    _, logs = cand.load_candidate_files(tmp_path / "work/2026-W01/candidates")
    assert logs[0]["coverage"][0]["found"] == 1


# 사내: 경쟁사 우선 편성 --------------------------------------------------------------

def comp(ev, subject="SK하이닉스"):
    ev["subject"] = subject
    return ev


def test_config_v9_keys():
    assert settings_mod.load().config["config_version"] == "10"
    assert (CFG["core_gap"], CFG["core_min_count"], CFG["core_alert_count"], CFG["competitor_core_min"], CFG["brief_max"],
            CFG["search_terms_max"], CFG["issue_body_max"]) == (10, 4, 8, 2, 4, 3, 9000)
    assert "core_max" not in CFG
    assert "core3_min_score" not in CFG and {c["name"] for c in CFG["competitors"]} == {c["name"] for c in COMPANIES}


def test_competitor_below_cut_is_added_to_core_minimum():
    evs = [mk("a", 95), mk("b", 94), mk("c", 93), mk("d", 92), comp(mk("k1", 82), "SK hynix"), comp(mk("k2", 81), "tsmc"), mk("x", 80)]
    sel.select(evs, CFG)
    s = {e["event_id"]: e["slot"] for e in evs}
    assert [s[k] for k in ("a", "b", "c", "d", "k1", "k2")] == ["core"] * 6 and s["x"] == "brief"           # 컷(92→82) 아래 경쟁사 2건을 더한다
    assert [e["event_id"] for e in sorted(evs, key=lambda e: e["rank"] or 99) if e["slot"] == "core"] == ["a", "b", "c", "d", "k1", "k2"]
    evs = [mk("a", 99), mk("b", 98), comp(mk("k1", 97), "SK hynix"), mk("c", 87), comp(mk("k2", 86), "tsmc")]
    sel.select(evs, CFG)
    assert _core(evs) == ["a", "b", "k1", "c", "k2"]                                                         # 최소 4건 + 그 뒤 낙차 없음 → 전부
    evs = [mk("a", 99), mk("b", 98), mk("c", 97), comp(mk("k1", 96), "SK hynix"), mk("d", 95), mk("e", 85), comp(mk("k2", 84), "tsmc")]
    sel.select(evs, CFG)
    assert _core(evs) == ["a", "b", "c", "k1", "d", "k2"] and {x["event_id"]: x["slot"] for x in evs}["e"] == "brief"   # 컷(95→85) 안 경쟁사 1건 + 아래 1건


def test_competitor_below_core_min_is_not_forced_and_one_competitor_only():
    evs = [mk("a", 95), mk("b", 94), mk("c", 93), comp(mk("k1", 70), "Micron"), mk("d", 92)]
    sel.select(evs, CFG)
    s = {e["event_id"]: e["slot"] for e in evs}
    assert s["k1"] == "brief" and [s[x] for x in "abcd"] == ["core"] * 4                                       # 핵심 자격 미달이면 우선하지 않는다
    evs = [mk("a", 95), mk("b", 94), mk("c", 93), mk("d", 92), comp(mk("k1", 82), "Micron")]
    sel.select(evs, CFG)
    s = {e["event_id"]: e["slot"] for e in evs}
    assert s["k1"] == "core" and sum(v == "core" for v in s.values()) == 5 and s["d"] == "core"              # 최소 2건 중 자격 있는 1건만 더한다


def _core(evs):
    return [e["event_id"] for e in sorted(evs, key=lambda e: e["rank"] or 99) if e["slot"] == "core"]


def test_core_and_brief_maximums_and_brief_cap_events_never_core():
    evs = [mk(f"k{i}", 90 - i) for i in range(6)] + [mk(f"e{i}", 70 - i) for i in range(8)]
    for i, e in enumerate(evs[:6]):                                # 같은 주체 한도(2)를 피하려고 서로 다른 경쟁사로 나눈다
        e["subject"] = ["Kioxia", "SanDisk", "CXMT", "YMTC", "Nanya", "Winbond"][i]
    sel.select(evs, CFG)
    assert sum(e["slot"] == "core" for e in evs) == 6 and sum(e["slot"] == "brief" for e in evs) == 4      # 핵심은 낙차가 없으면 상한 없이 전부, 단신은 최대 4
    capped = [comp(mk("c1", 95, cap="brief"), "TSMC"), comp(mk("c2", 90, cap="brief"), "UMC")]
    sel.select(capped, CFG)
    assert all(e["slot"] == "brief" for e in capped)


def test_tie_break_prefers_competitor_and_flags_tie():
    evs = [mk("a", 70), comp(mk("b", 70), "UMC"), mk("c", 70), mk("d", 70), mk("e", 70)]
    sel.select(evs, CFG)
    s = {e["event_id"]: e["slot"] for e in evs}
    assert s["b"] == "brief" and s["e"] == "out" and next(e for e in evs if e["event_id"] == "b")["rank"] == 1    # 단신 4건 중 경쟁사가 맨 앞
    assert "동점" not in next(e for e in evs if e["event_id"] == "b")["flags"]       # 경쟁사 여부까지 같아야 동점


def test_competitor_detection_uses_subject_then_publisher_and_title_only_when_subject_empty():
    r = {"d1": {"publisher": "Micron Technology", "title": "t"}}
    e1 = {"subject": "Micron", "rep_doc_id": "d1"}
    e2 = {"subject": "", "rep_doc_id": "d1"}
    e3 = {"subject": "삼성전자", "rep_doc_id": "d2"}
    assert sel.detect_competitor(e1, r, CFG) == "Micron" and sel.detect_competitor(e2, r, CFG) == "Micron"
    assert sel.detect_competitor(e3, {"d2": {"publisher": "x", "title": "삼성전자 vs SK하이닉스"}}, CFG) == ""
    assert sel.detect_competitor(e3, {}, dict(CFG, competitors=[])) == ""


def test_csv_competitor_column_and_review_mark(tmp_path):
    from internal import review as rv
    recs = {r["doc_id"]: r for r in sample_records()}
    d = sorted(recs)[0]
    evs = []
    for eid, subj in (("a", "Samsung"), ("b", "SK hynix")):
        e = mk(eid, 88)
        e.update(rep_doc_id=d, doc_ids=[d], subject=subj)
        evs.append(e)
    sel.select(evs, CFG)
    path = tmp_path / "sel.csv"
    sel.write_csv(path, evs, recs)
    rows = {r["event_id"]: r for r in csv.DictReader(open(path, encoding="utf-8"))}
    assert sel.CSV_COLUMNS[-1] == "competitor" and rows["a"]["competitor"] == "" and rows["b"]["competitor"] == "SK hynix"
    lines = {l.split()[0]: l for l in rv.render_table(evs).splitlines()[1:]}
    assert sum("[경쟁사]" in l for l in lines.values()) == 1


# 사내: 기술 이름·검색어 ---------------------------------------------------------------

def test_step_prompt_has_naming_scope_and_search_terms_rules():
    s = load_steps()
    a, h = s["s6_article"], s["s6_header"]
    assert "(Defect Agent)" in a and "원문 표기 그대로" in a and "어디부터 어디까지" in a and "지어내" in a
    assert "search_terms" in a and "검색어_최대_개수" in a and "그대로 있는" in a
    assert "원문 이름을 괄호로" in h and "(Defect Agent)" in h


def test_article_prompt_carries_search_terms_limit_and_terms_are_trimmed():
    assert draftmod.clean_search_terms([" Defect  Agent ", "defect agent", "", "FabSense", "A", "B"], 3) == ["Defect Agent", "FabSense", "A"]
    for slot in ("core", "brief"):
        assert "search_terms" in (draftmod.CORE_ARTICLE_SCHEMA if slot == "core" else draftmod.ARTICLE_SCHEMA)["properties"]


def test_search_terms_not_in_cited_evidence_are_dropped_with_warning():
    recs = {r["doc_id"]: r for r in sample_records()}
    d = sample()
    lam = next(r for r in recs.values() if r["publisher"] == "Lam Research")
    eid = f"{lam['doc_id']}/{lam['body']['blocks'][0]['id']}"
    a = d["articles"][0]
    a["evidence_ids"] = [eid]
    a["search_terms"] = ["FLEET analytics", "Defect Agent", "chamber matching"]
    warns = checks.autofix(d, recs)
    assert a["search_terms"] == ["FLEET analytics"] or a["search_terms"] == ["FLEET analytics", "chamber matching"]
    dropped = [w for w in warns if w["kind"] == "검색어"]
    assert any("Defect Agent" in w["message"] for w in dropped)
    assert all(w["scope"] and "근거 블록에 없어" in w["message"] for w in dropped)
    a["search_terms"] = ["없는 말"]
    checks.autofix(d, recs)
    assert "search_terms" not in a


def test_search_terms_rendered_in_html_and_text():
    for th in ("B6", "S4", "B4"):
        out = build(th)
        assert out["html_preview"].count("🔎 검색어 · ") == 8 and "🔎 검색어 · Defect Agent · 결함 원인 분석" in out["html_preview"]
        assert "\n검색어 · Defect Agent, 결함 원인 분석\n" in out["text"]
    d = sample()
    for a in d["articles"]:
        a.pop("search_terms")
    out = build("B6", d)
    assert "🔎" not in out["html_preview"] and "검색어 ·" not in out["text"]


def test_sample_has_four_core_four_brief_with_naming_and_terms():
    d = sample()
    assert [a["slot"] for a in d["articles"]].count("core") == 4 and [a["slot"] for a in d["articles"]].count("brief") == 4
    first = d["articles"][0]
    for text in (d["summary"], first["deck"], first["points"]["what"]):
        assert "결함 원인 분석 AI 에이전트(Defect Agent)" in text
    assert "결함 검출 → 유형 분류 → 원인 추정 → 조치 제안 중" in first["points"]["what"]
    assert all(a.get("search_terms") and len(a["search_terms"]) <= CFG["search_terms_max"] for a in d["articles"])
    assert sum("(견본)" in a["credits"][0] for a in d["articles"]) == 8 and "메모리 경쟁사 K" in d["articles"][2]["title"]
