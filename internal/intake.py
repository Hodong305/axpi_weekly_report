"""S1 반입 검증(설계서 9.1, 7.3): manifest의 바이트 수·SHA-256 확인 후 collector.validate.check를 같은 코드로 실행한다."""
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from collector.validate import check


class IntakeError(Exception):
    """반입 검증 실패. 메시지는 사용자에게 그대로 보여 준다."""


@dataclass
class Package:
    directory: Path
    doc: dict
    manifest: dict
    warnings: list = field(default_factory=list)

    @property
    def issue_week(self):
        return self.doc["issue_week"]

    @property
    def records(self):
        return {r["doc_id"]: r for r in self.doc["records"]}


def _latest(directory, pattern):
    found = []
    for p in Path(directory).glob(pattern):
        m = re.search(r"-r(\d+)\.", p.name)
        if m:
            found.append((int(m.group(1)), p))
    return max(found)[1] if found else None


def load_package(directory, lead_min_chars, intake_min=None, expected_week=None):
    d = Path(directory)
    if not d.is_dir():
        raise IntakeError(f"S1 반입 검증 실패: 패키지 폴더가 없습니다 ({d})")
    gz = _latest(d, "*-intake-r*.json.gz")
    if gz is None:
        raise IntakeError("S1 반입 검증 실패: intake 파일(*-intake-r*.json.gz)이 없습니다")
    rev = re.search(r"-r(\d+)\.", gz.name).group(1)
    mf = next(iter(sorted(d.glob(f"*-manifest-r{rev}.json"))), None)
    rp = next(iter(sorted(d.glob(f"*-report-r{rev}.md"))), None)
    if mf is None:
        raise IntakeError(f"S1 반입 검증 실패: manifest 파일(r{rev})이 없습니다")
    if rp is None:
        raise IntakeError(f"S1 반입 검증 실패: 리포트 파일(r{rev})이 없습니다")
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except ValueError as e:
        raise IntakeError(f"S1 반입 검증 실패: manifest를 읽을 수 없습니다 ({e})")

    listed = {f.get("name"): f for f in manifest.get("files", [])}
    for p in (gz, rp):
        info = listed.get(p.name)
        if info is None:
            raise IntakeError(f"S1 반입 검증 실패: manifest에 {p.name}이(가) 없습니다")
        data = p.read_bytes()
        if len(data) != info.get("bytes"):
            raise IntakeError(f"S1 반입 검증 실패: {p.name} 바이트 수 불일치 (manifest {info.get('bytes')}, 실제 {len(data)})")
        if hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise IntakeError(f"S1 반입 검증 실패: {p.name} SHA-256 불일치 — 파일이 손상됐거나 바뀌었습니다")

    try:
        doc = json.loads(gzip.decompress(gz.read_bytes()).decode("utf-8"))
    except (OSError, ValueError) as e:
        raise IntakeError(f"S1 반입 검증 실패: intake를 읽을 수 없습니다 ({e})")
    if doc.get("phase") != "intake":
        raise IntakeError(f"S1 반입 검증 실패: phase가 intake가 아닙니다 ({doc.get('phase')})")
    errors, warnings = check(doc, lead_min_chars, intake_min)
    if errors:
        raise IntakeError("S1 반입 검증 실패: 코드 검사(7.3) 오류\n" + "\n".join(errors[:20]))
    if manifest.get("issue_week") != doc["issue_week"] or manifest.get("revision") != doc["revision"]:
        raise IntakeError("S1 반입 검증 실패: manifest와 intake의 issue_week·revision이 다릅니다")
    if expected_week and expected_week != doc["issue_week"]:
        raise IntakeError(f"S1 반입 검증 실패: 패키지는 {doc['issue_week']}인데 {expected_week}를 요청했습니다")
    return Package(d, doc, manifest, warnings)
