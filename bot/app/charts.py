"""텍스트 기반 시각화 (순수 함수, 테스트 가능).

명세 9번 '그래프 이미지 생성 금지'를 지키기 위해 matplotlib 등 이미지 렌더링을
쓰지 않고, 메시지 안에 들어가는 유니코드 막대/달력만 만든다.
"""

from __future__ import annotations

import calendar as _calendar

_BLOCKS = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float | None]) -> str:
    """숫자 리스트를 블록 스파크라인으로. None 은 공백. 값이 2개 미만이면 빈 문자열.

    값이 클수록 높은 막대. (페이스처럼 '작을수록 좋은' 값은 호출자가 부호를 뒤집어
    넘기면 개선이 우상향으로 보인다.)
    """
    present = [v for v in values if v is not None]
    if len(present) < 2:
        return ""
    lo, hi = min(present), max(present)
    out: list[str] = []
    for v in values:
        if v is None:
            out.append(" ")
        elif hi == lo:
            out.append(_BLOCKS[len(_BLOCKS) // 2])
        else:
            idx = round((v - lo) / (hi - lo) * (len(_BLOCKS) - 1))
            out.append(_BLOCKS[idx])
    return "".join(out)


def month_calendar(year: int, month: int, run_days: set[int]) -> str:
    """ASCII 월 달력 텍스트(코드블록용). run_days(해당 월의 '일' 정수 집합)는 `*`로 표시.

    일요일 시작. 각 칸 3글자 폭으로 헤더와 정렬된다.
    """
    weeks = _calendar.Calendar(firstweekday=6).monthdayscalendar(year, month)  # 6=일요일
    lines = ["Su Mo Tu We Th Fr Sa"]
    for week in weeks:
        cells = []
        for day in week:
            if day == 0:
                cells.append("   ")
            else:
                mark = "*" if day in run_days else " "
                cells.append(f"{day:2d}{mark}")
        lines.append("".join(cells).rstrip())
    return "\n".join(lines)
