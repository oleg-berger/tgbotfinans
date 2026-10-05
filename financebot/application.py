"""Atomic event processing, authorization, and replay protection."""
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from .dialogs import Session
from .ledger import Ledger
from .parsing import ValidationError
from .screens import Screen


class Application:
    def __init__(self, db, allowed_users, clock=None):
        self.db = db
        self.allowed_users = set(allowed_users)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def handle(self, uid, event_key, *, text=None, callback=None):
        if uid not in self.allowed_users:
            return Screen(f"Доступ закрыт. Передайте владельцу ваш Telegram ID: <code>{uid}</code>.")
        async with self.db.transaction() as conn:
            ledger = Ledger(conn)
            await ledger.ensure_user(uid)
            cached = await ledger.one("SELECT response FROM events WHERE user_id=? AND event_key=?", (uid, event_key))
            if cached:
                return Screen.loads(cached["response"])
            user = await ledger.user(uid)
            today = self.clock().astimezone(ZoneInfo(user["timezone"])).date()
            session = Session(ledger, uid, today)
            await conn.execute("SAVEPOINT action")
            try:
                screen = await session.callback(callback) if callback is not None else await session.text(text or "")
            except (ValidationError, IndexError, KeyError, ValueError) as exc:
                await conn.execute("ROLLBACK TO action")
                message = str(exc) if isinstance(exc, ValidationError) else "Некорректный ввод или устаревшая кнопка."
                state = await ledger.dialog(uid)
                screen = await session.prompt(state) if state else session.menu()
                screen.text = "⚠️ " + escape(message) + "\n\n" + screen.text
            await conn.execute("RELEASE action")
            await conn.execute("INSERT INTO events(user_id,event_key,response,created_at) VALUES(?,?,?,?)", (uid, event_key, screen.dumps(), self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")))
            return screen
