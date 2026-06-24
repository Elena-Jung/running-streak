"""로컬 Tesseract 기반 부가정보 추출 — best-effort (명세 2).

거리·시간·페이스·칼로리를 각각 숫자로 뽑아 run_logs 에 분리 저장한다(집계용).
핵심 원칙: 여기서 무슨 일이 일어나도 스트릭 집계는 영향받지 않는다.
모든 예외를 흡수하고, 실패하면 빈 값으로 돌려준다.
클라우드 OCR/비전 LLM 은 쓰지 않는다(로컬 Tesseract 만).

전처리는 '가벼운' 선까지만 (명세 9: 과도한 파이프라인 금지):
흑백 변환 + 업스케일 + 대비 보정, PSM 6/11 시도, 정규식 파싱.
앱마다 레이아웃이 달라 정확도는 보장하지 않는다(부가정보 한정).
"""

from __future__ import annotations

import io
import logging
import re
from decimal import Decimal, InvalidOperation

log = logging.getLogger("ocr")

_TARGET_MIN_SIDE = 1000
_MAX_UPSCALE = 3.0

# ── 거리 ────────────────────────────────────────────────────────────────
# "12.34 km", "5.0 킬로미터", "5.02㎞" 등 단위 인접.
# 단위 인접. 단, "km/h"(평균 속도)는 거리가 아니므로 뒤에 "/h" 가 오면 제외.
_DISTANCE_RE = re.compile(
    r"(\d{1,3}(?:\s*[.,]\s*\d{1,2})?)\s*"
    r"(?:k\s*m|㎞|킬로미터|킬로\s*미터|킬로|키로)(?!\s*/\s*h)",
    re.IGNORECASE,
)
# 폴백: 단위 비인접 독립 소수(예: 값/라벨 2단 레이아웃의 "5.00").
_FALLBACK_RE = re.compile(r"(?<![\d:.'\"%])(\d{1,2}[.,]\d{1,2})(?![\d:'\"%])")
_NON_DISTANCE_TAILS = ("kcal", "spm", "bpm", "%")

# ── 시간(운동 지속시간) ─────────────────────────────────────────────────
# HH:MM:SS 또는 MM:SS. 상태바 시계와 구분하려고 상태바 힌트가 있는 줄은 건너뛴다.
_TIME_RE = re.compile(r"(?<!\d)(?:(\d{1,2}):)?(\d{1,2}):(\d{2})(?!\d)")
_STATUS_HINTS = ("kt", "skt", "lg u", "u+", "%", "am", "pm", "배터리", "오전", "오후")
# 날짜/타임스탬프 줄(예: "2026년 6월 10일 (수) 13:35", "2026.06.21 01:36") — 그 줄의 시각은
# 운동시간이 아니라 업로드 시각이므로 제외.
_DATE_LINE_RE = re.compile(
    r"\d{4}\s*[.\-/년]|오전|오후|\([월화수목금토일]\)|[월화수목금토일]요일", re.IGNORECASE
)

# ── 페이스 (분'초"/km) ──────────────────────────────────────────────────
_PACE_RE = re.compile(r"(\d{1,2})\s*['’]\s*(\d{2})\s*(?:[\"”]|'')?")
# 콜론형 페이스 "5:30/km". '/km' 가 붙어야 시간(MM:SS)과 구분된다.
_PACE_COLON_RE = re.compile(r"(\d{1,2}):(\d{2})\s*/\s*km", re.IGNORECASE)
# 시간 후보 바로 뒤에 이게 오면 페이스이므로 운동시간에서 제외.
_PACE_TAIL_RE = re.compile(r"\s*/\s*km", re.IGNORECASE)

# ── 칼로리 ──────────────────────────────────────────────────────────────
_CAL_RES = (
    re.compile(r"(\d{1,5})\s*kcal", re.IGNORECASE),
    re.compile(r"(\d{1,5})\s*칼로리"),
    re.compile(r"칼로리\s*(\d{1,5})"),
)

# ── 속도 (km/h) ───────────────────────────────────────────────────────────
# 삼성헬스 트레드밀(헬스장) 화면은 페이스 대신 "평균 속도 8.4 km/h" 를 보여준다.
# "km/h"(또는 kph/km/시) 앵커가 거리(소수만 인정)와 구분해 주므로 정수도 허용한다.
_SPEED_RE = re.compile(
    r"(\d{1,2}(?:\s*[.,]\s*\d{1,2})?)\s*(?:k\s*m\s*/\s*h|kph|km\s*/\s*시)",
    re.IGNORECASE,
)


def _reasonable_km(val: Decimal) -> bool:
    return Decimal("0") < val <= Decimal("100")  # 풀코스(~42km)를 넉넉히 포함


def extract_distance_km(text: str) -> Decimal | None:
    best: Decimal | None = None
    for m in _DISTANCE_RE.finditer(text):
        grp = m.group(1)
        # 거리는 보통 소수(X.XX km). 정수 단독은 OCR 노이즈일 확률이 높아 제외
        # (사진 위 흰 글씨 거리가 정수로 깨져 들어오는 오인식 방지).
        if "." not in grp and "," not in grp:
            continue
        raw = grp.replace(" ", "").replace(",", ".")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            continue
        if _reasonable_km(val) and (best is None or val > best):
            best = val
    if best is not None:
        return best
    # 폴백 (단위 인접 실패 시에만)
    for m in _FALLBACK_RE.finditer(text):
        e = m.span(1)[1]
        tail = text[e:e + 6].lower()
        tnorm = tail.replace(" ", "")
        # "km/h"(속도)·페이스(/km)·칼로리/케이던스/퍼센트가 뒤따르면 거리 아님.
        if tnorm[:1] == "/" or tnorm.startswith("km/h") or tnorm.startswith("kph"):
            continue
        if any(u in tail for u in _NON_DISTANCE_TAILS):
            continue
        raw = m.group(1).replace(",", ".")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            continue
        if _reasonable_km(val) and (best is None or val > best):
            best = val
    return best


def extract_duration_sec(text: str) -> int | None:
    """운동 지속시간(초). 상태바 시계가 있는 줄은 제외하고, 가장 긴 후보를 택한다."""
    best: int | None = None
    for line in text.splitlines():
        low = line.lower()
        if any(h in low for h in _STATUS_HINTS):
            continue
        if _DATE_LINE_RE.search(line):  # 날짜/업로드 시각 줄의 시각은 운동시간 아님
            continue
        for m in _TIME_RE.finditer(line):
            # 뒤에 "/km" 가 붙으면 페이스(예: 5:30/km)이므로 운동시간에서 제외.
            if _PACE_TAIL_RE.match(line, m.end()):
                continue
            h = int(m.group(1)) if m.group(1) else 0
            mm = int(m.group(2))
            ss = int(m.group(3))
            if ss >= 60:
                continue
            sec = h * 3600 + mm * 60 + ss
            if 30 <= sec <= 86400 and (best is None or sec > best):
                best = sec
    return best


def extract_pace_sec_per_km(text: str) -> int | None:
    """페이스(초/km). 2:00~30:00/km 범위만 인정.

    1) 분'초" 형식(6'00") 우선. 2) 없으면 콜론형 "5:30/km"('/km' 필수).
    """
    for rgx in (_PACE_RE, _PACE_COLON_RE):
        for m in rgx.finditer(text):
            mm = int(m.group(1))
            ss = int(m.group(2))
            if ss >= 60:
                continue
            sec = mm * 60 + ss
            if 120 <= sec <= 1800:
                return sec
    return None


def extract_calories(text: str) -> int | None:
    # 모든 매치를 훑어 첫 '유효 범위' 값을 채택 — '0 kcal' 같은 노이즈가 앞서도 건너뛴다.
    for rgx in _CAL_RES:
        for m in rgx.finditer(text):
            try:
                v = int(m.group(1))
            except ValueError:
                continue
            if 1 <= v <= 20000:  # 한 번 러닝의 상식적 칼로리 상한(OCR 노이즈 컷)
                return v
    return None


def extract_speed_kmh(text: str) -> Decimal | None:
    """평균 속도(km/h). 'km/h' 앵커가 있어 거리와 달리 정수도 허용. 1~40 범위만.

    저장하지 않고 extract_fields 의 역산 입력으로만 쓰인다(페이스로 환산).
    """
    for m in _SPEED_RE.finditer(text):
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            val = Decimal(raw)
        except InvalidOperation:
            continue
        if Decimal("1") <= val <= Decimal("40"):  # 걷기~엘리트 러닝 상식 범위
            return val
    return None


# 역산 결과 위생 검사용 범위(OCR/유도 노이즈가 DB 를 오염시키지 않게).
def _ok_pace(p: int | None) -> bool:
    return p is not None and 120 <= p <= 1800


def _ok_duration(d: int | None) -> bool:
    return d is not None and 30 <= d <= 86400


def _ok_distance(km: Decimal | None) -> bool:
    return km is not None and Decimal("0") < km <= Decimal("100")


def extract_fields(text: str) -> dict:
    """4개 부가정보를 한 번에. 못 찾은 항목은 거리/속력/시간 관계로 역산한다.

    역산 원칙(명세 2: OCR 은 부가정보):
    - **OCR 로 읽은 값은 절대 덮어쓰지 않는다.** 비어 있는(None) 필드만 채운다.
    - 페이스가 없으면 ① 거리·시간(코어)으로 `pace=시간/거리`(가장 정확), 없으면
      ② 속도로 `pace=3600/(km/h)`(폴백). 트레드밀 화면처럼 속도만 있는 경우 대응.
    - 거리 없으면 `시간/페이스`, 시간 없으면 `거리×페이스` 로 채운다.
    - 모든 유도값은 범위검사를 통과해야 저장(노이즈 차단). 속도는 DB 에 저장하지 않고
      페이스로 환산만 한다(스키마·표시 무변경).
    """
    distance = extract_distance_km(text)
    duration = extract_duration_sec(text)
    pace = extract_pace_sec_per_km(text)
    calories = extract_calories(text)
    speed = extract_speed_kmh(text)  # 역산 입력 전용(반환 dict 에는 넣지 않음)

    # 1) 페이스 유도(피벗). 코어(거리·시간) 우선, 없으면 속도 폴백.
    if pace is None:
        if duration and _ok_distance(distance):
            cand = round(duration / float(distance))
            if _ok_pace(cand):
                pace = cand
        if pace is None and speed is not None:
            cand = round(3600 / float(speed))
            if _ok_pace(cand):
                pace = cand

    # 2) 거리 유도: 시간/페이스.
    if distance is None and duration and pace:
        cand = (Decimal(duration) / Decimal(pace)).quantize(Decimal("0.01"))
        if _ok_distance(cand):
            distance = cand

    # 3) 시간 유도: 거리×페이스.
    if duration is None and _ok_distance(distance) and pace:
        cand = round(float(distance) * pace)
        if _ok_duration(cand):
            duration = cand

    return {
        "distance_km": distance,
        "duration_sec": duration,
        "pace_sec_per_km": pace,
        "calories": calories,
    }


def _maybe_upscale(gray):
    from PIL import Image

    w, h = gray.size
    short = min(w, h)
    if 0 < short < _TARGET_MIN_SIDE:
        factor = min(_MAX_UPSCALE, _TARGET_MIN_SIDE / short)
        return gray.resize((int(w * factor), int(h * factor)), Image.LANCZOS)
    return gray


def _preprocess(img):
    """변형 A: 흑백 → 업스케일 → 대비 보정 (일반 단색 배경에 강함). 실패하면 None."""
    try:
        from PIL import ImageOps

        gray = _maybe_upscale(ImageOps.grayscale(img))
        return ImageOps.autocontrast(gray)
    except Exception:  # noqa: BLE001
        return None


def _color_variant(img):
    """변형 C: 원본 컬러(필요 시 업스케일).

    삼성헬스의 컬러 글씨(예: 분홍색 칼로리 값)는 흑백/이진화에서 깨지기 쉬운데,
    원본 컬러로 OCR 하면 단위까지 깨끗이 읽히는 경우가 많다. 실패하면 None.
    """
    try:
        return _maybe_upscale(img.convert("RGB"))
    except Exception:  # noqa: BLE001
        return None


def _adaptive_variants(img) -> list:
    """변형 B: 적응형(국소 평균 대비) 이진화 2장 — 밝은 글씨용 / 어두운 글씨용.

    고정 임계값이나 위치 크롭에 의존하지 않는다. 각 픽셀을 '주변 평균'과 비교하므로
    사진 위 흰 히어로 글씨, 카드 위 어두운/컬러 글씨를 **위치·해상도·앱 무관**하게
    분리해 읽는다(블러 반경을 짧은 변에 비례 → 해상도 무관). 실패하면 빈 리스트.
    """
    try:
        from PIL import Image, ImageChops, ImageFilter, ImageOps

        gray = ImageOps.grayscale(img)
        short = min(gray.size)
        if 0 < short < _TARGET_MIN_SIDE:
            factor = min(_MAX_UPSCALE, _TARGET_MIN_SIDE / short)
            gray = gray.resize(
                (int(gray.size[0] * factor), int(gray.size[1] * factor)), Image.LANCZOS
            )
        radius = max(5, int(min(gray.size) / 30))  # 글자보다 충분히 큰 국소 창
        local_mean = gray.filter(ImageFilter.BoxBlur(radius))
        contrast = 18  # 국소 평균과의 최소 밝기차
        bright = ImageChops.subtract(gray, local_mean).point(
            lambda v: 0 if v > contrast else 255  # 주변보다 밝은(흰) 글씨 → 검정
        )
        dark = ImageChops.subtract(local_mean, gray).point(
            lambda v: 0 if v > contrast else 255  # 주변보다 어두운 글씨 → 검정
        )
        return [bright, dark]
    except Exception:  # noqa: BLE001
        return []


def _calorie_from_layout(img) -> int | None:
    """좌표 기반 칼로리(2단 그리드 대응).

    image_to_data 로 '칼로리'/'kcal' 라벨 위치를 찾고, 같은 열에서 가장 가까운 숫자를
    칼로리로 택한다(런데이 결과화면처럼 값과 라벨이 다른 줄에 있을 때). 실패 시 None.
    """
    try:
        import pytesseract

        d = pytesseract.image_to_data(
            img, lang="kor+eng", config="--psm 6", output_type=pytesseract.Output.DICT
        )
        labels, nums = [], []
        for i, raw in enumerate(d["text"]):
            t = raw.strip()
            if not t:
                continue
            if t == "칼로리" or "kcal" in t.lower():
                labels.append(i)
            elif t.isdigit() and 1 <= int(t) <= 20000:
                nums.append((i, int(t)))
        best, best_score = None, None
        for li in labels:
            lcx = d["left"][li] + d["width"][li] / 2
            lcy = d["top"][li] + d["height"][li] / 2
            lw = max(d["width"][li], 1)
            lh = max(d["height"][li], 1)
            for ni, val in nums:
                ncx = d["left"][ni] + d["width"][ni] / 2
                ncy = d["top"][ni] + d["height"][ni] / 2
                dx = abs(ncx - lcx)
                dy = abs(ncy - lcy)
                if dx > lw * 1.3 or dy > lh * 4:  # 같은 열 & 한 행 이내
                    continue
                score = dx + dy * 0.2
                if best_score is None or score < best_score:
                    best_score, best = score, val
        return best
    except Exception:  # noqa: BLE001
        return None


def try_extract(image_bytes: bytes) -> tuple[dict, str | None]:
    """이미지 바이트에서 (부가정보 dict, OCR 원문) 추출. 실패 시 (빈dict, None).

    절대 예외를 올리지 않는다(부가정보는 본류를 막지 않음).
    """
    empty = {
        "distance_km": None,
        "duration_sec": None,
        "pace_sec_per_km": None,
        "calories": None,
    }
    try:
        import pytesseract
        from PIL import Image

        # 디컴프레션 폭탄 방어: 과도한 픽셀 수면 PIL 이 예외 → 아래 except 가 빈 결과로 처리.
        Image.MAX_IMAGE_PIXELS = 64_000_000

        color = None
        with Image.open(io.BytesIO(image_bytes)) as raw_img:
            raw_img.load()  # with 종료 전에 픽셀 로드(이후 변형들이 안전)
            variants = []
            a = _preprocess(raw_img)  # A: 흑백+대비
            if a is not None:
                variants.append(a)
            color = _color_variant(raw_img)  # C: 원본 컬러(컬러 글씨용)
            if color is not None:
                variants.append(color)
            variants.extend(_adaptive_variants(raw_img))  # B: 적응형 이진화(밝은/어두운)

        texts: list[str] = []
        for img in variants:
            for psm in (6, 11):
                try:
                    texts.append(
                        pytesseract.image_to_string(img, lang="kor+eng", config=f"--psm {psm}")
                    )
                except Exception:  # noqa: BLE001
                    continue

        combined = "\n".join(texts).strip()
        fields = extract_fields(combined)
        # 칼로리가 텍스트로 안 잡혔으면(2단 그리드 등) 좌표 기반으로 한 번 더.
        if fields["calories"] is None and color is not None:
            cal = _calorie_from_layout(color)
            if cal is not None:
                fields["calories"] = cal
        return fields, (combined or None)
    except Exception as e:  # noqa: BLE001
        log.warning("OCR 추출 실패(무시): %s", e)
        return empty, None
