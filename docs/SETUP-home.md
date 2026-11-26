# 집 PC 설정 체크리스트 (사외 수집 운영 준비)

사외 수집은 외부 사이트에 접속할 수 있는 **집 PC**에서 실행한다. 이 문서는 처음 한 번 할 설정과 그 뒤 확인할 일을 순서대로 적어 둔 것이다.
세부 절차와 규칙은 `AGENTS.md`, 설계 기준은 `docs/MTC-AX-Unified-Design-v4.5.md`를 따른다.

## 1. 기본 설치 (처음 한 번)

- [ ] **Python 3.11 이상**: `python --version`으로 확인한다.
- [ ] **Git**: `git --version`으로 확인한다.
- [ ] **저장소 받기**: 작업 브랜치는 `main`이 아니라 `ccr-c705cfec-1w8uo7`이다.
  ```
  git clone -b ccr-c705cfec-1w8uo7 https://github.com/Hodong305/axpi_weekly_report
  cd axpi_weekly_report
  ```
- [ ] **가상환경과 패키지**:
  - Windows:
    ```
    python -m venv .venv
    .venv\Scripts\activate
    pip install -r requirements.txt
    ```
  - Mac/Linux:
    ```
    python -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    ```
- [ ] **(선택) 브라우저 재시도 기능**: 봇 차단·자바스크립트 페이지를 다시 시도할 때 쓴다.
  ```
  pip install playwright
  playwright install chromium
  ```
- [ ] **설치 확인**: 아래 두 명령이 모두 통과하면 된다.
  ```
  python -m pytest -q tests
  python -m collector window
  ```
  첫 번째는 모두 통과(passed)해야 하고, 두 번째는 이번 주차와 기간을 출력해야 한다.
- [ ] **(선택) 발행처 피드 접속 점검**: `python -m collector check-sources`. 하지 않아도 수집에는 문제가 없다.

## 2. 모바일에서 원격 실행하기 (처음 한 번)

- [ ] **Claude Code 설치·로그인**: 모바일과 **같은 계정**으로 로그인한다.
- [ ] **원격 연결 켜기**: 아래 둘 중 하나.
  - 저장소 폴더의 터미널에서 `claude remote-control` 실행
  - Claude 데스크톱 앱 > 설정 > Claude Code > Remote Control에서 이 폴더 추가
- [ ] **모바일 연결 확인**: 모바일 Claude Code 앱에서 집 PC 세션이 보이는지 확인한다.
- [ ] **절전 설정**: 집 PC가 절전 모드에 들어가면 연결이 끊긴다. 수집하는 날(일요일)에는 절전이 되지 않게 해 둔다.

## 3. 사내 메일 자동 발송 (처음 한 번 — 계정 정보 준비 후)

- [ ] **설정 파일 만들기**: `mail.local.example.yaml`을 `mail.local.yaml`로 복사해 채운다. 이 파일은 저장소에 올라가지 않는다.
  - `smtp_host`, `smtp_port`, `security`: Gmail이면 `smtp.gmail.com` / `587` / `starttls`
  - `username`, `from_addr`: 보내는 개인 메일 주소
  - `to_addrs`: 받을 사내 메일 주소
- [ ] **비밀번호 환경변수**: 비밀번호는 파일이 아니라 환경변수에 넣는다. Gmail은 계정 비밀번호가 아니라 **앱 비밀번호**를 쓴다.
  - Windows: `setx MTCAX_SMTP_PASSWORD "앱비밀번호"` 실행 후 터미널을 새로 연다.
  - Mac/Linux: `export MTCAX_SMTP_PASSWORD="앱비밀번호"` (셸 설정 파일에 넣어 둔다)
- [ ] **첫 발송 전 미리 보기**: `python -m collector send --issue-week YYYY-Www --dry-run`. 메일은 나가지 않고 첨부 목록·크기만 확인한다.
- [ ] **첫 실제 발송 후 사내 메일함 확인**: 첨부 4개(압축 intake, 원본 intake, 리포트, manifest)가 모두 들어왔는지 본다. 사내 메일이 `.gz`·`.json` 첨부를 막으면 첨부 방식을 바꾼다.

## 4. 첫 실행 확인 — W48 재수집 테스트 (설정 직후 권장)

예전 GPT·Gemini 결과 39건과 현재 수집기 결과를 비교하는 테스트다. 메일은 보내지 않는다.

- [ ] 탐색 프롬프트 만들기: `python -m collector prompt --issue-week 2026-W48`
- [ ] `work/2026-W48/prompts/`의 5개 파일을 Claude·GPT·Gemini(웹 검색 켜기)에 **수정 없이** 넣는다.
- [ ] 답을 `work/2026-W48/candidates/{claude|gpt|gemini}-{T1|T2|T3|T4|B}.json`으로 저장한다. 집 PC의 Claude Code에게 맡겨도 된다.
- [ ] 수집 실행: `python -m collector run --issue-week 2026-W48`
- [ ] 결과 비교:
  ```
  python tools/compare_runs.py <예전 39건 json> out/2026-W48/mtc-ax-2026W48-merged-r1.json --out compare-W48.md
  ```
- [ ] 비교 결과 파일(`compare-W48.md`)과 수집 리포트를 Claude에게 보여 주고 차이를 검토한다.

## 5. 매주 일요일 운영 (설정 완료 후)

`AGENTS.md`의 "매주 일요일 절차"를 그대로 따른다. 모바일에서는 집 PC 세션에 "이번 주 수집해줘"라고 하면 된다.

1. `window` → `prompt` → LLM 후보 탐색 → `run`
2. 재조사·수동 보완은 선택지로 물어본다.
3. `package --send`로 사내 메일 발송까지 끝난다.

## 아직 남은 준비 (집 PC 외)

- [ ] **사내 PC 설치**: 아래 정보를 확인하고, pip이 막혀 있으면 사외에서 패키지 묶음을 미리 준비한다.
  - 사내 PC 운영체제
  - Python 버전
  - 사내에서 pip 설치가 되는지
- [ ] **사내 모델 서버 연결**: `config.yaml`의 `internal.llm`(`base_url`, `model`)에 사내 서버 정보를 넣고 실제 모델로 한 번 실행한다.
- [ ] **소식지 디자인 확정**: A(현재)와 B(초안) 중 방향을 정하고 템플릿에 반영한다.
