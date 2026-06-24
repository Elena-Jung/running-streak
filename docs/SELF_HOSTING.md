# 셀프 호스팅 가이드 (Self-Hosting)

> 이 봇을 **본인의 디스코드 서버와 본인의 호스트**에 직접 띄우는 절차입니다.
> 봇은 호스트에 종속되지 않으며 설정은 전부 `.env`(환경변수)로 합니다.
> 동작 규칙의 단일 진실원은 [DESIGN.md](../DESIGN.md), 설계 근거는
> [RUNNING_STREAK_BOT_SPEC.md](../RUNNING_STREAK_BOT_SPEC.md) 입니다.
>
> ⚠️ **봇 UI·슬래시 명령어는 기본적으로 한국어**입니다. 다른 언어로 쓰려면 §6 을 참고하십시오.
> AI 에이전트에게 설치를 맡기는 경우 [AGENTS.md](../AGENTS.md) 를 함께 참고하십시오.

---

## 0. 한눈에 보기

| 단계 | 요약 |
|---|---|
| 1 | 디스코드 봇 생성 + **MESSAGE CONTENT 인텐트 켜기** + 서버 초대 (사람이 직접) |
| 2 | 서버(길드)·채널 ID 복사 |
| 3 | 클론 → 커밋 가드 훅 활성화 → `.env` 작성(토큰은 본인 터미널에서) |
| 4 | (선택) 시간대·언어 변경 |
| 5 | `docker compose up -d --build` |
| 6 | 검증(테스트·디스코드 동작) |

---

## 1. 사전 준비물

- **호스트**: 리눅스 권장. **Docker Engine + Docker Compose v2** (`docker compose version` 확인).
- **네트워크**: `discord.com` 으로의 **아웃바운드 HTTPS** 필요. **인바운드 포트는 필요 없습니다**(봇은 아웃바운드 전용, DB는 호스트 포트를 발행하지 않음).
- **자원**: 여유 메모리 약 1.5GB(컴포즈 상한: bot 1g / db 512m).
- **디스코드**: 계정 + 봇을 초대할 서버(길드)의 관리 권한.
- **git**.

> 엣지 케이스
> - **ARM(애플 실리콘·라즈베리파이)**: 베이스 이미지가 멀티아치라 별도 작업 없이 빌드됩니다.
> - **rootless Docker**: 사용 가능합니다. 해당 사용자가 `docker compose` 를 실행할 수 있으면 됩니다.
> - 봇 전용 DB는 내부 네트워크 전용이라 **호스트에 기존 PostgreSQL 이 있어도 충돌하지 않습니다**.

---

## 2. 디스코드 봇 만들기 (사람이 직접 — 에이전트가 대신 못 함)

1. https://discord.com/developers/applications → **New Application** 생성.
2. **Bot** 탭 → **Reset Token** → 토큰 복사. 이 토큰은 **§3 에서 본인이 직접 `.env` 에 붙여넣습니다**. 어디에도(특히 채팅·AI 도구) 공유하지 마십시오.
3. 같은 **Bot** 탭의 **Privileged Gateway Intents** → **MESSAGE CONTENT INTENT 를 켭니다.**

   > 🔴 **가장 흔한 실패 지점.** 이 인텐트가 꺼져 있으면 봇이 첨부 이미지를 보지 못해, 로그인·명령 동기화는 되지만 사진을 올려도 **아무 반응이 없습니다.**

4. **OAuth2 → URL Generator**:
   - scopes: `bot`, `applications.commands`
   - bot permissions: **View Channels**, **Send Messages**, **Read Message History**, **Add Reactions** (이 4개면 충분 — 봇은 '자기 리액션'만 바꾸므로 Manage Messages 는 필요 없습니다)
   - 생성된 URL 로 **본인 서버에 초대**합니다.

   > members 인텐트는 필요하지 않습니다(리더보드 이름은 `fetch_user` 로 조회).

---

## 3. 서버·채널 ID 얻기 (비밀 아님)

1. 디스코드 **설정 → 고급 → 개발자 모드** 켜기.
2. 서버 아이콘 우클릭 → **서버 ID 복사** = `DISCORD_GUILD_ID`.
3. 집계 대상 채널 우클릭 → **채널 ID 복사** = `TARGET_CHANNEL_ID`.

> 이 두 ID 는 비밀이 아니므로 도우미/에이전트에게 알려줘도 괜찮습니다. (토큰·DB 비밀번호는 절대 아닙니다.)

---

## 4. 클론 & `.env` 작성

```bash
git clone https://github.com/Elena-Jung/running-streak.git
cd running-streak

# 비밀 오커밋 방지 가드 훅 활성화 (클론마다 1회)
git config core.hooksPath .githooks

cp .env.example .env
```

`.env` 를 열어 채웁니다:

- `DISCORD_TOKEN` — **본인 터미널에서 직접** 붙여넣습니다(아래 보안 주의 참고).
- `DISCORD_GUILD_ID`, `TARGET_CHANNEL_ID` — §3 에서 복사한 값.
- `POSTGRES_PASSWORD` — 강한 무작위 값. 예: `openssl rand -base64 24`.
- `POSTGRES_USER`/`POSTGRES_DB` — 그대로 둬도 됩니다(기본 `streak`).
- `TZ`, `OCR_ENABLED` — 보통 기본값 유지.
- `BOT_PAUSED` — 킬스위치. `true` 면 사진 집계를 즉시 중단(조회 커맨드는 동작). 평소 `false`.

> `.env` 는 `.gitignore` 로 보호되어 **절대 커밋되지 않습니다**. 추적 대상은 `.env.example`(플레이스홀더)뿐입니다.

---

## 5. (선택) 시간대·언어 변경

본인 러너들이 한국에 있고 한국어 UI 가 괜찮다면 이 절은 건너뛰어도 됩니다.

### 5.1 시간대 — 스트릭 "하루 경계"가 **한국시간 04:00 으로 하드코딩**되어 있습니다

새벽 0시\~4시 러닝을 "전날"로 치는 규칙이 한국시간 기준입니다. 다른 시간대로 바꾸려면 아래를 편집하고 **재빌드**해야 합니다:

- `bot/app/events.py` 의 `KST = ZoneInfo("Asia/Seoul")` → 원하는 시간대(예: `ZoneInfo("America/New_York")`). 심볼명은 `KST` 그대로 둬도 됩니다(`to_run_date` 에서 참조).
- `bot/app/events.py` 의 `DAY_RESET_HOUR = 4` → 경계 시각. 자정 기준으로 하려면 `0`.
- `.env` 의 `TZ` → 같은 시간대(로그 타임스탬프용).
- `bot/Dockerfile` 의 `ENV TZ=Asia/Seoul` → 같은 시간대(이미지 기본값).

> DST(서머타임) 있는 시간대도 절대 시각에서 시간을 빼고 날짜를 취하므로 동작은 정확하나, DST 존을 선택했음을 인지하십시오. 한국시간은 DST 가 없습니다.

### 5.2 언어 — UI·명령어가 **전부 한국어**(i18n 없음)

영어 등으로 바꾸려면 다음 문자열을 직접 번역하고 재빌드해야 합니다(작업량이 적지 않습니다):

- `bot/app/commands.py` — `HELP_TEXT`, 모든 `name="..."`/`description="..."`(그룹 `달리기`, 명령 `등록`·`해제`·`취소`·`스트릭`·`기록`·`리더보드`·`캘린더`·`도움`), 모든 응답 문구.
- `bot/app/events.py` — 완료 메시지(`### 러닝 기록 완료. N일째 연속입니다.`)와 OCR 소프트 힌트.

> 명령 이름을 바꾸면 다음 기동 시 길드 스코프로 재동기화됩니다. Tesseract `kor` 언어팩은 설치돼 있어도 무해합니다.

> 모든 코드 편집 후에는 **반드시 재빌드**해야 반영됩니다(소스가 이미지에 `COPY` 되며 볼륨 마운트가 아님):
> `docker compose build bot && docker compose up -d`

---

## 6. 빌드 & 실행

```bash
docker compose up -d --build
docker compose logs -f bot   # "로그인:" 과 "슬래시 커맨드 N개 동기화 완료" 확인
```

---

## 7. 검증

```bash
# 단위 테스트
docker compose run --rm bot python -m pytest -q

# DB 테이블 생성 확인
#  (변수는 컨테이너 안에서 풀려야 하므로 sh -c 로 감쌉니다 — 호스트 셸엔 POSTGRES_* 가 없어
#   `psql -U "$POSTGRES_USER"` 를 그냥 쓰면 빈 값으로 확장돼 실패합니다)
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'

# DB 포트 미발행 확인 (db 의 PORTS 칸이 비어 있어야 정상)
docker compose ps
```

디스코드에서:
1. `/달리기 등록`
2. 대상 채널에 러닝 사진 업로드 → ✅ 반응 후 `러닝 기록 완료. 1일째 연속입니다.` 확인
3. 같은 날 또 올리면 무응답(하루 1회만) 확인
4. `/스트릭`, `/리더보드`, `/도움` 확인

> 잘 안 될 때: ① MESSAGE CONTENT 인텐트가 켜져 있는가, ② `TARGET_CHANNEL_ID` 가 정확한가, ③ 봇이 그 채널에서 보기/보내기/반응 권한이 있는가 를 먼저 확인하십시오. 셋 다 무음 실패의 원인입니다.

---

## 8. 운영

```bash
# 봇만 재시작
docker compose restart bot

# 업데이트 (소스 COPY라 재빌드 필요)
git pull && docker compose up -d --build

# 중지 (데이터는 pgdata 볼륨에 보존)
docker compose down
```

**킬스위치 / 유지보수 모드**: 잘못된 집계를 급히 멈추려면 `.env` 의 `BOT_PAUSED=true` 로 바꾸고 `docker compose up -d bot`. 조회 커맨드는 계속 동작합니다. 끝나면 `false` 로 되돌립니다.

**봇 무응답 런북**(사진에 반응 없음/슬래시 무응답):
1. `docker compose ps` — 컨테이너 상태 확인.
2. `docker compose logs --tail=100 bot` — 재연결 루프·예외 확인.
3. `docker compose restart bot` — 대개 복구됨.
> (오프라인 동안 올라온 사진은 재처리되지 않으므로, 사용자에게 재업로드를 안내하십시오.)

**백업/복원**(기본 미설정 — 꼭 구성하십시오). 봇은 스케줄러를 두지 않으므로 **호스트 cron** 으로 돌립니다.
```bash
# 백업 (변수는 컨테이너 안에서 풀리도록 sh -c)
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup-$(date +%F).sql
# 복원
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backup-YYYY-MM-DD.sql
# 예) 호스트 crontab — 매일 03:30 덤프 + 14일 경과분 삭제
# 30 3 * * * cd /path/to/running-streak && docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backups/$(date +\%F).sql && find backups -name '*.sql' -mtime +14 -delete
```

> ⚠️ `docker compose down -v` 는 `pgdata` 볼륨까지 삭제합니다(스트릭 전부 소실, 되돌릴 수 없음).
> `restart: unless-stopped` 로 재부팅 시 자동 복구되며, 로그는 자동 로테이션(10m × 3)됩니다.

---

## 9. 보안 체크리스트

- **`DISCORD_TOKEN`·`POSTGRES_PASSWORD` 를 채팅이나 AI 도구에 붙여넣지 마십시오** — 제3자 서버를 경유합니다. 본인 터미널에서 `.env` 에만 입력하십시오.
- 서버(길드)·채널 ID 는 비밀이 아닙니다.
- 커밋 가드 훅 활성화: `git config core.hooksPath .githooks` (민감 패턴이 섞인 커밋을 차단).
- 토큰이 유출되면 Developer Portal 에서 **Reset Token** 후 `.env` 갱신 → `docker compose up -d`.
- DB 에 호스트 포트 매핑을 추가하지 마십시오(내부 네트워크 전용 유지).

### 개인정보(타인 데이터) 취급
- 봇은 등록(옵트인) 사용자의 **디스코드 ID + 사진에서 읽은 수치(거리·시간·페이스·칼로리)** 만 저장합니다. **사진 원본·OCR 원문은 저장하지 않습니다**(데이터 최소화).
- 사용자는 `/달리기 전체삭제`(`확인=삭제`)로 **본인 데이터 전부를 직접 삭제**할 수 있습니다(삭제권). 운영자 수동 삭제가 필요하면:
  `docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DELETE FROM run_logs WHERE user_id=<ID>; DELETE FROM runners WHERE user_id=<ID>;"'`
- 공개 레포에 **타인의 식별정보(닉네임·실데이터)를 커밋하지 마십시오**. 본 레포는 커밋 가드 훅으로 비밀 패턴을 차단하지만, 닉네임 같은 자유 텍스트는 작성자가 직접 주의해야 합니다.

---

## 10. 참고

- [DESIGN.md](../DESIGN.md) — 동작·결정의 단일 진실원. **§3 불변 결정**(스케줄러 없음, OCR 부가정보, 날짜=업로드 KST 04시 경계, 유예 3일, 조회 읽기전용)은 임의로 바꾸지 마십시오.
- [RUNNING_STREAK_BOT_SPEC.md](../RUNNING_STREAK_BOT_SPEC.md) — 각 결정의 근거(WHY).
- [README.md](../README.md) — 요약 퀵스타트.
