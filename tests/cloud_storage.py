"""SQLite-backed test double for the synchronous Durable Object storage API."""
from contextlib import asynccontextmanager
import copy
import sqlite3
from types import SimpleNamespace

from financebot.cloud_database import DurableDatabase


class SQL:
    def __init__(self, conn):
        self.conn = conn

    def exec(self, sql, *params):
        if sql.lstrip().split()[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"):
            raise RuntimeError("Transaction SQL is forbidden by Cloudflare")
        cursor = self.conn.execute(sql, params)
        rows = [dict(row) for row in cursor.fetchall()]
        return SimpleNamespace(toArray=lambda: rows)


class KV:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def put(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


class LocalDurableDatabase(DurableDatabase):
    def __init__(self, path):
        self.path = path
        self.native = sqlite3.connect(path, isolation_level=None)
        self.native.row_factory = sqlite3.Row
        self.native.execute("PRAGMA foreign_keys=ON")
        self.kv = KV()
        self.depth = 0
        super().__init__(SimpleNamespace(sql=SQL(self.native), kv=self.kv), self.transaction_sync)

    def transaction_sync(self, action):
        name = f"cloud_{self.depth}"
        self.depth += 1
        before = copy.deepcopy(self.kv.values)
        self.native.execute(f"SAVEPOINT {name}")
        try:
            return action()
        except BaseException:
            self.native.execute(f"ROLLBACK TO {name}")
            self.kv.values = before
            raise
        finally:
            self.native.execute(f"RELEASE {name}")
            self.depth -= 1

    @asynccontextmanager
    async def transaction(self):
        # Only for the existing direct Ledger fixtures. Application itself uses
        # the real atomic()/savepoint() adapter under test.
        yield self.conn
