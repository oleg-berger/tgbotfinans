import json
import asyncio
import sqlite3
from pathlib import Path
from urllib.error import HTTPError

import pytest

from cloudflare import manage
from financebot.application import Application
from financebot.database import Database
from financebot.snapshots import export_snapshot
from financebot.schema import MIGRATIONS


async def test_sqlite_export_preserves_every_table_without_changing_source(tmp_path):
    source, output = tmp_path / "source.db", tmp_path / "snapshot.json"
    db = Database(source)
    await db.initialize()
    await Application(db).handle(77, "test", text="/start")
    expected = json.loads(await db.atomic(export_snapshot))
    manage.export_sqlite(source, output)
    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert json.loads(await db.atomic(export_snapshot)) == expected
    with pytest.raises(FileExistsError):
        manage.export_sqlite(source, output)


async def test_cloud_snapshot_roundtrip_creates_separate_sqlite(tmp_path):
    db = Database(tmp_path / "original.db")
    await db.initialize()
    await Application(db).handle(77, "test", text="/start")
    snapshot = json.loads(await db.atomic(export_snapshot))
    source, target = tmp_path / "cloud.json", tmp_path / "restored.db"
    source.write_text(json.dumps(snapshot), encoding="utf-8")
    await asyncio.to_thread(manage.snapshot_to_sqlite, source, target)
    restored = Database(target)
    await restored.initialize()
    assert json.loads(await restored.atomic(export_snapshot)) == snapshot
    with pytest.raises(FileExistsError):
        await asyncio.to_thread(manage.snapshot_to_sqlite, source, target)


async def test_invalid_snapshot_leaves_no_partial_sqlite(tmp_path):
    source, target = tmp_path / "cloud.json", tmp_path / "restored.db"
    source.write_text('{"format":"invalid"}', encoding="utf-8")
    with pytest.raises(ValueError):
        await asyncio.to_thread(manage.snapshot_to_sqlite, source, target)
    assert not target.exists()


def test_export_migrates_only_memory_copy_of_previous_schema(tmp_path):
    source, output = tmp_path / "old.db", tmp_path / "snapshot.json"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        for version, script in MIGRATIONS[:4]:
            conn.executescript(script)
            conn.execute("INSERT INTO schema_version VALUES(?)", (version,))
        conn.execute("INSERT INTO users(id) VALUES(77)")
    manage.export_sqlite(source, output)
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 5
    assert snapshot["tables"]["users"][0]["id"] == 77
    with sqlite3.connect(source) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 4


async def test_cleanup_queries_use_date_indexes_after_migration(tmp_path):
    db = Database(tmp_path / "indexed.db")
    await db.initialize()
    async def check(conn):
        for table, field in (("events", "created_at"), ("daily_reminders", "day")):
            rows = await (await conn.execute(f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE {field} < ?", ("2026-01-01",))).fetchall()
            assert any("SEARCH" in row[3] and "INDEX" in row[3] for row in rows)
    await db.atomic(check)


def test_sqlite_export_rejects_missing_source(tmp_path):
    with pytest.raises(ValueError, match="не найден"):
        manage.export_sqlite(tmp_path / "missing.db", tmp_path / "out.json")
    assert not (tmp_path / "missing.db").exists()


def test_configure_webhook_keeps_updates_and_serial_delivery(monkeypatch):
    secrets = iter(("123456:fake_token", "secret-123"))
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt: next(secrets))
    calls = []
    def request(url, payload, headers=None):
        calls.append((url, payload))
        return {"ok": True, "result": True}
    monkeypatch.setattr(manage, "request", request)
    manage.main(["--url", "https://example.workers.dev", "configure"])
    assert calls[0][0].endswith("/setMyCommands")
    assert len(calls[0][1]["commands"]) == 12
    assert calls[1][1] == {"url": "https://example.workers.dev/webhook", "secret_token": "secret-123", "max_connections": 1, "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False}


def test_admin_preview_and_confirmation_are_explicit(monkeypatch):
    monkeypatch.setattr(manage.getpass, "getpass", lambda prompt: "test")
    calls = []
    monkeypatch.setattr(manage, "request", lambda url, payload, headers: calls.append(payload) or {})
    manage.main(["--url", "https://example.workers.dev", "reset-user", "77"])
    manage.main(["--url", "https://example.workers.dev", "reset-user", "77", "--confirm"])
    assert calls == [{"uid": 77, "confirm": False}, {"uid": 77, "confirm": True}]
    with pytest.raises(SystemExit):
        manage.main(["--url", "https://example.workers.dev", "restore", "backup"])


def test_cli_http_error_never_prints_telegram_token(monkeypatch):
    def fail(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
    monkeypatch.setattr(manage, "urlopen", fail)
    with pytest.raises(ValueError) as error:
        manage.request("https://api.telegram.org/bot123456:private/setWebhook", {})
    assert "HTTP 403" in str(error.value)
    assert "private" not in str(error.value)


def test_admin_rejects_cleartext_remote_url():
    with pytest.raises(ValueError, match="HTTPS"):
        manage.main(["--url", "http://example.com", "backup"])
