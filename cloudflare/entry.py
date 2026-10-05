"""Private Python Durable Object: one serialized financial database."""
import asyncio
from datetime import datetime, timezone
import json
import logging
from urllib.parse import urlparse

from pyodide.ffi import create_proxy
from workers import DurableObject, WorkerEntrypoint, Request, Response, fetch

from financebot.cloud_database import DurableDatabase
from financebot.cloud_service import CloudService
from financebot.cloud_transport import Telegram
from financebot.snapshots import MAX_IMPORT_BYTES

log = logging.getLogger(__name__)


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)


class FinanceStore(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.lock = asyncio.Lock()
        self.service = None

    def transaction(self, action):
        result, failure = [], []
        def callback():
            try:
                result.append(action())
            except BaseException as exc:
                failure.append(exc)
                raise
        proxy = create_proxy(callback)
        try:
            self.ctx.storage.transactionSync(proxy)
        except BaseException:
            if failure:
                raise failure[0]
            raise
        finally:
            proxy.destroy()
        return result[0]

    async def fetch(self, request):
        async with self.lock:
            try:
                if self.service is None:
                    db = DurableDatabase(self.ctx.storage, self.transaction)
                    await db.initialize()
                    async def asset(path):
                        filename = path.rsplit("/", 1)[-1]
                        if filename not in {f"page-{i:02d}.png" for i in range(1, 8)}:
                            raise ValueError("Unknown guide image")
                        response = await self.env.ASSETS.fetch(Request(f"https://assets/guide/{filename}"))
                        if response.status != 200:
                            raise RuntimeError("Guide asset missing")
                        return await response.bytes()
                    async def telegram_fetch(url, **options):
                        binding = getattr(self.env, "TELEGRAM_API", None)
                        if binding is not None:
                            return await binding.fetch(Request(url, **options))
                        return await fetch(url, **options)
                    self.service = CloudService(db, Telegram(self.env.BOT_TOKEN, telegram_fetch, asset), self.ctx.storage.kv)
                path = urlparse(request.url).path
                if path == "/maintenance":
                    await self.service.tick(datetime.now(timezone.utc))
                    return Response("ok")
                body = await request.text()
                if len(body.encode()) > MAX_IMPORT_BYTES:
                    return Response("Too large", status=413)
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    return Response("Invalid request", status=400)
                if path == "/webhook":
                    await self.service.update(payload)
                    return Response("ok")
                if path.startswith("/admin/"):
                    try:
                        result = await self.service.admin(path.removeprefix("/admin/"), payload)
                    except (KeyError, ValueError):
                        return Response("Invalid administration request", status=400)
                    return Response.json(result)
                return Response("Not found", status=404)
            except (json.JSONDecodeError, UnicodeError):
                return Response("Invalid JSON", status=400)
            except Exception as exc:
                # Never log Telegram URLs, update contents, or exception strings.
                log.error("Cloud request failed (%s)", type(exc).__name__)
                return Response("Please retry", status=503)
