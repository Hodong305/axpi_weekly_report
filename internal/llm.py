"""사내 모델 클라이언트(OpenAI 호환 /v1/chat/completions)와 구조화 응답 판정기.

- 모든 단계가 같은 모델을 쓰고 temperature는 0이다.
- 먼저 response_format(json_schema)로 요청하고, 서버가 거부하면 스키마를 프롬프트에 넣은 일반 JSON 요청으로 바꾼다.
- 응답은 단계별 JSON 스키마와 코드 검증을 통과해야 한다. 실패하면 한 번 다시 요청하고, 그래도 실패하면
  AutoJudgeFailed로 올려 호출한 단계가 '자동 판정 실패'로 처리하게 한다(설계서 9.4).
"""
import json
import os
import re

import jsonschema
import requests

from .prompts import load_operator, load_steps


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

    def __init__(self, base_url, model, api_key=None, timeout=300, session=None):
        base = base_url.rstrip("/")
        self.url = base + ("/chat/completions" if base.endswith("/v1") else "/v1/chat/completions")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.json_schema_ok = None   # None: 아직 모름, False: 서버가 response_format을 거부함

    @classmethod
    def from_config(cls, llm_cfg):
        env = llm_cfg.get("api_key_env")
        return cls(llm_cfg["base_url"], llm_cfg["model"], os.environ.get(env) if env else None,
                   llm_cfg.get("timeout_sec", 300))

    def _post(self, body):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            return self.session.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise LLMError(f"사내 모델 접속 실패: {e}")

    def chat(self, messages, schema=None, name="result"):
        body = {"model": self.model, "messages": messages, "temperature": 0}
        use_rf = schema is not None and self.json_schema_ok is not False
        if use_rf:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": name, "schema": schema, "strict": True}}
        elif schema is not None:
            body["messages"] = _with_schema_hint(messages, schema)
        r = self._post(body)
        if use_rf and r.status_code in (400, 404, 415, 422, 501):
            # 서버가 json_schema 응답 형식을 지원하지 않음 → 일반 JSON 요청으로 전환
            self.json_schema_ok = False
            plain = {k: v for k, v in body.items() if k != "response_format"}
            plain["messages"] = _with_schema_hint(messages, schema)
            r = self._post(plain)
        elif use_rf and r.ok:
            self.json_schema_ok = True
        if not r.ok:
            raise LLMError(f"사내 모델 응답 오류 HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("사내 모델 응답 형식 오류(choices 없음)")


def _with_schema_hint(messages, schema):
    hint = ("\n\n다음 JSON 스키마를 따르는 JSON 객체 하나만 출력하라. 설명문·코드 펜스를 붙이지 마라.\n"
            + json.dumps(schema, ensure_ascii=False))
    out = [dict(m) for m in messages]
    out[-1]["content"] += hint
    return out


class Judge:
    """시스템 프롬프트 조립, 호출, 스키마·코드 검증, 1회 재시도."""

    def __init__(self, client, operator_prompt=None, steps=None, max_attempts=2):
        self.client = client
        self.operator = operator_prompt if operator_prompt is not None else load_operator()
        self.steps = steps if steps is not None else load_steps()
        self.max_attempts = max_attempts
        self.calls = []    # (step, 시도 번호, 성공 여부) 기록

    def system_prompt(self, step):
        return self.operator + "\n\n[단계 지시]\n" + self.steps[step]

    def ask(self, step, user, schema, validate=None):
        """validate(obj) -> 오류 문자열 목록(비어 있으면 통과). 통과한 객체를 반환한다."""
        errors = []
        validator = jsonschema.Draft202012Validator(schema)
        for attempt in range(1, self.max_attempts + 1):
            msg = user if not errors else (
                user + "\n\n[이전 응답의 오류 — 고쳐서 다시 답하라]\n" + "\n".join(f"- {e}" for e in errors[:10]))
            messages = [{"role": "system", "content": self.system_prompt(step)}, {"role": "user", "content": msg}]
            try:
                obj = extract_json(self.client.chat(messages, schema, step))
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


def source_block(rec, block_ids=None):
    """<source doc_id="..."> 태그. block_ids가 None이면 본문 전체. 태그 안은 평가할 데이터로만 쓰인다."""
    lines = [f"발행처: {rec['publisher']}", f"공개일: {rec['published_date']}", f"제목: {rec['title']}"]
    for b in rec["body"]["blocks"]:
        if block_ids is None or b["id"] in block_ids:
            lines.append(f"[{b['id']}] {b['text']}")
    return f'<source doc_id="{rec["doc_id"]}">\n' + _escape("\n".join(lines)) + "\n</source>"
