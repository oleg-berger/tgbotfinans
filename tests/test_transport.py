"""Exercise real aiogram dispatch; only Telegram HTTP is replaced."""
from datetime import datetime, timezone
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.client.default import DefaultBotProperties
from aiogram.methods import SendMessage, SendDocument, AnswerCallbackQuery, EditMessageText
from aiogram.methods import SendPhoto, EditMessageMedia, EditMessageReplyMarkup
from aiogram.types import Update, Message, Chat, User

from financebot.application import Application
from financebot.database import Database
from financebot.ledger import Ledger
from financebot.runtime import make_router
from financebot.instance import InstanceLock


class TelegramHTTP(BaseSession):
    def __init__(self):
        super().__init__()
        self.outbox = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.outbox.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendPhoto, EditMessageMedia, EditMessageReplyMarkup)):
            return Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type="private"))
        if isinstance(method, EditMessageText):
            return Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(message_id=len(self.outbox), date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type="private"), text=getattr(method, "text", None))
        raise AssertionError(f"Unexpected Telegram method: {type(method).__name__}")


async def test_actual_dispatch_onboards_records_and_ignores_group(tmp_path):
    db = Database(tmp_path / "transport.db")
    await db.initialize()
    app = Application(db, clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
    http = TelegramHTTP()
    bot = Bot("123456:TEST_TOKEN_NOT_REAL", session=http, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(app=app)
    dp.include_router(make_router())
    i = 0
    async def message(text, group=False):
        nonlocal i
        i += 1
        update = Update(update_id=i, message=Message(message_id=i, date=datetime.now(timezone.utc), chat=Chat(id=-100 if group else 1, type="group" if group else "private"), from_user=User(id=1, is_bot=False, first_name="Test"), text=text))
        await dp.feed_update(bot, update)
        return update
    async def click(label):
        nonlocal i
        from aiogram.types import CallbackQuery
        screen = http.outbox[-1]
        data = next(button.callback_data for row in screen.reply_markup.inline_keyboard for button in row if label in button.text)
        i += 1
        await dp.feed_update(bot, Update(update_id=i, callback_query=CallbackQuery(id=f"click-{i}", from_user=User(id=1, is_bot=False, first_name="Test"), chat_instance="private", message=Message(message_id=100, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private")), data=data)))
    try:
        await message("/start", group=True)
        assert http.outbox == []
        await message("/start")
        await click("Пройти гайд")
        assert isinstance(http.outbox[-1], EditMessageMedia)
        assert http.outbox[-1].media.media.path.name == "page-01.png"
        for _ in range(6):
            await click("Далее")
        assert http.outbox[-1].media.media.path.name == "page-07.png"
        await click("Создать категории")
        await click("Расход")
        await message("Продукты")
        await message("пр")
        await click("Банки")
        await click("Добавить банк")
        await message("БЦЦ")
        await message("бц")
        await message("10000")
        update = await message("250 пр")
        assert "9 750,00" in http.outbox[-1].text
        await dp.feed_update(bot, update)
        async with db.transaction() as conn:
            assert await Ledger(conn).balance(1) == 975000
    finally:
        await bot.session.close()


def test_instance_lock_prevents_second_process_and_releases(tmp_path):
    import subprocess
    import sys
    path = tmp_path / "instance.lock"
    code = "from financebot.instance import InstanceLock; import sys; lock=InstanceLock(sys.argv[1]); lock.__enter__(); lock.__exit__()"
    with InstanceLock(path):
        result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
        assert result.returncode != 0
        assert b"RuntimeError" in result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert result.returncode == 0
