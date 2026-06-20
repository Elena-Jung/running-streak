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
    for rgx in _CAL_RES:
        m = rgx.search(text)
        if m:
            try:
                v = int(m.group(1))
            except ValueError:
                continue
            if 1 <= v <= 20000:  # 한 번 러닝의 상식적 칼로리 상한(OCR 노이즈 컷)
                return v
    return None


def extract_fields(text: str) -> dict:
    """4개 부가정보를 한 번에. 못 찾은 항목은 None."""
    return {
        "distance_km": extract_distance_km(text),
        "duration_sec": extract_duration_sec(text),
        "pace_sec_per_km": extract_pace_sec_per_km(text),
        "calories": extract_calories(text),
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
    """변형 A: 흑백 → 업스케일 → 대비 보정 (일반 단색 배경에 강함)."""
    try:
        from PIL import ImageOps

        gray = _maybe_upscale(ImageOps.grayscale(img))
        return ImageOps.autocontrast(gray)
    except Exception:  # noqa: BLE001
        return img


def _color_variant(img):
    """변형 C: 원본 컬러(필요 시 업스케일).

    삼성헬스의 컬러 글씨(예: 분홍색 칼로리 값)는 흑백/이진화에서 깨지기 쉬운데,
    원본 컬러로 OCR 하면 단위까지 깨끗이 읽히는 경우가 많다. 실패하면 None.
    """
    try:
        return _maybe_upscale(img.convert("RGB"))
    except Exception:  # noqa: BLE001
        return None


def _bright_text_variant(img):
    """변형 B: 밝은 픽셀만 검은 글씨로 남기는 이진화.

    삼성헬스처럼 '사진 위 큰 흰 글씨'(거리)를 읽기 위함. 실패하면 None.
    """
    try:
        from PIL import ImageOps

        gray = _maybe_upscale(ImageOps.grayscale(img))
        # 밝은 픽셀(>임계) → 검정(0), 나머지 → 흰색(255). Tesseract 는 밝은 배경 위 검은 글씨 선호.
        return gray.point(lambda v: 0 if v > 190 else 255)
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

        with Image.open(io.BytesIO(image_bytes)) as raw_img:
            variants = [_preprocess(raw_img)]  # A: 일반(흑백)
            bright = _bright_text_variant(raw_img)  # B: 사진 위 흰 글씨용
            if bright is not None:
                variants.append(bright)
            color = _color_variant(raw_img)  # C: 컬러 글씨(분홍 칼로리 등)용
            if color is not None:
                variants.append(color)

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
        return extract_fields(combined), (combined or None)
    except Exception as e:  # noqa: BLE001
        log.warning("OCR 추출 실패(무시): %s", e)
        return empty, None
