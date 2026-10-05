"""Shared persistent reminder rules; delivery always follows a committed read."""
from datetime import datetime, timedelta, timezone
import logging
from zoneinfo import ZoneInfo

from .ledger import Ledger
from .screens import Screen

log = logging.getLogger(__name__)


class Reminders:
    def __init__(self, db, send):
        self.db, self.send = db, send

    async def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        async def prepare(conn):
            cutoff = (now - timedelta(days=30)).astimezone(timezone.utc).isoformat(timespec="seconds")
            await conn.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
            await conn.execute("DELETE FROM daily_reminders WHERE day < ?", (cutoff[:10],))
            return await Ledger(conn).rows("SELECT * FROM users")
        for user in await self.db.atomic(prepare):
            uid = user["id"]
            local = now.astimezone(ZoneInfo(user["timezone"]))
            async def due(conn):
                ledger = Ledger(conn)
                result = []
                if user["reminder_time"]:
                    hour, minute = map(int, user["reminder_time"].split(":"))
                    day = local.strftime("%Y-%m-%d")
                    if local >= local.replace(hour=hour, minute=minute, second=0, microsecond=0):
                        if not await ledger.one("SELECT 1 AS x FROM daily_reminders WHERE user_id=? AND day=?", (uid, day)):
                            result.append(("daily", day, Screen("✍️ День подходит к концу — запишите сегодняшние траты одной строкой: <code>250 пр бцц</code>", [[("☰ Меню", "menu")]])))
                if user["guide_completed"] and user["cashback_intro_shown"] and await ledger.objects(uid, "bank"):
                    if local >= local.replace(day=1, hour=9, minute=0, second=0, microsecond=0):
                        month = local.strftime("%Y-%m")
                        row = await ledger.one("SELECT delivered FROM reminders WHERE user_id=? AND month=?", (uid, month))
                        if not row or not row["delivered"]:
                            await conn.execute("INSERT OR IGNORE INTO reminders(user_id,month) VALUES(?,?)", (uid, month))
                            result.append(("monthly", month, Screen(f"🪙 Начался новый месяц — {month}. Заполните категории кэшбэка в ваших банках.", [[("Настроить кэшбэк", f"cash:{month}:0"), ("Копировать прошлый месяц", f"copy:{month}")]])))
                return result
            for kind, stamp, screen in await self.db.atomic(due):
                try:
                    await self.send(uid, screen)
                except Exception as exc:
                    log.warning("Reminder delivery failed (%s); will retry", type(exc).__name__)
                    continue
                async def delivered(conn):
                    if kind == "daily":
                        await conn.execute("INSERT OR IGNORE INTO daily_reminders VALUES(?,?)", (uid, stamp))
                    else:
                        await conn.execute("UPDATE reminders SET delivered=1 WHERE user_id=? AND month=?", (uid, stamp))
                await self.db.atomic(delivered)
