"""Versioned, validated snapshots shared by cloud administration and migration."""
import json

from .ledger import Ledger
from .schema import MIGRATIONS

# Insertion follows references; deletion uses the reverse order.
TABLES = ("users", "banks", "categories", "aliases", "operations", "rates", "dialogs", "events", "reminders", "daily_reminders")
MAX_IMPORT_BYTES = 20 * 1024 * 1024


async def export_snapshot(conn):
    ledger = Ledger(conn)
    tables = {table: await ledger.rows(f"SELECT * FROM {table}") for table in TABLES}
    return json.dumps({"format": "financebot-snapshot-v1", "schema_version": MIGRATIONS[-1][0], "tables": tables}, ensure_ascii=False)


async def import_snapshot(conn, text, *, replace=False):
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise ValueError("Snapshot is too large")
    snapshot = json.loads(text)
    # Versions 4 and 5 have identical columns; version 5 only adds indexes.
    if not isinstance(snapshot, dict) or snapshot.get("format") != "financebot-snapshot-v1" or snapshot.get("schema_version") not in (4, 5):
        raise ValueError("Unsupported snapshot version")
    tables = snapshot.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(TABLES):
        raise ValueError("Invalid snapshot tables")
    if not replace and await Ledger(conn).one("SELECT id FROM users LIMIT 1"):
        raise ValueError("Import requires an empty database")
    if replace:
        for table in reversed(TABLES):
            await conn.execute(f"DELETE FROM {table}")
    for table in TABLES:
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        columns = [row["name"] for row in await cursor.fetchall()]
        if not isinstance(tables[table], list):
            raise ValueError("Invalid snapshot rows")
        for row in tables[table]:
            if not isinstance(row, dict) or set(row) != set(columns):
                raise ValueError("Snapshot columns differ from schema")
            if table == "operations" and any(type(row[field]) is not int for field in ("amount", "cashback")):
                raise ValueError("Snapshot money must be integer tiyn")
            await conn.execute(f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(row[col] for col in columns))
    if await (await conn.execute("PRAGMA foreign_key_check")).fetchone():
        raise ValueError("Invalid snapshot references")
    # Ordinary foreign keys do not verify that both rows have the same owner.
    references = (
        ("users", "id", "default_bank", "banks"),
        ("aliases", "user_id", "bank_id", "banks"),
        ("aliases", "user_id", "category_id", "categories"),
        ("operations", "user_id", "bank_id", "banks"),
        ("operations", "user_id", "target_bank", "banks"),
        ("operations", "user_id", "category_id", "categories"),
        ("rates", "user_id", "bank_id", "banks"),
        ("rates", "user_id", "category_id", "categories"),
    )
    for table, owner, column, target in references:
        cursor = await conn.execute(f"SELECT 1 FROM {table} a LEFT JOIN {target} b ON b.id=a.{column} WHERE a.{column} IS NOT NULL AND (b.id IS NULL OR b.user_id!=a.{owner}) LIMIT 1")
        if await cursor.fetchone():
            raise ValueError("Snapshot contains another user's reference")


async def user_counts(conn, uid):
    if not await Ledger(conn).one("SELECT id FROM users WHERE id=?", (uid,)):
        raise ValueError("User does not exist")
    return {table: (await Ledger(conn).one(f"SELECT COUNT(*) AS count FROM {table} WHERE {'id' if table == 'users' else 'user_id'}=?", (uid,)))["count"] for table in TABLES}


async def delete_user(conn, uid):
    counts = await user_counts(conn, uid)
    for table in reversed(TABLES):
        await conn.execute(f"DELETE FROM {table} WHERE {'id' if table == 'users' else 'user_id'}=?", (uid,))
    return counts
