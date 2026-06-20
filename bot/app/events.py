"""사진 업로드 이벤트 처리 (명세 7.1).

등록된 선수가 지정 채널에 이미지를 올리면 스트릭을 갱신하고 응답한다.
모든 계산은 이 이벤트 시점에만 일어난다(스케줄러 없음 — 명세 9).
"""

from __future__ import annotations

import logging
from datetime import date, timezone
from zoneinfo import ZoneInfo

import discord

from .config import Config
from .db import Database
from .streak import compute_on_run

log = logging.getLogger("events")

KST = ZoneInfo("Asia/Seoul")

# 이미지로 취급할 콘텐츠 타입/확장자.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".bmp")

# OCR 을 시도할 최대 이미지 크기(디스코드 업로드 한도와 동일선). 초과 시 OCR 만 생략(집계는 정상).
_MAX_OCR_IMAGE_BYTES = 25 * 1024 * 1024


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


def to_kst_date(dt) -> date:
    """디스코드 메시지의 created_at(UTC, tz-aware) 을 KST 날짜로 환산 (명세 3)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).date()


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
    today = to_kst_date(message.created_at)

    record = await db.load(user_id)
    last_run = record.last_run_date if record else None
    cur = record.current_streak if record else 0
    cur_max = record.max_streak if record else 0

    new_streak, counted = compute_on_run(last_run, cur, today)
    if not counted:
        # gap == 0: 오늘 이미 기록됨 → 무음(메시지·반응 모두 없음).
        return

    # 즉시 접수 표시: 느린 OCR 이전에 ✅ 를 달아 사용자에게 빠른 피드백.
    # (봇에 '반응 추가하기' 권한 필요. 없으면 무시하고 진행.)
    try:
        await message.add_reaction("✅")
    except Exception as e:  # noqa: BLE001
        log.warning("리액션 추가 실패(무시): %s", e)

    new_max = max(cur_max, new_streak)

    # 4) OCR 부가정보(거리·시간·페이스·칼로리)는 본류 저장 전에 best-effort 로 뽑아둔다.
    fields: dict = {}
    raw_text = None
    if config.ocr_enabled:
        att = _first_image(message)
        if att is not None and (att.size or 0) <= _MAX_OCR_IMAGE_BYTES:
            try:
                from . import ocr

                image_bytes = await att.read()
                fields, raw_text = ocr.try_extract(image_bytes)
            except Exception as e:  # noqa: BLE001 — 부가정보 실패는 무해
                log.warning("첨부 OCR 처리 실패(무시): %s", e)

    # runners 갱신 + run_logs 원장 기록을 한 트랜잭션으로. 원장은 취소/재계산의 근거라 항상 남긴다.
    recorded = await db.record_run(
        user_id, today, new_streak, new_max, raw_text=raw_text, **fields
    )
    if not recorded:
        # 같은 날 이미 기록됨(거의 동시에 두 장 업로드한 경합 등) → 중복 집계 방지, 무음.
        return

    # 5) 응답 (명세 7.1 / 8). 메시지는 스트릭만 알리고, 누구 기록인지 멘션을 덧붙인다.
    #    OCR 거리는 신뢰도가 낮아 채널에 노출하지 않고 run_logs 에만 남긴다(부가정보).
    #    소프트 힌트: OCR 이 4필드를 모두 못 읽었으면(엉뚱한 이미지일 수 있음) 취소 안내만 덧붙인다.
    #    집계 자체는 막지 않는다 — 명세 2(OCR 이 스트릭을 좌우하지 않음) 준수.
    hint = ""
    if config.ocr_enabled and not any(v is not None for v in fields.values()):
        hint = "\n-# 러닝 정보를 읽지 못했습니다. 잘못 올린 경우 `/달리기 취소` 로 되돌릴 수 있습니다."
    await message.channel.send(
        f"### 오늘 러닝 기록 완료. {new_streak}일째 연속입니다.\n{message.author.mention}{hint}"
    )
