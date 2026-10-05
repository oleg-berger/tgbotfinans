"""Persistent monthly reminders and online SQLite backups."""
import asyncio
from contextlib import closing
from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3

from .reminders import Reminders

log = logging.getLogger(__name__)


class Maintenance(Reminders):
    def __init__(self, db, backup_dir, send):
        super().__init__(db, send)
        self.backup_dir, self.send = Path(backup_dir), send

    async def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        try:
            await self.backup(now)
        except Exception as exc:
            log.error("Backup failed (%s)", type(exc).__name__)
        await super().tick(now)

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
