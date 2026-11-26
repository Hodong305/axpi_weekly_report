# 사외 수집 운영 절차 (모든 실행 주체 공통)

이 저장소의 사외 수집은 사람이 직접 하든 Claude Code·Codex·Gemini CLI 같은 에이전트가 하든 **아래 절차와 명령만으로** 진행한다.
기준 문서는 `docs/MTC-AX-Unified-Design-v4.5.md`이고, 결과 형식은 `schema/MTC-AX-collection-v4.5.schema.json`이다.

## 에이전트가 지켜야 할 규칙

1. 결과 JSON(`out/`)을 직접 작성하거나 고치지 않는다. 모든 레코드는 `python -m collector run`이 만든다.
2. LLM이 하는 일은 **후보 URL 탐색뿐**이다(3단계). 본문·날짜·제목을 쓰지 않는다. 열어 보지 않은 URL을 만들지 않는다.
3. 탐색 결과 파일은 자기 이름(claude / gpt / gemini)으로만 저장한다. 다른 모델의 파일을 고치거나 지우지 않는다.
4. `--weeks 2`는 사용자가 "지난주 발행을 건너뛰었다"고 말한 경우에만 쓴다. `--force`·`--skip-registry`는 시험용이며 운영에서 쓰지 않는다.
5. `config.yaml`·`sources.yaml`·`queries.yaml`·`prompts/`는 사용자 승인 없이 바꾸지 않는다.
6. 아래 **[검토 요청]** 지점에서는 제시된 형식으로 사용자에게 묻고, 사용자가 고른 대로 진행한다.

## 매주 일요일 절차

0. 준비(처음 한 번): `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`
   - 선택: `pip install playwright && playwright install chromium` (봇 차단·자바스크립트 페이지 재시도)
   - 처음 한 번 `python -m collector check-sources`로 발행처를 점검하고, 정상인 항목은 `sources.yaml`에서 `verified: true`로 바꾼다.
1. 기간 확인: `python -m collector window` → 출력된 `issue_week`를 이후 명령에 쓴다.
2. 프롬프트 생성: `python -m collector prompt --issue-week YYYY-Www`
   → `work/YYYY-Www/prompts/` 에 `T1.txt T2.txt T3.txt T4.txt B.txt` 생성
3. 후보 탐색(LLM, 웹 검색 켜기): 프롬프트 파일 내용을 **수정 없이** 그대로 입력하고, 답을 그대로 저장한다.
   - 저장 위치: `work/YYYY-Www/candidates/{claude|gpt|gemini}-{T1|T2|T3|T4|B}.json`
   - 여러 모델을 돌릴수록 후보가 늘어난다. 같은 URL은 자동으로 하나로 합쳐진다.
   - 사람이 직접 찾은 URL은 `work/YYYY-Www/manual/urls.txt`에 한 줄씩 적는다.
4. 수집 실행: `python -m collector run --issue-week YYYY-Www`
5. 리포트 확인: `out/YYYY-Www/mtc-ax-YYYYWww-report-r{n}.md`
6. **[검토 요청] 재조사** — 리포트에 "재조사 필요"가 있으면 사용자에게 묻는다.
   ```
   complete {n}건으로 기준 {intake_min}건에 못 미칩니다.
   1) 재조사 실행 (권장): 재조사 프롬프트 4개를 만들어 LLM 탐색 후 다시 수집
   2) 이대로 패키징
   ```
   1이면 `python -m collector prompt --issue-week YYYY-Www --resurvey` → `candidates/{agent}-resurvey-{T1..T4}.json` 저장 → 4단계 재실행.
7. **[검토 요청] 수동 보완** — 리포트의 '수동 보완 큐'가 비어 있지 않으면 묻는다.
   ```
   수동 보완 대상 {n}건 (목록: 리포트 참조)
   1) 건너뛰고 패키징 (권장: 시간이 없을 때)
   2) 처리 후 재실행: 안내된 파일 저장·overrides.yaml 작성 → run 재실행
   ```
   실패 건은 다음 주에 다시 시도하지 않는다. 이번 실행에서 처리하지 않으면 그대로 끝난다.
8. 패키징: `python -m collector package --issue-week YYYY-Www`
   → `out/YYYY-Www/package/` 의 `intake ….json.gz`, `report ….md`, `manifest ….json` 세 파일을 사내 메일로 보낸다.

## 지난주를 건너뛴 경우(사용자 요청 시에만)

모든 명령에 `--weeks 2`를 붙인다. 수집 기간이 직전 14일이 된다. 같은 주차를 1주·2주로 섞어 실행하지 않는다.
