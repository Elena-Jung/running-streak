"""봇 엔트리포인트 — 멀티기능 코어.

- 기능은 cog 단위(app/cogs/)로 나뉘며, 기능↔길드 매핑에 따라 로드·동기화한다:
  러닝 스트릭 → DISCORD_GUILD_ID(단일), 음악 → MUSIC_GUILD_IDS(다중, 비면 비활성).
  cog 가 등록되지 않은 길드에는 해당 기능의 명령이 아예 존재하지 않는다(기능 선별 사용).
- intents.message_content: 첨부(이미지) 접근에 필수 (개발자 포털에서도 활성화 필요).
- 슬래시 커맨드는 길드 스코프로 동기화해 즉시 반영.
- 스케줄러 없음: 러닝 계산은 사진 이벤트/조회 시점에만 (명세 9).
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .cogs.music import Music
from .cogs.running import Running
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
        # 기능 레지스트리: (이름, cog, 대상 길드 id들). 길드가 비면 기능을 로드하지 않는다.
        features = [
            ("running", Running(bot, db, config), (config.guild_id,)),
            ("music", Music(bot), config.music_guild_ids),
        ]
        target_guild_ids: set[int] = set()
        for name, cog, guild_ids in features:
            if not guild_ids:
                log.info("기능 비활성: %s (대상 길드 없음)", name)
                continue
            await bot.add_cog(cog, guilds=[discord.Object(id=g) for g in guild_ids])
            target_guild_ids.update(guild_ids)
            log.info("기능 로드: %s → 길드 %s", name, list(guild_ids))
        for gid in sorted(target_guild_ids):
            synced = await bot.tree.sync(guild=discord.Object(id=gid))
            log.info("슬래시 커맨드 %d개 동기화 완료 (guild=%s)", len(synced), gid)

    @bot.event
    async def on_message(message: discord.Message):
        # 의도적 no-op: prefix 커맨드 처리(process_commands)를 막는다 — cog 전환 전에도
        # 호출하지 않던 동작을 보존. 실제 메시지 처리는 Running cog 리스너가 담당한다.
        pass

    @bot.tree.error
    async def on_app_command_error(interaction: discord.Interaction, error):
        # 슬래시 커맨드 콜백에서 처리 안 된 예외 → 사용자에게 일시 오류 안내(무응답/영구 로딩 방지).
        log.exception("슬래시 커맨드 오류: %s", getattr(error, "original", error))
        msg = "일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주십시오."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:  # noqa: BLE001
            log.warning("오류 안내 전송 실패: %s", e)

    @bot.event
    async def on_ready():
        log.info("로그인: %s (id=%s)", bot.user, getattr(bot.user, "id", "?"))
        log.info(
            "대상 채널 id=%s, OCR=%s, 음악 길드=%s",
            config.target_channel_id,
            config.ocr_enabled,
            list(config.music_guild_ids) or "비활성",
        )

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
