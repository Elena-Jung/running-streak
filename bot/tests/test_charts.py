"""텍스트 시각화(스파크라인·달력) 순수 함수 테스트."""

from app.charts import month_calendar, sparkline


def test_sparkline_empty_or_single():
    assert sparkline([]) == ""
    assert sparkline([5]) == ""
    assert sparkline([None, None]) == ""


def test_sparkline_increasing():
    s = sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert len(s) == 8
    assert s[0] == "▁"   # 최솟값
    assert s[-1] == "█"  # 최댓값


def test_sparkline_none_becomes_space():
    s = sparkline([1, None, 8])
    assert len(s) == 3
    assert s[1] == " "


def test_sparkline_all_equal_midblock():
    s = sparkline([4, 4, 4])
    assert set(s) == {"▅"}  # 모두 중간 블록


def test_month_calendar_header_and_marks():
    cal = month_calendar(2026, 6, {9, 10})
    lines = cal.splitlines()
    assert lines[0] == "Su Mo Tu We Th Fr Sa"
    # 9일, 10일이 * 로 표시되어야 함
    assert " 9*" in cal
    assert "10*" in cal
    # 표시 안 한 날은 * 가 없어야 함 (예: 11일)
    assert "11*" not in cal


def test_month_calendar_no_runs():
    cal = month_calendar(2026, 6, set())
    assert "*" not in cal
    assert cal.splitlines()[0] == "Su Mo Tu We Th Fr Sa"
