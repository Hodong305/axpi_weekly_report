"""GPT·Gemini 앱에 한 번에 넣을 통합 탐색 프롬프트를 만들고, 받은 통합 답을 주제별 후보 파일로 나눈다.

`python -m collector prompt`가 만든 T1~T4·B 프롬프트를 수정 없이 그대로 이어 붙이고,
출력 형식만 "주제 코드별 JSON 객체 하나"로 바꾸는 머리말·꼬리말을 덧붙인다.
답의 첫 키 "agent"에 모델이 스스로 서비스 이름을 적게 해서, 사용자가 표시하지 않아도 GPT·Gemini 답을 구분한다.

  python tools/combine_prompts.py combine --issue-week 2026-W48 [--resurvey]
      → work/2026-W48/prompts/ALL.txt (재조사면 ALL-resurvey.txt)
  python tools/combine_prompts.py split --issue-week 2026-W48 ANSWER.txt [이어받은 답.txt ...] [--agent gpt|gemini] [--resurvey]
      → work/2026-W48/candidates/gpt-T1.json … gpt-B.json (답에 있는 주제만)
      답이 잘렸으면 종료 코드 2로 끝난다. 앱에 "잘렸으니 이어서 계속 출력해줘"라고 요청해 받은 답을
      파일 뒤에 차례로 덧붙여 다시 실행한다. 받은 원문은 candidates/raw/에 함께 남긴다.

종료 코드: 0 저장 완료, 2 답이 잘림(이어받기 필요), 3 모델 구분 불가(--agent 필요), 1 그 밖의 오류.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOPICS = ["T1", "T2", "T3", "T4", "B"]
RESURVEY_TOPICS = ["T1", "T2", "T3", "T4"]

HEADER = """아래에 탐색 과제 {n}개({codes})가 이어서 나온다. 과제마다 웹 검색을 따로 충분히 수행하라.
과제마다 적힌 [출력] 형식 대신, 맨 끝의 [통합 출력] 형식으로 모든 과제의 결과를 한 번에 출력한다.
한 과제의 결과가 많다고 다른 과제를 건너뛰지 마라. 찾은 것이 없는 과제도 빈 배열로 반드시 포함한다.
"""

FOOTER = """
==================== [통합 출력] ====================
다른 설명 없이 아래 형식의 JSON 객체 하나만 출력한다.
첫 키 "agent"에는 너를 제공하는 서비스와 모델 이름을 그대로 적는다(예: "ChatGPT GPT-5", "Google Gemini 2.5 Pro").
나머지 키는 과제 코드이고, 값은 각 과제의 [출력] 형식과 같은 객체다. 각 후보의 topic_hints·track은 해당 과제에 적힌 대로 쓴다.
T1 과제의 선택 키 "coverage"(경쟁사별 확인 결과)는 따로 빼지 말고 "T1" 객체 안에 둔다.
출력이 길어 중간에 끊기면, "이어서 계속 출력해줘"라는 요청을 받았을 때 끊긴 바로 다음 글자부터 이어서 출력한다(앞부분 반복 금지).
{example}
"""


class Truncated(Exception):
    pass


def _paths(week):
    work = ROOT / "work" / week
    return work / "prompts", work / "candidates"


def combine(week, resurvey):
    pdir, _ = _paths(week)
    codes = RESURVEY_TOPICS if resurvey else TOPICS
    parts = [HEADER.format(n=len(codes), codes=", ".join(codes))]
    for c in codes:
        f = pdir / (f"resurvey-{c}.txt" if resurvey else f"{c}.txt")
        if not f.exists():
            sys.exit(f"프롬프트 파일이 없습니다: {f} — 먼저 python -m collector prompt --issue-week {week}"
                     + (" --resurvey" if resurvey else "") + " 를 실행하세요")
        parts.append(f"\n==================== [과제 {c}] ====================\n{f.read_text(encoding='utf-8').strip()}\n")
    example = '{"agent":"서비스와 모델 이름",' + ",".join(f'"{c}":{{"candidates":[...]}}' for c in codes) + "}"
    parts.append(FOOTER.format(example=example))
    out = pdir / ("ALL-resurvey.txt" if resurvey else "ALL.txt")
    out.write_text("".join(parts), encoding="utf-8")
    print(out)


def _strip_fences(text):
    return re.sub(r"^\s*```[a-zA-Z]*\s*$", "", text, flags=re.M).strip()


def join_parts(texts):
    """잘린 답과 이어받은 답을 하나로 잇는다. 각 조각의 코드 펜스는 지운다."""
    return "".join(_strip_fences(t) for t in texts)


def parse_answer(text):
    """반환: 통합 답 객체. 끝이 잘렸으면 Truncated."""
    start = text.find("{")
    if start < 0:
        raise ValueError("JSON 객체를 찾지 못했습니다")
    body = text[start:]
    try:
        obj, _ = json.JSONDecoder().raw_decode(body)
        return obj
    except json.JSONDecodeError as e:
        if body.count("{") > body.count("}") or body.count("[") > body.count("]") or e.pos >= len(body.rstrip()) - 1:
            raise Truncated(f"답이 {len(body):,}자에서 끊겼습니다") from e
        raise ValueError(f"JSON 형식 오류: {e}") from e


def detect_agent(text, obj=None):
    """답의 agent 표식(없으면 앞부분 글)으로 gpt·gemini를 가린다. 모르면 None."""
    label = obj.get("agent") if isinstance(obj, dict) else None
    if not isinstance(label, str):
        m = re.search(r'"agent"\s*:\s*"([^"]*)"', text)
        label = m.group(1) if m else ""
    s = label.lower()
    is_gem = any(k in s for k in ("gemini", "google", "bard"))
    is_gpt = any(k in s for k in ("gpt", "chatgpt", "openai"))
    if is_gem != is_gpt:
        return "gemini" if is_gem else "gpt"
    return None


def split(week, answer_paths, agent, resurvey):
    _, cdir = _paths(week)
    texts = [Path(p).read_text(encoding="utf-8") for p in answer_paths]
    text = join_parts(texts)
    try:
        obj = parse_answer(text)
    except Truncated as e:
        guess = detect_agent(text) or agent or "어느 모델"
        print(f"답이 잘렸습니다: {e}. {guess} 앱에 \"잘렸으니 이어서 계속 출력해줘\"라고 요청해 받은 답을 추가로 주세요.")
        sys.exit(2)
    except ValueError as e:
        sys.exit(f"읽을 수 없습니다: {e}")
    found_agent = detect_agent(text, obj)
    if agent and found_agent and agent != found_agent:
        print(f"주의: --agent {agent}로 지정했지만 답의 표식은 {found_agent}입니다. 지정한 값을 씁니다.")
    agent = agent or found_agent
    if not agent:
        print(f"답의 agent 표식({obj.get('agent')!r})으로 GPT·Gemini를 가릴 수 없습니다. --agent gpt|gemini로 지정하세요.")
        sys.exit(3)

    codes = RESURVEY_TOPICS if resurvey else TOPICS
    plan = []
    for c in codes:
        val = obj.get(c)
        if val is None:
            print(f"  {c}: 답에 없음(건너뜀)")
            continue
        if isinstance(val, list):
            val = {"candidates": val}
        dest = cdir / (f"{agent}-resurvey-{c}.json" if resurvey else f"{agent}-{c}.json")
        if dest.exists():
            sys.exit(f"이미 있습니다: {dest} — 덮어쓰지 않습니다")
        plan.append((c, val, dest))
    if not plan:
        sys.exit("주제 코드 키(T1~T4, B)가 있는 결과가 없습니다")

    cdir.mkdir(parents=True, exist_ok=True)
    raw = cdir / "raw"
    raw.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for i, t in enumerate(texts, 1):
        (raw / f"{agent}{'-resurvey' if resurvey else ''}-{stamp}-part{i}.txt").write_text(t, encoding="utf-8")
    print(f"모델: {agent} (표식 {obj.get('agent')!r})")
    for c, val, dest in plan:
        dest.write_text(json.dumps(val, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"  {c}: {len(val.get('candidates', []))}건 → {dest.name}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("combine")
    p.add_argument("--issue-week", required=True)
    p.add_argument("--resurvey", action="store_true")
    p = sub.add_parser("split")
    p.add_argument("--issue-week", required=True)
    p.add_argument("--agent", choices=["gpt", "gemini"], help="답의 표식으로 가릴 수 없을 때만 지정")
    p.add_argument("answers", nargs="+", help="받은 답 파일. 이어받은 답이 있으면 순서대로 덧붙인다")
    p.add_argument("--resurvey", action="store_true")
    a = ap.parse_args()
    if a.cmd == "combine":
        combine(a.issue_week, a.resurvey)
    else:
        split(a.issue_week, a.answers, a.agent, a.resurvey)


if __name__ == "__main__":
    main()
