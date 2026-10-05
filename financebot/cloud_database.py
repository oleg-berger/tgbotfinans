"""Synchronous SQL bridge: shared async rules execute inside transactionSync."""
from .schema import MIGRATIONS


def complete(coroutine):
    # Our ledger's awaits only delegate to immediate SQL calls. Never permit
    # network I/O or a suspended coroutine inside a financial transaction.
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    else:
        coroutine.close()
        raise RuntimeError("Transaction attempted asynchronous I/O")


class Cursor:
    def __init__(self, rows, lastrowid=None):
        self.rows, self.lastrowid = rows, lastrowid

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self, sql):
        self.sql = sql

    async def execute(self, statement, params=()):
        if any(type(p) is int and abs(p) > 2**53 - 1 for p in params):
            raise ValueError("Integer exceeds Cloudflare SQL binding precision")
        cursor = self.sql.exec(statement, *params)
        result = cursor.toArray()
        rows = result.to_py() if hasattr(result, "to_py") else result
        lastrowid = None
        if statement.lstrip().upper().startswith("INSERT"):
            value = self.sql.exec("SELECT last_insert_rowid() AS value").toArray()
            value = value.to_py() if hasattr(value, "to_py") else value
            lastrowid = value[0]["value"]
        return Cursor(rows, lastrowid)


class DurableDatabase:
    def __init__(self, storage, transaction):
        self.storage = storage
        self.conn = Connection(storage.sql)
        self._transaction = transaction

    async def atomic(self, action):
        return self._transaction(lambda: complete(action(self.conn)))

    async def savepoint(self, conn, action):
        # Nested transactionSync is a savepoint, rolled back independently.
        return self._transaction(lambda: complete(action()))

    async def initialize(self):
        async def migrate(conn):
            await conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY)")
            row = await (await conn.execute("SELECT COALESCE(MAX(version),0) AS version FROM schema_version")).fetchone()
            if row["version"] > MIGRATIONS[-1][0]:
                raise RuntimeError("База создана более новой версией приложения.")
            for version, script in MIGRATIONS:
                if version > row["version"]:
                    for statement in script.split(";"):
                        if statement.strip():
                            await conn.execute(statement)
                    await conn.execute("INSERT INTO schema_version VALUES(?)", (version,))
        await self.atomic(migrate)
