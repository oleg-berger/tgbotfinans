"""Thin aiogram transport; private chats only, no financial data in logs."""
import asyncio
from contextlib import suppress
import logging
from pathlib import Path
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.enums import ParseMode
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, BotCommand
from aiogram.types import FSInputFile, InputMediaPhoto

from .application import Application
from .database import Database
from .instance import InstanceLock
from .jobs import Maintenance

log = logging.getLogger(__name__)


def telegram_markup(screen):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, callback_data=data) for label, data in row] for row in screen.buttons]) if screen.buttons else None


async def deliver(bot, uid, screen):
    if screen.photo:
        await bot.send_photo(uid, FSInputFile(Path(__file__).parent / screen.photo), caption=screen.text, reply_markup=telegram_markup(screen))
    elif screen.document:
        await bot.send_document(uid, BufferedInputFile(screen.document, filename=screen.filename), caption=screen.text, reply_markup=telegram_markup(screen))
    else:
        await bot.send_message(uid, screen.text, reply_markup=telegram_markup(screen))


def make_router():
    router = Router()

    @router.message()
    async def message_handler(message: Message, app: Application, bot: Bot):
        if message.chat.type != "private" or not message.from_user or message.from_user.is_bot:
            return
        try:
            screen = await app.handle(message.from_user.id, f"m:{message.chat.id}:{message.message_id}", text=message.text or "")
            await deliver(bot, message.chat.id, screen)
        except Exception as exc:
            log.error("Message handling failed (%s)", type(exc).__name__)
            await message.answer("Не удалось обработать запрос или доставить ответ. Проверьте «Операции» перед повторной записью. /menu")

    @router.callback_query()
    async def callback_handler(query: CallbackQuery, app: Application, bot: Bot):
        if not query.message or query.message.chat.type != "private" or query.message.chat.id != query.from_user.id:
            await query.answer()
            return
        # Acknowledge early: monetary changes are independently protected in SQLite.
        await query.answer()
        try:
            screen = await app.handle(query.from_user.id, f"c:{query.id}", callback=query.data or "menu")
            if screen.replace and not screen.document:
                try:
                    if screen.photo:
                        await query.message.edit_media(InputMediaPhoto(media=FSInputFile(Path(__file__).parent / screen.photo), caption=screen.text), reply_markup=telegram_markup(screen))
                    elif query.message.photo:
                        await deliver(bot, query.from_user.id, screen)
                        await query.message.edit_reply_markup(reply_markup=None)
                    else:
                        await query.message.edit_text(screen.text, reply_markup=telegram_markup(screen))
                    return
                except TelegramBadRequest as exc:
                    if "not modified" in str(exc):
                        return
                    log.warning("Edit failed, sending new message (%s)", type(exc).__name__)
            await deliver(bot, query.from_user.id, screen)
        except Exception as exc:
            log.error("Callback handling failed (%s)", type(exc).__name__)
            await bot.send_message(query.from_user.id, "Не удалось завершить запрос. Проверьте «Операции» и откройте /menu.")

    return router


async def run(config):
    with InstanceLock(config.database.with_suffix(".lock")):
        db = Database(config.database)
        await db.initialize()
        app = Application(db)
        bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dispatcher = Dispatcher(app=app)
        dispatcher.include_router(make_router())
        async def send(uid, screen):
            await deliver(bot, uid, screen)
        maintenance = Maintenance(db, config.backups, send)
        worker = None
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands([BotCommand(command="start", description="Начать"), BotCommand(command="menu", description="Главное меню"), BotCommand(command="balance", description="Баланс"), BotCommand(command="stats", description="Статистика"), BotCommand(command="ops", description="Операции"), BotCommand(command="cashback", description="Кэшбэк"), BotCommand(command="categories", description="Категории"), BotCommand(command="banks", description="Банки"), BotCommand(command="settings", description="Настройки"), BotCommand(command="guide", description="Гайд по боту"), BotCommand(command="cancel", description="Отменить ввод"), BotCommand(command="help", description="Помощь")])
            worker = asyncio.create_task(maintenance.run())
            log.info("Bot started")
            # Sequential delivery preserves message order and durable dialog transitions.
            await dispatcher.start_polling(bot, handle_as_tasks=False, allowed_updates=["message", "callback_query"], close_bot_session=False)
        finally:
            if worker:
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
            await bot.session.close()
