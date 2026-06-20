"""슬래시 커맨드 (명세 8 + 확장).

/달리기 등록·해제·취소 · /스트릭 · /기록 · /리더보드 · /도움
조회 계열(/스트릭·/기록·/리더보드)은 읽기 전용: 저장값을 절대 변경하지 않는다 (명세 7.2).
"""

from __future__ import annotations

import logging
from datetime import datetime

import discord
from discord import app_commands

from .config import Config
from .db import Database
from .events import KST
from .streak import effective_streak

log = logging.getLogger("commands")


def _today_kst():
    return datetime.now(KST).date()


def _fmt_duration(total_sec) -> str:
    sec = int(total_sec or 0)
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


def _fmt_pace(pace_sec) -> str:
    if not pace_sec:
        return "—"
    p = int(round(float(pace_sec)))
    return f"{p // 60}'{p % 60:02d}\"/km"


HELP_TEXT = """## 🏃 러닝 스트릭 봇 사용설명서

지정된 채널에 **러닝 기록 사진**을 올리면, 봇이 자동으로 **연속 러닝 일수(스트릭)** 를 세어 줍니다.

### 시작하기
1. `/달리기 등록` — 선수로 등록(딱 한 번만 하면 됩니다).
2. 지정 채널에 러닝 앱 캡처(또는 러닝 사진)를 업로드.
3. 봇이 사진에 ✅ 를 달고 `오늘의 러닝 완료! N일째 연속!` 로 답하면 집계 완료!
   (✅ 는 접수 표시이고, 잠시 뒤 메시지가 따라옵니다.)

### 스트릭 규칙
- 날짜 기준은 **사진을 올린 시각(한국시간)** 입니다. (사진 속 날짜가 아님)
- **마지막 러닝 이후 3일 이내**에 다시 뛰면 스트릭이 **유지**되고, **4일 이상** 비면 **리셋**됩니다.
- **실제 뛴 날만** 1씩 올라갑니다. 쉰 날은 세지 않습니다.
- 같은 날 여러 번 올려도 **하루 1회**만 집계됩니다.

### 명령어
- `/달리기 등록` — 선수 등록(옵트인)
- `/달리기 해제` — 등록 취소 (기록은 보존)
- `/달리기 취소` — 내 **가장 최근 기록 1건** 되돌리기 (잘못 올렸을 때)
- `/스트릭` (또는 `/기록`) — 내 러닝 기록: 연속·최장·이번 달 + 누적 거리·시간·페이스·칼로리
- `/리더보드` — 등록 선수들의 스트릭 랭킹
- `/도움` — 이 설명서

### 참고
- 등록한 사람이 **지정된 채널**에 올린 **이미지**만 집계됩니다(다른 채널·미등록자·텍스트는 무시).
- 거리·시간·페이스·칼로리는 사진에서 자동 인식(OCR)한 값이라 **정확하지 않거나 일부 비어 있을 수 있습니다**(앱·화면마다 다름). 다만 **스트릭(연속 일수) 집계에는 전혀 영향이 없습니다.**"""


def setup_commands(bot: discord.Client, db: Database, config: Config) -> None:
    tree = bot.tree
    guild = discord.Object(id=config.guild_id)

    # --- /달리기 (그룹) + 등록/해제 ---------------------------------------
    dalligi = app_commands.Group(
        name="달리기",
        description="러닝 스트릭 선수 등록 관리",
        guild_ids=[config.guild_id],
    )

    @dalligi.command(name="등록", description="러닝 스트릭 선수로 등록(옵트인)합니다.")
    async def register(interaction: discord.Interaction):
        already = await db.is_registered(interaction.user.id)
        await db.register(interaction.user.id)
        if already:
            msg = "이미 등록되어 있습니다. 지정 채널에 러닝 사진을 올리면 자동으로 집계됩니다. 🏃"
        else:
            msg = (
                "등록이 완료되었습니다. 지정 채널에 러닝 사진을 올리면 자동으로 스트릭이 쌓입니다. 🏃\n"
                "현재 스트릭은 `/스트릭`, 랭킹은 `/리더보드` 로 확인할 수 있습니다."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @dalligi.command(name="해제", description="등록을 취소합니다(기록은 보존됩니다).")
    async def unregister(interaction: discord.Interaction):
        was = await db.unregister(interaction.user.id)
        msg = (
            "등록을 해제하였습니다. 다시 `/달리기 등록` 하면 이어서 집계됩니다."
            if was
            else "등록되어 있지 않습니다. `/달리기 등록` 으로 시작하십시오."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @dalligi.command(name="취소", description="내 가장 최근 러닝 기록 1건을 취소(삭제)합니다.")
    async def cancel(interaction: discord.Interaction):
        result = await db.undo_last_run(interaction.user.id)
        if result is None:
            await interaction.response.send_message(
                "취소할 기록이 없습니다.", ephemeral=True
            )
            return
        deleted_date, cur, total = result
        await interaction.response.send_message(
            f"최근 기록(`{deleted_date.isoformat()}`)을 취소하였습니다. "
            f"현재 연속 **{cur}일** · 총 **{total}회**.\n"
            "잘못 올린 기록이라면 올바른 날짜의 사진을 다시 올리십시오.",
            ephemeral=True,
        )

    tree.add_command(dalligi, guild=guild)

    # --- /스트릭 = /기록 (개인 통합 조회, 읽기 전용) ----------------------
    #     스트릭 + 누적 거리·시간·페이스·칼로리를 한 화면에. /기록 은 별칭(같은 출력).
    async def _send_my_stats(interaction: discord.Interaction):
        record = await db.load(interaction.user.id)
        if record is None or not record.registered:
            await interaction.response.send_message(
                "아직 기록이 없습니다. `/달리기 등록` 후 러닝 사진을 올리십시오.",
                ephemeral=True,
            )
            return

        today = _today_kst()
        eff = effective_streak(record.last_run_date, record.current_streak, today)
        name = interaction.user.display_name
        broken = "" if eff > 0 else "  _(끊김 — 오늘 다시 달리면 1일째!)_"
        lines = [
            f"## 🏃 {name} 님의 러닝 기록",
            f"🔥 현재 연속 **{eff}일**{broken}  ·  🏆 최장 {record.max_streak}일",
        ]

        if record.total_runs == 0:
            lines.append("아직 러닝 기록이 없습니다. 지정 채널에 사진을 올리십시오.")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)
            return

        month_count = await db.count_runs_in_month(
            interaction.user.id, today.year, today.month
        )
        s = await db.stats(interaction.user.id)
        runs = int(s.get("runs") or 0)
        dist_sum = s.get("dist_sum")
        dist = f"{float(dist_sum):.2f}km" if dist_sum is not None else "—"
        last = record.last_run_date.isoformat() if record.last_run_date else "—"
        lines += [
            f"📅 이번 달 {month_count}회  ·  📈 총 {record.total_runs}일",
            f"📏 누적 거리 {dist}  ·  ⏱️ 총 시간 {_fmt_duration(s.get('dur_sum'))}",
            f"⚡ 평균 페이스 {_fmt_pace(s.get('pace_avg'))}  ·  🔥 총 칼로리 {int(s.get('cal_sum') or 0)}kcal",
            f"🕒 마지막 러닝: {last}",
        ]
        if (s.get("dist_n") or 0) < runs or (s.get("cal_n") or 0) < runs:
            lines.append(
                f"-# OCR 인식 기준 — 거리 {s.get('dist_n') or 0}/{runs}, "
                f"칼로리 {s.get('cal_n') or 0}/{runs}회"
            )
        # 부가정보 정확도 disclaimer (스트릭은 무관함을 함께 안내)
        lines.append(
            "-# ⚠️ 거리·시간·페이스·칼로리는 사진 자동 인식(OCR) 값이라 정확하지 않을 수 있습니다. "
            "스트릭 집계에는 영향이 없습니다."
        )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(
        name="스트릭",
        description="내 러닝 기록(연속·누적 거리/시간/페이스/칼로리)을 봅니다.",
        guild=guild,
    )
    async def streak_cmd(interaction: discord.Interaction):
        await _send_my_stats(interaction)

    @tree.command(
        name="기록",
        description="내 러닝 기록(연속·누적 거리/시간/페이스/칼로리)을 봅니다. (/스트릭과 동일)",
        guild=guild,
    )
    async def records_cmd(interaction: discord.Interaction):
        await _send_my_stats(interaction)

    # --- /리더보드 (읽기 전용) -------------------------------------------
    @tree.command(
        name="리더보드",
        description="등록 선수들의 스트릭 랭킹을 봅니다.",
        guild=guild,
    )
    async def leaderboard_cmd(interaction: discord.Interaction):
        await interaction.response.defer()  # 멤버 조회로 약간 지연될 수 있음
        runners = await db.list_registered()
        today = _today_kst()
        dist_totals = await db.distance_totals()  # user_id -> 누적 거리(km)

        ranked = sorted(
            runners,
            key=lambda r: (
                effective_streak(r.last_run_date, r.current_streak, today),
                r.max_streak,
                r.total_runs,
            ),
            reverse=True,
        )

        if not ranked:
            await interaction.followup.send("아직 등록된 선수가 없습니다. `/달리기 등록` 으로 시작하십시오.")
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = ["## 🏃 러닝 스트릭 리더보드"]
        for i, r in enumerate(ranked):
            eff = effective_streak(r.last_run_date, r.current_streak, today)
            rank = medals[i] if i < 3 else f"{i + 1}."
            name = await _display_name(interaction, r.user_id)
            dist = dist_totals.get(r.user_id)
            dist_str = f" · 누적 {float(dist):.1f}km" if dist else ""
            lines.append(
                f"{rank} **{name}** — {eff}일 연속 (최장 {r.max_streak}일){dist_str}"
            )
        # 이름 표기로 인한 멘션 알림이 울리지 않도록 멘션 비활성화.
        await interaction.followup.send(
            "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
        )

    # --- /도움 (사용설명서) ----------------------------------------------
    @tree.command(name="도움", description="러닝 스트릭 봇 사용설명서를 봅니다.", guild=guild)
    async def help_cmd(interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)

    async def _display_name(interaction: discord.Interaction, user_id: int) -> str:
        """표시 이름: 캐시된 멤버 → REST fetch_user → 폴백. members intent 불필요."""
        guild_obj = interaction.guild
        if guild_obj is not None:
            member = guild_obj.get_member(user_id)
            if member is not None:
                return member.display_name
        try:
            user = await interaction.client.fetch_user(user_id)
            return user.display_name
        except Exception:  # noqa: BLE001
            return f"사용자({user_id})"
