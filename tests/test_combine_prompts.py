import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import combine_prompts as cp  # noqa: E402

FULL = ('설명\n```json\n{"agent":"ChatGPT GPT-5","T1":{"candidates":[{"url":"https://example.com/a","track":"A"}]},'
        '"T2":{"candidates":[]},"B":{"candidates":[{"url":"https://example.com/b","track":"B"}]}}\n```')


def test_detect_agent():
    assert cp.detect_agent("", {"agent": "ChatGPT GPT-5"}) == "gpt"
    assert cp.detect_agent("", {"agent": "Google Gemini 2.5 Pro"}) == "gemini"
    assert cp.detect_agent('{"agent":"Gemini","T1":{"cand', None) == "gemini"
    assert cp.detect_agent("", {"agent": "모름"}) is None


def test_truncated_then_joined():
    cut = 70
    first, rest = FULL[:cut], FULL[cut:]
    with pytest.raises(cp.Truncated):
        cp.parse_answer(cp.join_parts([first]))
    obj = cp.parse_answer(cp.join_parts([first, "```json\n" + rest]))
    assert obj["agent"] == "ChatGPT GPT-5" and len(obj["T1"]["candidates"]) == 1


def test_split_writes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(cp, "ROOT", tmp_path)
    ans = tmp_path / "ans.txt"
    ans.write_text(FULL, encoding="utf-8")
    cp.split("2026-W01", [str(ans)], None, False)
    cdir = tmp_path / "work" / "2026-W01" / "candidates"
    assert sorted(p.name for p in cdir.glob("*.json")) == ["gpt-B.json", "gpt-T1.json", "gpt-T2.json"]
    assert json.loads((cdir / "gpt-T1.json").read_text())["candidates"][0]["url"] == "https://example.com/a"
    assert len(list((cdir / "raw").iterdir())) == 1


def test_split_truncated_exit_code(tmp_path):
    ans = tmp_path / "cut.txt"
    ans.write_text(FULL[:70], encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "combine_prompts.py"), "split",
                        "--issue-week", "2099-W01", str(ans)], capture_output=True, text=True)
    assert r.returncode == 2 and "이어서 계속 출력해줘" in r.stdout
