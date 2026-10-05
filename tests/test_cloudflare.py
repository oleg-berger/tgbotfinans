import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from cloud_storage import LocalDurableDatabase
from financebot.cloud_database import complete
from financebot.cloud_service import CloudService
from financebot.cloud_transport import event, Telegram, TelegramError
from financebot.ledger import Ledger
from financebot.schema import MIGRATIONS
from financebot.screens import Screen
from financebot.snapshots import export_snapshot, import_snapshot

NOW = datetime(2026, 10, 5, 6, tzinfo=timezone.utc)


class Bot:
    def __init__(self):
        self.sent, self.acks = [], []
        self.fail = False

    async def call(self, method, fields):
        self.acks.append((method, fields))

    async def deliver(self, uid, screen, query=None):
        if self.fail:
            raise TelegramError(500, "test failure")
        self.sent.append((uid, screen, query))

    async def send(self, uid, screen):
        await self.deliver(uid, screen)


@pytest.fixture
async def service(tmp_path):
    db = LocalDurableDatabase(tmp_path / "cloud.db")
    await db.initialize()
    return CloudService(db, Bot(), db.kv, clock=lambda: NOW)


def message(text, mid=1, uid=1):
    return {"message": {"message_id": mid, "chat": {"id": uid, "type": "private"}, "from": {"id": uid, "is_bot": False}, "text": text}}


async def seed(service):
    async def action(conn):
        l = Ledger(conn)
        await l.ensure_user(1)
        await l.ensure_user(2)
        return await l.create_bank(1, "БЦЦ", [], 1000000, "2026-10-05")
    return await service.db.atomic(action)


async def test_daily_backup_failure_does_not_stop_delivery_retry(service, monkeypatch):
    await seed(service)
    service.telegram.fail = True
    with pytest.raises(TelegramError):
        await service.update(message("250"))
    service.telegram.fail = False
    async def fail(*args, **kwargs):
        raise RuntimeError("Backup failed")
    monkeypatch.setattr(service, "backup", fail)
    await service.tick(NOW)
    assert service.read("outbox") == []
    assert len(service.telegram.sent) == 1


async def test_snapshot_rejects_cross_user_references_and_fractional_money(service):
    await seed(service)
    original = await service.db.atomic(export_snapshot)
    for change in ("owner", "money"):
        snapshot = json.loads(original)
        if change == "owner":
            snapshot["tables"]["operations"][0]["user_id"] = 2
        else:
            snapshot["tables"]["operations"][0]["amount"] = 1.5
        async def load(conn):
            await import_snapshot(conn, json.dumps(snapshot), replace=True)
        with pytest.raises(ValueError):
            await service.db.atomic(load)
        assert await service.db.atomic(export_snapshot) == original


async def test_retry_after_delivery_failure_and_restart_does_not_spend_twice(service):
    await seed(service)
    service.telegram.fail = True
    with pytest.raises(TelegramError):
        await service.update(message("250"))
    restarted = CloudService(service.db, Bot(), service.kv, clock=lambda: NOW)
    await restarted.update(message("250"))
    await restarted.update(message("250"))
    assert len(restarted.telegram.sent) == 1
    async def check(conn):
        l = Ledger(conn)
        assert await l.balance(1) == 975000
        assert len(await l.operations(1)) == 2
        assert not await l.operations(2)
    await service.db.atomic(check)
    assert service.read("outbox") == []


async def test_failure_rollback_includes_dialog_event_and_outbox(service, monkeypatch):
    async def broken(uid, key, **kwargs):
        async def action(conn):
            await Ledger(conn).ensure_user(uid)
            await conn.execute("UPDATE users SET timezone='UTC' WHERE id=?", (uid,))
            raise RuntimeError("unexpected failure")
        return await service.db.atomic(action)
    monkeypatch.setattr(service.app, "handle", broken)
    with pytest.raises(RuntimeError):
        await service.update(message("/start"))
    async def check(conn):
        assert not await Ledger(conn).rows("SELECT * FROM users")
        assert not await Ledger(conn).rows("SELECT * FROM events")
    await service.db.atomic(check)
    assert not service.read("outbox", [])


async def test_validation_rolls_back_action_but_stores_error_response(service):
    await seed(service)
    await service.update(message("250\n31.02.2026"))
    assert "⚠️" in service.telegram.sent[-1][1].text
    async def check(conn):
        assert len(await Ledger(conn).operations(1)) == 1
        assert await Ledger(conn).one("SELECT * FROM events")
    await service.db.atomic(check)


async def test_network_yield_inside_transaction_is_rejected_and_rolled_back(service):
    async def forbidden(conn):
        await conn.execute("INSERT INTO users(id) VALUES(100)")
        await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="asynchronous I/O"):
        await service.db.atomic(forbidden)
    async def check(conn):
        assert not await Ledger(conn).one("SELECT id FROM users WHERE id=100")
    await service.db.atomic(check)


async def test_backup_rotation_restore_and_user_reset(service):
    await seed(service)
    for n in range(9):
        await service.backup(f"finance-2026-10-{n + 1:02d}", daily=True)
    assert len(service.read("backups")) == 7
    preview = await service.admin("reset-user", {"uid": 1})
    assert preview["counts"]["operations"] == 1 and preview["backup"] is None
    result = await service.admin("reset-user", {"uid": 1, "confirm": True})
    async def check(conn):
        assert not await Ledger(conn).one("SELECT id FROM users WHERE id=1")
        assert await Ledger(conn).user(2)
    await service.db.atomic(check)
    await service.admin("restore", {"name": result["backup"], "confirm": True})
    async def restored(conn):
        assert await Ledger(conn).balance(1) == 1000000
    await service.db.atomic(restored)
    assert result["backup"] in service.read("backups")


async def test_failed_backup_prevents_reset(service, monkeypatch):
    await seed(service)
    monkeypatch.setattr(service.kv, "put", lambda *args: (_ for _ in ()).throw(RuntimeError("storage failed")))
    with pytest.raises(RuntimeError):
        await service.admin("reset-user", {"uid": 1, "confirm": True})
    async def check(conn):
        assert await Ledger(conn).balance(1) == 1000000
    await service.db.atomic(check)


async def test_import_rejects_populated_database_and_bad_references_atomically(service, tmp_path):
    await seed(service)
    snapshot = json.loads(await service.db.atomic(export_snapshot))
    target = LocalDurableDatabase(tmp_path / "target.db")
    await target.initialize()
    async def load(conn):
        await import_snapshot(conn, json.dumps(snapshot))
    await target.atomic(load)
    with pytest.raises(ValueError, match="empty"):
        await target.atomic(load)
    broken = json.loads(json.dumps(snapshot))
    broken["tables"]["operations"][0]["bank_id"] = 999
    async def replace(conn):
        await import_snapshot(conn, json.dumps(broken), replace=True)
    with pytest.raises(Exception):
        await target.atomic(replace)
    assert json.loads(await target.atomic(export_snapshot)) == snapshot


async def test_migrate_previous_schema_and_reject_unknown_version(tmp_path):
    db = LocalDurableDatabase(tmp_path / "old.db")
    db.native.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
    for version, script in MIGRATIONS[:3]:
        db.native.executescript(script)
        db.native.execute("INSERT INTO schema_version VALUES(?)", (version,))
    await db.initialize()
    assert db.native.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == MIGRATIONS[-1][0]
    db.native.execute("INSERT INTO schema_version VALUES(999)")
    with pytest.raises(RuntimeError):
        await db.initialize()


async def test_balances_above_js_safe_integer_remain_exact(service):
    async def action(conn):
        l = Ledger(conn)
        await l.ensure_user(1)
        bid = await l.create_bank(1, "Банк", [], 0, "2026-10-05")
        for _ in range(100):
            await l.insert_op(1, "opening", 99_999_999_999_999, "2026-10-05", bid)
        expected = 100 * 99_999_999_999_999
        assert (await l.balances(1))[bid] == expected
        assert await l.balance(1) == expected
    await service.db.atomic(action)


def test_groups_and_inline_callbacks_are_ignored():
    update = message("250")
    update["message"]["chat"]["type"] = "group"
    assert event(update) is None
    assert event({"callback_query": {"id": "1", "from": {"id": 1}}}) is None


class HTTP:
    def __init__(self, results=None):
        self.calls = []
        self.results = results or []

    async def __call__(self, url, **kwargs):
        self.calls.append((url.rsplit("/", 1)[-1], kwargs))
        value = self.results.pop(0) if self.results else {"ok": True, "result": {}}
        async def result():
            return value
        return SimpleNamespace(status=200, json=result)


async def test_transport_photos_csv_html_and_edits():
    http = HTTP()
    bot = Telegram("test-token", http, asset=lambda _: b"png-test")
    await bot.send(1, Screen("guide", photo="assets/guide/page-01.png"))
    await bot.send(1, Screen("CSV", document=b"\xef\xbb\xbfdate;sum", filename="finance.csv"))
    query = {"message": {"message_id": 2, "photo": [{}]}}
    await bot.deliver(1, Screen("guide2", photo="assets/guide/page-02.png"), query)
    await bot.deliver(1, Screen("<b>menu</b>"), query)
    assert [method for method, _ in http.calls] == ["sendPhoto", "sendDocument", "editMessageMedia", "sendMessage", "editMessageReplyMarkup"]
    assert b"png-test" in http.calls[0][1]["body"]
    assert b"finance.csv" in http.calls[1][1]["body"]
    assert json.loads(http.calls[3][1]["body"])["parse_mode"] == "HTML"


async def test_edit_not_modified_and_edit_fallback():
    http = HTTP([{"ok": False, "error_code": 400, "description": "message is not modified"}])
    bot = Telegram("test", http)
    await bot.deliver(1, Screen("same"), {"message": {"message_id": 2}})
    assert len(http.calls) == 1
    http.results = [{"ok": False, "error_code": 400, "description": "can't edit"}]
    await bot.deliver(1, Screen("new"), {"message": {"message_id": 2}})
    assert [method for method, _ in http.calls][-2:] == ["editMessageText", "sendMessage"]


async def test_reminders_same_timing_after_restart_and_delivery_retry(service):
    await seed(service)
    async def setup(conn):
        await conn.execute("UPDATE users SET guide_completed=1,cashback_intro_shown=1,reminder_time='10:00' WHERE id=1")
    await service.db.atomic(setup)
    service.telegram.fail = True
    await service.tick(NOW)
    assert not service.telegram.sent
    service.telegram.fail = False
    await service.tick(NOW)
    assert len(service.telegram.sent) == 2
    await CloudService(service.db, service.telegram, service.kv).tick(NOW)
    assert len(service.telegram.sent) == 2


async def test_pending_delivery_for_one_user_does_not_block_another(service):
    await seed(service)
    service.telegram.fail = True
    with pytest.raises(TelegramError):
        await service.update(message("250", uid=1))
    service.telegram.fail = False
    await service.update(message("/menu", uid=2))
    assert service.telegram.sent[-1][0] == 2
    assert service.read("outbox")[0]["uid"] == 1
