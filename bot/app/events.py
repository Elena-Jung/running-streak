"""사진 업로드 이벤트 처리 (명세 7.1).

등록된 선수가 지정 채널에 이미지를 올리면 스트릭을 갱신하고 응답한다.
모든 계산은 이 이벤트 시점에만 일어난다(스케줄러 없음 — 명세 9).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from .config import Config
from .db import Database
from .streak import compute_on_run

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


async def _run_ocr(image_bytes: bytes):
    """OCR 을 워커 스레드에서 실행(이벤트 루프 비차단) + 동시 실행 수 제한."""
    from . import ocr

    async with _OCR_SEMAPHORE:
        return await asyncio.to_thread(ocr.try_extract, image_bytes)


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
    # 2) 이미지 첨부 없으면 무시.
    if not _has_image(message):
        return
    # 3) 등록된 선수만 집계.
    if not await db.is_registered(message.author.id):
        return

    user_id = message.author.id
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

    # 4) OCR 부가정보 — 워커 스레드로 오프로드(이벤트 루프·하트비트 비차단) + 동시 수 제한.
    fields: dict = {}
    raw_text = None
    if config.ocr_enabled:
        att = _first_image(message)
        if att is not None and (att.size or 0) <= _MAX_OCR_IMAGE_BYTES:
            try:
                image_bytes = await att.read()
                fields, raw_text = await _run_ocr(image_bytes)
            except Exception as e:  # noqa: BLE001 — 부가정보 실패는 무해
                log.warning("첨부 OCR 처리 실패(무시): %s", e)

    # 5) runners 갱신 + run_logs 원장 기록(트랜잭션, 원장 재계산). 실패 시 ✅→⚠️ 로 알려 거짓 접수 방지.
    try:
        recorded, new_streak = await db.record_run(
            user_id, today, raw_text=raw_text, **fields
        )
    except Exception:  # noqa: BLE001 — 저장 실패를 사용자에게 알린다(✅만 남아 오인하는 일 방지)
        log.exception("record_run 실패")
        try:
            me = message.guild.me if message.guild else None
            if me is not None:
                await message.remove_reaction("✅", me)
            await message.add_reaction("⚠️")
        except Exception as e:  # noqa: BLE001
            log.warning("실패 표시 리액션 변경 실패: %s", e)
        return
    if not recorded:
        # 같은 날 이미 기록됨(동시 업로드 경합 등) → 중복 집계 방지, 무음.
        return

    # 6) 응답 (명세 7.1 / 8). 스트릭만 알리고 업로더 멘션을 덧붙인다(OCR 거리는 채널 미노출).
    #    소프트 힌트: OCR 4필드를 모두 못 읽었으면 취소 안내만 덧붙인다(집계는 막지 않음 — 명세 2).
    hint = ""
    if config.ocr_enabled and not any(v is not None for v in fields.values()):
        hint = "\n-# 러닝 정보를 읽지 못했습니다. 잘못 올린 경우 `/달리기 취소` 로 되돌릴 수 있습니다."
    # 새벽(00:00~03:59 KST) 업로드는 04시 리셋으로 '전날'에 집계된다. 그 시간대에만 안내를 덧붙여
    # '오늘'이라는 오해를 막는다(낮 시간대는 기존처럼 간결하게 둔다 — today 가 곧 달력상 오늘).
    created = message.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    day_note = ""
    if created.astimezone(KST).hour < DAY_RESET_HOUR:
        day_note = f"\n-# 새벽 러닝은 전날({today.month}/{today.day}) 기록으로 집계됩니다(하루 경계 오전 4시)."
    await message.channel.send(
        f"### 러닝 기록 완료. {new_streak}일째 연속입니다.\n{message.author.mention}{day_note}{hint}"
    )
