"""유튜브 음악 기능 cog (/play, /stop) — 미니멀 버전(대기열 없음, 곡 교체 방식).

yt-dlp 로 스트림 URL 을 추출해 FFmpegOpusAudio 로 재생한다(라이브러리 측 Opus 재인코딩
생략). 유튜브 쪽 변경으로 추출이 깨지면 requirements 의 yt-dlp 핀을 올려 재빌드가 1순위.
길드 스코프는 main 의 add_cog(guilds=...) 주입으로 정해진다(이 파일엔 길드 지식 없음).
DB 를 쓰지 않으며 러닝 스트릭 기능과 상태를 공유하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("cogs.music")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch",
    "quiet": True,
    "no_warnings": True,
}

# googlevideo 스트림 URL 은 만료·순단이 있어 재접속 옵션이 필요.
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

ytdl = yt_dlp.YoutubeDL(YDL_OPTS)


def _short_error(error: Exception) -> str:
    lines = str(error).splitlines()
    return (lines[0] if lines else "알 수 없는 오류")[:200]


def _on_playback_end(error: Exception | None) -> None:
    if error:
        log.error("재생 중 오류: %s", error)
    else:
        log.info("재생 완료")


async def resolve_track(query: str) -> tuple[str, str]:
    """URL 또는 검색어를 (스트림 URL, 제목)으로 변환한다."""
    info = await asyncio.to_thread(ytdl.extract_info, query, download=False)
    if info and "entries" in info:
        entries = [entry for entry in info["entries"] if entry]
        info = entries[0] if entries else None
    if not info or "url" not in info:
        raise LookupError("검색 결과가 없습니다.")
    return info["url"], info.get("title", "제목 없음")


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="play", description="유튜브 URL이나 검색어로 노래를 재생합니다.")
    @app_commands.describe(query="유튜브 URL 또는 검색어")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        voice = interaction.user.voice
        if voice is None or voice.channel is None:
            await interaction.response.send_message(
                "먼저 음성 채널에 들어가 주십시오.", ephemeral=True
            )
            return

        await interaction.response.defer()  # 추출에 수 초 걸릴 수 있어 3초 ack 한도 회피

        try:
            stream_url, title = await resolve_track(query)
        except (yt_dlp.utils.DownloadError, LookupError) as error:
            await interaction.followup.send(
                f"오디오를 가져오지 못했습니다: {_short_error(error)}"
            )
            return

        vc = interaction.guild.voice_client
        if vc is None:
            vc = await voice.channel.connect()
        elif vc.channel != voice.channel:
            await vc.move_to(voice.channel)

        if vc.is_playing() or vc.is_paused():
            vc.stop()  # 대기열 없이 현재 곡을 교체(미니멀 설계)

        vc.play(discord.FFmpegOpusAudio(stream_url, **FFMPEG_OPTS), after=_on_playback_end)
        await interaction.followup.send(f"재생 중: **{title}**")

    @app_commands.command(name="stop", description="재생을 멈추고 음성 채널에서 나갑니다.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None:
            await interaction.response.send_message(
                "음성 채널에 접속해 있지 않습니다.", ephemeral=True
            )
            return
        vc.stop()
        await vc.disconnect()
        await interaction.response.send_message("재생을 멈추고 나왔습니다.")
