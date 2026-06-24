"""OCR 파싱 순수 함수 테스트 (Tesseract 불필요 — 정규식/파서만).

주의: 모든 테스트 문자열은 **실제 사용자 데이터가 아닌 합성(가공) 값**이다.
타인의 러닝 기록을 픽스처로 쓰지 않는다.
"""

from decimal import Decimal

from app.ocr import (
    extract_calories,
    extract_distance_km,
    extract_duration_sec,
    extract_fields,
    extract_pace_sec_per_km,
)


# --- 거리: 단위 인접 / 폴백 / 속도(km/h) 오인식 방지 ----------------------

def test_distance_unit_adjacent():
    assert extract_distance_km("거리 5.00 km") == Decimal("5.00")
    assert extract_distance_km("7.30km") == Decimal("7.30")
    assert extract_distance_km("7.30㎞") == Decimal("7.30")


def test_distance_fallback_two_column():
    # 값/라벨 2단 레이아웃: 단위 비인접 독립 소수.
    assert extract_distance_km("25:00   5.00   6'00\"") == Decimal("5.00")


def test_speed_kmh_is_not_distance_main():
    # "평균 속도 9.0km/h" 는 거리가 아님 (km/h).
    assert extract_distance_km("평균 속도 9.0km/h") is None


def test_speed_kmh_is_not_distance_fallback():
    # 거리 단위가 없고 속도만 있는 화면 → 폴백도 9.0 을 잡으면 안 됨.
    assert extract_distance_km("걸음 3,000 평균 속도 9.0km/h") is None


def test_distance_integer_alone_rejected():
    # 정수 단독은 OCR 노이즈로 제외(거리는 소수).
    assert extract_distance_km("9 km") is None


# --- 시간 / 페이스 / 칼로리 ----------------------------------------------

def test_duration_excludes_status_clock():
    text = "KT 07:00  80%\n운동시간\n25:00  6'00\"/km"
    assert extract_duration_sec(text) == 25 * 60  # 상태바 07:00 제외, 25:00 채택


def test_pace_quote_form():
    assert extract_pace_sec_per_km("평균 페이스 6'00\"/km") == 6 * 60


def test_pace_colon_with_km():
    # 콜론형 페이스 "5:30/km" 도 인식.
    assert extract_pace_sec_per_km("평균 페이스 5:30/km") == 5 * 60 + 30


def test_pace_quote_form_preferred_over_colon():
    assert extract_pace_sec_per_km("6'00\" ... 5:30/km") == 6 * 60


def test_duration_ignores_pace_colon():
    # "5:30/km"(페이스)가 운동시간으로 잘못 잡히면 안 됨.
    text = "운동시간 25:00\n평균 페이스 5:30/km"
    assert extract_duration_sec(text) == 25 * 60


def test_duration_none_when_only_pace_colon():
    assert extract_duration_sec("평균 페이스 5:30/km") is None


def test_duration_excludes_date_line():
    # 날짜줄의 시각(업로드 시각)을 운동시간으로 잘못 잡으면 안 됨.
    text = "2026년 6월 10일 (수) 13:35\n운동 시간\n35:00"
    assert extract_duration_sec(text) == 35 * 60


def test_duration_excludes_dotted_date_line():
    text = "2026.06.21 01:36\n운동 시간 22:19"
    assert extract_duration_sec(text) == 22 * 60 + 19


def test_calories_label_both_directions():
    assert extract_calories("200 kcal") == 200
    assert extract_calories("칼로리\n350") == 350


def test_calories_absurd_value_rejected():
    # OCR 노이즈로 비상식값(>20000)이 들어오면 제외.
    assert extract_calories("50000 kcal") is None


def test_calories_skips_zero_noise_before_real():
    # '0 kcal' 노이즈가 앞서도 다음 유효값을 잡아야 함(첫 매치만 보지 않음).
    assert extract_calories("0 kcal ... 320 kcal") == 320


# --- 통합: 속도만 있고 거리 없는 화면(합성) -------------------------------

def test_distance_derived_from_time_and_pace():
    # 거리 항목 없는 화면: 시간·페이스로 거리 유도 (780s / 390s/km = 2.00km).
    text = "운동시간 13:00\n평균 페이스 6:30/km"
    f = extract_fields(text)
    assert f["distance_km"] == Decimal("2.00")
    assert f["duration_sec"] == 780
    assert f["pace_sec_per_km"] == 390


def test_distance_ocr_wins_over_derivation():
    # 거리 OCR 이 잡히면 유도하지 않고 OCR 값을 쓴다.
    text = "거리 5.00 km\n운동시간 13:00\n평균 페이스 6:30/km"
    assert extract_fields(text)["distance_km"] == Decimal("5.00")


def test_fields_speed_only_screen_has_no_distance():
    text = (
        "운동 상세정보\n운동시간 평균 페이스\n25:00  6'00\"/km\n"
        "운동 칼로리 평균 케이던스\n200 kcal  160spm\n걸음 평균 속도\n3,000  9.0km/h"
    )
    f = extract_fields(text)
    assert f["distance_km"] != Decimal("9.0")      # 9.0km/h(속도)를 거리로 잡지 않음
    assert f["distance_km"] == Decimal("4.17")     # 거리 항목 없음 → 시간/페이스로 유도(1500/360)
    assert f["duration_sec"] == 25 * 60
    assert f["pace_sec_per_km"] == 6 * 60
    assert f["calories"] == 200
