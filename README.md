# SynBEE Lab Paper Bot — Pipeline A (알림 + 필터링)

> 양동수 교수(고려대 화공생명공학과, SynBEE Lab) 연구실용 논문 알림·필터링 봇.
> 매일 PubMed / bioRxiv / RSS 신규 논문을 모아 LLM 필터를 통과한 항목만
> Slack에 푸시한다.

## Architecture

```
[Stage 1 — collect]                    [Stage 2 — filter]            [Deliver]
PubMed (journals + keywords)   ─┐
bioRxiv (keywords)              ├──►  Gemini Flash-Lite     ───►  Slack #papers-daily
arXiv q-bio (optional)          │     (yes/no + score)            (Block Kit cards)
RSS (top-tier journals)        ─┘
                                       ↓
                                seen.db (SQLite)
                                wiki_queue (button)
```

## Project layout

```
synbee-paper-bot/
├── config/
│   ├── journals.yml              # 관심 학술지 (티어별, NLM 약어)
│   ├── keywords.yml              # 미션별 키워드 그룹
│   ├── filter_prompt.md          # Stage 2 LLM 프롬프트
│   └── config.yml.example        # 메인 설정 템플릿 (복사 후 사용)
├── synbee_bot/                   # 패키지
│   ├── config.py                 # .env + config.yml 로더
│   ├── models.py                 # Paper / Verdict 데이터클래스
│   ├── sources.py                # PubMed (E-utilities) + bioRxiv API + RSS
│   ├── filter.py                 # Gemini / Anthropic 필터 (Stage 2)
│   ├── slack_dispatch.py         # Block Kit 메시지 빌더
│   └── storage.py                # SQLite seen.db + wiki_queue
├── scripts/
│   ├── build_query.py            # YAML → PubMed/bioRxiv 쿼리 생성
│   ├── sanity_check.py           # 실시간 PubMed hit 수 검증
│   ├── run_daily.py              # 메인 orchestrator (cron 진입점)
│   ├── slack_setup_helper.py     # 토큰 검증 + 채널 목록
│   └── slack_app_manifest.yaml   # Slack App 자동 생성용 manifest
├── .github/workflows/
│   ├── daily.yml                 # 매일 KST 08:00 cron
│   └── sanity-check.yml          # 수동 트리거 hit 수 검증
├── data/                         # SQLite 등 (gitignored)
├── .env.example
├── requirements.txt
├── SETUP.md                      # 1회성 셋업 가이드
└── README.md
```

## Quickstart

전체 셋업은 [SETUP.md](SETUP.md) 참고. 빠른 시작:

```powershell
# 0. 환경
cd D:\AI_Projects\synbee-paper-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env
cp config\config.yml.example config\config.yml

# 1. 쿼리 합성 검증
py scripts\build_query.py

# 2. 실시간 hit 수 검증
py scripts\sanity_check.py --windows 1 7 30

# 3. 데이터 경로 스모크 테스트 (LLM·Slack 모두 건너뜀)
py scripts\run_daily.py --dry-run --no-llm --no-slack --since-days 1 --limit 5

# 4. .env에 GEMINI_API_KEY + SLACK_BOT_TOKEN 채운 뒤 LLM 필터까지
py scripts\run_daily.py --dry-run --since-days 1

# 5. 정식 — 테스트 채널로 푸시
py scripts\run_daily.py --since-days 1
```

## 설정 흐름

1. **`config/journals.yml`** — 사용자 지정 + Critical/Medium/Optional 티어로 정리.
   `active: true`인 그룹만 PubMed 쿼리에 포함. `Optional`은 노이즈 가능성으로 기본 비활성.

2. **`config/keywords.yml`** — SynBEE Lab 3대 미션별 키워드 그룹 + auxiliary + exclude.
   PubMed의 `[tiab]` 필드로 매핑. Mission 키워드는 OR로 묶임.

3. **`config/filter_prompt.md`** — Stage 2에서 Gemini Flash-Lite가 사용하는 판정
   프롬프트. JSON 출력 (verdict / mission / score / one_liner).

4. **`config/config.yml`** — `.example` 복사 후 Slack 토큰·채널 ID·LLM provider 등을 채움.

## 튜닝 사이클 (운영 1~2주 후)

| 빈도 | 행동 |
|---|---|
| 매일 | Slack 푸시 결과 보고 노이즈/누락 라벨링 |
| 주 1회 | `keywords.yml` exclude 보강 (노이즈 패턴 3건 이상이면 추가) |
| 주 1회 | 누락 사례에서 키워드/저널 보강 |
| 월 1회 | `filter_prompt.md` 규칙 보강 |

## Stage 2 LLM 모델 선택

기본: **Gemini 2.5 Flash-Lite** (필터링 yes/no는 가장 저렴한 모델로 충분).

비용 비교(논문 1편당, abstract 입력 ~500 토큰 + JSON 출력 ~100 토큰):

| 모델 | 1편당 | 일 100편 × 30일 |
|---|---|---|
| Gemini Flash-Lite | ~$0.00005 | **~$0.15/월** |
| Gemini 2.5 Flash | ~$0.0002 | ~$0.6/월 |
| Claude Haiku 4.5 | ~$0.0006 | ~$1.8/월 |

## 다음 단계

- [ ] PaperBee fork — Gemini adapter 패치
- [ ] 메인 실행 스크립트 (`scripts/run_daily.py`) — Stage 1+2 통합
- [ ] Slack Block Kit 메시지 빌더 + "위키에 정리" 버튼
- [ ] GitHub Actions cron 워크플로우
- [ ] Pipeline B (PDF 심층 요약) — 별도 repo (`synbee-pdf-bot`)

## CLAUDE.md 통합

이 봇이 알림 / 필터링한 논문은 Slack에서 "📥 위키 후보" 버튼으로 SynBEE Wiki
(`D:\Obsidian_Vault\Dongsoo`)의 ingest 큐에 적재된다. Wiki에서는
`CLAUDE.md` 모드 A 절차로 정리.

## License

Internal use — SynBEE Lab.
