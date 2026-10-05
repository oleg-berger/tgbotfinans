"""Versioned SQLite storage; an event is committed together with its response."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite


MIGRATIONS = [(1, """
CREATE TABLE users(id INTEGER PRIMARY KEY, timezone TEXT NOT NULL DEFAULT 'Asia/Qyzylorda', default_bank INTEGER);
CREATE TABLE banks(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
CREATE TABLE categories(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('income','expense')), system TEXT, archived INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,system));
CREATE TABLE aliases(user_id INTEGER NOT NULL REFERENCES users(id), word TEXT NOT NULL, bank_id INTEGER REFERENCES banks(id), category_id INTEGER REFERENCES categories(id), PRIMARY KEY(user_id,word), CHECK((bank_id IS NULL) != (category_id IS NULL)));
CREATE TABLE operations(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL CHECK(kind IN ('income','expense','opening','transfer','adjustment')), amount INTEGER NOT NULL, day TEXT NOT NULL, bank_id INTEGER NOT NULL REFERENCES banks(id), target_bank INTEGER REFERENCES banks(id), category_id INTEGER REFERENCES categories(id), rate TEXT, cashback INTEGER NOT NULL DEFAULT 0, payout_month TEXT, deleted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX operations_user_day ON operations(user_id,day);
CREATE UNIQUE INDEX one_payout ON operations(user_id,bank_id,payout_month) WHERE payout_month IS NOT NULL AND deleted=0;
CREATE TABLE rates(user_id INTEGER NOT NULL REFERENCES users(id), bank_id INTEGER NOT NULL REFERENCES banks(id), category_id INTEGER NOT NULL REFERENCES categories(id), month TEXT NOT NULL, rate TEXT NOT NULL, PRIMARY KEY(user_id,bank_id,category_id,month));
CREATE TABLE dialogs(user_id INTEGER PRIMARY KEY REFERENCES users(id), payload TEXT NOT NULL);
CREATE TABLE events(user_id INTEGER NOT NULL REFERENCES users(id), event_key TEXT NOT NULL, response TEXT NOT NULL, PRIMARY KEY(user_id,event_key));
CREATE TABLE reminders(user_id INTEGER NOT NULL REFERENCES users(id), month TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,month));
"""), (2, """
ALTER TABLE events ADD COLUMN created_at TEXT;
UPDATE events SET created_at = strftime('%Y-%m-%dT%H:%M:%S+00:00','now');
"""), (3, """
ALTER TABLE users ADD COLUMN reminder_time TEXT;
CREATE TABLE daily_reminders(user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL, PRIMARY KEY(user_id,day));
""")]


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = asyncio.Lock()

    async def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as conn:
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY)")
            row = await (await conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version")).fetchone()
            if row[0] > MIGRATIONS[-1][0]:
                raise RuntimeError("База создана более новой версией приложения.")
            for version, script in MIGRATIONS:
                if version > row[0]:
                    await conn.executescript("BEGIN IMMEDIATE;\n" + script + f"\nINSERT INTO schema_version VALUES({version});\nCOMMIT;")

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            async with aiosqlite.connect(self.path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA foreign_keys=ON")
                await conn.execute("PRAGMA busy_timeout=5000")
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    await conn.commit()
                except BaseException:
                    await conn.rollback()
                    raise
