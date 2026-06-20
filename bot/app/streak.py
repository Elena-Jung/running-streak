"""순수 스트릭 로직 (명세 5·7).

이 모듈은 IO(디스코드/DB)에 의존하지 않는 순수 함수만 담는다.
스트릭의 정확성이 봇의 생명이므로, 여기를 단위 테스트로 못박는다.

규칙 요약:
- 날짜 기준은 호출자가 KST date 로 환산해 넘긴다 (명세 3).
- 유예(grace): 마지막 러닝 이후 간격 ≤ 3일 유지, ≥ 4일 리셋 (명세 5.1).
- 실제 뛴 날만 카운트, 유예일은 카운트하지 않음 (명세 5.2).
- 조회는 저장값을 바꾸지 않고 표시 시점에만 유효성을 덧씌운다 (명세 7.2).
"""

from __future__ import annotations

from datetime import date

# 유예 허용 최대 간격(일). gap 이 이 값 이하이면 스트릭 유지.
GRACE_MAX_GAP = 3


def compute_on_run(
    last_run_date: date | None,
    current_streak: int,
    today: date,
) -> tuple[int, bool]:
    """러닝 사진이 올라왔을 때의 새 스트릭을 계산한다 (명세 7.1).

    Returns:
        (new_streak, counted)
        - counted=False 이면 호출자는 저장/응답을 모두 생략한다(오늘 이미 기록됨).
        - counted=True 이면 호출자는 new_streak 으로 저장하고 응답 메시지를 보낸다.
    """
    # 첫 러닝(기록 없음): 오늘부터 1일째.
    if last_run_date is None:
        return 1, True

    gap = (today - last_run_date).days

    if gap <= 0:
        # gap == 0: 오늘 이미 기록됨 → 하루 1회만, 무시.
        # gap < 0: 저장된 날짜가 미래(시계 이상 등) → 방어적으로 무시.
        return current_streak, False

    if gap <= GRACE_MAX_GAP:
        # 유예 안에서 유지 → 실제 뛴 오늘을 +1.
        return current_streak + 1, True

    # gap >= 4: 끊김 → 오늘부터 새로 시작.
    return 1, True


def recompute_from_dates(
    dates: list[date],
) -> tuple[int, int, date | None, int]:
    """뛴 날짜 목록으로부터 스트릭 상태를 처음부터 재계산한다 (취소/되돌리기용).

    run_logs(하루 1행) 를 원장으로 삼아 정확히 되돌리기 위함. 정렬·중복제거 후
    동일한 유예 규칙(간격 ≤ 3 유지, ≥ 4 리셋)을 적용한다.

    Returns:
        (current_streak, max_streak, last_run_date, total_runs)
        — current_streak 은 '마지막 뛴 날' 기준값(저장 규약과 동일). 기록이 없으면 (0,0,None,0).
    """
    uniq = sorted(set(dates))
    if not uniq:
        return 0, 0, None, 0

    cur = 0
    mx = 0
    prev: date | None = None
    for d in uniq:
        if prev is None:
            cur = 1
        else:
            gap = (d - prev).days
            cur = cur + 1 if gap <= GRACE_MAX_GAP else 1
        mx = max(mx, cur)
        prev = d
    return cur, mx, uniq[-1], len(uniq)


def effective_streak(
    last_run_date: date | None,
    current_streak: int,
    today: date,
) -> int:
    """조회 시점의 '효력 있는' 스트릭 (명세 7.2).

    저장된 current_streak 은 마지막 러닝 시점의 값이라, 그 뒤 오래 지나면
    거짓("유령 스트릭")이 된다. 보여줄 때만 today-last_run_date 로 유효성을
    덧씌운다. 이 함수는 저장값을 변경하지 않는다(읽기 전용, 부수효과 없음).
    """
    if last_run_date is None:
        return 0

    gap = (today - last_run_date).days
    if gap > GRACE_MAX_GAP:
        return 0  # 이미 끊김
    return current_streak
