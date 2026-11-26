"""D2 LLM 후보 파일 읽기와 탐색 프롬프트 생성.

Claude·GPT·Gemini 모두 같은 프롬프트를 받고 같은 형식으로 답한다.
스크립트는 URL·분야 힌트·트랙·탐색 경로만 쓰고 나머지 값은 원문에서 다시 추출하므로,
어느 모델이 찾았는지는 레코드의 discovery 목록에만 남는다."""
import json
import re
from pathlib import Path

from .competitors import complete_counts
from .settings import LLM_AGENTS, PROMPT_TOPICS, TOPIC_CODES, TOPIC_SHORT

FILE_RE = re.compile(r"^(?P<agent>claude|gpt|gemini)-(?P<resurvey>resurvey-)?(?P<topic>T1|T2|T3|T4|B)\.(json|txt|md)$")


def _extract_json(text):
    """모델별 출력 습관(코드 펜스, 앞뒤 설명문)을 흡수해 첫 JSON 객체를 꺼낸다."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start = text.find("{")
    if start < 0:
        raise ValueError("JSON 객체 없음")
    dec = json.JSONDecoder()
    obj, _ = dec.raw_decode(text[start:])
    return obj


def _norm_topics(values, default):
    out = []
    for v in values or []:
        v = str(v).strip()
        code = TOPIC_SHORT.get(v[:2]) if v[:2] in TOPIC_SHORT else (v if v in TOPIC_CODES else None)
        if code and code not in out:
            out.append(code)
    return out or default


def _norm_coverage(raw):
    """T1 파일의 선택 키 coverage: [{"company","checked":[...],"found":n}]. 형식이 틀린 항목은 버린다."""
    out = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict) or not isinstance(c.get("company"), str) or not c["company"].strip():
            continue
        checked = [str(x) for x in c.get("checked") if str(x).strip()] if isinstance(c.get("checked"), list) else []
        found = c.get("found")
        out.append({"company": c["company"].strip(), "checked": checked,
                    "found": found if isinstance(found, int) and not isinstance(found, bool) and found >= 0 else None})
    return out


def load_candidate_files(directory):
    """반환: (후보 목록, 파일별 로그)."""
    directory = Path(directory)
    cands, logs = [], []
    if not directory.exists():
        return cands, logs
    for p in sorted(directory.iterdir()):
        m = FILE_RE.match(p.name)
        if not m:
            logs.append({"file": p.name, "agent": None, "topic": None, "resurvey": False,
                         "valid": 0, "invalid": 0, "error": "파일 이름 규칙 불일치(무시)"})
            continue
        agent, topic, resurvey = m["agent"], m["topic"], bool(m["resurvey"])
        log = {"file": p.name, "agent": agent, "topic": topic, "resurvey": resurvey, "valid": 0, "invalid": 0, "error": None,
               "coverage": []}
        try:
            obj = _extract_json(p.read_text(encoding="utf-8"))
            items = obj.get("candidates", []) if isinstance(obj, dict) else []
            log["coverage"] = _norm_coverage(obj.get("coverage") if isinstance(obj, dict) else None)
        except (ValueError, json.JSONDecodeError) as e:
            log["error"] = f"JSON 파싱 실패: {e}"[:200]
            logs.append(log)
            continue
        default_topics = [TOPIC_SHORT[topic]] if topic in TOPIC_SHORT else []
        for it in items:
            url = it.get("url") if isinstance(it, dict) else None
            if not isinstance(url, str) or not re.match(r"^https?://", url.strip()):
                log["invalid"] += 1
                continue
            track = it.get("track") if it.get("track") in ("A", "B") else ("B" if topic == "B" else "A")
            query = it.get("found_via") if isinstance(it.get("found_via"), str) and it.get("found_via").strip() else None
            cands.append({
                "url": url.strip(), "date_hint": None, "at_hint": None,
                "discovery": {"route": "llm_search", "agent": agent, "source_id": None,
                              "query": (("[재조사] " if resurvey else "") + query[:300]) if query else ("[재조사]" if resurvey else None)},
                "topic_hints": _norm_topics(it.get("topic_hints"), default_topics),
                "track": track, "resurvey": resurvey, "prompt_topic": topic,
            })
            log["valid"] += 1
        logs.append(log)
    return cands, logs


def load_manual_urls(path):
    """manual/urls.txt: 한 줄에 URL 하나(# 주석). 운영자가 직접 찾은 자료."""
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and re.match(r"^https?://", line):
            out.append({"url": line, "date_hint": None, "at_hint": None,
                        "discovery": {"route": "manual", "agent": "manual", "source_id": None, "query": None},
                        "topic_hints": [], "track": "A", "resurvey": False})
    return out


def zero_complete_companies(settings, issue_week):
    """이번 주 최신 병합 결과에서 complete가 0건인 경쟁사 이름(재조사 우선 대상). 결과가 없으면 None."""
    files = sorted(Path(settings.out_dir(issue_week)).glob(f"mtc-ax-{issue_week.replace('-', '')}-merged-r*.json"),
                   key=lambda p: int(p.stem.rsplit("-r", 1)[1]) if p.stem.rsplit("-r", 1)[1].isdigit() else 0)
    if not files:
        return None
    try:
        recs = json.loads(files[-1].read_text(encoding="utf-8")).get("records", [])
    except (ValueError, OSError):
        return None
    counts = complete_counts(settings.queries.get("competitors", []), recs)
    return [n for n, k in counts.items() if k == 0]


def _competitor_block(companies, priority):
    """T1 프롬프트의 [경쟁사별 확인 목록]. priority 회사를 맨 앞에 둔다(재조사)."""
    order = [c for c in companies if c["name"] in (priority or [])] + [c for c in companies if c["name"] not in (priority or [])]
    lines = ["[경쟁사별 확인 목록] 아래 회사를 하나도 빠뜨리지 말고 하나씩 확인하라.",
             "경쟁사 소식이라도 설계·개발이 아니라 제조 현장(팹 운영)의 AI·자동화에 관한 것만 찾는다. "
             "설계·개발 소식(칩·회로 설계, 설계용 AI·EDA, 신제품·신기술 개발·출시, AI 칩 판매, R&D 투자, 실적·주가·가격)은 반환하지 않는다."]
    if priority:
        lines.append("지난 수집 결과에서 complete가 0건이던 회사를 앞에 두었다. 이 회사들부터 확인하라: " + ", ".join(priority))
    for i, c in enumerate(order, 1):
        mark = " ★재조사 우선" if c["name"] in (priority or []) else ""
        lines.append(f"{i}. {c['name']}{mark}")
        lines.append(f"   - 공식 채널: {', '.join(c.get('channels') or ['(없음)'])}")
        lines.append(f"   - 검색어: {' / '.join(c.get('queries') or ['(없음)'])}")
    lines += [
        "회사마다 다음 ①~⑤를 확인한다.",
        "  ① 공식 채널의 이번 기간 발표",
        "  ② 회사 이름 + 제조 AI 검색어(위 검색어와 현지어)",
        "  ③ 학회·컨퍼런스 발표(SEMICON, IEDM, VLSI, ASMC, SPIE, 회사 자체 기술 행사)",
        "  ④ 실적 발표·IR에서 팹 AI·자동화 언급",
        "  ⑤ 현지어 산업 매체(대만 經濟日報·DigiTimes, 일본 日経·日刊工業新聞, 중국 集微网, 한국 전자신문·디일렉·ZDNet Korea, 미국 EE Times·Semiconductor Engineering)",
        "이 범위 안에서는 넓게 반환한다. 기간 안에 직접적인 사례가 없어도 그 회사의 팹 자동화·스마트 팩토리·제조 데이터 플랫폼·팹 로봇에 관한 기술 발표·인터뷰·학회 발표처럼 제조 AX와 관련될 가능성이 있는 자료는 반환한다(판정은 다른 단계에서 한다).",
    ]
    return "\n".join(lines)


COVERAGE_KEY = ',\n "coverage":[{"company":"SK hynix","checked":["뉴스룸","검색어 3개","DigiTimes"],"found":2}]'
COVERAGE_NOTE = ('"coverage"에는 위 목록의 모든 회사를 한 번씩 적는다. company는 목록의 이름, checked는 실제로 확인한 채널·검색어·매체, '
                 'found는 그 회사에서 찾아 candidates에 넣은 URL 수다. 확인하지 못한 회사도 checked를 빈 배열로 적는다.')


def render_prompt(settings, topic, start, end, resurvey=False, priority_companies=None):
    if topic not in PROMPT_TOPICS:
        raise ValueError(f"topic은 {PROMPT_TOPICS} 중 하나")
    tpl = (settings.root / "prompts" / "discovery.md").read_text(encoding="utf-8")
    tpl = tpl.split("<!-- PROMPT START -->", 1)[1].split("<!-- PROMPT END -->", 1)[0].strip()
    q = settings.queries
    tdef = q["topics"][topic]
    crawl = ("rss", "sitemap", "list_html")
    crawled = [s["publisher"] for s in settings.sources if s["method"] in crawl and s.get("verified")]

    def relevant(s):
        if topic == "B":
            return s.get("track") == "B"
        return s.get("track", "A") == "A" and (TOPIC_SHORT[topic] in s.get("topics", []) or not s.get("topics"))
    # 순회하지 않거나 아직 확인되지 않은 발행처는 LLM이 직접 확인한다(순회 실패 시 누락 방지)
    search_first = [f"{s['publisher']} ({s.get('home_url', '')})" for s in settings.sources
                    if relevant(s) and (s["method"] not in crawl or not s.get("verified"))]
    queries = tdef.get("resurvey_queries" if resurvey else "queries", [])
    fmt = lambda xs: "\n".join(f"  - {x}" for x in xs) or "  - (없음)"
    values = {
        "window_start": start.isoformat(), "window_end_exclusive": end.isoformat(),
        "topic_code": topic, "topic_name": tdef["name"], "topic_scope": tdef["scope"].strip(),
        "queries": fmt(queries), "crawled_publishers": fmt(crawled), "search_first": fmt(search_first),
        "mode": "재조사(기존 탐색에서 놓친 공식 채널·현지어 원문·대체 원발표 위주)" if resurvey else "정규 탐색",
        "default_track": "B" if topic == "B" else "A",
    }
    companies = q.get("competitors", [])
    values["competitor_names"] = ", ".join(c["name"] for c in companies) or "(목록 없음)"
    values["competitor_checklist"] = _competitor_block(companies, priority_companies) if topic == "T1" and companies else ""
    values["coverage_key"] = COVERAGE_KEY if topic == "T1" and companies else ""
    values["coverage_note"] = COVERAGE_NOTE if topic == "T1" and companies else ""
    for k, v in values.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return re.sub(r"\n{3,}", "\n\n", tpl).strip()


def candidate_filename(agent, topic, resurvey=False):
    if agent not in LLM_AGENTS:
        raise ValueError(f"agent는 {LLM_AGENTS} 중 하나")
    return f"{agent}-{'resurvey-' if resurvey else ''}{topic}.json"
