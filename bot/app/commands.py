"""슬래시 커맨드 (명세 8 + 확장).

/달리기 등록·해제·취소 · /스트릭 · /기록 · /리더보드 · /도움
조회 계열(/스트릭·/기록·/리더보드)은 읽기 전용: 저장값을 절대 변경하지 않는다 (명세 7.2).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urlencode

import discord
from discord import app_commands

from . import charts
from .config import Config
from .db import Database
from .events import current_run_date
from .streak import MILESTONE_STEP, effective_streak, highest_milestone

log = logging.getLogger("commands")


def _today_run_date():
    """조회 기준 'today' = 러닝 하루(KST 04시 리셋). 기록·조회가 같은 기준을 쓰게 한다."""
    return current_run_date()


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


def build_brag_url(
    base: str,
    *,
    streak: int,
    tier: int,
    dist_km=None,
    dur_sec=None,
    pace_sec=None,
    runs: int = 0,
    name: str = "",
) -> str:
    """자랑 카드 정적 페이지 URL을 만든다.

    통계는 쿼리스트링이 아니라 **프래그먼트(#)** 에 담는다 → 브라우저가 서버로 전송하지 않으므로
    웹서버/프록시 접근 로그에도 남지 않는다(개인정보 최소화). None 통계는 생략한다.
    """
    params: dict[str, str] = {"streak": str(int(streak)), "tier": str(int(tier))}
    if runs:
        params["runs"] = str(int(runs))
    if dist_km is not None:
        params["dist"] = f"{float(dist_km):.2f}"
    if dur_sec is not None:
        params["time"] = str(int(dur_sec))
    if pace_sec is not None:
        params["pace"] = str(int(round(float(pace_sec))))
    if name:
        params["name"] = name
    return f"{base.rstrip('/')}/#{urlencode(params)}"


def _chunk_lines(lines: list[str], limit: int = 1900) -> list[str]:
    """줄 리스트를 limit 자 이하 메시지 여러 개로 나눈다(디스코드 2000자 한도 회피)."""
    chunks, buf, length = [], [], 0
    for ln in lines:
        add = len(ln) + 1
        if buf and length + add > limit:
            chunks.append("\n".join(buf))
            buf, length = [], 0
        buf.append(ln)
        length += add
    if buf:
        chunks.append("\n".join(buf))
    return chunks or [""]


HELP_TEXT = """## 🏃 러닝 스트릭 봇 사용설명서

지정된 채널에 **러닝 기록 사진**을 올리면, 봇이 자동으로 **연속 러닝 일수(스트릭)** 를 세어 줍니다.

### 시작하기
1. `/달리기 등록` — 선수로 등록(딱 한 번만 하면 됩니다).
2. 지정 채널에 러닝 앱 캡처(또는 러닝 사진)를 업로드.
3. 봇이 사진에 ✅ 를 달고 `러닝 기록 완료. N일째 연속입니다.` 로 답하면 집계 완료!
   (✅ 는 접수 표시이고, 잠시 뒤 메시지가 따라옵니다.)

### 스트릭 규칙
- 날짜 기준은 **사진을 올린 시각(한국시간)** 입니다. (사진 속 날짜가 아님)
- **하루의 경계는 자정이 아니라 새벽 4시(한국시간)** 입니다. 즉 **0시~새벽 4시 사이의 러닝은 "전날" 기록으로 집계**됩니다(밤늦게·새벽에 뛰는 분 배려).
- **마지막 러닝 이후 3일 이내**에 다시 뛰면 스트릭이 **유지**되고, **4일 이상** 비면 **리셋**됩니다.
- **실제 뛴 날만** 1씩 올라갑니다. 쉰 날은 세지 않습니다.
- 같은 날 여러 번 올려도 **하루 1회**만 집계됩니다.

### 명령어
- `/달리기 등록` — 선수 등록(옵트인)
- `/달리기 해제` — 등록 취소 (기록은 보존)
- `/달리기 취소` — 내 **가장 최근 기록 1건** 되돌리기 (잘못 올렸을 때)
- `/달리기 전체삭제` — 내 **모든 데이터 영구 삭제** (되돌릴 수 없음)
- `/스트릭` (또는 `/기록`) — 내 러닝 기록: 연속·최장·이번 달 + 누적 거리·시간·페이스·칼로리 + 페이스 추세 그래프
- `/캘린더 [월] [연도]` — 러닝 달력 + 주간·월간 합계 (`월`·`연도` 생략 시 이번 달; 과거 달·연도도 조회 가능)
- `/리더보드` — 등록 선수들의 스트릭 랭킹
- `/자랑` — 마일스톤(연속 10일마다: 10·20·30…) 달성 시, 배경 사진에 기록을 얹은 자랑 카드 만들기
- `/도움` — 이 설명서

### 참고
- 등록한 사람이 **지정된 채널**에 올린 **이미지**만 집계됩니다(다른 채널·미등록자·텍스트는 무시).
- 거리·시간·페이스·칼로리는 사진에서 자동 인식(OCR)한 값이라 **정확하지 않거나 일부 비어 있을 수 있습니다**(앱·화면마다 다름). 다만 **스트릭(연속 일수) 집계에는 전혀 영향이 없습니다.**
- 개인정보: 집계를 위해 **디스코드 ID와 사진에서 읽은 수치(거리·시간·페이스·칼로리)** 만 저장하며, 사진 원본·OCR 원문은 저장하지 않습니다. 내 데이터 전체 삭제는 `/달리기 전체삭제`."""


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
                "현재 스트릭은 `/스트릭`, 랭킹은 `/리더보드` 로 확인할 수 있습니다.\n"
                "-# 집계를 위해 디스코드 ID와 사진에서 읽은 거리·시간·페이스·칼로리를 저장합니다. "
                "전체 삭제는 `/달리기 전체삭제` 로 언제든 가능합니다."
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
        await interaction.response.defer(ephemeral=True)  # DELETE+재계산이 3초 넘을 수 있어 먼저 ack
        result = await db.undo_last_run(interaction.user.id)
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
    async def purge(interaction: discord.Interaction, 확인: str = ""):
        # 파괴적·비가역 → 명시적 확인 토큰을 요구(버튼 대신 입력값으로 단순·검증가능하게).
        if 확인.strip() != "삭제":
            await interaction.response.send_message(
                "⚠️ 되돌릴 수 없는 작업입니다. 정말 삭제하려면 `확인` 옵션에 `삭제` 를 입력해 다시 실행하십시오.\n"
                "(내 디스코드 ID·연속 일수·모든 러닝 기록이 영구 삭제됩니다.)",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)  # DELETE 가 3초 넘을 수 있어 먼저 ack
        runs, had_row = await db.purge_user(interaction.user.id)
        if runs == 0 and not had_row:
            await interaction.followup.send("삭제할 데이터가 없습니다.", ephemeral=True)
            return
        await interaction.followup.send(
            f"내 러닝 데이터를 전부 삭제하였습니다(기록 {runs}건). 다시 시작하려면 `/달리기 등록` 하십시오.",
            ephemeral=True,
        )

    tree.add_command(dalligi, guild=guild)

    # --- /스트릭 = /기록 (개인 통합 조회, 읽기 전용) ----------------------
    #     스트릭 + 누적 거리·시간·페이스·칼로리를 한 화면에. /기록 은 별칭(같은 출력).
    async def _send_my_stats(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # 다중 DB 조회 → 3초 ack 한도 회피
        record = await db.load(interaction.user.id)
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

        # 성장 추세: 최근 러닝들의 페이스 스파크라인(페이스는 작을수록 빠름 → 부호 뒤집어 우상향=개선).
        paces = [
            r["pace_sec_per_km"]
            for r in await db.recent_runs(interaction.user.id, 12)
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

    # --- /자랑 (마일스톤 자랑 카드 링크, 읽기 전용·pull) -------------------
    #     현재 스트릭이 10일 단위 마일스톤(10·20·30…)에 닿으면, 통계를 프래그먼트에 담은
    #     정적 카드 페이지 링크를 '본인에게만' 회신한다. 서버·DB 에 새로 저장하는 것은 없다.
    @tree.command(
        name="자랑",
        description="스트릭 마일스톤(연속 10일마다) 자랑 카드를 만듭니다.",
        guild=guild,
    )
    async def brag_cmd(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # DB 다중 조회 → 3초 ack 회피
        record = await db.load(interaction.user.id)
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

        s = await db.stats(interaction.user.id)
        runs = int(s.get("runs") or 0)
        dist_sum = s.get("dist_sum")
        dur_sum = s.get("dur_sum")
        # 평균 페이스: 거리·시간이 '둘 다' 인식된 기록만 짝지은 총시간/총거리(가중·자기일관) 우선.
        # (dist_sum/dur_sum 은 서로 다른 행 집합을 합산할 수 있어 직접 나누면 왜곡 — db.stats 참고.)
        pace_sec = None
        p_dist, p_dur = s.get("paired_dist"), s.get("paired_dur")
        if p_dist is not None and p_dur is not None and float(p_dist) > 0:
            pace_sec = float(p_dur) / float(p_dist)
        elif s.get("pace_avg") is not None:
            pace_sec = float(s["pace_avg"])

        dist_txt = f"{float(dist_sum):.2f}km" if dist_sum is not None else "—"
        dur_txt = _fmt_duration(dur_sum) if dur_sum is not None else "—"
        stat_line = (
            f"🔥 현재 연속 **{eff}일** · **{tier}일 마일스톤 달성**\n"
            f"📏 총 거리 {dist_txt} · ⏱️ 총 시간 {dur_txt} · "
            f"⚡ 평균 페이스 {_fmt_pace(pace_sec)}"
        )

        if not config.brag_base_url:  # URL 미설정 → 텍스트 폴백
            await interaction.followup.send(
                f"## 🎉 {tier}일 연속 달성!\n{stat_line}\n"
                "-# 자랑 카드 페이지 주소(`BRAG_BASE_URL`)가 설정되지 않아 링크를 만들지 못했습니다.",
                ephemeral=True,
            )
            return

        url = build_brag_url(
            config.brag_base_url,
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
    @tree.command(
        name="리더보드",
        description="등록 선수들의 스트릭 랭킹을 봅니다.",
        guild=guild,
    )
    async def leaderboard_cmd(interaction: discord.Interaction):
        await interaction.response.defer()  # 멤버 조회로 약간 지연될 수 있음
        runners = await db.list_registered()
        today = _today_run_date()
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

        # 표시 이름은 캐시 미스 시 REST fetch_user 가 필요 → 순차 대신 동시 조회로 지연 누적 방지.
        names = await asyncio.gather(
            *(_display_name(interaction, r.user_id) for r in ranked)
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
        for chunk in _chunk_lines(lines, 1900):
            await interaction.followup.send(
                chunk, allowed_mentions=discord.AllowedMentions.none()
            )

    # --- /캘린더 (월 달력 + 주/월 합계, 읽기 전용·온디맨드) ---------------
    @tree.command(
        name="캘린더",
        description="러닝 달력과 주간·월간 합계를 봅니다.",
        guild=guild,
    )
    @app_commands.describe(
        month="조회할 월 (1-12, 생략 시 이번 달)",
        year="조회할 연도 (생략 시 자동: 미래 월이면 작년)",
    )
    async def calendar_cmd(
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

        rows = await db.month_run_metrics(interaction.user.id, year, m)
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
            f"📊 {m}월 합계: {mcnt}회 · {mdist:.1f}km · {_fmt_duration(mdur)} · {mcal}kcal",
        ]
        if year == today.year and m == today.month:  # 현재 달일 때만 '이번 주' 합계
            # 달력 그리드가 일요일 시작이므로 주간 합계도 일~토로 맞춘다(주 경계 일치).
            sun = today - timedelta(days=(today.weekday() + 1) % 7)
            sat = sun + timedelta(days=6)
            w = await db.period_summary(interaction.user.id, sun, sat)
            wdist = float(w.get("dist") or 0)
            lines.append(
                f"🗓️ 이번 주(일~토): {int(w.get('cnt') or 0)}회 · {wdist:.1f}km · "
                f"{_fmt_duration(w.get('dur') or 0)} · {int(w.get('cal') or 0)}kcal"
            )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

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
