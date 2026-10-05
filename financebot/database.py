"""Versioned SQLite storage; an event is committed together with its response."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import aiosqlite


from .schema import MIGRATIONS


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

    async def atomic(self, action):
        async with self.transaction() as conn:
            return await action(conn)

    async def savepoint(self, conn, action):
        await conn.execute("SAVEPOINT action")
        try:
            return await action()
        except BaseException:
            await conn.execute("ROLLBACK TO action")
            raise
        finally:
            await conn.execute("RELEASE action")

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
