"""스트릭 경계값 테스트 (명세 11번 테스트 케이스).

gap=3 유지 / gap=4 리셋 / 유령 스트릭 / 첫 러닝 / 같은 날 무시 / max 갱신.
"""

from datetime import date

from app.streak import (
    GRACE_MAX_GAP,
    compute_on_run,
    effective_streak,
    recompute_from_dates,
)

MON = date(2026, 6, 15)  # 기준일(월요일)


# --- compute_on_run ---------------------------------------------------------

def test_first_run_starts_at_one():
    assert compute_on_run(None, 0, MON) == (1, True)


def test_same_day_is_ignored():
    # 오늘 이미 기록됨 → 카운트 안 함, 스트릭 그대로.
    assert compute_on_run(MON, 5, MON) == (5, False)


def test_gap_one_maintains():
    assert compute_on_run(MON, 5, date(2026, 6, 16)) == (6, True)


def test_gap_two_maintains():
    assert compute_on_run(MON, 5, date(2026, 6, 17)) == (6, True)


def test_gap_three_maintains_boundary():
    # 명세: 3일까지 봐줌.
    assert compute_on_run(MON, 5, date(2026, 6, 18)) == (6, True)


def test_gap_four_resets_boundary():
    # 명세: 4일 이상이면 리셋, 오늘부터 새로.
    assert compute_on_run(MON, 5, date(2026, 6, 19)) == (1, True)


def test_large_gap_resets():
    assert compute_on_run(MON, 99, date(2026, 7, 30)) == (1, True)


def test_future_last_run_is_ignored_defensively():
    # 저장 날짜가 미래(시계 이상) → 무시.
    assert compute_on_run(date(2026, 6, 20), 3, MON) == (3, False)


def test_grace_constant_is_three():
    assert GRACE_MAX_GAP == 3


# --- effective_streak (읽기 전용 조회) --------------------------------------

def test_effective_none_is_zero():
    assert effective_streak(None, 0, MON) == 0


def test_effective_within_grace_keeps_value():
    assert effective_streak(MON, 7, date(2026, 6, 18)) == 7  # gap 3


def test_effective_ghost_streak_is_zero():
    # gap 4 → 이미 끊김. 저장값이 7이어도 표시는 0.
    assert effective_streak(MON, 7, date(2026, 6, 19)) == 0


def test_effective_same_day():
    assert effective_streak(MON, 7, MON) == 7


# --- max_streak 갱신 시나리오 (호출자 로직 검증 보조) -----------------------

def test_recompute_empty():
    assert recompute_from_dates([]) == (0, 0, None, 0)


def test_recompute_single():
    assert recompute_from_dates([MON]) == (1, 1, MON, 1)


def test_recompute_three_consecutive():
    ds = [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17)]
    assert recompute_from_dates(ds) == (3, 3, date(2026, 6, 17), 3)


def test_recompute_gap_three_maintains():
    ds = [date(2026, 6, 15), date(2026, 6, 18)]  # gap 3
    assert recompute_from_dates(ds) == (2, 2, date(2026, 6, 18), 2)


def test_recompute_gap_four_resets_but_max_kept():
    ds = [date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 20)]  # 1,2 then gap4 →1
    assert recompute_from_dates(ds) == (1, 2, date(2026, 6, 20), 3)


def test_recompute_unsorted_and_dupes():
    ds = [date(2026, 6, 17), date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 16)]
    assert recompute_from_dates(ds) == (3, 3, date(2026, 6, 17), 3)


def test_streak_progression_for_max_tracking():
    """월→화→수 연속, 목 쉬고 금 다시: 명세 5.2 예시(2일째, 4일째 아님)."""
    streak, counted = compute_on_run(None, 0, date(2026, 6, 15))  # 월: 1
    assert (streak, counted) == (1, True)
    streak, counted = compute_on_run(date(2026, 6, 15), streak, date(2026, 6, 16))  # 화: 2
    assert (streak, counted) == (2, True)
    # 수·목 쉬고 금요일(gap 3, 화 기준) 다시 → 유지되어 3
    streak, counted = compute_on_run(date(2026, 6, 16), streak, date(2026, 6, 19))  # 금: gap3 → 3
    assert (streak, counted) == (3, True)
