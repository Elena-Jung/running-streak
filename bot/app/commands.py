"""러닝 스트릭 표시 헬퍼 + 사용설명서 (명세 8 보조).

명령 배선은 cogs/running.py 로 옮겼다. 이 모듈은 Discord 게이트웨이 없이 검증 가능한
포맷/URL 헬퍼와 텍스트만 남긴다(tests/test_commands.py 대상). display_name 만 예외적으로
REST 폴백 때문에 interaction 을 받지만, 상태를 갖지 않는다.
"""

from __future__ import annotations

from urllib.parse import urlencode

import discord


def fmt_duration(total_sec) -> str:
    sec = int(total_sec or 0)
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분"
    return f"{m}분"


def fmt_pace(pace_sec) -> str:
    if not pace_sec:
        return "—"
    p = int(round(float(pace_sec)))
    return f"{p // 60}'{p % 60:02d}\"/km"


def weighted_pace_sec(stats: dict) -> float | None:
    """누적 평균 페이스(초/km) — 거리·시간이 '둘 다' 인식된 기록의 총시간/총거리(가중·자기일관).

    각 sum 은 NULL 을 독립적으로 건너뛰므로 dur_sum/dist_sum 을 그냥 나누면 서로 다른 행
    집합이 섞여 왜곡된다(db.stats 의 paired_* 참고). 짝지은 기록이 없으면 pace_avg(단순
    평균) 폴백. **/스트릭·/자랑 이 같은 값을 보이도록 두 표면 모두 이 헬퍼만 쓴다**
    (2026-07-20 민원: 두 명령이 다른 공식을 써서 사용자에게 다른 평균이 보였음).
    """
    p_dist, p_dur = stats.get("paired_dist"), stats.get("paired_dur")
    if p_dist is not None and p_dur is not None and float(p_dist) > 0:
        return float(p_dur) / float(p_dist)
    if stats.get("pace_avg") is not None:
        return float(stats["pace_avg"])
    return None


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


def chunk_lines(lines: list[str], limit: int = 1900) -> list[str]:
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


async def display_name(interaction: discord.Interaction, user_id: int) -> str:
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
