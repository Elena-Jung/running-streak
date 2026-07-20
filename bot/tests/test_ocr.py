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
    extract_speed_kmh,
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
    # 거리 OCR 이 잡히고 페이스와 일관(±15% 이내)이면 유도하지 않고 OCR 값을 쓴다
    # (유도값이면 780/390 = 2.00 이 됐을 것).
    text = "거리 2.05 km\n운동시간 13:00\n평균 페이스 6:30/km"
    assert extract_fields(text)["distance_km"] == Decimal("2.05")


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


# --- 교차 일관성 검사: 상호 모순 삼중값 처리 -------------------------------

def test_inconsistent_triple_drops_distance_and_pace():
    # 폴백 거리(단위 비인접 독립 소수)가 화면의 다른 소수를 잡아 페이스와 모순되면,
    # 무엇이 오염인지 판별 불가 → 틀린 값을 저장하지 않도록 거리·페이스를 비우고
    # 앵커가 견고한 시간만 남긴다.
    text = "7.6\n운동시간 32:06\n평균 페이스 7'47\""
    f = extract_fields(text)
    assert f["duration_sec"] == 1926
    assert f["distance_km"] is None
    assert f["pace_sec_per_km"] is None


def test_inconsistent_triple_rescued_by_speed():
    # 속도가 함께 읽힌 경우엔 독립 신호로 복원: 페이스=3600/속도, 거리=시간/페이스.
    text = "7.6\n운동시간 32:06\n평균 페이스 7'47\"\n평균 속도 7.7 km/h"
    f = extract_fields(text)
    assert f["pace_sec_per_km"] == 468          # round(3600/7.7)
    assert f["distance_km"] == Decimal("4.12")  # 1926/468 (2자리 반올림)


def test_consistent_distance_not_touched():
    # 일관 범위(±15% 이내)면 OCR 거리를 그대로 둔다(검사 미발동).
    text = "거리 4.19 km\n운동시간 31:19\n평균 페이스 7'27\""
    assert extract_fields(text)["distance_km"] == Decimal("4.19")


def test_speed_kmh_across_newline_not_distance():
    # 값과 단위가 줄로 갈린 "7.6\nkm/h" 도 거리로 잡지 않는다(개행 무시 가드).
    assert extract_distance_km("7.6\nkm/h") is None


def test_pace_prefers_average_labeled_line():
    # 최고 페이스가 먼저 나와도 '평균' 줄의 페이스를 채택(최고 페이스 오채택 방지).
    text = "최고 페이스 5'28\"\n평균 페이스 6'53\""
    assert extract_pace_sec_per_km(text) == 6 * 60 + 53


# --- 속도(km/h) 파싱: 트레드밀/헬스장 화면 -------------------------------

def test_speed_kmh_decimal_with_space():
    assert extract_speed_kmh("평균 속도 8.4 km/h") == Decimal("8.4")


def test_speed_kmh_decimal_no_space():
    assert extract_speed_kmh("8.4km/h") == Decimal("8.4")


def test_speed_kmh_integer_allowed():
    # km/h 앵커가 있어 정수도 허용(거리와 달리).
    assert extract_speed_kmh("평균 속도 8 km/h") == Decimal("8")


def test_speed_kmh_kph_form():
    assert extract_speed_kmh("8.4 kph") == Decimal("8.4")


def test_speed_kmh_out_of_range_rejected():
    assert extract_speed_kmh("80 km/h") is None   # 비현실적 고속(노이즈)
    assert extract_speed_kmh("0 km/h") is None     # 0 노이즈


def test_speed_kmh_none_when_absent():
    assert extract_speed_kmh("거리 5.00 km") is None  # km(/h 아님)는 속도 아님


def test_speed_kmh_not_grabbed_as_distance():
    # 방어: 속도 표기는 거리로 잡히지 않는다.
    assert extract_distance_km("평균 속도 8.4 km/h") is None


# --- 역산(거리/속력/시간) -------------------------------------------------

def test_pace_derived_from_speed_only():
    # 시간+속도만(거리·페이스 없음) → 페이스=3600/속도, 거리=시간/페이스.
    f = extract_fields("운동 시간 21:55\n평균 속도 8.4 km/h")
    assert f["duration_sec"] == 21 * 60 + 55      # 1315
    assert f["pace_sec_per_km"] == 429            # round(3600/8.4)
    assert f["distance_km"] == Decimal("3.07")    # 1315/429


def test_duration_derived_from_distance_and_speed():
    # 거리+속도만(시간·페이스 없음) → 페이스=3600/속도, 시간=거리×페이스.
    f = extract_fields("거리 3.10 km\n평균 속도 8.4 km/h")
    assert f["pace_sec_per_km"] == 429            # round(3600/8.4)
    assert f["duration_sec"] == 1330              # round(3.10*429)
    assert f["distance_km"] == Decimal("3.10")    # OCR 값 유지


def test_pace_derived_from_core_not_speed():
    # 거리·시간이 둘 다 있으면 페이스는 시간/거리(코어)로 — 속도(반올림값)보다 정확.
    f = extract_fields("거리 3.10 km\n운동 시간 21:55\n평균 속도 8.4 km/h")
    assert f["pace_sec_per_km"] == 424            # round(1315/3.10), NOT 429(=3600/8.4)


def test_speed_ignored_when_pace_ocrd():
    # OCR 페이스가 있으면 속도는 무시(덮어쓰지 않음).
    f = extract_fields("평균 페이스 6'00\"/km\n평균 속도 8.4 km/h")
    assert f["pace_sec_per_km"] == 6 * 60         # 360, 속도 환산(429) 아님


def test_derived_pace_out_of_bounds_dropped():
    # 속도 40km/h → pace=90s/km 는 범위(120~1800) 밖 → 채우지 않음.
    f = extract_fields("운동 시간 20:00\n평균 속도 40 km/h")
    assert f["pace_sec_per_km"] is None
    assert f["distance_km"] is None               # 페이스 없으니 거리도 유도 안 함
    assert f["duration_sec"] == 20 * 60


def test_speed_only_yields_pace_no_distance_or_duration():
    # 속도만 있으면 페이스는 환산되지만 거리·시간은 둘 다 없어 유도 불가.
    f = extract_fields("평균 속도 8.4 km/h")
    assert f["pace_sec_per_km"] == 429
    assert f["distance_km"] is None
    assert f["duration_sec"] is None


def test_fields_treadmill_screen_full():
    # 삼성헬스 트레드밀(헬스장) 화면 합성: 페이스 대신 속도 표시.
    text = (
        "3.10 km\n달리기\n"
        "운동 시간 21:55\n평균 속도 8.4 km/h\n"
        "평균 심박수 174 bpm\n운동 칼로리 185 kcal\n"
        "평균 케이던스 142 spm\n걸음 3,128\n"
        "2026년 6월 24일 (수) 오후 11:32"
    )
    f = extract_fields(text)
    assert f["distance_km"] == Decimal("3.10")
    assert f["duration_sec"] == 1315              # 21:55, 날짜줄 11:32 는 제외
    assert f["pace_sec_per_km"] == 424            # 코어(시간/거리) 유도, 속도 아님
    assert f["calories"] == 185
