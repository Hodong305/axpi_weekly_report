"""프롬프트 로딩. 운영 프롬프트는 prompts/internal-operator.md, 단계 프롬프트는 prompts/internal-steps.md."""
import re

from collector.settings import ROOT

PROMPTS = ROOT / "prompts"


def load_operator(path=None):
    text = (path or PROMPTS / "internal-operator.md").read_text(encoding="utf-8")
    m = re.search(r"<!-- PROMPT START -->\n(.*?)<!-- PROMPT END -->", text, re.S)
    if not m:
        raise ValueError("internal-operator.md에서 PROMPT START/END 구간을 찾지 못했습니다")
    return m.group(1).strip()


def load_steps(path=None):
    """`<!-- STEP name START -->` ~ `<!-- STEP name END -->` 구간을 {name: 본문}으로 읽는다."""
    text = (path or PROMPTS / "internal-steps.md").read_text(encoding="utf-8")
    steps = {m.group(1): m.group(2).strip()
             for m in re.finditer(r"<!-- STEP (\w+) START -->\n(.*?)<!-- STEP \1 END -->", text, re.S)}
    if not steps:
        raise ValueError("internal-steps.md에서 단계 프롬프트를 찾지 못했습니다")
    return steps
