"""봇 엔트리포인트.

- intents.message_content: 첨부(이미지) 접근에 필수 (개발자 포털에서도 활성화 필요).
- 슬래시 커맨드는 길드 스코프로 동기화해 즉시 반영.
- 스케줄러 없음: 모든 계산은 사진 이벤트/조회 시점에만 (명세 9).
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from . import events
from .commands import setup_commands
from .config import load_config
from .db import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")


def build_bot(config, db: Database) -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True  # 첨부/본문 접근 (유일하게 필요한 특권 intent)
    intents.guilds = True
    # members(특권) intent 는 쓰지 않는다. 리더보드 이름은 fetch_user 로 조회.

    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def setup_hook():
        await db.connect()
        setup_commands(bot, db, config)
        guild = discord.Object(id=config.guild_id)
        synced = await bot.tree.sync(guild=guild)
        log.info("슬래시 커맨드 %d개 동기화 완료 (guild=%s)", len(synced), config.guild_id)

    @bot.event
    async def on_ready():
        log.info("로그인: %s (id=%s)", bot.user, getattr(bot.user, "id", "?"))
        log.info("대상 채널 id=%s, OCR=%s", config.target_channel_id, config.ocr_enabled)

    @bot.event
    async def on_message(message: discord.Message):
        try:
            await events.handle_message(
                message,
                config=config,
                db=db,
                bot_user_id=bot.user.id if bot.user else 0,
            )
        except Exception:  # noqa: BLE001 — 한 메시지 처리 실패가 봇을 죽이지 않게
            log.exception("on_message 처리 중 오류")

    return bot


async def _amain() -> None:
    config = load_config()
    db = Database(config.dsn)
    bot = build_bot(config, db)
    try:
        await bot.start(config.discord_token)
    finally:
        await db.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("종료 요청 수신, 봇을 닫습니다.")


if __name__ == "__main__":
    main()
