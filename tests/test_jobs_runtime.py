from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import pytest

from financebot.application import Application
from financebot.database import Database, MIGRATIONS
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
        for uid in (1, 2):
            await l.create_bank(uid, "БЦЦ", [], 1000000, "2026-09-01")
            # Existing accounts have already seen the introductory offer.
            await conn.execute("UPDATE users SET guide_completed=1,cashback_intro_shown=1 WHERE id=?", (uid,))
    return db


async def test_reminder_local_time_catchup_and_no_repeat(db, tmp_path):
    delivered = []
    async def send(uid, screen):
        delivered.append((uid, screen.text))
    job = Maintenance(db, tmp_path / "backups", send)
    await job.tick(datetime(2026, 9, 1, 3, 59, tzinfo=timezone.utc))
    assert delivered == []
    await job.tick(datetime(2026, 9, 1, 4, 0, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1]
    # Restart after missing 09:00 in Moscow.
    job = Maintenance(db, tmp_path / "backups", send)
    await job.tick(datetime(2026, 9, 5, 9, tzinfo=timezone.utc))
    await job.tick(datetime(2026, 9, 5, 10, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1, 2]
    await job.tick(datetime(2026, 10, 1, 9, tzinfo=timezone.utc))
    assert [uid for uid, _ in delivered] == [1, 2, 1, 2]


async def test_failed_delivery_retries_for_all_users(db, tmp_path):
    async def failed(uid, screen):
        raise OSError("offline")
    job = Maintenance(db, tmp_path / "backups", failed)
    now = datetime(2026, 9, 5, tzinfo=timezone.utc)
    await job.tick(now)
    delivered = []
    async def send(uid, screen):
        delivered.append(uid)
    job.send = send
    await job.tick(now)
    assert delivered == [1, 2]


async def test_new_user_reminder_waits_for_guide_and_bank_then_next_month(db, tmp_path):
    now = datetime(2026, 9, 5, 6, tzinfo=timezone.utc)
    app = Application(db, clock=lambda: now)
    delivered = []
    async def send(uid, screen):
        if uid == 3:
            delivered.append(screen)
    job = Maintenance(db, tmp_path / "backups", send)
    screen = await app.handle(3, "start", text="/start")
    await job.tick(now)
    assert delivered == []
    async def click(screen, label, key):
        route = next(data for row in screen.buttons for text, data in row if label in text)
        return await app.handle(3, key, callback=route)
    screen = await click(screen, "Пройти гайд", "guide")
    for number in range(6):
        screen = await click(screen, "Далее", f"page:{number}")
        await job.tick(now)
        assert delivered == []
    await click(screen, "Завершить гайд", "finish")
    await job.tick(now)
    assert delivered == []
    await app.handle(3, "name", text="Kaspi")
    await app.handle(3, "aliases", text="-")
    await job.tick(now)
    assert delivered == []
    screen = await app.handle(3, "opening", text="0")
    assert "Теперь настрой первый кэшбэк" in screen.text
    await job.tick(now)
    assert delivered == []
    # Skipping the initial rate setup still enables the regular next-month reminder.
    await app.handle(3, "later", callback="menu")
    job = Maintenance(db, tmp_path / "backups", send)
    await job.tick(datetime(2026, 10, 1, 3, 59, tzinfo=timezone.utc))
    assert delivered == []
    await job.tick(datetime(2026, 10, 1, 4, 0, tzinfo=timezone.utc))
    await job.tick(datetime(2026, 10, 1, 4, 1, tzinfo=timezone.utc))
    assert len(delivered) == 1
    assert "2026-10" in delivered[0].text


async def test_migration_preserves_existing_accounts_and_unfinished_onboarding(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for version, script in MIGRATIONS[:3]:
            conn.executescript(script)
            conn.execute("INSERT INTO schema_version VALUES(?)", (version,))
        conn.executemany("INSERT INTO users(id) VALUES(?)", [(1,), (2,)])
        conn.execute("INSERT INTO banks(id,user_id,name) VALUES(1,1,'Bank')")
        conn.commit()
    db = Database(path)
    await db.initialize()
    await db.initialize()
    async with db.transaction() as conn:
        l = Ledger(conn)
        existing, unfinished = await l.user(1), await l.user(2)
        assert existing["guide_completed"] and existing["cashback_intro_shown"]
        assert not unfinished["guide_completed"] and not unfinished["cashback_intro_shown"]
        assert (await l.objects(1, "bank"))[0]["name"] == "Bank"
        await l.ensure_user(3)
        assert not (await l.user(3))["guide_completed"]


async def test_backup_is_restorable_and_retains_seven(db, tmp_path):
    async def send(uid, screen):
        pass
    job = Maintenance(db, tmp_path / "backups", send)
    for day in range(1, 10):
        await job.tick(datetime(2026, 9, day, tzinfo=timezone.utc))
    copies = sorted((tmp_path / "backups").glob("finance-*.sqlite3"))
    assert len(copies) == 7
    with sqlite3.connect(copies[-1]) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT SUM(amount) FROM operations WHERE user_id=1").fetchone()[0] == 1000000
    restored = Database(copies[-1])
    await restored.initialize()
    async with restored.transaction() as conn:
        assert await Ledger(conn).balance(1) == 1000000


def test_config_requires_token_without_allowlist(monkeypatch, tmp_path):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    with pytest.raises(ValueError):
        Config.load(tmp_path / "missing.env")
    monkeypatch.setenv("BOT_TOKEN", "123456:TEST_TOKEN_NOT_REAL")
    config = Config.load(tmp_path / "missing.env")
    assert config.token == "123456:TEST_TOKEN_NOT_REAL"
    assert config.database.is_absolute()
    # A legacy setting must not restrict access or prevent startup.
    monkeypatch.setenv("ALLOWED_USER_IDS", "all")
    assert Config.load(tmp_path / "missing.env") == config
    monkeypatch.setenv("BOT_TOKEN", "invalid")
    with pytest.raises(ValueError):
        Config.load(tmp_path / "missing.env")


def test_markup_carries_actions_and_router_constructs():
    markup = telegram_markup(Screen("test", [[("Баланс", "balance")]]))
    assert markup.inline_keyboard[0][0].callback_data == "balance"
    assert make_router() is not None


async def test_events_older_than_thirty_days_are_purged(db, tmp_path):
    async def send(uid, screen):
        pass
    job = Maintenance(db, tmp_path / "backups", send)
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
    job = Maintenance(db, tmp_path / "backups", send)
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
