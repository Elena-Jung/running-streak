"""handle_message(집계 핵심 경로) 단위 테스트 — 디스코드/DB 를 페이크로 대체.

asyncio.run 으로 코루틴을 돌려 pytest-asyncio 의존 없이 검증한다.
"""

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timezone

from app import events
from app.config import Config
from app.db import Runner


# --- 페이크 ----------------------------------------------------------------

@dataclass
class FakeAuthor:
    id: int = 100
    bot: bool = False
    mention: str = "<@100>"
    display_name: str = "tester"


class FakeAttachment:
    def __init__(self, content_type="image/png", filename="a.png", size=1000, data=b"x"):
        self.content_type = content_type
        self.filename = filename
        self.size = size
        self._data = data

    async def read(self):
        return self._data


class FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, content, **kw):
        self.sent.append(content)


class FakeGuild:
    me = object()


class FakeMessage:
    def __init__(self, *, channel_id, attachments, author=None, guild=True):
        self.author = author or FakeAuthor()
        self.guild = FakeGuild() if guild else None
        self.channel = FakeChannel(channel_id)
        self.attachments = attachments
        self.created_at = datetime.now(timezone.utc)
        self.reactions = []
        self.removed = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, member):
        self.removed.append(emoji)


class FakeDB:
    def __init__(self, registered=True, record=None, rr=(True, 1)):
        self._registered = registered
        self._record = record
        self._rr = rr
        self.record_run_calls = []

    async def is_registered(self, uid):
        return self._registered

    async def load(self, uid):
        return self._record

    async def record_run(self, uid, run_date, **kw):
        self.record_run_calls.append((uid, run_date, kw))
        if isinstance(self._rr, Exception):
            raise self._rr
        return self._rr


TARGET = 555


def _cfg(ocr=False):
    return Config(discord_token="t", guild_id=1, target_channel_id=TARGET,
                  pg_host="db", pg_port=5432, pg_user="u", pg_password="p",
                  pg_db="d", ocr_enabled=ocr)


def _run(msg, db, cfg):
    asyncio.run(events.handle_message(msg, config=cfg, db=db, bot_user_id=999))


def _img_msg(**kw):
    return FakeMessage(channel_id=TARGET, attachments=[FakeAttachment()], **kw)


# --- 테스트 ----------------------------------------------------------------

def test_unregistered_ignored():
    m = _img_msg()
    db = FakeDB(registered=False)
    _run(m, db, _cfg())
    assert m.channel.sent == [] and m.reactions == [] and db.record_run_calls == []


def test_wrong_channel_ignored():
    m = FakeMessage(channel_id=TARGET + 1, attachments=[FakeAttachment()])
    db = FakeDB()
    _run(m, db, _cfg())
    assert m.channel.sent == [] and db.record_run_calls == []


def test_no_image_ignored():
    m = FakeMessage(channel_id=TARGET, attachments=[])
    db = FakeDB()
    _run(m, db, _cfg())
    assert m.channel.sent == [] and db.record_run_calls == []


def test_bot_author_ignored():
    m = _img_msg(author=FakeAuthor(bot=True))
    db = FakeDB()
    _run(m, db, _cfg())
    assert m.channel.sent == [] and db.record_run_calls == []


def test_first_run_counts_and_reacts():
    m = _img_msg()
    db = FakeDB(record=None, rr=(True, 1))
    _run(m, db, _cfg())
    assert "✅" in m.reactions
    assert len(db.record_run_calls) == 1
    assert m.channel.sent and "1일째 연속" in m.channel.sent[0]


def test_same_day_is_silent():
    m = _img_msg()
    today = events.to_run_date(m.created_at)  # 메시지 시각과 동일 기준으로 맞춤(경계 플레이크 방지)
    rec = Runner(user_id=100, registered=True, last_run_date=today,
                 current_streak=3, max_streak=3, total_runs=3)
    db = FakeDB(record=rec)
    _run(m, db, _cfg())
    assert m.reactions == [] and m.channel.sent == [] and db.record_run_calls == []


def test_record_run_conflict_no_message():
    m = _img_msg()
    db = FakeDB(record=None, rr=(False, 0))
    _run(m, db, _cfg())
    assert "✅" in m.reactions
    assert m.channel.sent == []  # 동시 업로드 경합 → 무음


def test_record_run_failure_marks_warning():
    m = _img_msg()
    db = FakeDB(record=None, rr=RuntimeError("db down"))
    _run(m, db, _cfg())
    assert "✅" in m.reactions and "⚠️" in m.reactions
    assert m.channel.sent == []


def test_ocr_all_none_adds_hint():
    # OCR 켜고 비이미지 바이트 → try_extract 가 빈 결과 → 힌트가 붙어야 함.
    m = FakeMessage(channel_id=TARGET, attachments=[FakeAttachment(data=b"notimage")])
    db = FakeDB(record=None, rr=(True, 1))
    _run(m, db, _cfg(ocr=True))
    assert m.channel.sent and "/달리기 취소" in m.channel.sent[0]


# --- 러닝 하루 경계: KST 04시 리셋 (to_run_date) ----------------------------
#     하루가 자정이 아니라 04:00 에 바뀐다. 00:00~03:59 새벽 러닝은 '전날'로 친다.

def _kst(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=events.KST)


def test_run_date_dawn_belongs_to_previous_day():
    assert events.to_run_date(_kst(2026, 6, 25, 0, 0)) == date(2026, 6, 24)   # 자정 직후
    assert events.to_run_date(_kst(2026, 6, 25, 2, 0)) == date(2026, 6, 24)   # 새벽 2시
    assert events.to_run_date(_kst(2026, 6, 25, 3, 59)) == date(2026, 6, 24)  # 03:59 (경계 직전)


def test_run_date_boundary_4am_is_new_day():
    assert events.to_run_date(_kst(2026, 6, 25, 4, 0)) == date(2026, 6, 25)   # 정확히 04:00 → 당일
    assert events.to_run_date(_kst(2026, 6, 25, 4, 1)) == date(2026, 6, 25)


def test_run_date_daytime_and_late_night_same_day():
    assert events.to_run_date(_kst(2026, 6, 25, 12, 0)) == date(2026, 6, 25)   # 정오
    assert events.to_run_date(_kst(2026, 6, 25, 22, 40)) == date(2026, 6, 25)  # 밤 10시 40분
    assert events.to_run_date(_kst(2026, 6, 25, 23, 59)) == date(2026, 6, 25)  # 자정 직전


def test_run_date_handles_naive_utc_input():
    # tz 없는 입력은 UTC 로 간주. UTC 17:00 06-24 = KST 02:00 06-25 → 전날(06-24).
    assert events.to_run_date(datetime(2026, 6, 24, 17, 0)) == date(2026, 6, 24)
    # UTC 19:00 06-24 = KST 04:00 06-25 → 당일(06-25).
    assert events.to_run_date(datetime(2026, 6, 24, 19, 0)) == date(2026, 6, 25)


def test_current_run_date_matches_to_run_date():
    # current_run_date 는 to_run_date(now) 의 얇은 래퍼 — 같은 기준을 써야 한다.
    assert events.current_run_date() == events.to_run_date(datetime.now(timezone.utc))


def test_completion_message_omits_boundary_note():
    # 완료 메시지는 04시 경계 안내를 포함하지 않는다(그 설명은 /스트릭·/캘린더로 이동).
    # 새벽(02:00 KST) 업로드여도 '전날' 안내가 붙지 않아야 함.
    m = _img_msg()
    m.created_at = _kst(2026, 6, 25, 2, 0)
    db = FakeDB(record=None, rr=(True, 1))
    _run(m, db, _cfg())
    assert m.channel.sent and "전날" not in m.channel.sent[0] and "1일째 연속" in m.channel.sent[0]
