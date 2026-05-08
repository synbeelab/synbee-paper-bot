# Setup Guide — SynBEE Paper Bot Pipeline A

> 처음 운영을 시작하기 위한 1회성 셋업 절차. 약 30분.

## 0. 사전 요구

- Python 3.10 이상 (권장 3.12)
- Slack 워크스페이스 admin 권한 (앱 설치 권한)
- Google AI Studio 계정 → Gemini API 키
- (선택) NCBI 계정 → API 키 (rate limit 3→10 req/sec)

---

## 1. 로컬 환경

```powershell
cd D:\AI_Projects\synbee-paper-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env
cp config\config.yml.example config\config.yml
```

---

## 2. Gemini API 키

1. https://aistudio.google.com/apikey 접속 → Google 계정 로그인
2. **Create API key** → 새 프로젝트에 생성 권장
3. 발급된 키를 `.env`의 `GEMINI_API_KEY=` 뒤에 붙여넣음
4. **무료 티어 한도** (2026-05 기준):
   - Gemini 2.5 Flash-Lite: 분당 ~30 RPM, 일 ~1,000 RPD
   - Pipeline A 일평균 18편 → 무료로 충분

검증:

```powershell
py -c "import os, google.generativeai as g; from dotenv import load_dotenv; load_dotenv(); g.configure(api_key=os.environ['GEMINI_API_KEY']); m = g.GenerativeModel('gemini-2.5-flash-lite'); print(m.generate_content('say ok').text)"
```

---

## 3. Slack App 생성

1. https://api.slack.com/apps → **Create New App** → **From a manifest**
2. 워크스페이스 선택 (예: SynBEE Lab)
3. `scripts/slack_app_manifest.yaml` 내용을 그대로 붙여넣기 → **Next** → **Create**
4. 좌측 **OAuth & Permissions** → **Install to Workspace** → 권한 승인
5. 발급된 **Bot User OAuth Token** (`xoxb-…`)을 `.env`의 `SLACK_BOT_TOKEN=`에 붙여넣기

### 채널 생성

Slack에서 다음 채널을 생성 (기존 채널을 써도 무방):

| 채널 | 용도 |
|---|---|
| `#papers-test` | 운영 시작 1~2주는 여기로만 푸시 (튜닝 단계) |
| `#papers-daily` | 검증 후 정식 일일 다이제스트 |
| `#papers-priority` | score 9+만 별도로 받고 싶을 때 (선택) |

각 채널에서 봇 초대:

```
/invite @SynBEE Paper Bot
```

### 토큰 검증 + 채널 ID 조회

```powershell
py scripts\slack_setup_helper.py
```

출력에서 채널 ID(`C0XXXXXXXX`) 복사 → `config/config.yml`에 붙여넣기:

```yaml
slack:
  enabled: true
  channels:
    daily_digest:  "C0XXXXXXXX"
    high_priority: "C0XXXXXXXX"
    test:          "C0XXXXXXXX"
  use_test_channel: true   # 1~2주 후 false로
```

테스트 메시지 송신:

```powershell
py scripts\slack_setup_helper.py --post-test "🐝 SynBEE Bot 셋업 완료!" --channel C0XXXXXXXX
```

---

## 4. NCBI API key (선택, 권장)

1. https://account.ncbi.nlm.nih.gov/ 로그인 → Account Settings
2. **API Key Management** → **Create an API Key**
3. 발급된 키를 `.env`의 `NCBI_API_KEY=`에 붙여넣기
4. 효과: rate limit 3 → 10 req/sec (sanity check `--per-journal` 3배 빨라짐)

---

## 5. 첫 dry-run

```powershell
# 1. PubMed만, LLM·Slack 모두 건너뛰고 데이터 경로 확인
py scripts\run_daily.py --dry-run --no-llm --no-slack --no-rss --no-biorxiv --since-days 1 --limit 5

# 2. 전체 소스 + LLM 필터까지 가동, Slack은 건너뛰기 (비용·노이즈 점검)
py scripts\run_daily.py --dry-run --no-slack --since-days 1

# 3. 최종 — 테스트 채널로 푸시
py scripts\run_daily.py --since-days 1
```

---

## 6. GitHub Actions 자동화

### 6.1 Repo 생성 + 푸시

```powershell
cd D:\AI_Projects\synbee-paper-bot
git init -b main
git add .
git commit -m "feat: initial Pipeline A setup"
gh repo create synbee-paper-bot --private --source . --push
```

(GitHub CLI `gh`가 없다면 GitHub 웹에서 repo 만들고 `git remote add origin … && git push`)

### 6.2 Secrets 등록

GitHub repo → **Settings → Secrets and variables → Actions → New repository secret**

| Secret 이름 | 값 |
|---|---|
| `SLACK_BOT_TOKEN` | `.env`의 `xoxb-…` |
| `GEMINI_API_KEY` | Gemini API 키 |
| `SLACK_DAILY_CHANNEL` | `C0XXXXXXXX` (#papers-daily) |
| `SLACK_PRIORITY_CHANNEL` | (선택) |
| `SLACK_TEST_CHANNEL` | `C0XXXXXXXX` (#papers-test) |
| `USE_TEST_CHANNEL` | `true` (1~2주 후 `false`) |
| `NCBI_API_KEY` | (선택) |
| `NCBI_EMAIL` | `dosoyang@korea.ac.kr` |
| `ANTHROPIC_API_KEY` | (선택, 비교 검증용) |

### 6.3 수동 트리거로 첫 실행

repo → **Actions → SynBEE daily paper digest → Run workflow**

- `since_days`: 비워두면 1, 백필하려면 7
- `dry_run`: 첫 실행은 `true` 권장

성공하면 다음날 KST 08:00부터 자동 실행.

---

## 7. 운영 첫 2주 체크리스트

- [ ] Slack 푸시 결과 매일 살펴보기
- [ ] `use_test_channel: true` 유지 (오류 시 워크스페이스 노이즈 방지)
- [ ] False positive 패턴 발견 → `config/keywords.yml`의 `exclude` 추가
- [ ] 누락 사례 발견 → 키워드/저널 추가 후 `python scripts/sanity_check.py`로 hit 수 재확인
- [ ] 주 1회 `python scripts/build_query.py`로 합성 쿼리 검토
- [ ] 2주차에 `use_test_channel: false`로 전환

---

## 8. 자주 겪는 문제

| 증상 | 원인·해결 |
|---|---|
| `HTTP 414 Request-URI Too Long` | NCBI 쿼리가 길어졌을 때. `sanity_check.py`는 이미 POST 사용 중. `paperscraper` 등 외부 도구 쓸 때 발생할 수 있음. |
| `not_in_channel` Slack 오류 | 봇이 채널에 초대 안 됨. `/invite @SynBEE Paper Bot` |
| `invalid_auth` Slack 오류 | `xoxb-` 토큰이 잘못됨. App-Level(`xapp-`)와 혼동했을 가능성. |
| Gemini quota 초과 | 무료 티어 일 할당량 초과. 모델을 `gemini-2.5-flash`(유료지만 더 큰 quota)로 변경하거나 Pro로 업그레이드. |
| 한 편당 LLM 응답이 5초+ | parallel_requests 4 → 8로 올림 (config.yml). |
| 매일 같은 논문이 다시 푸시됨 | seen.db가 매번 초기화됨. GitHub Actions cache 키 확인. |

---

## 9. 다음 단계

- [ ] **Pipeline B 시작** — `D:\AI_Projects\synbee-pdf-bot` 별도 repo로 PDF 심층 요약 봇 구축
- [ ] "📥 위키 후보 저장" 버튼이 실제로 SynBEE Wiki(`D:\Obsidian_Vault\Dongsoo`)의 모드 A를 트리거하도록 `process_queue.py` 작성
- [ ] 월 1회 Sonnet 4.6 vs Gemini 비교 회귀 테스트
