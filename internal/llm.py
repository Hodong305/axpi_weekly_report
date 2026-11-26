"""사내 모델 클라이언트(OpenAI 호환 /v1/chat/completions)와 구조화 응답 판정기.

- 모든 단계가 같은 모델을 쓰고 temperature는 0이다.
- 먼저 response_format(json_schema)로 요청하고, 서버가 거부하면 스키마를 프롬프트에 넣은 일반 JSON 요청으로 바꾼다.
- 응답은 단계별 JSON 스키마와 코드 검증을 통과해야 한다. 실패하면 한 번 다시 요청하고, 그래도 실패하면
  AutoJudgeFailed로 올려 호출한 단계가 '자동 판정 실패'로 처리하게 한다(설계서 9.4).
"""
import hashlib
import json
import os
import re
from pathlib import Path

import jsonschema
import requests

from .prompts import load_operator, load_steps

TRUNC_MARK = "(이하 생략)"
_DEFAULT_PHRASES = {
    "x_retry": "[이전 응답의 오류 — 고쳐서 다시 답하라]",
    "x_schema": "다음 JSON 스키마를 따르는 JSON 객체 하나만 출력하라. 설명문·코드 펜스를 붙이지 마라.",
    "x_regen": "[재생성 요청] 이전 결과가 다음 검사에 걸렸다. 고쳐서 다시 써라.",
}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LLMError(Exception):
    """접속·응답 형식 오류."""


class AutoJudgeFailed(Exception):
    """재시도 후에도 검증을 통과하지 못함 → '자동 판정 실패'."""

    def __init__(self, step, errors):
        super().__init__(f"{step}: 자동 판정 실패 ({'; '.join(errors)[:300]})")
        self.step = step
        self.errors = errors


def extract_json(text):
    """응답에서 JSON 객체를 꺼낸다. 코드 펜스·앞뒤 설명문이 섞여도 첫 객체를 읽는다."""
    if text is None:
        raise LLMError("빈 응답")
    s = text.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    s = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except ValueError:
        pass
    i = s.find("{")
    if i < 0:
        raise LLMError("JSON 객체를 찾지 못함")
    try:
        obj, _ = json.JSONDecoder().raw_decode(s[i:])
    except ValueError as e:
        raise LLMError(f"JSON 해석 실패: {e}")
    return obj


class LLMClient:
    """chat(messages, schema, name) -> 응답 본문 문자열. 시험에서는 같은 메서드를 가진 가짜 객체로 바꾼다."""

    def __init__(self, base_url, model, api_key=None, timeout=300, session=None, seed=None, schema_hint=None):
        base = base_url.rstrip("/")
        self.url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.seed = seed
        self.schema_hint = schema_hint
        self.session = session or requests.Session()
        self.json_schema_ok = None   # None: 아직 모름, False: 서버가 response_format을 거부함
        self.seed_ok = None          # False: 서버가 seed를 거부함
        self.served_model = None     # 서버가 응답에 적어 준 모델 이름(실행 지문, 9.11)

    @classmethod
    def from_config(cls, llm_cfg):
        env = llm_cfg.get("api_key_env")
        return cls(llm_cfg["base_url"], llm_cfg["model"], os.environ.get(env) if env else None,
                   llm_cfg.get("timeout_sec", 300), seed=llm_cfg.get("seed"))

    def _post(self, body):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            return self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"사내 모델 접속 실패: {e}")

    def chat(self, messages, schema=None, name="result"):
        """요청 → 응답 본문. 서버가 response_format 또는 seed를 거부하면(400 등) 해당 항목을 빼고 다시 요청하고 기억한다."""
        for _ in range(3):
            body = {"model": self.model, "messages": messages, "temperature": 0}
            use_seed = self.seed is not None and self.seed_ok is not False
            use_rf = schema is not None and self.json_schema_ok is not False
            if use_seed:
                body["seed"] = self.seed
            if use_rf:
                body["response_format"] = {"type": "json_schema",
                                           "json_schema": {"name": name, "schema": schema, "strict": True}}
            elif schema is not None:
                body["messages"] = _with_schema_hint(messages, schema, self.schema_hint)
            r = self._post(body)
            if r.status_code in (400, 404, 415, 422, 501) and (use_seed or use_rf):
                # 어느 항목을 거부했는지 응답에 'seed'가 있으면 seed, 아니면 response_format을 먼저 뺀다
                if use_seed and ("seed" in (r.text or "").lower() or not use_rf):
                    self.seed_ok = False
                else:
                    self.json_schema_ok = False
                continue
            if use_rf and r.ok:
                self.json_schema_ok = True
            if use_seed and r.ok:
                self.seed_ok = True
            break
        if not r.ok:
            raise LLMError(f"사내 모델 응답 오류 HTTP {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
            self.served_model = data.get("model") or self.served_model
            return data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LLMError("사내 모델 응답 형식 오류(choices 없음)")


def _with_schema_hint(messages, schema, phrase=None):
    phrase = phrase or load_steps().get("x_schema") or _DEFAULT_PHRASES["x_schema"]
    hint = "\n\n" + phrase + "\n" + json.dumps(schema, ensure_ascii=False)
    out = [dict(m) for m in messages]
    out[-1]["content"] += hint
    return out


class ResponseCache:
    """모델 응답 저장(9.11): (단계, 프롬프트 해시, 입력 해시) → 응답 본문. 같은 주를 다시 실행하거나 재개하면 그대로 쓴다.
    읽기를 끄면(--fresh) 저장된 응답을 쓰지 않고 새 응답만 덧붙인다."""

    def __init__(self, path, read=True):
        self.path = Path(path)
        self.read = read
        self.entries = {}
        if read and self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    e = json.loads(ln)
                    self.entries[(e["step"], e["prompt"], e["input"])] = e

    def get(self, key):
        return self.entries.get(key)

    def put(self, key, text, model):
        e = {"step": key[0], "prompt": key[1], "input": key[2], "model": model, "response": text}
        self.entries[key] = e
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


class Judge:
    """시스템 프롬프트 조립, 호출, 스키마·코드 검증, 1회 재시도."""

    def __init__(self, client, operator_prompt=None, steps=None, max_attempts=2, max_input_chars=None):
        self.client = client
        self.operator = operator_prompt if operator_prompt is not None else load_operator()
        self.steps = steps if steps is not None else load_steps()
        self.max_attempts = max_attempts
        self.max_input_chars = max_input_chars
        self.cache = None
        self.calls = []    # (step, 시도 번호, 성공 여부) 기록
        self.cached_model = None

    @property
    def served_model(self):
        return getattr(self.client, "served_model", None) or self.cached_model

    def phrase(self, name):
        return self.steps.get(name) or _DEFAULT_PHRASES[name]

    def source(self, rec, ids=None):
        return source_block(rec, ids, self.max_input_chars)

    def system_prompt(self, step):
        return self.operator + "\n\n[단계 지시]\n" + self.steps[step]

    def _respond(self, step, messages, schema):
        if self.cache is None:
            return self.client.chat(messages, schema, step)
        key = (step, sha(messages[0]["content"]), sha(messages[-1]["content"]))
        hit = self.cache.get(key) if self.cache.read else None
        if hit:
            self.cached_model = hit.get("model") or self.cached_model
            return hit["response"]
        text = self.client.chat(messages, schema, step)
        self.cache.put(key, text, self.served_model)
        return text

    def ask(self, step, user, schema, validate=None):
        """validate(obj) -> 오류 문자열 목록(비어 있으면 통과). 통과한 객체를 반환한다."""
        errors = []
        validator = jsonschema.Draft202012Validator(schema)
        for attempt in range(1, self.max_attempts + 1):
            msg = user if not errors else (
                user + "\n\n" + self.phrase("x_retry") + "\n" + "\n".join(f"- {e}" for e in errors[:10]))
            messages = [{"role": "system", "content": self.system_prompt(step)}, {"role": "user", "content": msg}]
            try:
                obj = extract_json(self._respond(step, messages, schema))
                errs = [f"스키마 {'/'.join(map(str, e.absolute_path))}: {e.message[:120]}"
                        for e in validator.iter_errors(obj)]
                if not errs and validate:
                    errs = list(validate(obj))
            except LLMError as e:
                obj, errs = None, [str(e)]
            self.calls.append((step, attempt, not errs))
            if not errs:
                return obj
            errors = errs
        raise AutoJudgeFailed(step, errors)


# 기사 본문 전달 ------------------------------------------------------------

def _escape(text):
    return text.replace("</source", "<\\/source")


def body_chars(rec):
    return sum(len(b["text"]) for b in rec["body"]["blocks"])


def is_truncated(rec, max_chars):
    return bool(max_chars) and body_chars(rec) > max_chars


def source_block(rec, block_ids=None, max_chars=None):
    """<source doc_id="..."> 태그. block_ids가 None이면 본문 전체. 태그 안은 평가할 데이터로만 쓰인다.
    본문 블록 합계가 max_chars를 넘으면 문서 순서대로 한도까지만 넣고 끝에 (이하 생략)을 붙인다(9.11)."""
    lines = [f"발행처: {rec['publisher']}", f"공개일: {rec['published_date']}", f"제목: {rec['title']}"]
    used, cut = 0, False
    for b in rec["body"]["blocks"]:
        if block_ids is not None and b["id"] not in block_ids:
            continue
        text = b["text"]
        if max_chars and used + len(text) > max_chars:
            room = max_chars - used
            if room > 0:
                lines.append(f"[{b['id']}] {text[:room]}")
            cut = True
            break
        used += len(text)
        lines.append(f"[{b['id']}] {text}")
    if cut:
        lines.append(TRUNC_MARK)
    return f'<source doc_id="{rec["doc_id"]}">\n' + _escape("\n".join(lines)) + "\n</source>"
