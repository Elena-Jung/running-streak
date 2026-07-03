"""자랑 카드 순수 함수 테스트 — highest_milestone / build_brag_url.

Discord 런타임 없이 검증 가능한 로직만 대상으로 한다(커맨드 핸들러는 통합 동작).
핵심 불변식: 통계는 반드시 **프래그먼트(#)** 로만 실려 서버로 전송되지 않아야 한다.
"""

from urllib.parse import parse_qs, urlparse

from app.commands import build_brag_url
from app.streak import MILESTONE_STEP, highest_milestone


# --- highest_milestone (연속 10일마다: 10·20·30…) ----------------------------

def test_milestone_step_constant():
    assert MILESTONE_STEP == 10


def test_highest_milestone_boundaries():
    assert highest_milestone(0) is None
    assert highest_milestone(9) is None
    assert highest_milestone(10) == 10
    assert highest_milestone(19) == 10
    assert highest_milestone(20) == 20
    assert highest_milestone(25) == 20
    assert highest_milestone(99) == 90
    assert highest_milestone(100) == 100
    assert highest_milestone(137) == 130
    assert highest_milestone(250) == 250


# --- build_brag_url ---------------------------------------------------------

def _frag(url: str) -> dict:
    """프래그먼트(#) 파라미터만 파싱. 통계가 쿼리(?)로 새지 않았는지도 함께 검증."""
    parsed = urlparse(url)
    # 쿼리로 새면 브라우저가 서버로 전송함 → 반드시 비어 있어야 한다(개인정보 미전송 불변식).
    assert parsed.query == ""
    return parse_qs(parsed.fragment)


def test_build_brag_url_all_fields():
    url = build_brag_url(
        "https://run.example.org",
        streak=27, tier=20, dist_km=123.456, dur_sec=36000, pace_sec=291.6,
        runs=30, name="테스트 러너",
    )
    assert url.startswith("https://run.example.org/#")
    f = _frag(url)
    assert f["streak"] == ["27"]
    assert f["tier"] == ["20"]
    assert f["runs"] == ["30"]
    assert f["dist"] == ["123.46"]   # 소수 2자리
    assert f["time"] == ["36000"]
    assert f["pace"] == ["292"]      # round(291.6)
    assert f["name"] == ["테스트 러너"]   # 공백 포함 원복(합성명)


def test_build_brag_url_omits_none_stats():
    url = build_brag_url("https://run.example.org", streak=10, tier=10)
    f = _frag(url)
    assert f["streak"] == ["10"]
    assert f["tier"] == ["10"]
    for k in ("dist", "time", "pace", "runs", "name"):
        assert k not in f


def test_build_brag_url_strips_trailing_slash():
    url = build_brag_url("https://run.example.org/", streak=10, tier=10)
    assert url.startswith("https://run.example.org/#")
    assert "//#" not in url


def test_build_brag_url_name_special_chars_do_not_break_params():
    # 이름에 & = 가 섞여도 파라미터 구분을 깨지 않아야 한다.
    # (JS 쪽은 location.hash 가 아니라 location.href 의 원문 프래그먼트를 파싱한다 —
    #  Firefox 가 hash 를 %-디코드해 돌려주는 문제 회피. web/index.html 참조.)
    url = build_brag_url("https://example.com", streak=10, tier=10, name="a&b=c")
    f = _frag(url)
    assert f["name"] == ["a&b=c"]
    assert f["streak"] == ["10"]
    assert f["tier"] == ["10"]


def test_build_brag_url_runs_zero_omitted():
    url = build_brag_url("https://example.com", streak=15, tier=10, runs=0)
    assert "runs" not in _frag(url)
