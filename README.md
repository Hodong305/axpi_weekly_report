# axpi_weekly_report
MTC AX/PI팀의 제조 AI관련 주간 소식지 발간을 위한 리포

## 구성
- `docs/SETUP-home.md` — 집 PC 설정 체크리스트(사외 수집 운영 준비)
- `docs/MTC-AX-Unified-Design-v4.7.md` — 통합 설계서(현행 기준)
- `schema/MTC-AX-collection-v4.6.schema.json` — 사외 수집 JSON 스키마(Draft 2020-12)
- `AGENTS.md` — 사외 수집 주간 절차(사람·Claude·GPT·Gemini 공통)
- `collector/` — 사외 수집기, `config.yaml`·`sources.yaml`·`queries.yaml` — 설정
- `prompts/` — LLM 후보 탐색 프롬프트, 사내 운영·단계 프롬프트
- `collector/media.py` — 공식 발표 이미지 후보 추출·내려받기·축소(설계서 4.4), `internal/templates/newsletter.html`·`newsletter-b3.html`·`newsletter-b4.html`·`newsletter-b5.html`, `themes.yaml` — 발송본 HTML 템플릿(표 기반·인라인 스타일, 링크·원격 이미지 없음)과 테마 A·B·C·B3·B4·B5(`internal.newsletter_theme`, 기본 B4 요점 카드)
- `tools/render_sample.py`, `docs/samples/` — 견본 원고로 테마별 발송본을 만들어 비교(`python tools/render_sample.py --png` → `out_internal/samples/`), v4.7 결정 사항은 `docs/samples/v4.7-결정과-비교.md`
- `glossary.yaml` — 소식지 약어·용어 풀이(이번 호 첫 등장에 괄호 풀이, 새 용어는 검토 요청 ②의 [G]에서 확정되면 발송 때 추가)
- `tools/compare_runs.py` — 이전 수집 결과와 새 수집 결과 비교(`python tools/compare_runs.py OLD.json NEW.json --out report.md`)
- `internal/` — 사내 파이프라인(`python -m internal run --package DIR`, 설계서 9장). `--llm-backend file`이면 요청·응답 파일로 사람·에이전트가 모델 역할을 하고(종료 코드 3), `resume --answer "1"`로 검토 요청에 비대화형으로 답한다(종료 코드 4). `--answer`를 여러 번 쓰면 상한 확인·C 재수정 확인 같은 이어지는 질문에 차례로 답하고, 질문에서 멈추면 다음 `resume --answer`가 그 질문에 이어서 답한다
- `tools/combine_prompts.py` — 탐색 프롬프트 5개를 GPT·Gemini 앱용 통합 프롬프트 하나로 합치고(`combine`), 받은 통합 답을 주제별 후보 파일로 나눔(`split`, 잘린 답 이어받기 지원)
- `tools/migrate_v42.py` — v4.2 수집 결과를 v4.6으로 이관(`python tools/migrate_v42.py OLD.json --issue-week 2026-W48`)
- `tests/` — 오프라인 통합 시험
- `docs/archive/` — 이전 버전 설계서

## 빠른 시작(사외)
```
pip install -r requirements.txt
python -m collector check-sources          # 선택: 발행처 피드 접속 점검
python -m collector window                 # 이번 주차·기간 확인
python -m collector prompt                 # LLM 탐색 프롬프트 5개 생성 → 결과를 candidates/에 저장
python -m collector run                    # 수집·추출·검증·리포트
python -m collector package --send         # 사내 발송 파일 생성 + 사내 메일 발송(첫 주는 send --dry-run으로 확인)
```
