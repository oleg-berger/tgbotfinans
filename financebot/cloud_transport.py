"""Telegram Bot API transport with an injectable Workers fetch implementation."""
import json
import inspect
from pathlib import Path
import uuid


class TelegramError(RuntimeError):
    def __init__(self, code, description):
        super().__init__("Telegram delivery failed")
        self.code, self.description = code, description


def event(update):
    if not isinstance(update, dict):
        raise ValueError("Invalid update")
    if "callback_query" in update:
        query = update["callback_query"]
        message, user = query.get("message", {}), query.get("from", {})
        chat = message.get("chat", {})
        if chat.get("type") != "private" or chat.get("id") != user.get("id") or user.get("is_bot"):
            return None
        return {"uid": user["id"], "key": f"c:{query['id']}", "callback": query.get("data") or "menu", "query": query}
    if "message" in update:
        message = update["message"]
        user, chat = message.get("from", {}), message.get("chat", {})
        if chat.get("type") != "private" or not user.get("id") or user.get("is_bot") or chat.get("id") != user["id"]:
            return None
        return {"uid": user["id"], "key": f"m:{chat['id']}:{message['message_id']}", "text": message.get("text") or ""}
    return None


def markup(screen):
    return {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in screen.buttons]}


def multipart(fields, files):
    boundary = "financebot-" + uuid.uuid4().hex
    parts = []
    for key, value in fields.items():
        encoded = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{encoded}\r\n'.encode())
    for key, filename, content_type, content in files:
        parts.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode(), content, b"\r\n"])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class Telegram:
    def __init__(self, token, fetch, asset=None):
        self.token, self.fetch = token, fetch
        self.asset = asset or (lambda path: (Path(__file__).parent / path).read_bytes())

    async def photo(self, path):
        value = self.asset(path)
        return await value if inspect.isawaitable(value) else value

    async def call(self, method, fields, files=()):
        if files:
            body, content_type = multipart(fields, files)
        else:
            body, content_type = json.dumps(fields, ensure_ascii=False), "application/json"
        response = await self.fetch(f"https://api.telegram.org/bot{self.token}/{method}", method="POST", headers={"Content-Type": content_type}, body=body)
        value = await response.json()
        if not value.get("ok"):
            raise TelegramError(value.get("error_code", response.status), value.get("description", ""))
        return value.get("result")

    async def send(self, uid, screen):
        fields = {"chat_id": uid, "parse_mode": "HTML", "reply_markup": markup(screen)}
        if screen.photo:
            await self.call("sendPhoto", fields | {"caption": screen.text}, [("photo", "guide.png", "image/png", await self.photo(screen.photo))])
        elif screen.document:
            await self.call("sendDocument", fields | {"caption": screen.text}, [("document", screen.filename, "text/csv", screen.document)])
        else:
            await self.call("sendMessage", fields | {"text": screen.text})

    async def deliver(self, uid, screen, query=None):
        if query and screen.replace and not screen.document:
            message = query["message"]
            fields = {"chat_id": uid, "message_id": message["message_id"], "reply_markup": markup(screen)}
            try:
                if screen.photo:
                    media = {"type": "photo", "media": "attach://photo", "caption": screen.text, "parse_mode": "HTML"}
                    await self.call("editMessageMedia", fields | {"media": media}, [("photo", "guide.png", "image/png", await self.photo(screen.photo))])
                elif message.get("photo"):
                    await self.send(uid, screen)
                    # Removing buttons is best-effort after a successful send.
                    try:
                        await self.call("editMessageReplyMarkup", fields | {"reply_markup": {"inline_keyboard": []}})
                    except TelegramError:
                        pass
                else:
                    await self.call("editMessageText", fields | {"text": screen.text, "parse_mode": "HTML"})
                return
            except TelegramError as exc:
                if exc.code != 400:
                    raise
                if "not modified" in exc.description.lower():
                    return
        await self.send(uid, screen)
