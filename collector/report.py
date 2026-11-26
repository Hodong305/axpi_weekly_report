"""수집 리포트(Markdown). 운영자가 일요일에 확인하고 사내로 함께 보낸다."""
import random

from .competitors import complete_counts, find_company
from .settings import LLM_AGENTS, TOPIC_CODES

ACTION = {
    "ACCESS_BLOCKED": "개인 브라우저로 열어(필요하면 무료 로그인) 페이지를 저장 → `{manual}/{did}.html` (PDF는 `.pdf`)",
    "FETCH_FAILED": "잠시 후 `run` 재실행. 계속 실패하면 브라우저로 저장 → `{manual}/{did}.html`",
    "BODY_MISSING": "브라우저로 저장 → `{manual}/{did}.html`. 스캔 PDF면 건너뜀",
    "BODY_TRUNCATED": "전문이 맞으면 overrides.yaml에 `accept_short: true`, 아니면 전체 페이지 저장",
    "TABLE_MISSING": "표가 보이는 상태로 페이지 저장 → `{manual}/{did}.html`",
    "DATE_UNCERTAIN": "원문에서 날짜 확인 → overrides.yaml에 `published_date`와 `note`",
    "MERGE_CONFLICT": "원문 발행일 확인 → overrides.yaml에 `published_date`와 `note`",
}


def _agents(r):
    return sorted({d["agent"] for d in r["discovery"]})


def coverage_table(settings, recs, llm_files):
    """경쟁사 탐색 범위(설계서 4.5): 회사 × 모델의 확인 여부·후보 수, 수집 결과 complete 수. 재조사 대상 표시."""
    companies = settings.queries.get("competitors", [])
    if not companies:
        return []
    cov = {}          # (회사, 모델) → {"checked": 확인 항목 수, "found": 후보 수}
    submitted = False
    for f in llm_files:
        if f.get("topic") != "T1" or not f.get("agent"):
            continue
        for c in f.get("coverage") or []:
            name = find_company(companies, c["company"])
            if not name:
                continue
            submitted = True
            cur = cov.setdefault((name, f["agent"]), {"checked": 0, "found": 0})
            cur["checked"] += len(c["checked"])
            cur["found"] += c["found"] or 0
    counts = complete_counts(companies, recs)
    L = ["## 경쟁사 탐색 범위",
         "| 경쟁사 | " + " | ".join(LLM_AGENTS) + " | complete | 비고 |",
         "|---|" + "---|" * (len(LLM_AGENTS) + 2)]
    todo = []
    for c in companies:
        cells, any_checked = [], False
        for a in LLM_AGENTS:
            v = cov.get((c["name"], a))
            if v is None:
                cells.append("-")
            elif v["checked"]:
                any_checked = True
                cells.append(f"확인 · 후보 {v['found']}")
            else:
                cells.append(f"미확인 · 후보 {v['found']}")
        # coverage가 한 건도 없으면(모델이 안 적었거나 T1 파일이 없음) 확인 여부로는 판단하지 않는다
        need = counts[c["name"]] == 0 or (submitted and not any_checked)
        if need:
            todo.append(c["name"])
        L.append(f"| {c['name']} | " + " | ".join(cells) + f" | {counts[c['name']]} | {'**재조사 대상**' if need else ''} |")
    if not submitted:
        L.append("")
        L.append("T1 후보 파일에 coverage가 없어 확인 여부는 알 수 없습니다. complete 수만 표시했습니다.")
    if todo:
        L.append("")
        L.append("- 재조사 대상: " + ", ".join(todo) + " (재조사 T1 프롬프트가 이 회사를 맨 앞에 나열합니다)")
    L.append("")
    return L


def write_report(settings, docs, stats, meta, errors, path):
    m = docs["merged"]
    recs = m["records"]
    manual = f"work/{m['issue_week']}/manual"
    L = []
    L.append(f"# 수집 리포트 {m['issue_week']} r{m['revision']}")
    L.append("")
    L.append(f"- 기간: {m['window_start']} ~ {m['window_end_exclusive']} ({m['window_weeks']}주)")
    L.append(f"- 생성: {m['generated_at']} · 수집기 {m['generator']['version']} · 레지스트리 {m['generator']['registry_version']} · 설정 {m['generator']['config_version']}")
    s = m["summary"]
    L.append(f"- **complete {s['complete']}** / partial {s['partial']} / drop {s['drop']} (유료 {s['paywall']}) / 전체 {s['records']}")
    imin = settings.c["thresholds"]["intake_min"]
    if s["complete"] < imin and not m.get("resurvey"):
        L.append(f"- ⚠ complete가 intake_min({imin}) 미만입니다. **재조사 필요**: "
                 f"`python -m collector prompt --issue-week {m['issue_week']} --resurvey` 로 프롬프트를 만들고, "
                 f"결과를 `candidates/{{agent}}-resurvey-{{T1..T4}}.json`으로 저장한 뒤 `run`을 다시 실행하세요.")
    if m.get("resurvey"):
        L.append(f"- 재조사: {m['resurvey']['report']}")
    L.append(f"- 기간 이후 발행이라 다음 주로 넘긴 후보: {len(stats['future'])}건")
    L.append("")

    errs = {k: v for k, v in errors.items() if v[0] or v[1]}
    L.append("## 검증")
    if not errs:
        L.append("모든 단계 파일 통과.")
    for k, (e, w) in errs.items():
        for x in e[:20]:
            L.append(f"- ❌ {k}: {x}")
        for x in w:
            L.append(f"- ⚠ {k}: {x}")
    L.append("")

    L.append("## 분야·경로별 complete")
    L.append("| 분야 | complete |")
    L.append("|---|---|")
    comp = [r for r in recs if r["status"] == "complete"]
    for t in TOPIC_CODES:
        L.append(f"| {t} | {sum(t in r['topic_hints'] for r in comp)} |")
    L.append(f"| (분야 힌트 없음) | {sum(not r['topic_hints'] for r in comp)} |")
    L.append("")
    L.append("| 발견 주체 | 후보 레코드 | complete | 이 주체만 찾은 complete |")
    L.append("|---|---|---|---|")
    for a in ["script", "claude", "gpt", "gemini", "manual"]:
        mine = [r for r in recs if a in _agents(r)]
        only = [r for r in comp if _agents(r) == [a]]
        L.append(f"| {a} | {len(mine)} | {sum(r['status'] == 'complete' for r in mine)} | {len(only)} |")
    L.append("")

    L.append("## LLM 후보 파일")
    if not stats["llm_files"]:
        L.append("없음. 레지스트리 결과만 사용했습니다.")
    for f in stats["llm_files"]:
        L.append(f"- {f['file']}: 유효 {f['valid']}, 무효 {f['invalid']}" + (f" — {f['error']}" if f["error"] else ""))
    for b in stats["bad_urls"]:
        L.append(f"- 잘못된 URL 무시: {b['url']} ({b['error']})")
    L.append("")

    L += coverage_table(settings, recs, stats["llm_files"])

    L.append("## 레지스트리 순회")
    L.append("| id | 방식 | HTTP | 항목 | 채택 | 비고 |")
    L.append("|---|---|---|---|---|---|")
    for g in stats["registry"]:
        if g["status"] == "skip":
            continue
        L.append(f"| {g['id']} | {g['method']} | {g['status']} | {g['items']} | {g['kept']} | {g['note']} |")
    L.append("")

    L.append("## 수동 보완 큐 (선택 사항, 이번 실행에서만 처리)")
    part = [r for r in recs if r["status"] == "partial"]
    if not part:
        L.append("없음.")
    for r in part:
        acts = "; ".join(ACTION[c].format(manual=manual, did=r["doc_id"]) for c in r["reason_codes"] if c in ACTION)
        L.append(f"- `{r['doc_id']}` {r['title'] or '(제목 없음)'} — {r['publisher'] or '-'}")
        L.append(f"  - URL: {r['source_url']}")
        L.append(f"  - 사유: {', '.join(r['reason_codes'])} → {acts}")
    L.append("")

    L.append("## 제외(drop)")
    drops = [r for r in recs if r["status"] == "drop"]
    if not drops:
        L.append("없음.")
    for r in drops:
        L.append(f"- {', '.join(r['reason_codes'])}: {r['title'] or r['source_url']} ({r['publisher'] or '-'}, {r['published_date'] or '날짜 없음'})")
    L.append("")

    L.append("## 추출 표본 확인 (complete 3건, 원문과 앞·뒤 문단 대조)")
    rnd = random.Random(m["issue_week"] + str(m["revision"]))
    for r in rnd.sample(comp, min(3, len(comp))):
        b = r["body"]["blocks"]
        L.append(f"- **{r['title']}** ({r['publisher']}, {r['published_date']}) — {r['source_url']}")
        L.append(f"  - 처음: {b[0]['text'][:160]}")
        L.append(f"  - 끝: {b[-1]['text'][-160:]}")
    L.append("")

    L.append("## complete 목록")
    for r in comp:
        L.append(f"- {r['published_date']} · {r['publisher']} · {r['title']} · {','.join(x[:2] for x in r['topic_hints']) or '-'} · 발견 {'/'.join(_agents(r))}")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    return path
