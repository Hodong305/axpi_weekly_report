"""회귀 사례 비교 스크립트.
  python tests/regression/run.py            # 저장된 결과(expected.json)와 비교, 다르면 차이를 보이고 종료 코드 1
  python tests/regression/run.py --update   # 현재 결과로 expected.json 갱신(바꾼 이유를 시범 로그에 남길 것, 설계서 9.11)
같은 비교는 `python -m pytest -q tests/test_regression.py`로도 실행된다."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402


def main(argv):
    actual = harness.run_all()
    path = harness.HERE / "expected.json"
    if "--update" in argv:
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"갱신했습니다: {path}")
        return 0
    expected = harness.load_expected()
    bad = 0
    for name in sorted(set(actual) | set(expected)):
        if actual.get(name) != expected.get(name):
            bad += 1
            print(f"[다름] {name}\n  기대: {json.dumps(expected.get(name), ensure_ascii=False)}\n  실제: {json.dumps(actual.get(name), ensure_ascii=False)}")
        else:
            print(f"[같음] {name}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
