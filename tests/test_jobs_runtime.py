from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import pytest

from financebot.database import Database
from financebot.ledger import Ledger
from financebot.jobs import Maintenance
from financebot.config import Config
from financebot.runtime import make_router, telegram_markup
from financebot.screens import Screen


@pytest.fixture
async def db(tmp_path):
    db = Database(tmp_path / "finance.db")
    await db.initialize()
    async with db.transaction() as conn:
        l = Ledger(conn)
        await l.ensure_user(1)
        await l.ensure_user(2)
        await l.set_timezone(2, "Europe/Moscow")
    return db


async def test_reminder_local_time_catchup_and_no_repeat(db, tmp_path):
    delivered = []
    async def send(uid, screen):
        delivered.append((uid, screen.text))
    job = Maintenance(db, {1, 2}, tmp_path / "backups", send)
    await job.tick(datetime(2026, 9, 1, 3, 59, tzinfo=timezone.utc))
    assert delivered == []
    await job.tick(datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1]
    # Restart after missing 09:00 in Moscow.
    job = Maintenance(db, {1, 2}, tmp_path / "backups", send)
    await job.tick(datetime(2026, 9, 5, 9, tzinfo=timezone.utc))
    await job.tick(datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1, 2]
    await job.tick(datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1, 2, 1, 2]


async def test_failed_delivery_retries_and_removed_users_skipped(db, tmp_path):
    async def failed(uid, screen):
        raise OSError("offline")
    job = Maintenance(db, {1}, tmp_path / "backups", failed)
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    await job.tick(now)
    delivered = []
    async def send(uid, screen):
        delivered.append(uid)
    job.send = send
    await job.tick(now)
    assert delivered == [1]


async def test_backup_is_restorable_and_retains_seven(db, tmp_path):
    async def send(uid, screen):
        pass
    job = Maintenance(db, set(), tmp_path / "backups", send)
    async with db.transaction() as conn:
        l = Ledger(conn)
        await l.create_bank(1, "БЦЦ", [], 1000000, "2026-09-01")
    for day in range(1, 10):
        await job.tick(datetime(2026, 9, day, tzinfo=timezone.utc))
    copies = sorted((tmp_path / "backups").glob("finance-*.sqlite3"))
    assert len(copies) == 7
    with sqlite3.connect(copies[-1]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT SUM(amount) FROM operations").fetchone()[0] == 1000000
    restored = Database(copies[-1])
    await restored.initialize()
    async with restored.transaction() as conn:
        assert await Ledger(conn).balance(1) == 1000000


def test_config_rejects_missing_secret_and_bad_allowlist(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    with pytest.raises(ValueError):
        Config.load(tmp_path / "missing.env")
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN_NOT_REAL")
    monkeypatch.setenv("ALLOWED_USER_IDS", "1,2")
    config = Config.load(tmp_path / "missing.env")
    assert config.allowed_users == {1, 2}
    monkeypatch.setenv("ALLOWED_USER_IDS", "all")
    with pytest.raises(ValueError):
        Config.load(tmp_path / "missing.env")


def test_markup_carries_actions_and_router_constructs():
    markup = telegram_markup(Screen("test", [[("Баланс", "balance")]]))
    assert markup.inline_keyboard[0][0].callback_data == "balance"
    assert make_router() is not None


async def test_events_older_than_thirty_days_are_purged(db, tmp_path):
    async def send(uid, screen):
        pass
    job = Maintenance(db, set(), tmp_path / "backups", send)
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO events(user_id,event_key,response,created_at) VALUES(1,'old','{}','2026-08-01T00:00:00+00:00')")
        await conn.execute("INSERT INTO events(user_id,event_key,response,created_at) VALUES(1,'new','{}','2026-09-04T00:00:00+00:00')")
    await job.tick(datetime(2026, 9, 5, tzinfo=timezone.utc))
    async with db.transaction() as conn:
        keys = [r["event_key"] for r in await Ledger(conn).rows("SELECT event_key FROM events")]
    assert keys == ["new"]


async def test_daily_reminder_at_local_time_once_and_retry(db, tmp_path):
    async with db.transaction() as conn:
        l = Ledger(conn)
        await l.set_reminder(1, "21:00")
        await l.set_reminder(2, "21:00")
    delivered = []
    async def send(uid, screen):
        if "траты" in screen.text:
            delivered.append(uid)
    job = Maintenance(db, {1, 2}, tmp_path / "backups", send)
    # 20:30 in Qyzylorda (UTC+5) and 18:30 in Moscow (UTC+3): nobody is due yet.
    await job.tick(datetime(2026, 9, 5, 15, 30, tzinfo=timezone.utc))
    assert delivered == []
    # 21:00 in Qyzylorda, 19:00 in Moscow.
    await job.tick(datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc))
    assert delivered == [1]
    await job.tick(datetime(2026, 9, 5, 16, 30, tzinfo=timezone.utc))
    assert delivered == [1]
    # 21:00 in Moscow.
    await job.tick(datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc))
    assert delivered == [1, 2]
    await job.tick(datetime(2026, 9, 5, 18, 30, tzinfo=timezone.utc))
    assert delivered == [1, 2]
    # Next day fires again.
    await job.tick(datetime(2026, 9, 6, 16, 5, tzinfo=timezone.utc))
    assert delivered == [1, 2, 1]
    # Failure does not mark the day: next tick retries.
    async def failed(uid, screen):
        raise OSError("offline")
    job.send = failed
    async with db.transaction() as conn:
        await conn.execute("DELETE FROM daily_reminders")
    await job.tick(datetime(2026, 9, 7, 18, 0, tzinfo=timezone.utc))
    job.send = send
    await job.tick(datetime(2026, 9, 7, 18, 5, tzinfo=timezone.utc))
    assert delivered[-1] == 2
