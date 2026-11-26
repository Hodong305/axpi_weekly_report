"""실행 지문(설계서 9.11): 어떤 조건에서 만든 결과인지 남기고, 직전 호와 비교해 바뀐 항목을 알린다."""
import csv
import hashlib
import re

import collector

from . import __version__ as INTERNAL_VERSION
from .prompts import PROMPTS
from .settings import ROOT

FP_KEYS = ["model", "operator_sha256", "steps_sha256", "config_version", "design_version", "collector_version",
           "internal_version"]
LABEL = {"model": "모델 이름", "operator_sha256": "운영 프롬프트", "steps_sha256": "단계 프롬프트", "config_version": "설정 버전",
         "design_version": "설계서 버전", "collector_version": "수집기 버전", "internal_version": "사내 코드 버전"}


def _file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def design_version():
    p = ROOT / "docs" / "MTC-AX-Unified-Design-v4.6.md"
    m = re.search(r"\*\*(v\d+(?:\.\d+)*)\b", p.read_text(encoding="utf-8")) if p.exists() else None
    return m.group(1) if m else "unknown"


def current(settings, model=None):
    return {"model": model, "operator_sha256": _file_sha(PROMPTS / "internal-operator.md"),
            "steps_sha256": _file_sha(PROMPTS / "internal-steps.md"),
            "config_version": str(settings.config.get("config_version", "")), "design_version": design_version(),
            "collector_version": collector.__version__, "internal_version": INTERNAL_VERSION}


def previous(pilot_csv, issue_week):
    """pilot.csv에서 issue_week보다 앞선 가장 최근 호의 지문. 없으면 None."""
    if not pilot_csv.exists():
        return None
    with open(pilot_csv, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r["issue_week"] < issue_week]
    if not rows:
        return None
    r = max(rows, key=lambda r: r["issue_week"])
    fp = {k: r.get("fp_" + k) or None for k in FP_KEYS}
    fp["config_version"] = r.get("config_version") or None
    return fp


def changed(prev, cur):
    """바뀐 항목 키 목록. 한쪽 값이 없는 항목(예: 아직 알 수 없는 모델 이름)은 비교하지 않는다."""
    if not prev:
        return []
    return [k for k in FP_KEYS if prev.get(k) and cur.get(k) and prev[k] != cur[k]]


def notice(keys):
    return ("※ 실행 조건 변경: " + ", ".join(LABEL[k] for k in keys) + " — 직전 호와 다른 조건이므로 판정 차이가 입력 때문만은 아닐 수 있음") if keys else None
