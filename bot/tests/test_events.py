"""handle_message(집계 핵심 경로) 단위 테스트 — 디스코드/DB 를 페이크로 대체.

asyncio.run 으로 코루틴을 돌려 pytest-asyncio 의존 없이 검증한다.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

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
    today = events.to_kst_date(datetime.now(timezone.utc))
    rec = Runner(user_id=100, registered=True, last_run_date=today,
                 current_streak=3, max_streak=3, total_runs=3)
    m = _img_msg()
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
