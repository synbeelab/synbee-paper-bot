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
RSS (top-tier journals)         │
Crossref ToC (full issue)      ─┘
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
│   ├── toc_journals.yml          # ToC 전수 스윕 대상 (이메일이 부분 목록인 저널)
│   ├── spam_rescue.yml           # 스팸함 구제 설정 (라벨·임계값·안전장치)
│   ├── spam_rescue_prompt.md     # 스팸/정상 판정 프롬프트
│   └── config.yml.example        # 메인 설정 템플릿 (복사 후 사용)
├── synbee_bot/                   # 패키지
│   ├── config.py                 # .env + config.yml 로더
│   ├── models.py                 # Paper / Verdict 데이터클래스
│   ├── sources.py                # PubMed (E-utilities) + bioRxiv API + RSS
│   ├── crossref.py               # Crossref 전수 ToC 스윕 (키워드 게이트 없음)
│   ├── filter.py                 # Gemini / Anthropic 필터 (Stage 2)
│   ├── slack_dispatch.py         # Block Kit 메시지 빌더
│   ├── storage.py                # SQLite seen.db + wiki_queue
│   └── spam_rescue/              # Gmail 스팸함 구제 (독립 서브패키지)
│       ├── gmail.py              #   Gmail REST 클라이언트 (refresh token)
│       ├── classify.py           #   Gemini 스팸/정상 판정
│       └── rescue.py             #   판정 → 라벨 조작 + 안전장치
├── scripts/
│   ├── build_query.py            # YAML → PubMed/bioRxiv 쿼리 생성
│   ├── sanity_check.py           # 실시간 PubMed hit 수 검증
│   ├── run_daily.py              # 메인 orchestrator (cron 진입점)
│   ├── run_spam_rescue.py        # 스팸함 구제 진입점
│   ├── catchup_guard.py          # catch-up cron이 중복 배달하지 않게 막는 guard
│   ├── gmail_auth_setup.py       # Gmail refresh token 1회 발급 헬퍼
│   ├── slack_setup_helper.py     # 토큰 검증 + 채널 목록
│   └── slack_app_manifest.yaml   # Slack App 자동 생성용 manifest
├── .github/workflows/
│   ├── daily.yml                 # 매일 KST 07:47 cron (+ catch-up 10:23·13:23)
│   ├── spam-rescue.yml           # 매일 KST 06:47 cron — 스팸함 구제 (+ catch-up)
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

## Gmail 스팸함 구제 봇

논문 알림과는 별개로, 이 repo에는 매일 **KST 07:00**에 Gmail 스팸함을 스크리닝해
오분류된 정상 메일(타대학 인턴 지원, 세미나 초청, 학회·저널·연구재단 공지 등)만
**스팸 해제 + `안전함` 라벨 + 받은편지함 이동**시키는 워크플로우가 함께 있다.
읽음 상태는 건드리지 않고, 아무것도 삭제하지 않는다.

```powershell
py scripts\run_spam_rescue.py --dry-run    # 판정만 출력, 라벨 변경 없음
py scripts\run_spam_rescue.py              # 적용
```

셋업(Google OAuth 클라이언트 + refresh token 발급)과 안전장치 설명은
[SETUP.md §9-B](SETUP.md) 참고. 별도 repo로 분리하지 않은 이유는
`GEMINI_API_KEY`를 공유하고, 매일 도는 `daily.yml` 덕분에 repo가 계속 활성
상태라 GitHub의 **60일 무활동 자동 비활성화**에 걸리지 않기 때문이다.

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

## 주간 delta 다이제스트와 Crossref ToC 스윕

주간 잡(`scripts/run_weekly.py`, 토 09:30 KST)은 두 갈래로 모아 하나의 delta를 만든다.

1. **PubMed 스윕** — 저널 화이트리스트 + 넓힌 키워드 그물
2. **Crossref ToC 스윕** — `config/toc_journals.yml`의 저널이 그 창에 발행한 **모든** 논문.
   키워드 게이트가 없다.

2번이 필요한 이유는 두 가지다.

**(a) 이메일이 그 호·기간 목록을 다 담지 못하는 저널이 있다.** 2026-08-22 실측 —
Nature Communications는 주당 214~296편 중 12편만(섹션당 3편), PNAS는 호당 84~106편 중
front matter 15~18개만, iScience는 465편 중 50편, Cell Reports는 145편 중 53편.
Cell Press 템플릿이 ~50개 항목에서 끊기 때문에 호가 50을 넘는 둘만 잘린다.

**(a') 이메일은 '호' 단위인데 online-first 논문은 호 배정 전까지 어느 메일에도 없다.**
created 2026-06-01~08-14 기준 미배정 비율 — ScienceDirect 100%, Wiley 79%,
Cell Press 10~87%(Trends Biotechnol 87%, 호 배정 지연 중위 121일), Nature 월간 ~39%,
ACS 21%(J Nat Prod 62%, ACS Cent Sci 49%). AAAS만 0%로 예외다.
영구 손실은 아니지만 지연이 2주~4개월이라 주간 인지로는 누락이 된다.

> ⚠️ 2026-08-24 정정: 첫 감사에서 ACS를 "호의 1.1~6.8%만 노출"로 적었던 것은 **오류**다.
> 제목의 페이지 범위를 목록 범위로 해석하고 본문을 세지 않았다. 세어보니 호 전수였다
> (JACS 148(32) 110=110, Biochemistry 65(16) 18=18). ACS를 스윕에 유지하는 근거는
> 호 커버리지가 아니라 위 ASAP 노출이다.

**(b) PubMed 스윕에 키워드 게이트가 있다.** 제목·초록이 키워드를 비켜가면 애초에 안 잡힌다.
2일 창 실측: 키워드 게이트 스윕 17편 vs Crossref 스윕(두 저널만) 280편.

publisher 사이트를 직접 긁는 방법은 막혀 있다 — pnas.org는 HTTP 403,
nature.com은 303 → idp.nature.com 쿠키 핸드셰이크(RSS도 303).

```bash
python scripts/run_weekly.py --dry-run          # Slack·DB 건드리지 않음
python scripts/run_weekly.py --no-toc           # ToC 스윕만 끄고 기존 동작
```

한 주 분량은 582편(PubMed 85 + Crossref 503, 2026-08-23 실측)이고 전부 LLM 판정을 거친다.
대상 저널을 늘리려면 `config/toc_journals.yml`에서 `active: true`로 바꾸면 된다
(ACS 8종·iScience·Cell Reports는 이미 등재돼 있고 꺼져 있다).
