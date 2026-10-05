from contextlib import closing
from pathlib import Path
import sqlite3

import pytest

from financebot.database import Database
from financebot.ledger import Ledger
from financebot.instance import InstanceLock
from financebot.reset_user import reset_user


@pytest.fixture
async def sample(tmp_path):
    db = Database(tmp_path / "finance.sqlite3")
    await db.initialize()
    async with db.transaction() as conn:
        ledger = Ledger(conn)
        for uid in (11, 22):
            await ledger.ensure_user(uid)
            bid = await ledger.create_bank(uid, "Bank", [], 100000, "2026-09-01")
            cid = await ledger.create_category(uid, "Food", "expense", ["food"])
            await ledger.set_rate(uid, bid, cid, "2026-09", "3", False)
            await ledger.record(uid, 25000, "2026-09-05", bid, cid)
            await ledger.payout(uid, bid, "2026-09", 750, "2026-09-06")
            await ledger.save_dialog(uid, {"step": "test"})
            await conn.execute("INSERT INTO events VALUES(?,?,?,?)", (uid, "m:1", "{}", "2026-09-06"))
            await conn.execute("INSERT INTO reminders VALUES(?,?,?)", (uid, "2026-09", 1))
            await conn.execute("INSERT INTO daily_reminders VALUES(?,?)", (uid, "2026-09-06"))
    return db, tmp_path / "backups"


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


async def test_preview_does_not_change_database(sample):
    db, backups = sample
    before = snapshot(db.path)
    result = reset_user(db.path, backups, 11)
    assert result["counts"]["operations"] == 3
    assert result["backup"] is None
    assert snapshot(db.path) == before
    assert not backups.exists()


async def test_reset_only_one_user_and_backup_restores_original(sample):
    db, backups = sample
    before = snapshot(db.path)
    result = reset_user(db.path, backups, 11, confirm=True)
    assert snapshot(Path(result["backup"])) == before
    async with db.transaction() as conn:
        l = Ledger(conn)
        assert await l.one("SELECT * FROM users WHERE id=11") is None
        for table in result["counts"]:
            if table != "users":
                assert await l.rows(f"SELECT * FROM {table} WHERE user_id=11") == []
        assert await l.balance(22) == 75750
        assert (await l.dialog(22))["step"] == "test"
        assert await l.rows("PRAGMA foreign_key_check") == []
        await l.ensure_user(11)
        assert await l.balance(11) == 0
        assert await l.objects(11, "bank") == []
        assert len(await l.objects(11, "category")) == 2


async def test_missing_user_and_running_bot_are_not_reset(sample):
    db, backups = sample
    before = snapshot(db.path)
    with pytest.raises(ValueError):
        reset_user(db.path, backups, 99, confirm=True)
    with InstanceLock(db.path.with_suffix(".lock")):
        with pytest.raises(RuntimeError):
            reset_user(db.path, backups, 11, confirm=True)
    assert snapshot(db.path) == before


async def test_backup_failure_prevents_deletion(sample):
    db, backups = sample
    backups.write_text("Not a directory")
    before = snapshot(db.path)
    with pytest.raises(OSError):
        reset_user(db.path, backups, 11, confirm=True)
    assert snapshot(db.path) == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError):
        reset_user(path, tmp_path / "backups", 11, confirm=True)
    assert not path.exists()
