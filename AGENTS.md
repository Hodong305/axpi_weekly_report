# 사외 수집 운영 절차 (모든 실행 주체 공통)

이 저장소의 사외 수집은 사람이 직접 하든 Claude Code·Codex·Gemini CLI 같은 에이전트가 하든 **아래 절차와 명령만으로** 진행한다.
기준 문서는 `docs/MTC-AX-Unified-Design-v4.6.md`이고, 결과 형식은 `schema/MTC-AX-collection-v4.6.schema.json`이다.

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
   - (선택) `python -m collector check-sources`로 발행처 피드 접속을 점검할 수 있다. 하지 않아도 수집에는 문제가 없다:
     `verified: false`인 발행처는 LLM 프롬프트의 '우선 확인 발행처'에도 들어가 중복으로 확인되고, 순회 실패는 매주 리포트의 '레지스트리 순회' 표에 나온다.
1. 기간 확인: `python -m collector window` → 출력된 `issue_week`를 이후 명령에 쓴다.
2. 프롬프트 생성: `python -m collector prompt --issue-week YYYY-Www`
   → `work/YYYY-Www/prompts/` 에 `T1.txt T2.txt T3.txt T4.txt B.txt` 생성
3. 후보 탐색(LLM, 웹 검색 켜기): 프롬프트 파일 내용을 **수정 없이** 그대로 입력하고, 답을 그대로 저장한다.
   - 저장 위치: `work/YYYY-Www/candidates/{claude|gpt|gemini}-{T1|T2|T3|T4|B}.json`
   - 여러 모델을 돌릴수록 후보가 늘어난다. 같은 URL은 자동으로 하나로 합쳐진다.
   - 사람이 직접 찾은 URL은 `work/YYYY-Www/manual/urls.txt`에 한 줄씩 적는다.
   - (선택) GPT·Gemini 앱에 한 번씩만 넣고 싶을 때: `python tools/combine_prompts.py combine --issue-week YYYY-Www`
     → `prompts/ALL.txt`를 그대로 입력한다. 받은 답은 파일로 저장해
     `python tools/combine_prompts.py split --issue-week YYYY-Www 답.txt`로 주제별 파일로 나눈다(모델은 답의 `agent` 표식으로 구분).
     종료 코드 2(답이 잘림)면 사용자에게 "잘렸으니 이어서 계속 출력해줘"로 받은 답을 추가로 요청하고, 받은 순서대로 파일을 덧붙여 다시 실행한다.
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
8. 패키징·발송: `python -m collector package --issue-week YYYY-Www --send`
   → `out/YYYY-Www/package/`에 압축 intake, 원본 intake, 리포트, manifest(공식 발표 이미지가 있으면 이미지 묶음 zip도)를 만들고 `mail.local.yaml`의 사내 주소로 바로 보낸다.
   - 처음 한 번: `mail.local.example.yaml`을 `mail.local.yaml`로 복사해 채우고, 비밀번호는 환경변수(기본 `MTCAX_SMTP_PASSWORD`)에 넣는다.
   - 첫 주는 `python -m collector send --issue-week YYYY-Www --dry-run`으로 먼저 확인한다.
   - 이미 보낸 revision은 다시 보내지 않는다. 재발송은 사용자가 요청할 때만 `--resend`.

## 지난주를 건너뛴 경우(사용자 요청 시에만)

모든 명령에 `--weeks 2`를 붙인다. 수집 기간이 직전 14일이 된다. 같은 주차를 1주·2주로 섞어 실행하지 않는다.
