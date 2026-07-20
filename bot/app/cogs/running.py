"""러닝 스트릭 기능 cog (명세 7.1 + 8).

명령 배선과 사진 이벤트 리스너만 담당한다. 규칙 계산은 streak.py, 업로드 이벤트 처리는
events.handle_message, 표시 헬퍼·사용설명서는 commands.py (순수 로직 — 테스트 대상).
길드 스코프는 main 의 add_cog(guilds=...) 주입으로 정해진다(이 파일엔 길드 지식 없음).
조회 계열(/스트릭·/기록·/리더보드·/캘린더)은 읽기 전용: 저장값을 절대 변경하지 않는다 (명세 7.2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .. import charts, events
from ..commands import (
    HELP_TEXT,
    build_brag_url,
    chunk_lines,
    display_name,
    fmt_duration,
    fmt_pace,
    weighted_pace_sec,
)
from ..config import Config
from ..db import Database
from ..streak import MILESTONE_STEP, effective_streak, highest_milestone

log = logging.getLogger("cogs.running")


def _today_run_date():
    """조회 기준 'today' = 러닝 하루(KST 04시 리셋). 기록·조회가 같은 기준을 쓰게 한다."""
    return events.current_run_date()


class Running(commands.Cog):
    def __init__(self, bot: commands.Bot, db: Database, config: Config) -> None:
        self.bot = bot
        self.db = db
        self.config = config

    # --- 사진 업로드 이벤트 (명세 7.1) ------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await events.handle_message(
                message,
                config=self.config,
                db=self.db,
                bot_user_id=self.bot.user.id if self.bot.user else 0,
            )
        except Exception:  # noqa: BLE001 — 한 메시지 처리 실패가 봇을 죽이지 않게
            log.exception("on_message 처리 중 오류")

    # --- /달리기 (그룹) + 등록/해제 ---------------------------------------
    dalligi = app_commands.Group(name="달리기", description="러닝 스트릭 선수 등록 관리")

    @dalligi.command(name="등록", description="러닝 스트릭 선수로 등록(옵트인)합니다.")
    async def register(self, interaction: discord.Interaction):
        already = await self.db.is_registered(interaction.user.id)
        await self.db.register(interaction.user.id)
        if already:
            msg = "이미 등록되어 있습니다. 지정 채널에 러닝 사진을 올리면 자동으로 집계됩니다. 🏃"
        else:
            msg = (
                "등록이 완료되었습니다. 지정 채널에 러닝 사진을 올리면 자동으로 스트릭이 쌓입니다. 🏃\n"
                "현재 스트릭은 `/스트릭`, 랭킹은 `/리더보드` 로 확인할 수 있습니다.\n"
                "-# 집계를 위해 디스코드 ID와 사진에서 읽은 거리·시간·페이스·칼로리를 저장합니다. "
                "전체 삭제는 `/달리기 전체삭제` 로 언제든 가능합니다."
            )
        await interaction.response.send_message(msg, ephemeral=True)

    @dalligi.command(name="해제", description="등록을 취소합니다(기록은 보존됩니다).")
    async def unregister(self, interaction: discord.Interaction):
        was = await self.db.unregister(interaction.user.id)
        msg = (
            "등록을 해제하였습니다. 다시 `/달리기 등록` 하면 이어서 집계됩니다."
            if was
            else "등록되어 있지 않습니다. `/달리기 등록` 으로 시작하십시오."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @dalligi.command(name="취소", description="내 가장 최근 러닝 기록 1건을 취소(삭제)합니다.")
    async def cancel(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # DELETE+재계산이 3초 넘을 수 있어 먼저 ack
        result = await self.db.undo_last_run(interaction.user.id)
        if result is None:
            await interaction.followup.send("취소할 기록이 없습니다.", ephemeral=True)
            return
        deleted_date, cur, total = result
        await interaction.followup.send(
            f"최근 기록(`{deleted_date.isoformat()}`)을 취소하였습니다. "
            f"현재 연속 **{cur}일** · 총 **{total}회**.\n"
            "잘못 올린 기록이라면 올바른 날짜의 사진을 다시 올리십시오.",
            ephemeral=True,
        )

    @dalligi.command(
        name="전체삭제",
        description="내 러닝 데이터(스트릭·모든 기록)를 영구 삭제합니다. 되돌릴 수 없습니다.",
    )
    @app_commands.describe(확인="정말 지우려면 '삭제' 를 입력하십시오.")
    async def purge(self, interaction: discord.Interaction, 확인: str = ""):
        # 파괴적·비가역 → 명시적 확인 토큰을 요구(버튼 대신 입력값으로 단순·검증가능하게).
        if 확인.strip() != "삭제":
            await interaction.response.send_message(
                "⚠️ 되돌릴 수 없는 작업입니다. 정말 삭제하려면 `확인` 옵션에 `삭제` 를 입력해 다시 실행하십시오.\n"
                "(내 디스코드 ID·연속 일수·모든 러닝 기록이 영구 삭제됩니다.)",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)  # DELETE 가 3초 넘을 수 있어 먼저 ack
        runs, had_row = await self.db.purge_user(interaction.user.id)
        if runs == 0 and not had_row:
            await interaction.followup.send("삭제할 데이터가 없습니다.", ephemeral=True)
            return
        await interaction.followup.send(
            f"내 러닝 데이터를 전부 삭제하였습니다(기록 {runs}건). 다시 시작하려면 `/달리기 등록` 하십시오.",
            ephemeral=True,
        )

    # --- /스트릭 = /기록 (개인 통합 조회, 읽기 전용) ----------------------
    #     스트릭 + 누적 거리·시간·페이스·칼로리를 한 화면에. /기록 은 별칭(같은 출력).
    async def _send_my_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # 다중 DB 조회 → 3초 ack 한도 회피
        record = await self.db.load(interaction.user.id)
        if record is None or not record.registered:
            await interaction.followup.send(
                "아직 기록이 없습니다. `/달리기 등록` 후 러닝 사진을 올리십시오.",
                ephemeral=True,
            )
            return

        today = _today_run_date()
        eff = effective_streak(record.last_run_date, record.current_streak, today)
        name = interaction.user.display_name
        broken = "" if eff > 0 else "  _(끊김 — 지금 다시 달리면 1일째부터!)_"
        lines = [
            f"## 🏃 {name} 님의 러닝 기록",
            f"🔥 현재 연속 **{eff}일**{broken}  ·  🏆 최장 {record.max_streak}일",
        ]

        if record.total_runs == 0:
            lines.append("아직 러닝 기록이 없습니다. 지정 채널에 사진을 올리십시오.")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return

        month_count = await self.db.count_runs_in_month(
            interaction.user.id, today.year, today.month
        )
        s = await self.db.stats(interaction.user.id)
        runs = int(s.get("runs") or 0)
        dist_sum = s.get("dist_sum")
        dist = f"{float(dist_sum):.2f}km" if dist_sum is not None else "—"
        last = record.last_run_date.isoformat() if record.last_run_date else "—"
        lines += [
            f"📅 이번 달 {month_count}회  ·  📈 총 {record.total_runs}일",
            f"📏 누적 거리 {dist}  ·  ⏱️ 총 시간 {fmt_duration(s.get('dur_sum'))}",
            f"⚡ 평균 페이스 {fmt_pace(weighted_pace_sec(s))}  ·  🔥 총 칼로리 {int(s.get('cal_sum') or 0)}kcal",
            f"🕒 마지막 러닝: {last}",
        ]

        # 성장 추세: 최근 러닝들의 페이스 스파크라인(페이스는 작을수록 빠름 → 부호 뒤집어 우상향=개선).
        paces = [
            r["pace_sec_per_km"]
            for r in await self.db.recent_runs(interaction.user.id, 12)
            if r["pace_sec_per_km"] is not None
        ]
        spark = charts.sparkline([-p for p in paces])
        if spark:
            lines.append(f"📈 페이스 추세(최근 {len(paces)}회, 높을수록 빠름): `{spark}`")

        if (s.get("dist_n") or 0) < runs or (s.get("cal_n") or 0) < runs:
            lines.append(
                f"-# OCR 인식 기준 — 거리 {s.get('dist_n') or 0}/{runs}, "
                f"칼로리 {s.get('cal_n') or 0}/{runs}회"
            )
        # 하루 경계(04시) 안내 — 새벽 러닝이 전날로 잡히는 이유를 여기서 알린다(완료 메시지엔 미표시).
        lines.append(
            "-# 🕓 하루 경계는 새벽 4시입니다 — 0시~새벽 4시 러닝은 전날로 집계됩니다."
        )
        # 부가정보 정확도 disclaimer (스트릭은 무관함을 함께 안내)
        lines.append(
            "-# ⚠️ 거리·시간·페이스·칼로리는 사진 자동 인식(OCR) 값이라 정확하지 않을 수 있습니다. "
            "스트릭 집계에는 영향이 없습니다."
        )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="스트릭",
        description="내 러닝 기록(연속·누적 거리/시간/페이스/칼로리)을 봅니다.",
    )
    async def streak_cmd(self, interaction: discord.Interaction):
        await self._send_my_stats(interaction)

    @app_commands.command(
        name="기록",
        description="내 러닝 기록(연속·누적 거리/시간/페이스/칼로리)을 봅니다. (/스트릭과 동일)",
    )
    async def records_cmd(self, interaction: discord.Interaction):
        await self._send_my_stats(interaction)

    # --- /자랑 (마일스톤 자랑 카드 링크, 읽기 전용·pull) -------------------
    #     현재 스트릭이 10일 단위 마일스톤(10·20·30…)에 닿으면, 통계를 프래그먼트에 담은
    #     정적 카드 페이지 링크를 '본인에게만' 회신한다. 서버·DB 에 새로 저장하는 것은 없다.
    @app_commands.command(
        name="자랑",
        description="스트릭 마일스톤(연속 10일마다) 자랑 카드를 만듭니다.",
    )
    async def brag_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # DB 다중 조회 → 3초 ack 회피
        record = await self.db.load(interaction.user.id)
        if record is None or not record.registered:
            await interaction.followup.send(
                "아직 기록이 없습니다. `/달리기 등록` 후 러닝 사진을 올리십시오.",
                ephemeral=True,
            )
            return

        today = _today_run_date()
        eff = effective_streak(record.last_run_date, record.current_streak, today)
        tier = highest_milestone(eff)
        if tier is None:
            await interaction.followup.send(
                f"현재 연속 **{eff}일** 입니다. **{MILESTONE_STEP}일 연속**부터 자랑 카드를 만들 수 있습니다. "
                "조금만 더 달려 보십시오! 🏃",
                ephemeral=True,
            )
            return

        s = await self.db.stats(interaction.user.id)
        runs = int(s.get("runs") or 0)
        dist_sum = s.get("dist_sum")
        dur_sum = s.get("dur_sum")
        pace_sec = weighted_pace_sec(s)  # /스트릭과 동일 공식(가중·자기일관)

        dist_txt = f"{float(dist_sum):.2f}km" if dist_sum is not None else "—"
        dur_txt = fmt_duration(dur_sum) if dur_sum is not None else "—"
        stat_line = (
            f"🔥 현재 연속 **{eff}일** · **{tier}일 마일스톤 달성**\n"
            f"📏 총 거리 {dist_txt} · ⏱️ 총 시간 {dur_txt} · "
            f"⚡ 평균 페이스 {fmt_pace(pace_sec)}"
        )

        if not self.config.brag_base_url:  # URL 미설정 → 텍스트 폴백
            await interaction.followup.send(
                f"## 🎉 {tier}일 연속 달성!\n{stat_line}\n"
                "-# 자랑 카드 페이지 주소(`BRAG_BASE_URL`)가 설정되지 않아 링크를 만들지 못했습니다.",
                ephemeral=True,
            )
            return

        url = build_brag_url(
            self.config.brag_base_url,
            streak=eff,
            tier=tier,
            dist_km=dist_sum,
            dur_sec=dur_sum,
            pace_sec=pace_sec,
            runs=runs,
            name=interaction.user.display_name,
        )
        lines = [
            f"## 🎉 {tier}일 연속 달성! 자랑 카드를 만들어 보십시오.",
            stat_line,
            f"👉 <{url}>",
            "-# 링크를 열어 배경 사진을 고르고 **이미지로 저장**하면 됩니다. "
            "통계는 URL 조각(#)에만 담겨 서버로 전송·저장되지 않으며, 이 링크는 나에게만 보입니다.",
        ]
        if (s.get("dist_n") or 0) < runs or (s.get("dur_n") or 0) < runs:
            lines.append(
                f"-# 거리·시간은 OCR 인식된 기록 기준(거리 {s.get('dist_n') or 0}/{runs} · "
                f"시간 {s.get('dur_n') or 0}/{runs}회)이라 실제보다 적을 수 있습니다."
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # --- /리더보드 (읽기 전용) -------------------------------------------
    @app_commands.command(
        name="리더보드",
        description="등록 선수들의 스트릭 랭킹을 봅니다.",
    )
    async def leaderboard_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer()  # 멤버 조회로 약간 지연될 수 있음
        runners = await self.db.list_registered()
        today = _today_run_date()
        dist_totals = await self.db.distance_totals()  # user_id -> 누적 거리(km)

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

        # 표시 이름은 캐시 미스 시 REST fetch_user 가 필요 → 순차 대신 동시 조회로 지연 누적 방지.
        names = await asyncio.gather(
            *(display_name(interaction, r.user_id) for r in ranked)
        )
        medals = ["🥇", "🥈", "🥉"]
        lines = ["## 🏃 러닝 스트릭 리더보드"]
        for i, r in enumerate(ranked):
            eff = effective_streak(r.last_run_date, r.current_streak, today)
            rank = medals[i] if i < 3 else f"{i + 1}."
            name = names[i]
            dist = dist_totals.get(r.user_id)
            dist_str = f" · 누적 {float(dist):.1f}km" if dist else ""
            lines.append(
                f"{rank} **{name}** — {eff}일 연속 (최장 {r.max_streak}일){dist_str}"
            )
        # 인원이 많아 2000자를 넘기면 분할 전송(단일 메시지 한도). 멘션 알림은 비활성화.
        for chunk in chunk_lines(lines, 1900):
            await interaction.followup.send(
                chunk, allowed_mentions=discord.AllowedMentions.none()
            )

    # --- /캘린더 (월 달력 + 주/월 합계, 읽기 전용·온디맨드) ---------------
    @app_commands.command(
        name="캘린더",
        description="러닝 달력과 주간·월간 합계를 봅니다.",
    )
    @app_commands.describe(
        month="조회할 월 (1-12, 생략 시 이번 달)",
        year="조회할 연도 (생략 시 자동: 미래 월이면 작년)",
    )
    async def calendar_cmd(
        self,
        interaction: discord.Interaction,
        month: int | None = None,
        year: int | None = None,
    ):
        await interaction.response.defer(ephemeral=True)  # DB 조회 → 3초 ack 회피
        today = _today_run_date()
        m = today.month if month is None else month
        if not 1 <= m <= 12:
            await interaction.followup.send("월은 1-12 사이로 입력해 주십시오.", ephemeral=True)
            return
        if year is not None:
            y = year
        elif month is None or m <= today.month:
            y = today.year
        else:
            y = today.year - 1  # 올해 아직 안 온 미래 월이면 작년으로 해석
        year = y

        rows = await self.db.month_run_metrics(interaction.user.id, year, m)
        run_days = {r["run_date"].day for r in rows}
        cal_text = charts.month_calendar(year, m, run_days)
        mcnt = len(rows)
        mdist = float(sum((r["distance_km"] or 0) for r in rows))
        mdur = sum((r["duration_sec"] or 0) for r in rows)
        mcal = sum((r["calories"] or 0) for r in rows)

        lines = [
            f"## 📅 {year}년 {m}월 러닝 달력",
            f"```\n{cal_text}\n```",
            "`*` = 달린 날",
            "-# 🕓 하루 경계는 새벽 4시 — 0시~새벽 4시 러닝은 전날 날짜로 집계됩니다.",
            f"📊 {m}월 합계: {mcnt}회 · {mdist:.1f}km · {fmt_duration(mdur)} · {mcal}kcal",
        ]
        if year == today.year and m == today.month:  # 현재 달일 때만 '이번 주' 합계
            # 달력 그리드가 일요일 시작이므로 주간 합계도 일~토로 맞춘다(주 경계 일치).
            sun = today - timedelta(days=(today.weekday() + 1) % 7)
            sat = sun + timedelta(days=6)
            w = await self.db.period_summary(interaction.user.id, sun, sat)
            wdist = float(w.get("dist") or 0)
            lines.append(
                f"🗓️ 이번 주(일~토): {int(w.get('cnt') or 0)}회 · {wdist:.1f}km · "
                f"{fmt_duration(w.get('dur') or 0)} · {int(w.get('cal') or 0)}kcal"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # --- /도움 (사용설명서) ----------------------------------------------
    @app_commands.command(name="도움", description="러닝 스트릭 봇 사용설명서를 봅니다.")
    async def help_cmd(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)
