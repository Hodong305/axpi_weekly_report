"""사외 수집 실행: D1·D2 후보 → pass1 → pass2 → 병합 → 파일 생성 (설계서 4.3, 7.1)."""
import copy
import gzip
import hashlib
import json
from datetime import datetime
from pathlib import Path

import yaml

from . import __version__
from .build import build_record, doc_key, failed_record, sort_attempts, sorted_discovery
from .candidates import load_candidate_files, load_manual_urls
from .extract import plausible_date
from .fetch import Fetched, Fetcher, now_kst
from .ref import KST, weekly_window
from .registry import crawl_source
from . import media as media_mod
from .report import write_report
from .settings import TOPIC_CODES, file_name
from .validate import check, dir_lookup, summarize

STATUS_RANK = {"complete": 0, "drop": 1, "partial": 2}
TEMP_FAIL = {"FETCH_FAILED", "ACCESS_BLOCKED"}


# 후보 묶기 -----------------------------------------------------------------

def group_candidates(cands, log):
    groups = {}
    for c in cands:
        try:
            _, did = doc_key(c["url"])
        except ValueError as e:
            log.append({"url": c["url"], "error": str(e)})
            continue
        g = groups.setdefault(did, {"url": c["url"], "discovery": [], "topic_hints": [], "tracks": [],
                                    "date_hint": None, "at_hint": None, "only_resurvey": True, "prompt_topics": set()})
        g["discovery"].append(c["discovery"])
        g["topic_hints"] += [t for t in c["topic_hints"] if t not in g["topic_hints"]]
        g["tracks"].append(c["track"])
        g["date_hint"] = g["date_hint"] or c.get("date_hint")
        g["at_hint"] = g["at_hint"] or c.get("at_hint")
        g["only_resurvey"] = g["only_resurvey"] and c.get("resurvey", False)
        if c.get("prompt_topic"):
            g["prompt_topics"].add(c["prompt_topic"])
        # 대표 후보 URL: 사전순으로 가장 앞선 것(입력 순서와 무관하게 결정)
        if c["url"] < g["url"]:
            g["url"] = c["url"]
    return groups


def merge_same_doc(a, b):
    """같은 doc_id 레코드 둘을 합친다. 상태가 더 좋은 쪽의 본문을 쓰고 이력·발견 경로는 합친다."""
    better, other = (a, b) if (STATUS_RANK[a["status"]], -a["body"]["utf8_bytes"]) <= (STATUS_RANK[b["status"]], -b["body"]["utf8_bytes"]) else (b, a)
    out = copy.deepcopy(better)
    out["discovery"] = sorted_discovery(a["discovery"] + b["discovery"])
    out["attempts"] = sort_attempts(a["attempts"] + b["attempts"])
    out["topic_hints"] = [t for t in TOPIC_CODES if t in set(a["topic_hints"]) | set(b["topic_hints"])]
    if "A" in (a["track"], b["track"]):
        out["track"] = "A"
    return out


def _put(records, rec, meta, gmeta):
    did = rec["doc_id"]
    records[did] = merge_same_doc(records[did], rec) if did in records else rec
    m = meta.setdefault(did, {"only_resurvey": True, "prompt_topics": set()})
    m["only_resurvey"] = m["only_resurvey"] and gmeta["only_resurvey"]
    m["prompt_topics"] |= gmeta["prompt_topics"]


def load_overrides(path):
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: (v or {}) for k, v in data.items()}


def manual_fetched(manual_dir, did, url):
    for ext, ctype in ((".html", "text/html"), (".htm", "text/html"), (".pdf", "application/pdf")):
        p = Path(manual_dir) / f"{did}{ext}"
        if p.exists():
            return Fetched(url, url, 200, ctype, p.read_bytes(), None, now_kst(), agent="manual"), p.name
    return None, None


# 실행 ---------------------------------------------------------------------

def next_revision(out_dir, issue_week):
    revs = [0]
    for p in Path(out_dir).glob(f"mtc-ax-{issue_week.replace('-', '')}-merged-r*.json"):
        try:
            revs.append(int(p.stem.rsplit("-r", 1)[1]))
        except ValueError:
            pass
    return max(revs) + 1


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _media_cands(media_cands, rec, f, cfg):
    """complete이고 이미지 정책 대상인 새 본문(HTML)에서 이미지 후보를 뽑아 둔다."""
    if rec["status"] == "complete" and rec["source_type"] in cfg["collection"]["media_source_types"] and not f.is_pdf:
        media_cands[rec["doc_id"]] = media_mod.extract_candidates(f.content, f.final_url, cfg)


def run(settings, issue_week, weeks=1, force=False, log=print, skip_registry=False):
    cfg = settings.c
    start, end = weekly_window(issue_week, weeks)
    now = datetime.now(KST)
    if now < end and not force:
        raise SystemExit(f"수집 기간이 아직 끝나지 않았습니다: {end.isoformat()} 이후 실행하세요.")
    out_dir, work = settings.out_dir(issue_week), settings.work_dir(issue_week)
    out_dir.mkdir(parents=True, exist_ok=True)
    (work / "candidates").mkdir(parents=True, exist_ok=True)
    (work / "manual").mkdir(parents=True, exist_ok=True)
    revision = next_revision(out_dir, issue_week)
    prev = {}
    if revision > 1:
        prevdoc = load_json(out_dir / file_name(issue_week, "merged", revision - 1))
        if prevdoc.get("window_weeks") != weeks:
            raise SystemExit(f"이전 revision의 window_weeks={prevdoc.get('window_weeks')}와 다릅니다. 같은 주차는 같은 기간으로만 재실행합니다.")
        prev = {r["doc_id"]: r for r in prevdoc["records"]}
    log(f"[{issue_week}] 기간 {start.isoformat()} ~ {end.isoformat()} ({weeks}주), revision r{revision}")

    fetcher = Fetcher(cfg)
    stats = {"registry": [], "llm_files": [], "bad_urls": [], "future": [], "browser": fetcher.browser_available()}
    try:
        # D1 레지스트리
        cands = []
        if not skip_registry:
            for src in settings.sources:
                items, slog = crawl_source(src, fetcher, start, end, cfg["collection"]["feed_date_margin_days"],
                                           cfg["collection"]["max_items_per_source"])
                stats["registry"].append(slog)
                cands += items
                if slog["status"] != "skip":
                    log(f"  D1 {src['id']}: {slog['kept']}/{slog['items']} {slog['note']}")
        # D2 LLM 후보, 수동 URL
        llm, stats["llm_files"] = load_candidate_files(work / "candidates")
        cands += llm + load_manual_urls(work / "manual" / "urls.txt")
        log(f"  후보 {len(cands)}건 (레지스트리 {len(cands) - len(llm)}, LLM {len(llm)})")
        groups = group_candidates(cands, stats["bad_urls"])
        overrides = load_overrides(work / "manual" / "overrides.yaml")
        window = (start, end)
        alias_to_prev = {}
        for r in prev.values():
            for a in r["attempts"]:
                try:
                    alias_to_prev[doc_key(a["url"])[1]] = r["doc_id"]
                except ValueError:
                    pass

        # pass1
        records, meta = {}, {}
        media_cands = {}      # doc_id → 이미지 후보(내려받기 전). 이번 실행에서 새로 받은 본문에서만 만든다
        for did in sorted(groups):
            g = groups[did]
            pid = did if did in prev else alias_to_prev.get(did)
            # 7.1-4 예외(v4.7): 담당자가 overrides.yaml로 값을 확인했거나, 이전 기록의 발행일이 말이 안 되는 값(1970-01-01 등)이면
            # 이전 complete·drop을 그대로 가져오지 않고 다시 조회해 판정한다(보정이 반영될 기회를 막지 않는다)
            recheck = bool(pid) and (bool(overrides.get(did) or overrides.get(pid))
                                     or (prev[pid].get("published_date") and not plausible_date(prev[pid]["published_date"])))
            if pid and prev[pid]["status"] in ("complete", "drop") and not recheck:
                rec = copy.deepcopy(prev[pid])
                rec["discovery"] = sorted_discovery(rec["discovery"] + g["discovery"])
                _put(records, rec, meta, g)
                continue
            f = fetcher.get(g["url"])
            if f is None:
                rec = failed_record(g, 1, "조회 실패(응답 없음)", now_kst())
                _put(records, rec, meta, g)
                continue
            rec, where = build_record(g, f, 1, settings, window, overrides.get(did))
            if where == "FUTURE":
                stats["future"].append(rec["source_url"])
                continue
            if rec["doc_id"] in overrides and rec["doc_id"] != did:
                rec, where = build_record(g, f, 1, settings, window, overrides.get(rec["doc_id"]))
            _put(records, rec, meta, g)
            _media_cands(media_cands, rec, f, cfg)
        pass1 = copy.deepcopy(records)
        log(f"  pass1: {summarize(list(pass1.values()))}")

        # pass2: 남은 partial만
        touched = {}
        for did in sorted(records):
            rec = records[did]
            if rec["status"] != "partial":
                continue
            g = {"url": rec["source_url"], "discovery": rec["discovery"], "topic_hints": rec["topic_hints"],
                 "tracks": [rec["track"]], "date_hint": rec["published_date"], "at_hint": rec["published_at"]}
            f, mfile = manual_fetched(work / "manual", did, rec["source_url"])
            codes = set(rec["reason_codes"])
            if f is None and "FETCH_FAILED" in codes:
                f = fetcher.get(rec["source_url"])
            if f is None and codes & {"ACCESS_BLOCKED", "BODY_MISSING", "BODY_TRUNCATED", "TABLE_MISSING"}:
                f = fetcher.render(rec["source_url"])
            if f is None:
                continue
            new, where = build_record(g, f, 2, settings, window, overrides.get(did), manual_file=mfile)
            if where == "FUTURE":
                continue
            new["attempts"] = sort_attempts(rec["attempts"] + new["attempts"])
            _media_cands(media_cands, new, f, cfg)
            replace = new["status"] in ("complete", "drop") or new["body"]["utf8_bytes"] > rec["body"]["utf8_bytes"]
            if replace:
                if new["doc_id"] != did:
                    records.pop(did)
                    meta[new["doc_id"]] = meta.pop(did)
                    records[new["doc_id"]] = merge_same_doc(records[new["doc_id"]], new) if new["doc_id"] in records else new
                    touched[new["doc_id"]] = records[new["doc_id"]]
                else:
                    records[did] = new
                    touched[did] = new
            else:
                rec["attempts"] = new["attempts"]
                touched[did] = rec
        log(f"  pass2: 재시도 {len(touched)}건, 브라우저 {'사용' if stats['browser'] else '미설치'}")

        # 별칭 통합: 발행처와 본문 해시가 같으면 하나로
        by_hash = {}
        for did, r in records.items():
            if r["body"]["sha256"]:
                by_hash.setdefault((r["publisher"], r["body"]["sha256"]), []).append(did)
        for dids in by_hash.values():
            if len(dids) < 2:
                continue
            dids.sort(key=lambda d: (not records[d]["source_url"].startswith("https"), len(records[d]["normalized_url"]), records[d]["normalized_url"]))
            rep = dids[0]
            for d in dids[1:]:
                alias = records.pop(d)
                if d in media_cands and rep not in media_cands:
                    media_cands[rep] = media_cands[d]
                for a in alias["attempts"]:
                    a["observation"] = "별칭 URL: " + a["observation"]
                records[rep] = merge_same_doc(records[rep], alias)
                m = meta.pop(d, {"only_resurvey": True, "prompt_topics": set()})
                meta[rep]["only_resurvey"] = meta[rep]["only_resurvey"] and m["only_resurvey"]
                meta[rep]["prompt_topics"] |= m["prompt_topics"]
                touched.pop(d, None)

        # 이전 revision과 병합 (7.1-4, 7.1-5)
        for pid, p in prev.items():
            if pid not in records:
                records[pid] = copy.deepcopy(p)
                meta.setdefault(pid, {"only_resurvey": False, "prompt_topics": set()})
                continue
            n = records[pid]
            if p["status"] == "complete" and n["status"] != "complete" and set(n["reason_codes"]) <= TEMP_FAIL:
                keep = copy.deepcopy(p)
                keep["attempts"] = sort_attempts(p["attempts"] + n["attempts"])
                keep["discovery"] = sorted_discovery(p["discovery"] + n["discovery"])
                records[pid] = keep
            elif p["status"] == "complete" and n["status"] == "complete" and p["body"]["sha256"] != n["body"]["sha256"]:
                n["change_note"] = f"r{revision - 1} 대비 본문 변경(이전 sha256 {p['body']['sha256'][:12]})"
            # 발행일 불일치 → MERGE_CONFLICT. 단 이전 값이 말이 안 되는 날짜였거나 담당자가 overrides.yaml로 확인한 문서는 새 값을 따른다(v4.7)
            if (p.get("published_date") and n.get("published_date") and p["published_date"] != n["published_date"]
                    and records[pid]["status"] == "complete" and plausible_date(p["published_date"]) and not overrides.get(pid)):
                c = records[pid]
                c["status"], c["reason_codes"] = "partial", ["MERGE_CONFLICT"]
                c["body"]["extent"] = "partial"
            records[pid]["attempts"] = sort_attempts(p["attempts"] + records[pid]["attempts"])
        # 이미지 내려받기(4.4): 이번에 새로 받은 complete 레코드만. 이전 revision에서 가져온 레코드는 다시 받지 않는다
        media_dir = out_dir / "media"
        n_media = 0
        for did, r in records.items():
            r.setdefault("media", [])
            if r["status"] != "complete" or r["source_type"] not in cfg["collection"]["media_source_types"]:
                r["media"] = []
                continue
            pv = prev.get(did)
            if pv and pv["status"] == "complete" and pv["body"]["sha256"] == r["body"]["sha256"]:
                # 같은 본문이면 이전 revision의 이미지를 그대로 쓴다(파일은 out/{주차}/media/에 이미 있다)
                r["media"] = [m for m in pv.get("media", []) if (out_dir / m["file"]).is_file()]
            elif did in media_cands:
                items, fails = media_mod.download(fetcher, media_cands[did], did, media_dir, cfg)
                r["media"] = items
                n_media += len(items)
                for url, status, why in fails:
                    r["attempts"] = sort_attempts(r["attempts"] + [{"pass": 1, "agent": "script", "at": now_kst(), "url": url,
                                                                    "http_status": status, "observation": f"이미지 제외: {why}"}])
            else:
                r["media"] = [m for m in r["media"] if (out_dir / m["file"]).is_file()]
        log(f"  이미지: {n_media}장 새로 받음")
    finally:
        fetcher.close()

    # 재조사 기록
    resurvey = build_resurvey(records, meta, stats["llm_files"])

    base = {"schema_version": "4.6", "revision": revision, "issue_week": issue_week, "window_weeks": weeks,
            "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
            "generated_at": now_kst(),
            "generator": {"name": "mtcax-collector", "version": __version__,
                          "registry_version": str(settings.source_meta.get("registry_version", "unknown")),
                          "config_version": str(cfg.get("config_version", "unknown"))}}

    def make(phase, recs, with_resurvey=False):
        recs = [recs[k] for k in sorted(recs)]
        d = dict(base, phase=phase, summary=summarize(recs))
        if with_resurvey and resurvey:
            d["resurvey"] = resurvey
        d["records"] = recs
        return d

    docs = {"pass1": make("pass1", pass1), "pass2": make("pass2", touched),
            "merged": make("merged", records, True),
            "intake": make("intake", {k: v for k, v in records.items() if v["status"] == "complete"}, True)}
    lead_min = cfg["extraction"]["lead_min_chars"]
    all_errors = {}
    for phase, d in docs.items():
        errs, warns = check(d, lead_min, cfg["thresholds"]["intake_min"] if phase == "intake" else None, dir_lookup(out_dir))
        all_errors[phase] = (errs, warns)
        path = out_dir / file_name(issue_week, phase, revision)
        path.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    report_path = write_report(settings, docs, stats, meta, all_errors, out_dir / file_name(issue_week, "report", revision, "md"))
    log(f"  merged: {docs['merged']['summary']}")
    log(f"  리포트: {report_path}")
    bad = {k: v[0] for k, v in all_errors.items() if v[0]}
    if bad:
        log("  검증 실패: " + json.dumps(bad, ensure_ascii=False)[:2000])
    return docs, all_errors



def build_resurvey(records, meta, llm_logs):
    rs_logs = [l for l in llm_logs if l["resurvey"]]
    if not rs_logs:
        return None
    complete = [d for d, r in records.items() if r["status"] == "complete"]
    new = [d for d in complete if meta.get(d, {}).get("only_resurvey")]
    before, after = len(complete) - len(new), len(complete)
    topics = []
    for short, code in (("T1", "T1_CHIPMAKER"), ("T2", "T2_EQUIPMENT"), ("T3", "T3_SOLUTION"), ("T4", "T4_PHYSICAL_AI")):
        logs = [l for l in rs_logs if l["topic"] == short]
        if not logs:
            topics.append({"topic": code, "method": "재조사 프롬프트 미실행", "result": "미실행"})
            continue
        agents = ", ".join(sorted({l["agent"] for l in logs}))
        n_c = sum(l["valid"] for l in logs)
        n_new = sum(1 for d in new if short in meta[d]["prompt_topics"])
        topics.append({"topic": code, "method": f"재조사 프롬프트({agents}) 후보 {n_c}건 조회",
                       "result": f"신규 complete {n_new}건"})
    report = f"{before}건 → {after}건. 재조사로 {after - before}건 추가."
    return {"before_count": before, "after_count": after, "topics_checked": topics, "report": report}


def package(settings, issue_week):
    out_dir = settings.out_dir(issue_week)
    rev = next_revision(out_dir, issue_week) - 1
    if rev < 1:
        raise SystemExit("먼저 run을 실행하세요.")
    intake = out_dir / file_name(issue_week, "intake", rev)
    report = out_dir / file_name(issue_week, "report", rev, "md")
    doc = load_json(intake)
    errs, warns = check(doc, settings.c["extraction"]["lead_min_chars"], settings.c["thresholds"]["intake_min"], dir_lookup(out_dir))
    if errs:
        raise SystemExit("intake 검증 실패로 패키징하지 않습니다:\n" + "\n".join(errs[:30]))
    pkg = out_dir / "package"
    pkg.mkdir(exist_ok=True)
    gz = pkg / (intake.name + ".gz")
    with open(intake, "rb") as fi, gzip.GzipFile(gz, "wb", mtime=0) as fo:
        fo.write(fi.read())
    raw = pkg / intake.name        # 같은 내용의 원본(압축을 받지 못하는 메일 환경 대비, 설계서 4.7)
    raw.write_bytes(intake.read_bytes())
    rep = pkg / report.name
    rep.write_bytes(report.read_bytes())
    zpath = pkg / file_name(issue_week, "media", rev, "zip")
    media_files = sorted({m["file"] for r in doc["records"] for m in r["media"]})
    if media_files:      # 이미지가 있을 때만 묶음을 만든다(압축 없이 묶음: 이미지는 이미 압축돼 있다)
        media_mod.write_zip(zpath, [(n, (out_dir / n).read_bytes()) for n in media_files])
    elif zpath.exists():
        zpath.unlink()
    files = []
    for p in (gz, raw, rep) + ((zpath,) if media_files else ()):
        b = p.read_bytes()
        files.append({"name": p.name, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()})
    manifest = {"schema_version": "4.6", "issue_week": issue_week, "revision": rev,
                "window_weeks": doc["window_weeks"], "summary": doc["summary"], "files": files,
                "warnings": warns, "generated_at": now_kst()}
    mf = pkg / file_name(issue_week, "manifest", rev)
    mf.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return [gz, raw, rep, mf] + ([zpath] if media_files else []), warns
