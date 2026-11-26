"""D2 LLM 후보 파일 읽기와 탐색 프롬프트 생성.

Claude·GPT·Gemini 모두 같은 프롬프트를 받고 같은 형식으로 답한다.
스크립트는 URL·분야 힌트·트랙·탐색 경로만 쓰고 나머지 값은 원문에서 다시 추출하므로,
어느 모델이 찾았는지는 레코드의 discovery 목록에만 남는다."""
import json
import re
from pathlib import Path

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
        log = {"file": p.name, "agent": agent, "topic": topic, "resurvey": resurvey, "valid": 0, "invalid": 0, "error": None}
        try:
            obj = _extract_json(p.read_text(encoding="utf-8"))
            items = obj.get("candidates", []) if isinstance(obj, dict) else []
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


def render_prompt(settings, topic, start, end, resurvey=False):
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
    for k, v in values.items():
        tpl = tpl.replace("{{" + k + "}}", v)
    return tpl


def candidate_filename(agent, topic, resurvey=False):
    if agent not in LLM_AGENTS:
        raise ValueError(f"agent는 {LLM_AGENTS} 중 하나")
    return f"{agent}-{'resurvey-' if resurvey else ''}{topic}.json"
