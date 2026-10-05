"""Serialized cloud service, durable delivery retries, snapshots and admin tools."""
from datetime import datetime, timedelta, timezone
import json
import logging
import uuid

from .application import Application
from .cloud_transport import event, TelegramError
from .reminders import Reminders
from .screens import Screen
from .snapshots import export_snapshot, import_snapshot, user_counts, delete_user

log = logging.getLogger(__name__)


class CloudService:
    def __init__(self, db, telegram, kv, clock=None):
        self.db, self.telegram, self.kv = db, telegram, kv
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.app = Application(db, self.clock)

    def read(self, key, default=None):
        value = self.kv.get(key)
        return json.loads(value) if value is not None else default

    def write(self, key, value):
        self.kv.put(key, json.dumps(value, ensure_ascii=False))

    async def update(self, update):
        incoming = event(update)
        if not incoming:
            query = update.get("callback_query", {})
            if query.get("id"):
                await self.ack(query["id"])
            return
        # No later dialog is processed while an earlier answer awaits delivery.
        await self.flush(incoming["uid"])
        query = incoming.get("query")
        if query:
            await self.ack(query["id"])
        receipt = f"{incoming['uid']}:{incoming['key']}"
        async def prepare(conn):
            delivered = self.read("deliveries", {})
            if receipt in delivered:
                return
            screen = await self.app.handle(incoming["uid"], incoming["key"], **({"callback": incoming["callback"]} if query else {"text": incoming["text"]}))
            pending = self.read("outbox", [])
            pending.append({"receipt": receipt, "uid": incoming["uid"], "screen": screen.dumps(), "query": query})
            self.write("outbox", pending)
        await self.db.atomic(prepare)
        await self.flush(incoming["uid"])

    async def ack(self, query_id):
        try:
            await self.telegram.call("answerCallbackQuery", {"callback_query_id": query_id})
        except TelegramError as exc:
            # Telegram may retry a callback after its acknowledgement expires.
            if exc.code != 400:
                raise

    async def flush(self, uid=None):
        failed = set()
        for item in self.read("outbox", []):
            if (uid is not None and item["uid"] != uid) or item["uid"] in failed:
                continue
            try:
                await self.telegram.deliver(item["uid"], Screen.loads(item["screen"]), item["query"])
            except Exception as exc:
                if not isinstance(exc, TelegramError) or exc.code != 403:
                    if uid is not None:
                        raise
                    failed.add(item["uid"])
                    log.warning("Outbox delivery failed (%s); will retry", type(exc).__name__)
                    continue
            async def mark(conn):
                pending = self.read("outbox", [])
                pending = [r for r in pending if r["receipt"] != item["receipt"]]
                self.write("outbox", pending)
                delivered = self.read("deliveries", {})
                delivered[item["receipt"]] = self.clock().astimezone(timezone.utc).isoformat(timespec="seconds")
                self.write("deliveries", delivered)
            await self.db.atomic(mark)

    async def tick(self, now=None):
        now = now or self.clock()
        try:
            await self.backup(f"finance-{now.astimezone(timezone.utc):%Y-%m-%d}", daily=True)
        except Exception as exc:
            log.warning("Cloud backup failed (%s); will retry", type(exc).__name__)
        await self.flush()
        await Reminders(self.db, self.telegram.send).tick(now)
        async def prune(conn):
            cutoff = (now - timedelta(days=30)).astimezone(timezone.utc).isoformat(timespec="seconds")
            self.write("deliveries", {key: stamp for key, stamp in self.read("deliveries", {}).items() if stamp >= cutoff})
        await self.db.atomic(prune)

    async def backup(self, name=None, *, daily=False):
        name = name or "manual-" + uuid.uuid4().hex
        async def save(conn):
            index = self.read("backups", {})
            if name in index:
                return name
            data = await export_snapshot(conn)
            # Keep individual values below storage's per-value limit.
            chunks = [data[i:i + 16000] for i in range(0, len(data), 16000)]
            for i, chunk in enumerate(chunks):
                self.kv.put(f"backup:{name}:{i}", chunk)
            index[name] = {"chunks": len(chunks), "daily": daily}
            daily_names = sorted(key for key, value in index.items() if value["daily"])
            for old in daily_names[:-7]:
                for i in range(index[old]["chunks"]):
                    self.kv.delete(f"backup:{old}:{i}")
                del index[old]
            self.write("backups", index)
            return name
        return await self.db.atomic(save)

    def snapshot(self, name):
        meta = self.read("backups", {}).get(name)
        if not meta:
            raise ValueError("Backup not found")
        chunks = [self.kv.get(f"backup:{name}:{i}") for i in range(meta["chunks"])]
        if any(chunk is None for chunk in chunks):
            raise ValueError("Backup incomplete")
        return "".join(chunks)

    async def admin(self, action, payload):
        if action == "export":
            return json.loads(await self.db.atomic(export_snapshot))
        if action == "backups":
            return self.read("backups", {})
        if action == "backup":
            return {"backup": await self.backup()}
        if action == "download":
            return json.loads(self.snapshot(payload["name"]))
        if action == "import":
            async def load(conn):
                await import_snapshot(conn, json.dumps(payload["snapshot"], ensure_ascii=False))
            await self.db.atomic(load)
            return {"imported": True}
        if action == "restore":
            if payload.get("confirm") is not True:
                raise ValueError("Explicit confirmation required")
            text = self.snapshot(payload["name"])
            previous = await self.backup("before-restore-" + uuid.uuid4().hex)
            async def restore(conn):
                await import_snapshot(conn, text, replace=True)
                self.write("outbox", [])
                self.write("deliveries", {})
            await self.db.atomic(restore)
            return {"restored": True, "backup": previous}
        if action == "reset-user":
            uid = payload.get("uid")
            if type(uid) is not int or not 0 < uid < 2**52:
                raise ValueError("Invalid Telegram ID")
            async def preview(conn):
                return await user_counts(conn, uid)
            counts = await self.db.atomic(preview)
            if payload.get("confirm") is not True:
                return {"counts": counts, "backup": None}
            backup = await self.backup(f"before-reset-{uid}-" + uuid.uuid4().hex)
            async def remove(conn):
                await delete_user(conn, uid)
                self.write("outbox", [r for r in self.read("outbox", []) if r["uid"] != uid])
                self.write("deliveries", {k: v for k, v in self.read("deliveries", {}).items() if not k.startswith(f"{uid}:")})
            await self.db.atomic(remove)
            return {"counts": counts, "backup": backup}
        raise ValueError("Unknown admin action")
