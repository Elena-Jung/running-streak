# 러닝 스트릭 디스코드 봇

개인 디스코드 서버의 지정 채널에 **등록된 사용자**가 러닝 사진을 올리면, 봇이 자동으로
**연속 러닝 일수(스트릭)** 를 기록하고 `/스트릭`·`/리더보드`로 조회를 제공한다.

설계 근거는 `RUNNING_STREAK_BOT_SPEC.md`(1단계 명세) 참고. 이 저장소는 2단계(구현) 산출물.

## 구성

- **bot** 컨테이너: Python 3.12 + discord.py. 사진 이벤트/슬래시 커맨드 처리.
- **db** 컨테이너: PostgreSQL 16 (봇 전용, 호스트 포트 미발행 → 기존 호스트 PG와 충돌 없음).
- 스케줄러 없음. 모든 계산은 **사진 업로드 시점** 또는 **조회 시점**에만.

## 스트릭 규칙 (요약)

- 날짜 기준 = **메시지 업로드 시각(KST)**. 단 **하루 경계는 자정이 아니라 새벽 4시** — 0시~새벽 4시 업로드는 **전날**로 집계(새벽 러닝 배려). (OCR 날짜는 쓰지 않음)
- 마지막 러닝 이후 **간격 ≤ 3일 유지, ≥ 4일 리셋**. 실제 뛴 날만 카운트.
- 조회는 읽기 전용 — 끊긴 스트릭은 표시 때 0으로 보정(유령 스트릭 방지).

## 명령어

| 명령어 | 동작 |
|--------|------|
| `/달리기 등록` | 선수 등록(옵트인). 이후 지정 채널 사진 자동 집계 시작 |
| `/달리기 해제` | 등록 취소(기록은 보존) |
| `/달리기 취소` | 내 가장 최근 기록 1건 되돌리기 |
| `/달리기 전체삭제` | 내 모든 데이터 영구 삭제(`확인=삭제` 필요) |
| `/스트릭` (또는 `/기록`) | 내 현재 연속 일수 + 누적 통계 (읽기 전용, 본인만 보임) |
| `/캘린더 [월] [연도]` | 러닝 달력 + 주간·월간 합계 |
| `/리더보드` | 등록 선수들의 스트릭 랭킹 |
| `/자랑` | 마일스톤(10·25·50·100일 연속) 달성 시, 배경 사진에 기록을 얹은 **자랑 카드** 링크(본인만 보임) |

자동: 등록 선수가 지정 채널에 사진 업로드 → 스트릭 갱신 + `### 러닝 기록 완료. N일째 연속입니다.` (04시 경계 안내는 `/스트릭`·`/캘린더`에 표시)

`/자랑` 은 통계를 URL 조각(`#`)에 담은 정적 카드 페이지([`web/index.html`](web/index.html)) 링크를 돌려줍니다. 이 페이지를 아무 정적 호스트로 서빙하고 그 주소를 `.env` 의 `BRAG_BASE_URL` 에 넣으면 됩니다(미설정 시 링크 대신 통계 텍스트만 표시). 사진은 브라우저 밖으로 나가지 않고, 수치는 서버로 전송되지 않습니다.

---

## 셀프 호스팅 (Self-hosting)

이 봇은 호스트 비종속(`.env` 기반)이라 누구나 자신의 디스코드 서버·호스트에 배포할 수 있습니다.

- **사람용 상세 가이드**: [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md)
- **AI 에이전트용 배포 지침**: [`AGENTS.md`](AGENTS.md) — 저장소를 건네받은 에이전트가 자동으로 읽는 파일.

> 비밀값(`DISCORD_TOKEN`, `POSTGRES_PASSWORD`)은 **본인 터미널에서 `.env` 에 직접** 입력하십시오. 채팅·AI 도구에 붙여넣지 마십시오(제3자 서버 경유).

아래 1)~4) 는 (원 서버 기준) 요약 퀵스타트입니다.

---

## 1) Discord 봇 발급 (최초 1회, 사용자 수동)

1. https://discord.com/developers/applications → **New Application** 생성.
2. **Bot** 탭 → **Reset Token** → 토큰 복사 → 나중에 `.env` 의 `DISCORD_TOKEN` 에 붙여넣기.
3. 같은 **Bot** 탭의 **Privileged Gateway Intents** → **MESSAGE CONTENT INTENT** 를 **켠다**.
   (이미지 첨부를 읽으려면 필수)
4. **OAuth2 → URL Generator**:
   - scopes: `bot`, `applications.commands`
   - bot permissions: `View Channels`, `Send Messages`, `Read Message History`, `Add Reactions`
   - 생성된 URL 을 열어 **봇을 서버에 초대**.
5. 디스코드 앱: **설정 → 고급 → 개발자 모드** 켜기.
   - 서버 아이콘 우클릭 → **ID 복사** = `DISCORD_GUILD_ID`
   - 집계할 채널 우클릭 → **ID 복사** = `TARGET_CHANNEL_ID`

## 2) 설정 파일 작성

```bash
# 커밋 가드 훅 활성화(클론 후 1회, 커밋 전에) — 민감 패턴 오커밋 차단
git config core.hooksPath .githooks

cp .env.example .env
# .env 를 열어 DISCORD_TOKEN / DISCORD_GUILD_ID / TARGET_CHANNEL_ID 와
# POSTGRES_PASSWORD(임의 강한 값) 를 채운다.
```

## 3) 빌드 & 실행

```bash
docker compose up -d --build
docker compose logs -f bot      # "로그인:" 과 "슬래시 커맨드 N개 동기화 완료" 확인
```

## 4) 검증

```bash
# 단위 테스트 (스트릭 경계값)
docker compose run --rm bot python -m pytest -q

# DB 테이블 생성 확인 (변수는 컨테이너 내부에서 풀리도록 sh -c 로 감싼다 —
#  호스트 셸엔 POSTGRES_* 가 없어 그냥 쓰면 빈 값으로 확장됨)
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\dt"'
```

디스코드에서:
1. `/달리기 등록`
2. 대상 채널에 러닝 사진 업로드 → `러닝 기록 완료. 1일째 연속입니다.` 응답 확인
3. 같은 날 또 올리면 무응답(하루 1회만) 확인
4. `/스트릭`, `/리더보드` 확인

## 운영

```bash
docker compose ps          # 상태 (db 는 PORTS 비어 있어야 정상 = 호스트 미발행)
docker compose restart bot # 봇만 재시작
docker compose down        # 중지 (데이터는 pgdata 볼륨에 보존)
docker compose up -d --build   # 코드 수정 후 재배포
```

서버 재부팅 후에도 `restart: unless-stopped` 로 자동 복구된다.

**킬스위치**: 잘못된 집계를 급히 멈추려면 `.env` 의 `BOT_PAUSED=true` 로 바꾸고 `docker compose up -d bot`. (조회 커맨드는 계속 동작.)

**봇 무응답(사진에 반응 없음/슬래시 무응답) 런북**: `docker compose ps` → `docker compose logs --tail=100 bot` 으로 재연결/예외 확인 → `docker compose restart bot`.

**백업(권장, 기본 미설정)**: 호스트 cron 으로 매일 덤프 + N일 보존, 복원은 덤프를 psql 로 주입.
```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup-$(date +%F).sql   # 백업
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backup-YYYY-MM-DD.sql      # 복원
```

> 보안: `.env` 는 `.gitignore` 에 있어 커밋되지 않는다. 토큰이 노출되면 Developer Portal 에서 **Reset Token**. 상세 셀프호스팅·삭제·보존은 [`docs/SELF_HOSTING.md`](docs/SELF_HOSTING.md).
