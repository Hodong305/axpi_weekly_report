# axpi_weekly_report
MTC AX/PI팀의 제조 AI관련 주간 소식지 발간을 위한 리포

## 구성
- `docs/MTC-AX-Unified-Design-v4.5.md` — 통합 설계서(현행 기준)
- `schema/MTC-AX-collection-v4.5.schema.json` — 사외 수집 JSON 스키마(Draft 2020-12)
- `AGENTS.md` — 사외 수집 주간 절차(사람·Claude·GPT·Gemini 공통)
- `collector/` — 사외 수집기, `config.yaml`·`sources.yaml`·`queries.yaml` — 설정
- `prompts/` — LLM 후보 탐색 프롬프트, 사내 운영 프롬프트
- `tests/` — 오프라인 통합 시험
- `docs/archive/` — 이전 버전 설계서

## 빠른 시작(사외)
```
pip install -r requirements.txt
python -m collector check-sources          # 처음 한 번: 발행처 점검
python -m collector window                 # 이번 주차·기간 확인
python -m collector prompt                 # LLM 탐색 프롬프트 5개 생성 → 결과를 candidates/에 저장
python -m collector run                    # 수집·추출·검증·리포트
python -m collector package                # 사내 발송 파일
```
