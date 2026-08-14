# Setup Guide — SynBEE Paper Bot Pipeline A

> 처음 운영을 시작하기 위한 1회성 셋업 절차. 약 30~60분 (Python 설치 시간 포함).
> 이 문서는 실제 셋업 과정에서 부딪힌 함정들을 모두 반영한 버전입니다.

## 0. 사전 요구

- **Python 3.13.x** (python.org 공식 인스톨러로 설치된 것 — 자세한 이유는 §1 참고)
  - ❌ Python 3.14 권장 안 함 — 일부 패키지·표준 라이브러리 경로 이슈
  - ❌ Microsoft Store의 Python 스텁 사용 금지
  - ❌ Python Install Manager (PyMan) MSIX 배포 비권장
- **Slack 워크스페이스 admin 권한** (앱 설치 가능해야 함)
- **Google AI Studio 계정** → Gemini API 키
- (선택) **NCBI 계정** → API 키 (rate limit 3→10 req/sec)
- **Windows 11 + PowerShell** 환경 가정 (다른 OS는 명령만 변경)

---

## 1. Python 환경 정비 ⚠️

### 1.1 기존 Python 점검

```powershell
python --version
```

| 출력 | 상태 |
|---|---|
| `Python 3.13.x` | ✅ 정상 |
| `Python 3.14.x` | ⚠️ 표준 라이브러리 경로 이슈 가능 — 3.13 다운그레이드 권장 |
| `Python` 한 줄만 출력 | ❌ Microsoft Store 스텁. 진짜 Python 아님. §1.2로 |
| 명령 자체가 안 뜸 | Python 미설치. §1.2로 |

**`sys.base_prefix` 검증** (3.14 사용자라면 반드시):

```powershell
python -c "import sys; print(sys.base_prefix)"
```

기대: `C:\Python313` (또는 설치 경로). 만약 **현재 작업 디렉터리가 출력**되거나
`Could not find platform independent libraries <prefix>` 경고가 함께 뜨면 →
설치 깨짐. 재설치 필요.

### 1.2 Python 3.13 설치 (권장 경로)

1. **기존 Python 제거** (Settings GUI):
   ```powershell
   start ms-settings:appsfeatures
   ```
   검색: `Python` → 발견되는 모든 항목 제거 (`Python 3.14`, `Python Launcher`, `Python Install Manager` 등)

2. **Microsoft Store 스텁 끄기**:
   - **Settings → Apps → Advanced app settings → App execution aliases**
   - `App Installer · python.exe`, `python3.exe` → **OFF**

3. **python.org 공식 인스톨러 다운로드**:
   - https://www.python.org/downloads/release/python-3132/
   - 페이지 하단 → **Windows installer (64-bit)** (`python-3.13.2-amd64.exe`)

4. **설치**:
   - ✅ **Add python.exe to PATH** 체크 필수
   - **Customize installation**:
     - Optional Features: 모두 체크
     - Advanced Options:
       - ✅ Install Python 3.13 for all users
       - **Customize install location**: `C:\Python313`

5. **새 PowerShell 창 열고 검증**:
   ```powershell
   python --version                                # Python 3.13.2
   python -c "import sys; print(sys.base_prefix)"  # C:\Python313 ← 이게 핵심
   ```

### 1.3 venv + 의존성

```powershell
cd D:\AI_Projects\synbee-paper-bot

# (이전에 만든 깨진 venv가 있으면)
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue

# venv 생성
python -m venv .venv
```

**ExecutionPolicy 확인** — Activate.ps1이 차단되는 흔한 케이스:

```powershell
Get-ExecutionPolicy -Scope CurrentUser
```

`Restricted` 또는 `Undefined`로 나오면:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
# Y 입력
```

활성화 + 의존성 설치:

```powershell
.\.venv\Scripts\Activate.ps1
# 프롬프트가 (.venv) 로 시작하면 활성화 성공

python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 1.4 설정 파일 복사

```powershell
copy .env.example .env
copy config\config.yml.example config\config.yml
```

### 1.5 import 검증

```powershell
python -c "import yaml, slack_sdk, slack_bolt, google.generativeai, requests, feedparser, dotenv; print('all imports OK')"
```

`all imports OK` 나오면 §1 완료.

---

## 2. Gemini API 키

1. https://aistudio.google.com/apikey 접속 → Google 계정 로그인
2. **Create API key** → "Create API key in new project" 권장
3. 발급된 키를 `.env`의 `GEMINI_API_KEY=` 뒤에 붙여넣음:
   ```powershell
   notepad .env
   ```

**무료 티어 한도** (2026-05 기준 — 모델별 분리됨, 주의):

| 모델 | RPM | RPD (일) |
|---|---|---|
| Gemini 2.5 Flash | 10 | **250** ← 권장 |
| Gemini 2.5 Flash-Lite | 15 | **20** (매우 적음, 18편/일이면 사실상 부족) |
| Gemini 2.5 Pro | 5 | 50 |

→ `config\config.yml`에서 `model: "gemini-2.5-flash"` 사용 권장.
   18편/일 운영이면 free tier로 충분.

**Fallback 체인** (`config\config.yml`에 기본 설정됨):

```yaml
llm_filter:
  model: "gemini-2.5-flash"          # primary
  fallback_models:
    - "gemini-2.5-flash-lite"        # primary 503/quota 시 자동 전환
```

primary가 503/quota/SDK 에러면 자동으로 Flash-Lite로 fallback. 추가 설정 불필요.

**더 안정적으로 가려면 Tier 1 빌링 활성화**:

1. https://aistudio.google.com/apikey → 좌측 **Billing**
2. **Set up Billing** → Google Cloud 결제 계정 연결
3. 카드 등록만 하면 자동으로 Tier 1 적용

| 항목 | Free Tier | Tier 1 (Billing on) |
|---|---|---|
| Flash 2.5 RPD | 250 | 무제한 (실질) |
| Flash-Lite RPD | 20 | 무제한 (실질) |
| Pro 2.5 RPD | 50 | 무제한 (실질) |
| 월 비용 (18편/일 운영 시) | $0 | **~$0.05** |

빌링 켜도 사용한 만큼만 청구됩니다 — 월 5센트 정도. 226편 백필도 가능.

검증:

```powershell
python -c "import os; from dotenv import load_dotenv; load_dotenv(); from google import genai; client = genai.Client(api_key=os.environ['GEMINI_API_KEY']); resp = client.models.generate_content(model='gemini-2.5-flash', contents='say ok'); print(resp.text)"
```

`ok` 비슷한 응답 나오면 API 연결 정상.

> ⚠️ Client를 반드시 변수에 할당하고 사용하세요.
> `genai.Client(...).models.generate_content(...)`처럼 체이닝하면
> `RuntimeError: Cannot send a request, as the client has been closed`
> 발생 (google-genai 2.0+의 GC 이슈).

---

## 3. Slack App 생성

### 3.1 manifest로 앱 만들기

1. https://api.slack.com/apps → **Create New App** → **From a manifest**
2. 워크스페이스 선택 (예: SynBEE Lab)
3. **YAML / JSON 탭 둘 중 하나 선택**:

   | 탭 | 사용할 파일 |
   |---|---|
   | **YAML** (기본 권장) | `scripts\slack_app_manifest.yaml` |
   | **JSON** | `scripts\slack_app_manifest.json` |

   ⚠️ **YAML 내용을 JSON 탭에 붙여넣으면** `Expecting 'STRING','NUMBER',...` 같은
   파서 에러가 납니다. 탭과 파일을 정확히 맞추세요.

4. 클립보드에 복사:
   ```powershell
   # YAML 사용 시
   Get-Content scripts\slack_app_manifest.yaml | Set-Clipboard
   # JSON 사용 시
   Get-Content scripts\slack_app_manifest.json -Raw | Set-Clipboard
   ```

5. 해당 탭에 붙여넣기 → **Next** → **Create**
6. 좌측 **OAuth & Permissions** → **Install to Workspace** → 권한 승인
7. 발급된 **Bot User OAuth Token** (`xoxb-…`)을 `.env`의 `SLACK_BOT_TOKEN=`에 붙여넣기

### 3.2 채널 생성

Slack에서 다음 채널을 생성 (기존 채널 재사용 가능):

| 채널 | 용도 |
|---|---|
| `#papers-test` | 운영 시작 1~2주는 여기로만 푸시 (튜닝 단계) |
| `#papers-daily` | 검증 후 정식 일일 다이제스트 |
| `#papers-priority` | score 9+만 별도로 받고 싶을 때 (선택) |

각 채널에서 봇 초대:

```
/invite @SynBEE Paper Bot
```

### 3.3 토큰 검증 + 채널 ID 조회

```powershell
python scripts\slack_setup_helper.py
```

출력에서 채널 ID(`C0XXXXXXXX`) 복사 → `config\config.yml` 편집:

```yaml
slack:
  enabled: true
  channels:
    daily_digest:  "C0XXXXXXXX"
    high_priority: "C0XXXXXXXX"   # 없으면 빈 문자열 ""
    test:          "C0XXXXXXXX"
  use_test_channel: true   # 1~2주 후 false로
```

### 3.4 테스트 메시지 송신

```powershell
python scripts\slack_setup_helper.py --post-test "🐝 SynBEE Bot 셋업 완료!" --channel C0XXXXXXXX
```

⚠️ `--channel`과 채널 ID 사이에 **공백 한 칸 필수**. 붙여 쓰면
`unrecognized arguments: --channelC0...` 에러가 납니다.

Slack 채널에 메시지가 뜨면 §3 완료.

---

## 4. NCBI API key (선택, 권장)

1. https://account.ncbi.nlm.nih.gov/ 로그인 → Account Settings
2. **API Key Management** → **Create an API Key**
3. 발급된 키를 `.env`의 `NCBI_API_KEY=`에 붙여넣기
4. 효과: PubMed E-utilities rate limit 3 → 10 req/sec
   (`sanity_check.py --per-journal` 약 3배 빨라짐)

---

## 5. 첫 dry-run

```powershell
# 1. PubMed만, LLM·Slack 모두 건너뛰고 데이터 경로 확인
python scripts\run_daily.py --dry-run --no-llm --no-slack --no-rss --no-biorxiv --since-days 1 --limit 5

# 2. 전체 소스 + LLM 필터까지 가동, Slack은 건너뜀 (비용·노이즈 점검)
python scripts\run_daily.py --dry-run --no-slack --since-days 1

# 3. 정식 — 테스트 채널로 푸시
python scripts\run_daily.py --since-days 1
```

`#papers-test` 채널에 다이제스트 + 논문 카드들이 뜨면 로컬 운영 준비 완료.

---

## 6. GitHub Actions 자동화

### 6.1 Repo 생성 + 푸시

#### git 첫 사용 시 — identity 설정 (1회성)

```powershell
git config --global user.name "Dongsoo Yang"
git config --global user.email "dosoyang@korea.ac.kr"
# 또는 GitHub noreply 이메일 (권장):
# git config --global user.email "<id>+<username>@users.noreply.github.com"
```

확인: `git config --global --list`

#### 초기 commit + push

```powershell
cd D:\AI_Projects\synbee-paper-bot
git init -b main
git add .
git commit -m "feat: initial Pipeline A setup"

# GitHub CLI(gh) 사용 시
gh repo create synbee-paper-bot --private --source . --push

# 없으면 https://github.com/new 에서 빈 repo 만들고
# git remote add origin https://github.com/<user>/synbee-paper-bot.git
# git push -u origin main
```

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

### 6.4 test → daily 채널 전환 절차

첫 1~2주 `#papers-test` 에서 푸시 품질을 검증한 후 정식 운영 채널
(`#papers-daily`) 로 전환하는 단계입니다.

**메커니즘**: GitHub Actions 가 매 실행마다 `config.yml.example` + GitHub
Secrets 로부터 실제 `config.yml` 을 재생성합니다. 따라서 진짜 운영 스위치는
**`USE_TEST_CHANNEL` GitHub Secret** 입니다 (로컬 `config.yml` 의 같은 키는
informational).

**0. 사전 확인 (Slack에서)**:
```
/invite @SynBEE Paper Bot
```
`#papers-daily` 에 봇이 멤버인지 확인. 안 들어가 있으면 위 명령으로 초대 —
이거 안 하면 `chat.postMessage` 가 `not_in_channel` 에러로 실패.

**1. GitHub Secret 변경**:
1. https://github.com/<owner>/synbee-paper-bot/settings/secrets/actions
2. **`USE_TEST_CHANNEL`** → Update → 값을 `false` 로 변경 → Save
3. **`SLACK_DAILY_CHANNEL`** 값이 `#papers-daily` 의 채널 ID 와 일치하는지 확인

**2. Dry-run 안전 검증**:
1. https://github.com/<owner>/synbee-paper-bot/actions/workflows/daily.yml
2. 우측 **Run workflow** → `Dry run (no Slack push)` ✅ 체크 → Run
3. 로그의 `Rendered config/config.yml` 스텝에서 `use_test_channel: false` 확인
4. `Run daily digest` 스텝 에러 없음 확인

**3. 정식 발송 트리거**:
1. 같은 Actions 페이지 → **Run workflow** (dry_run 체크 해제)
2. 1~5분 후 `#papers-daily` 에 digest 카드 도착 확인

이후 매일 KST 08:00 자동 cron 이 `#papers-daily` 로 발송.

**4. 로컬 config.yml 동기화 (선택)**:
GitHub Actions 은 자체 secret 으로 재생성하므로 로컬 `config.yml` 은 운영에
영향 없지만, 로컬 수동 테스트 (`python scripts/run_daily.py`) 시 일관성을
위해 같이 변경:
```yaml
slack:
  use_test_channel: false
```

**5. `#papers-test` 채널 정리 (선택)**:

| 옵션 | 작업 | 추천 |
|---|---|---|
| **유지 (dormant)** | 그대로 둠. 추후 새 prompt·필터 테스트 시 `USE_TEST_CHANNEL=true` 만 잠시 토글하면 즉시 재활용 | ★ 권장 |
| **봇 제거** | 채널에서 `/remove @SynBEE Paper Bot` | 중 |
| **완전 archive** | 채널 우측 ⚙️ → Settings → Archive channel | 비추 |

### 6.5 daily channel 임시 비활성 (운영 일시 중단)

신규 prompt·필터 변경 등으로 일시적으로 daily 발송을 멈추려면:
- 옵션 A: `USE_TEST_CHANNEL` 을 다시 `true` 로 → test 채널로만 발송
- 옵션 B: Actions 페이지 → 우상단 **⋯** → **Disable workflow** → cron 자체 중단

복귀: 반대 작업.

---

## 7. 운영 첫 2주 체크리스트

- [ ] Slack 푸시 결과 매일 살펴보기
- [ ] `use_test_channel: true` 유지 (오류 시 워크스페이스 노이즈 방지)
- [ ] False positive 패턴 발견 → `config\keywords.yml`의 `exclude` 추가
- [ ] 누락 사례 발견 → 키워드/저널 추가 후 `python scripts\sanity_check.py`로 hit 수 재확인
- [ ] 주 1회 `python scripts\build_query.py`로 합성 쿼리 검토
- [ ] 2주차에 `use_test_channel: false`로 전환

---

## 8. 트러블슈팅

### 8.1 Python 환경

| 증상 | 원인·해결 |
|---|---|
| `python -m venv .venv` 실행 후 출력이 `Python` 한 줄뿐 | Microsoft Store 스텁 호출 중. **Settings → Apps → App execution aliases**에서 python.exe 별칭 OFF, python.org 인스톨러로 정식 설치. (§1.2) |
| `Could not find platform independent libraries <prefix>` 경고 | Python 표준 라이브러리 경로 깨짐 (Python 3.14에서 빈번). 3.13으로 재설치. (§1.2) |
| `sys.base_prefix`가 CWD로 출력됨 | 위와 동일 — 설치 깨짐. 재설치. |
| `Activate.ps1 ... 보안 오류 ... UnauthorizedAccess` | PowerShell ExecutionPolicy 차단. `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` 후 Y. (§1.3) |
| `Activate.ps1 ... 인식되지 않습니다` | venv 자체가 안 만들어진 것. Microsoft Store 스텁이 원인. §1.2 |
| Python Install Manager 사이드로드 차단 | 권장하지 않는 경로. python.org 인스톨러 직접 사용. (§1.2) |
| `pip install` 일부 패키지에서 wheel 빌드 실패 | Python 3.14 호환성 이슈 가능. 3.13 다운그레이드. |

### 8.2 Slack manifest

| 증상 | 원인·해결 |
|---|---|
| `Expecting 'STRING','NUMBER','NULL','TRUE','FALSE','{','[', got: 'INVALID'` | YAML 내용을 JSON 탭에 붙여넣음. **YAML 탭** 선택 또는 `slack_app_manifest.json` 사용. (§3.1) |
| `event_subscriptions requires request_url` | manifest에 events가 있는데 socket_mode 미사용. Pipeline A는 송신 전용이므로 events 항목 자체 제거. |
| `invalid scope` | Pipeline A에 불필요한 scope. 현재 manifest는 최소 scope만 (chat:write, chat:write.public, channels/groups/im/mpim:read). |

### 8.3 명령행 / git

| 증상 | 원인·해결 |
|---|---|
| `unrecognized arguments: --channelC0XXXXXXXX` | `--channel`과 ID 사이 공백 누락. `--channel C0XXXXXXXX` 또는 `--channel=C0XXXXXXXX`. (§3.4) |
| `cp` 명령 안 됨 | PowerShell에서는 `cp`가 alias로 동작하지만 안전하게 `copy` 사용. |
| `git commit ... Author identity unknown` | git 첫 사용. `git config --global user.name "..."` + `user.email "..."` 설정. (§6.1) |
| `unable to auto-detect email address (got 'user@HOSTNAME.(none)')` | 위와 동일. global user.email 설정 누락. |

### 8.4 Slack API 호출

| 증상 | 원인·해결 |
|---|---|
| `not_in_channel` Slack 오류 | 봇이 채널에 초대 안 됨. `/invite @SynBEE Paper Bot` |
| `invalid_auth` Slack 오류 | `xoxb-` 토큰이 잘못됨. App-Level(`xapp-`)와 혼동했을 가능성. |
| `channel_not_found` | 채널 ID 오타. `slack_setup_helper.py`로 재조회. |
| 메시지가 형식 깨져서 보임 | Block Kit 빌더 버그 가능성. `slack_dispatch.py` 점검. |

### 8.5 PubMed / 데이터 소스

| 증상 | 원인·해결 |
|---|---|
| `HTTP 414 Request-URI Too Long` | 쿼리가 길어 GET 한도 초과. 우리 `sanity_check.py`는 POST 사용 중이라 자동 회피. 다른 도구 쓸 때 발생할 수 있음. |
| 매일 hit 수가 0~1편 | 키워드/저널 너무 좁음. `python scripts\sanity_check.py --windows 7 30 365`로 분포 확인 후 보강. |
| 매일 hit 수가 200편+ | 너무 넓음. exclude 강화 또는 저널 티어 좁힘. |

### 8.6 LLM (Gemini)

| 증상 | 원인·해결 |
|---|---|
| `429 RESOURCE_EXHAUSTED` / `quota exceeded` | 무료 티어 일 할당량 초과. **Flash-Lite는 free tier가 20 RPD뿐**이라 빈번. `config\config.yml`에서 `model: "gemini-2.5-flash"`로 변경 (250 RPD)하거나 Google AI Studio → Billing 연결 (Tier 1으로 100배 ↑). 이미 소진됐으면 KST 다음날 자정 이후 reset. |
| `503 UNAVAILABLE` / `high demand` | Google 측 서버 일시 과부하. 봇은 자동 재시도 (10s/30s/60s) → 실패 시 `fallback_models`로 자동 전환. 보통 5~30분 내 회복. |
| `500 INTERNAL` / `504 DEADLINE_EXCEEDED` | 위와 동일 — transient. 자동 재시도 + fallback. |
| `Cannot send a request, as the client has been closed` | google-genai 2.0+ Client lifecycle 이슈. **체이닝 금지** (`Client(...).models.generate_content(...)`). 변수에 할당 후 사용. (filter.py는 이미 처리됨) |
| `Model usage:` 라인이 fallback 모델만 기록 | primary가 항상 실패 중. stderr 메시지로 원인 확인 (대부분 quota 또는 transient 503). |
| `Verdict distribution: {'NO/score=0': N}` 만 출력 | 모든 호출이 silently 실패 중. stderr의 `[gemini] error` 메시지 확인. 보통 quota 또는 API 키 오류. |
| 한 편당 LLM 응답이 5초+ | `config.yml`의 `parallel_requests` 4 → 8로 증가. |
| Stage 2 결과가 모두 `verdict: NO` | `filter_prompt.md`가 너무 엄격. 기준 완화 또는 `min_score` 6 → 5로 조정. |
| JSON parse 실패 다발 | Gemini가 코드 블록으로 감싸 응답 중. `filter.py`의 정규식이 처리 중이지만 빈도 높으면 모델을 Flash → Flash-Lite로 변경 (Lite가 JSON 출력 더 안정적). |

### 6.7 GitHub Actions

| 증상 | 원인·해결 |
|---|---|
| 매일 같은 논문이 다시 푸시됨 | `seen.db`가 매번 초기화됨. workflow의 cache 키 점검 (`.github/workflows/daily.yml`). |
| 워크플로우 실패 — secret 미인식 | repo Settings → Secrets에 정확한 이름으로 등록됐는지 확인 (오타·대소문자 주의). |
| KST 시간 안 맞음 | cron은 UTC 기준. `0 23 * * 0-4` UTC = `08:00 KST 월~금`. |

---

## 9. Wiki 후보 등록 워크플로우 (GitHub Issue 기반)

Slack 메시지의 **"📥 위키 후보 등록"** 버튼을 클릭하면 미리 채워진 GitHub
Issue 생성 페이지가 새 탭으로 열립니다. **Submit new issue**만 누르면
`wiki-queue` 라벨이 붙은 Issue가 생성됨.

이 Issue들을 로컬에서 일괄 처리하여 `D:\Obsidian_Vault\Dongsoo\raw\`에
markdown으로 저장:

### 9.1 사전 — gh CLI 인증

```powershell
gh --version            # 설치 확인
gh auth status          # 인증 확인. 미인증 시:
gh auth login           # 브라우저 인증 진행
```

### 9.2 큐 처리

```powershell
.\.venv\Scripts\Activate.ps1

# 미리보기 (파일 안 씀, Issue 안 닫음)
python scripts\process_wiki_queue.py --dry-run

# 실제 처리 — raw/에 markdown 저장 (Issue는 열어둠)
python scripts\process_wiki_queue.py

# 처리 + Issue 자동 close
python scripts\process_wiki_queue.py --close
```

생성된 파일: `D:\Obsidian_Vault\Dongsoo\raw\YYYYMMDD_<slug>.md`

각 markdown에는 paper_id·journal·DOI·KR/EN 요약·Abstract·GitHub Issue 링크가
frontmatter + 본문으로 포함됨.

### 9.3 SynBEE Wiki에서 정리

```
"raw/20260511_engineered-polyketide-synthase.md 정리해줘"
```

별도 Claude 세션에서 위 trigger를 던지면 `D:\Obsidian_Vault\Dongsoo\CLAUDE.md`의
**모드 A** 절차로 wiki 페이지 생성됨.

### 9.4 운영 팁

- 주 1회 처리: 금요일 오후에 `python scripts\process_wiki_queue.py --close`
- 라벨 변경 시 wiki-queue 외에 mission별 라벨(`mission-1`, `mission-2`, `mission-3`)도
  GitHub Issue UI에서 수동으로 추가 가능

---

## 9-B. Gmail 스팸함 구제 봇 (`spam-rescue.yml`)

매일 **KST 07:00**에 Gmail 스팸함을 훑어, 스팸이 아닌 메일(타대학 인턴 지원,
세미나 초청, 학회·저널·연구재단 공지 등)만 골라 **스팸 해제 + `안전함` 라벨 +
받은편지함 이동**을 수행한다. 읽음 상태는 건드리지 않으므로 복구된 메일은
안 읽음으로 남는다. **아무것도 삭제하지 않는다.**

> 왜 별도 OAuth가 필요한가: claude.ai Gmail 커넥터는 스팸함을 차단한다
> (`in:spam`·`label:SPAM` 검색이 빈 결과, `unmark_message_spam`은 permission
> denied). 스팸함에 접근할 수 있는 경로는 Gmail REST API 직접 호출뿐이다.

### 9-B.1 Google OAuth 클라이언트 발급 (1회)

1. <https://console.cloud.google.com/> → 프로젝트 생성 (예: `synbee-gmail-bot`)
2. **APIs & Services → Library** → "Gmail API" → **Enable**
3. **APIs & Services → OAuth consent screen**
   - User Type: **External**, 앱 이름 아무거나, 지원 이메일 `dosoyang@korea.ac.kr`
   - **Test users**에 `dosoyang@korea.ac.kr` 추가 (앱을 게시할 필요 없음)
   - Scope는 여기서 추가하지 않아도 된다 (요청 시 동적으로 붙는다)
4. **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - 생성된 **Client ID / Client secret** 복사

> Test user 상태의 앱은 refresh token이 **7일 후 만료**된다. 계속 쓰려면
> OAuth consent screen에서 **Publish app**을 눌러 Production으로 전환할 것
> (심사 없이 즉시 전환된다 — 본인 계정만 쓰는 앱이라 "unverified" 경고만 뜬다).

### 9-B.2 Refresh token 발급 (1회, 브라우저 있는 PC에서)

```powershell
cd D:\AI_Projects\synbee-paper-bot
.\.venv\Scripts\Activate.ps1
python scripts\gmail_auth_setup.py --client-id "<CLIENT_ID>" --client-secret "<SECRET>"
```

브라우저가 열리면 `dosoyang@korea.ac.kr`로 로그인 → "Google에서 확인하지 않은
앱" 경고에서 **고급 → 안전하지 않은 페이지로 이동** → 권한 허용.
터미널에 등록할 secret 3개가 출력된다. **토큰은 파일로 저장되지 않는다.**

### 9-B.3 GitHub Secrets 등록

Settings → Secrets and variables → Actions → New repository secret

| Secret | 값 |
|---|---|
| `GMAIL_CLIENT_ID` | 9-B.1에서 복사 |
| `GMAIL_CLIENT_SECRET` | 9-B.1에서 복사 |
| `GMAIL_REFRESH_TOKEN` | 9-B.2 출력값 |
| `GEMINI_API_KEY` | (이미 등록되어 있음 — 공유) |

### 9-B.4 첫 실행은 반드시 dry-run

Actions → **Gmail spam rescue** → Run workflow → `dry_run` 체크 → Run.

로그에 스팸함 전체에 대한 판정이 한 줄씩 찍힌다:

```
RESCUE student@snu.ac.kr    Inquiry about internship  | student_inquiry: 인턴 지원 문의
KEEP   contact@ccsend.com   Distinguished Speaker...  | predatory: 약탈적 학회 초청
```

`RESCUE` 줄을 훑어보고 오판이 없으면 `dry_run` 없이 다시 실행. 이후로는 매일
07:00에 자동으로 돈다.

### 9-B.5 안전장치

| 장치 | 동작 |
|---|---|
| **Circuit breaker** | 한 번에 `max_rescues_per_run`(기본 40)건을 넘게 구제하려 하면 **아무것도 적용하지 않고** 실패 종료. 프롬프트 오작동이 스팸함을 통째로 받은편지함에 쏟아붓는 사고를 막는다. |
| **Spoof guard** | SPF·DKIM·DMARC가 **모두** 실패한 메일은 모델이 뭐라 하든 구제하지 않는다. 하나만 실패하는 건 정상 메일에도 흔하므로(메일링 리스트 전달 등) 셋 다 실패할 때만 막는다. |
| **오류 = 재시도** | 분류에 실패한 메일은 `SpamRescueChecked` 라벨을 붙이지 않는다 → 다음 날 다시 판정된다. 일시적 API 오류로 메일이 조용히 유실되지 않는다. |
| **삭제 불가** | 스코프가 `gmail.modify`뿐이라 영구 삭제 권한 자체가 없다. |

### 9-B.6 운영

- **결과 리포트 채널은 없다.** 무엇을 왜 옮겼는지는 각 run의 artifact
  (`spam-rescue-log-*`, 30일 보관)에서 확인한다. 실패한 run은 GitHub이 메일로 알린다.
- 오판을 발견하면 `config/spam_rescue_prompt.md`의 RESCUE/SPAM 규칙에 그 사례를
  한 줄 추가하는 것이 가장 효과적이다.
- 너무 적게 구제하면 `config/spam_rescue.yml`의 `min_confidence`를 5로 낮추고,
  너무 많이 구제하면 7~8로 올린다.
- `SpamRescueChecked` 라벨은 "이미 판정함" 표시일 뿐이다. 이 라벨이 붙은 채
  스팸함에 남은 메일은 Gmail이 30일 후 자동 삭제한다.

---

## 10. 다음 단계

- [ ] **Pipeline B 시작** — `D:\AI_Projects\synbee-pdf-bot\` 별도 repo로 PDF 심층 요약 봇 구축. `DIRECTION.md` 참조.
- [ ] 월 1회 Sonnet 4.6 vs Gemini 비교 회귀 테스트 (`ANTHROPIC_API_KEY` 활용)
- [ ] 키워드/저널 튜닝 자동화 — `scripts/tune.py` 작성

---

## 부록 — 빠른 재셋업 (이미 한 번 셋업했던 경우)

```powershell
cd D:\AI_Projects\synbee-paper-bot
.\.venv\Scripts\Activate.ps1
python scripts\run_daily.py --since-days 1
```

이게 안 되면 §8.1 트러블슈팅으로.
