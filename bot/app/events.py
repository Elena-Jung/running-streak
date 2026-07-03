"""사진 업로드 이벤트 처리 (명세 7.1).

등록된 선수가 지정 채널에 이미지를 올리면 스트릭을 갱신하고 응답한다.
모든 계산은 이 이벤트 시점에만 일어난다(스케줄러 없음 — 명세 9).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from .config import Config
from .db import Database
from .streak import MILESTONE_STEP, compute_on_run

log = logging.getLogger("events")

KST = ZoneInfo("Asia/Seoul")

# 러닝 '하루'의 경계 시각(KST). 자정이 아니라 04시를 기준으로 날이 바뀐다.
# 하절기 새벽 러닝(00:00~03:59)을 '전날'의 러닝으로 인정하기 위함(명세 3).
# 한국시간(Asia/Seoul)은 DST가 없어 고정 +9 → 시간 빼기 연산이 안전하다.
DAY_RESET_HOUR = 4

# 이미지로 취급할 콘텐츠 타입/확장자.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp")

# OCR 을 시도할 최대 이미지 크기(디스코드 업로드 한도와 동일선). 초과 시 OCR 만 생략(집계는 정상).
_MAX_OCR_IMAGE_BYTES = 25 * 1024 * 1024

# 동시 OCR 개수 제한: Tesseract 는 CPU 무겁고 블로킹이라, 여러 명이 동시에 올려도
# 워커 스레드 수를 묶어 호스트를 보호한다(이벤트 루프는 to_thread 로 비차단).
_OCR_SEMAPHORE = asyncio.Semaphore(2)

# 다운로드+OCR 전체를 묶는 입장 세마포어. att.read()(최대 25MB)가 OCR 세마포어 '이전'이라,
# 이게 없으면 동시 업로드가 각자 최대 25MB 를 메모리에 들고 대기 → OOM 여지. 동시 무거운 처리 수 상한.
_HEAVY_SEMAPHORE = asyncio.Semaphore(4)

# 사용자별 처리 쿨다운(초)과 마지막 처리 시각. 한 사람의 연속 업로드 폭주가 자원을 독점하지 못하게.
_USER_COOLDOWN_SEC = 3.0
_LAST_HANDLED: dict[int, float] = {}


async def _run_ocr(image_bytes: bytes):
    """OCR 을 워커 스레드에서 실행(이벤트 루프 비차단) + 동시 실행 수 제한."""
    from . import ocr

    async with _OCR_SEMAPHORE:
        return await asyncio.to_thread(ocr.try_extract, image_bytes)


async def _swap_reaction(message, *, remove: str | None = None, add: str | None = None) -> None:
    """봇 '자신의' 리액션을 제거/추가한다. 자기 리액션이라 Manage Messages 권한이 필요 없다. 실패는 무시."""
    try:
        me = message.guild.me if message.guild else None
        if remove and me is not None:
            await message.remove_reaction(remove, me)
        if add:
            await message.add_reaction(add)
    except Exception as e:  # noqa: BLE001
        log.warning("리액션 교체 실패(무시): %s", e)


def _has_image(message: discord.Message) -> bool:
    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        if ctype.startswith("image/"):
            return True
        if att.filename.lower().endswith(_IMAGE_EXTS):
            return True
    return False


def _first_image(message: discord.Message) -> discord.Attachment | None:
    for att in message.attachments:
        ctype = (att.content_type or "").lower()
        if ctype.startswith("image/") or att.filename.lower().endswith(_IMAGE_EXTS):
            return att
    return None


def to_run_date(dt) -> date:
    """업로드 시각(UTC, tz-aware) 을 '러닝 하루' 날짜로 환산 (명세 3).

    하루 경계가 자정이 아니라 KST 04:00 이다. 즉 00:00~03:59(KST) 의 새벽 러닝은
    '전날'의 러닝으로 친다(하절기 새벽 러닝 배려). 구현은 KST 로 옮긴 뒤
    DAY_RESET_HOUR 만큼 빼고 날짜를 취하는 것과 동치다.
    예) 06-25 02:00 KST → 06-24, 06-25 04:00 KST → 06-25, 06-25 23:00 KST → 06-25.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    kst = dt.astimezone(KST)
    return (kst - timedelta(hours=DAY_RESET_HOUR)).date()


def current_run_date() -> date:
    """지금 시점의 '러닝 하루' 날짜(조회 시점의 today). to_run_date 와 동일 기준."""
    return to_run_date(datetime.now(timezone.utc))


async def handle_message(
    message: discord.Message,
    *,
    config: Config,
    db: Database,
    bot_user_id: int,
) -> None:
    # 1) 봇 자기 메시지/다른 봇/DM 무시, 지정 채널만 대상.
    if message.author.bot or message.author.id == bot_user_id:
        return
    if message.guild is None:
        return
    if message.channel.id != config.target_channel_id:
        return
    # 1.5) 킬스위치/유지보수 모드: 집계 즉시 중단(조회 커맨드는 영향 없음).
    if config.paused:
        return
    # 2) 이미지 첨부 없으면 무시.
    if not _has_image(message):
        return
    # 3) 등록된 선수만 집계.
    if not await db.is_registered(message.author.id):
        return

    user_id = message.author.id
    # 3.5) 사용자별 쿨다운: 폭주(연속 업로드)로 다운로드·OCR 자원을 독점하지 못하게 최소 간격.
    now_mono = time.monotonic()
    last = _LAST_HANDLED.get(user_id)
    if last is not None and now_mono - last < _USER_COOLDOWN_SEC:
        return
    _LAST_HANDLED[user_id] = now_mono

    today = to_run_date(message.created_at)

    record = await db.load(user_id)
    last_run = record.last_run_date if record else None
    cur = record.current_streak if record else 0

    _, counted = compute_on_run(last_run, cur, today)
    if not counted:
        # gap <= 0: 오늘 이미 기록/시계이상 → 무음(메시지·반응 모두 없음).
        return

    # 즉시 접수 표시: 느린 OCR 이전에 ✅ 로 빠른 피드백. (권한 없으면 무시)
    try:
        await message.add_reaction("✅")
    except Exception as e:  # noqa: BLE001
        log.warning("리액션 추가 실패(무시): %s", e)

    # 4) OCR 부가정보 — 다운로드+OCR 를 입장 세마포어로 묶어 동시 메모리 상한(OOM 방지),
    #    OCR 자체는 워커 스레드로 오프로드(이벤트 루프·하트비트 비차단). raw_text 는 저장하지 않는다(데이터 최소화).
    fields: dict = {}
    ocr_attempted = False
    if config.ocr_enabled:
        att = _first_image(message)
        if att is not None and (att.size or 0) <= _MAX_OCR_IMAGE_BYTES:
            try:
                async with _HEAVY_SEMAPHORE:
                    image_bytes = await att.read()
                    fields, _ = await _run_ocr(image_bytes)
                ocr_attempted = True
            except Exception as e:  # noqa: BLE001 — 부가정보 실패는 무해
                log.warning("첨부 OCR 처리 실패(무시): %s", e)

    # 5) runners 갱신 + run_logs 원장 기록(트랜잭션, 원장 재계산). 실패 시 ✅→⚠️ 로 알려 거짓 접수 방지.
    try:
        recorded, new_streak = await db.record_run(user_id, today, **fields)
    except Exception:  # noqa: BLE001 — 저장 실패를 사용자에게 알린다(✅만 남아 오인하는 일 방지)
        log.exception("record_run 실패")
        await _swap_reaction(message, remove="✅", add="⚠️")
        return
    if not recorded:
        # 같은 날 이미 기록됨(동시 업로드 경합 등) → 중복 집계 방지. 거짓 접수로 남지 않게 ✅ 제거.
        await _swap_reaction(message, remove="✅")
        return

    # 6) 응답 (명세 7.1 / 8). 스트릭만 알리고 업로더 멘션을 덧붙인다(OCR 거리는 채널 미노출).
    #    소프트 힌트: OCR 을 '시도했으나' 4필드를 모두 못 읽었을 때만 취소 안내(집계는 막지 않음 — 명세 2).
    #    이미지 >25MB·다운로드 실패로 시도조차 못 한 경우엔 붙이지 않는다.
    hint = ""
    if ocr_attempted and not any(v is not None for v in fields.values()):
        hint = "\n-# 러닝 정보를 읽지 못했습니다. 잘못 올린 경우 `/달리기 취소` 로 되돌릴 수 있습니다."
    # 마일스톤(연속 10일마다) '달성 당일'에만 /자랑 권유 한 줄을 덧붙인다.
    # 사진 이벤트에 대한 응답이므로 스케줄러/푸시 알림이 아니다(DESIGN §3 예외 범위).
    milestone = ""
    if new_streak >= MILESTONE_STEP and new_streak % MILESTONE_STEP == 0:
        milestone = (
            f"\n🎉 연속 **{new_streak}일** 달성! `/자랑` 으로 자랑 카드를 만들어 보십시오."
        )
    # 완료 메시지는 간결하게 유지한다. 04시 경계(새벽 러닝=전날) 설명은 매번 띄우지 않고
    # /스트릭·/캘린더 안내로 옮겼다(사용자 결정 2026-06-25).
    await message.channel.send(
        f"### 러닝 기록 완료. {new_streak}일째 연속입니다.\n{message.author.mention}{milestone}{hint}"
    )
