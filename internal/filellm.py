"""파일 모드 모델 백엔드: 사람이나 에이전트가 사내 모델 역할을 한다.
모델 호출마다 review/{issue_week}-llm/{seq:04d}-{step}.request.json을 쓰고, 같은 번호의 .response.json(모델이 돌려줄 JSON 객체)을 읽는다.
응답 파일이 없으면 Pending으로 멈추고, 응답이 오면 resume에서 같은 검증·재시도 규칙으로 이어 간다.
seq는 (단계, 프롬프트 해시, 입력 해시) 키마다 한 번 정해져 index.json에 남으므로, 단계를 다시 실행해도 같은 요청은 같은 번호를 쓴다."""
import json
from pathlib import Path

from .llm import LLMError, Pending, sha


class FileClient:
    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else None
        self.served_model = None

    def set_dir(self, directory):
        self.dir = Path(directory)

    def _index(self):
        p = self.dir / "index.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _paths(self, messages, name):
        self.dir.mkdir(parents=True, exist_ok=True)
        key = f"{name}|{sha(messages[0]['content'])}|{sha(messages[-1]['content'])}"
        idx = self._index()
        if key not in idx:
            idx[key] = max(idx.values(), default=0) + 1
            (self.dir / "index.json").write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
        stem = f"{idx[key]:04d}-{name}"
        return key, self.dir / f"{stem}.request.json", self.dir / f"{stem}.response.json"

    def peek(self, messages, schema, name):
        """요청 파일을 써 두고, 응답이 아직 없으면 요청 파일 경로를, 있으면 None을 돌려준다."""
        key, req, resp = self._paths(messages, name)
        if resp.exists():
            return None
        if not req.exists():
            req.write_text(json.dumps({"step": name, "system": messages[0]["content"], "messages": messages,
                                       "json_schema": schema, "cache_key": key}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return req

    def chat(self, messages, schema=None, name="result"):
        pending = self.peek(messages, schema, name)
        if pending is not None:
            raise Pending([pending])
        _, _, resp = self._paths(messages, name)
        try:
            obj = json.loads(resp.read_text(encoding="utf-8"))
        except ValueError as e:
            raise LLMError(f"응답 파일을 JSON으로 읽을 수 없음({resp.name}): {e}")
        model = None
        props = (schema or {}).get("properties", {})
        if isinstance(obj, dict) and "model" in obj and "model" not in props:
            model = obj.pop("model")          # 선택 항목: 응답을 쓴 쪽의 이름(실행 지문용)
        self.served_model = f"file:{model}" if model else "file"
        return json.dumps(obj, ensure_ascii=False)
