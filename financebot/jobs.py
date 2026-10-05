"""Persistent monthly reminders and online SQLite backups."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .ledger import Ledger
from .screens import Screen

log = logging.getLogger(__name__)


class Maintenance:
    def __init__(self, db, backup_dir, send):
        self.db = db
        self.backup_dir, self.send = Path(backup_dir), send

    async def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            await self.backup(now)
        except Exception as exc:
            log.error("Backup failed (%s)", type(exc).__name__)
        async with self.db.transaction() as conn:
            cutoff = (now - timedelta(days=30)).astimezone(timezone.utc).isoformat(timespec="seconds")
            await conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            await conn.execute("DELETE FROM daily_reminders WHERE day < ?", (cutoff[:10],))
            users = await Ledger(conn).rows("SELECT * FROM users")
        for user in users:
            uid = user["id"]
            local = now.astimezone(ZoneInfo(user["timezone"]))
            if user["reminder_time"]:
                hour, minute = map(int, user["reminder_time"].split(":"))
                day = local.strftime("%Y-%m-%d")
                if local >= local.replace(hour=hour, minute=minute, second=0, microsecond=0):
                    async with self.db.transaction() as conn:
                        row = await Ledger(conn).one("SELECT 1 AS x FROM daily_reminders WHERE user_id=? AND day=?", (uid, day))
                    if not row:
                        try:
                            await self.send(uid, Screen("✍️ День подходит к концу — запишите сегодняшние траты одной строкой: <code>250 пр бцц</code>", [[("☰ Меню", "menu")]]))
                        except Exception as exc:
                            log.warning("Daily reminder delivery failed (%s); will retry", type(exc).__name__)
                        else:
                            async with self.db.transaction() as conn:
                                await conn.execute("INSERT OR IGNORE INTO daily_reminders VALUES(?,?)", (uid, day))
            if not user["guide_completed"] or not user["cashback_intro_shown"]:
                continue
            async with self.db.transaction() as conn:
                if not await Ledger(conn).objects(uid, "bank"):
                    continue
            if local < local.replace(day=1, hour=9, minute=0, second=0, microsecond=0):
                continue
            month = local.strftime("%Y-%m")
            async with self.db.transaction() as conn:
                l = Ledger(conn)
                row = await l.one("SELECT delivered FROM reminders WHERE user_id=? AND month=?", (uid, month))
                if row and row["delivered"]:
                    continue
                await conn.execute("INSERT OR IGNORE INTO reminders(user_id,month) VALUES(?,?)", (uid, month))
            try:
                await self.send(uid, Screen(f"🪙 Начался новый месяц — {month}. Заполните категории кэшбэка в ваших банках.", [[("Настроить кэшбэк", f"cash:{month}:0"), ("Копировать прошлый месяц", f"copy:{month}")]]))
            except Exception as exc:
                log.warning("Reminder delivery failed (%s); will retry", type(exc).__name__)
                continue
            async with self.db.transaction() as conn:
                await conn.execute("UPDATE reminders SET delivered=1 WHERE user_id=? AND month=?", (uid, month))

    async def backup(self, now):
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        destination = self.backup_dir / f"finance-{now.astimezone(timezone.utc):%Y-%m-%d}.sqlite3"
        if destination.exists():
            return
        async with self.db.lock:
            await asyncio.to_thread(self._copy, destination)
        copies = sorted(self.backup_dir.glob("finance-????-??-??.sqlite3"))
        for old in copies[:-7]:
            old.unlink()

    def _copy(self, destination):
        temporary = destination.with_suffix(".tmp")
        with closing(sqlite3.connect(self.db.path)) as source, closing(sqlite3.connect(temporary)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup integrity check failed")
        temporary.replace(destination)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                log.error("Maintenance failed (%s)", type(exc).__name__)
            await asyncio.sleep(60)
