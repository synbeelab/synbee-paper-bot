# SynBEE Paper Bot — Routine TO-DO List

> 매일·매주·매월 무엇을 해야 하는지 요약. 자세한 셋업·트러블슈팅은 [SETUP.md](SETUP.md) 참조.

---

## 📅 매일 (1~2분) — Slack에서

오전 시간대 (KST 08:00 이후) Slack `#papers-test` 채널 확인:

- [ ] 봇이 다이제스트를 푸시했는지 확인 (도착 안 했으면 [§ 응급 처치](#-응급-처치) 참조)
- [ ] 각 논문 카드 빠르게 훑기 (KR/EN 한 줄 요약 기준)
- [ ] 관심 논문에 **[📥 위키 후보 등록]** 버튼 클릭
   - 새 탭에서 GitHub Issue 생성 페이지가 열림 (제목·본문 자동 채워짐)
   - 화면 하단 **[Submit new issue]** 클릭 → 끝
   - 한 논문당 약 5초

---

## 📆 주 1회 (10분) — 금요일 오후 추천

지난 한 주 쌓인 wiki 큐를 일괄 처리해서 SynBEE Wiki(`D:\Obsidian_Vault\Dongsoo\raw\`)로 옮깁니다.

### Step 1 — 큐 미리보기 (선택)

```powershell
cd D:\AI_Projects\synbee-paper-bot
.\.venv\Scripts\Activate.ps1
python scripts\process_wiki_queue.py --dry-run
```

쌓인 Issue 목록과 어떤 파일이 만들어질지 확인.

### Step 2 — 실제 처리 + Issue close

```powershell
python scripts\process_wiki_queue.py --close
```

- `D:\Obsidian_Vault\Dongsoo\raw\YYYYMMDD_<slug>.md` 생성됨
- 해당 GitHub Issue는 자동으로 close + "Ingested into SynBEE Wiki raw/" 코멘트

### Step 3 — Wiki 정리 (별도 Claude Code 세션)

`D:\Obsidian_Vault\Dongsoo`에서 Claude Code 새 세션 열고:

```
raw/20260511_*.md 일괄 정리해줘
```

또는 한 편씩:

```
raw/20260511_engineered-polyketide.md 정리해줘
```

→ `CLAUDE.md` 모드 A 절차로 자동 wiki 페이지 생성·교차참조.

---

## 🗓 격주 (10분) — 키워드·저널 튜닝

운영하면서 노이즈/누락 패턴이 보이면 보정합니다.

### 노이즈가 자주 나오는 키워드 발견 시

`config\keywords.yml`의 `exclude` 섹션에 추가:

```yaml
exclude:
  active: true
  keywords:
    - "human clinical trial"
    - "cancer chemotherapy"
    - "<새로 발견한 노이즈 키워드>"   # ← 추가
```

### 봐야 할 논문이 안 잡혔던 경우

원인 추적:

1. **저널 누락?** `config\journals.yml`에 추가
   - NLM 약어 확인: https://www.ncbi.nlm.nih.gov/nlmcatalog/journals
2. **키워드 누락?** `config\keywords.yml`의 해당 미션 그룹에 추가
3. **저자가 abstract를 짧게 썼나?** Stage 2 LLM이 정보 부족으로 NO 판정한 케이스. 운영 못 함.

### 변경 검증

```powershell
# 쿼리 합성 확인
python scripts\build_query.py

# 실제 hit 수 확인
python scripts\sanity_check.py --windows 7 30
```

### Push (cron 다음 실행에 자동 반영)

```powershell
git add config\keywords.yml config\journals.yml
git commit -m "tune: <변경 요약>"
git push
```

---

## 🗒 월 1회 (5~10분) — 점검

### 1. 저널별 hit 분포 — 너무 적게 잡히는 곳 식별

```powershell
python scripts\sanity_check.py --per-journal --windows 30
```

30일 동안 0~2건뿐인 저널은 retire 후보. 단, top-tier journal(Nature, Cell 등)은 적어도 유지.

### 2. seen.db 통계

```powershell
python -c "from synbee_bot.storage import SeenDB; from pathlib import Path; db = SeenDB(Path('data/seen.db')); print(db.stats())"
```

### 3. 비용 점검

https://aistudio.google.com/apikey → 좌측 **Usage** 메뉴에서 지난 30일 호출량 확인.
- 무료 티어 운영 중: <50% 사용이면 안전. 90% 초과 시 빌링 활성화 검토
- Tier 1 운영 중: $1 이하면 정상

### 4. (선택) Sonnet 4.6 vs Gemini 회귀 비교

품질 회귀 모니터링용. 같은 키워드 셋으로 두 모델 결과 비교:

```yaml
# config\config.yml 임시 변경
llm_filter:
  provider: "anthropic"
  model: "claude-sonnet-4-6"
```

테스트 후 원상복구. 운영용은 Gemini 유지 (가성비).

---

## 🚨 응급 처치

### 아침에 Slack 다이제스트가 안 옴

1. GitHub Actions 페이지 열기:
   https://github.com/synbeelab/synbee-paper-bot/actions
2. 최근 "Scheduled" 트리거 실행 확인
   - **빨간 X**: 실패. 클릭해서 어느 step에서 죽었는지 확인
   - **녹색 ✓ but Slack 침묵**: `Run daily digest` step 로그에서 "Filter: 0 pass" 또는 채널 ID 누락 여부
   - **트리거 자체가 없음**: workflow가 disabled 됐을 가능성. Actions 페이지 우측 상단 "Enable workflow"

### Filter가 갑자기 모두 NO만 뱉음

`Run daily digest` step 로그에서 stderr 확인:

| 메시지 | 처치 |
|---|---|
| `[gemini] DAILY QUOTA EXHAUSTED` | Gemini quota 소진. KST 자정 후 회복 or [billing 활성화](https://aistudio.google.com/apikey) |
| `[gemini] server overload` | 일시적 503. fallback 체인이 처리 — 다음 실행 정상화 |
| `[gemini] error ... 401` or `403` | API 키 문제. `.env` + GitHub Secret `GEMINI_API_KEY` 점검 |
| `not_in_channel` | 봇이 채널에 없음. Slack에서 `/invite @SynBEE Paper Bot` |

### 같은 논문이 매일 다시 푸시됨

`seen.db` 캐시 깨짐. GitHub repo → **Actions → Caches** → `synbee-seen-*` 캐시 전체 삭제 → 다음 cron부터 회복.

### Wiki 큐가 처리 안 됨

```powershell
gh auth status    # 만료된 경우 gh auth login 재실행
gh issue list --label wiki-queue --repo synbeelab/synbee-paper-bot
```

Issue가 보이지 않으면 라벨이 잘못 붙은 것. GitHub 웹에서 수동으로 `wiki-queue` 라벨 추가 후 재실행.

---

## 🎚 운영 모드 전환 — 2주 후

`#papers-test`에서 충분히 검증했으면 정식 `#papers-daily` 채널로:

1. GitHub repo → **Settings → Secrets and variables → Actions**
2. `USE_TEST_CHANNEL` → **Update** → `false`로 변경 → Save
3. (선택) 로컬에서도 동기화:
   ```powershell
   notepad config\config.yml
   # use_test_channel: false 로 변경
   ```

다음 cron부터 `#papers-daily`로 전환.

---

## 🔗 빠른 링크

| 위치 | 용도 |
|---|---|
| https://github.com/synbeelab/synbee-paper-bot/actions | cron 실행 로그 |
| https://github.com/synbeelab/synbee-paper-bot/issues?q=label%3Awiki-queue+is%3Aopen | 미처리 wiki 큐 |
| https://github.com/synbeelab/synbee-paper-bot/issues?q=label%3Awiki-queue+is%3Aclosed | 처리 완료 (참고용) |
| https://aistudio.google.com/apikey | Gemini API 사용량·키 관리 |
| https://api.slack.com/apps | Slack App 설정 |
| [SETUP.md](SETUP.md) | 전체 셋업 가이드 |
| [config/keywords.yml](config/keywords.yml) | 키워드 튜닝 |
| [config/journals.yml](config/journals.yml) | 저널 추가/제거 |
| [config/filter_prompt.md](config/filter_prompt.md) | Stage 2 LLM 프롬프트 |
| [D:\Obsidian_Vault\Dongsoo\raw\](file:///D:/Obsidian_Vault/Dongsoo/raw/) | wiki 후보 markdown 저장 위치 |
| [D:\Obsidian_Vault\Dongsoo\wiki\](file:///D:/Obsidian_Vault/Dongsoo/wiki/) | 정리된 wiki 페이지 |

---

## 📊 시간 투자 요약

| 빈도 | 시간 | 활동 |
|---|---|---|
| 매일 | 1~2분 | Slack 다이제스트 확인 + 관심 논문 [📥 위키 후보 등록] 클릭 |
| 주 1회 | 10분 | `process_wiki_queue.py --close` + Claude 세션에서 raw/ 정리 |
| 격주 | 10분 | 키워드·저널 튜닝 (필요할 때만) |
| 월 1회 | 5~10분 | 분포·비용 점검 |
| **합계** | **주 15~20분** | |
