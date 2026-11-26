"""S1 반입 검증(설계서 9.1, 7.3): manifest의 바이트 수·SHA-256 확인 후 collector.validate.check를 같은 코드로 실행한다."""
import gzip
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from collector.validate import check, zip_lookup


class IntakeError(Exception):
    """반입 검증 실패. 메시지는 사용자에게 그대로 보여 준다."""


@dataclass
class Package:
    directory: Path
    doc: dict
    manifest: dict
    warnings: list = field(default_factory=list)
    media_zip: Path | None = None       # 이미지 묶음(없으면 이미지 없이 만든다)
    media_note: str = ""

    def image_bytes(self, file):
        """패키지의 이미지 묶음에서 파일을 읽는다. 묶음이 없거나 파일이 없으면 None."""
        if self.media_zip is None:
            return None
        with zipfile.ZipFile(self.media_zip) as z:
            return z.read(file) if file in z.namelist() else None

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
    zp = next(iter(sorted(d.glob(f"*-media-r{rev}.zip"))), None)
    media_note = ""
    if zp is not None:        # 이미지 묶음은 선택이지만, 있으면 manifest 해시와 K12(파일·해시)를 확인한다
        info = listed.get(zp.name)
        data = zp.read_bytes()
        if info is None or len(data) != info.get("bytes") or hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise IntakeError(f"S1 반입 검증 실패: 이미지 묶음 {zp.name}이(가) manifest의 바이트 수·SHA-256과 다릅니다")
        try:
            lookup = zip_lookup(zp)
        except zipfile.BadZipFile:
            raise IntakeError(f"S1 반입 검증 실패: 이미지 묶음 {zp.name}을(를) 열 수 없습니다")
    else:
        lookup = None
        n = sum(len(r.get("media") or []) for r in doc.get("records", []))
        media_note = f"이미지 묶음이 없어 이미지 후보 {n}장을 쓰지 않고 이미지 없이 만듭니다" if n else "이미지 후보가 없습니다"
    errors, warnings = check(doc, lead_min_chars, intake_min, lookup)
    if errors:
        raise IntakeError("S1 반입 검증 실패: 코드 검사(7.3) 오류\n" + "\n".join(errors[:20]))
    if manifest.get("issue_week") != doc["issue_week"] or manifest.get("revision") != doc["revision"]:
        raise IntakeError("S1 반입 검증 실패: manifest와 intake의 issue_week·revision이 다릅니다")
    if expected_week and expected_week != doc["issue_week"]:
        raise IntakeError(f"S1 반입 검증 실패: 패키지는 {doc['issue_week']}인데 {expected_week}를 요청했습니다")
    return Package(d, doc, manifest, warnings, zp, media_note)
