from datetime import datetime, timezone
from pathlib import Path

import pytest

from financebot.application import Application
from financebot.database import Database
from financebot.ledger import Ledger
from financebot.guide import guide_screen
from financebot.screens import Screen


def test_guide_images_exist_and_survive_serialization():
    root = Path(__file__).resolve().parents[1] / "financebot"
    for page in range(7):
        screen = guide_screen(page, "test")
        restored = Screen.loads(screen.dumps())
        assert restored.photo == screen.photo
        assert restored.text == screen.text
        assert (root / screen.photo).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert len(screen.text) < 1024
    assert Screen.loads('{"text":"old","document":null}').photo is None


@pytest.fixture
async def chat(tmp_path):
    db = Database(tmp_path / "guide.db")
    await db.initialize()
    return Chat(Application(db, clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc)))


class Chat:
    def __init__(self, app):
        self.app = app
        self.counter = 0
        self.screen = None

    async def send(self, **kwargs):
        self.counter += 1
        self.screen = await self.app.handle(1, f"guide-test:{self.counter}", **kwargs)
        return self.screen

    def action(self, label):
        return next(data for row in self.screen.buttons for text, data in row if label in text)

    async def click(self, label):
        return await self.send(callback=self.action(label))


async def last_page(chat):
    await chat.send(text="/start")
    await chat.click("Пройти гайд")
    for _ in range(6):
        await chat.click("Далее")


async def test_start_greets_before_any_bank_or_category_prompt(chat):
    screen = await chat.send(text="/start")
    assert "Привет" in screen.text
    assert "<b>короткий гайд</b>" in screen.text
    assert chat.action("Пройти гайд")
    assert "Введите название банка" not in screen.text
    async with chat.app.db.transaction() as conn:
        assert await Ledger(conn).objects(1, "bank") == []


async def test_seven_pages_back_restart_and_final_cta(chat):
    await chat.send(text="/start")
    await chat.click("Пройти гайд")
    first = chat.screen.text
    assert "1/7" in first
    await chat.click("Далее")
    second = chat.screen.text
    await chat.click("Назад")
    assert chat.screen.text == first
    await chat.click("Далее")
    assert chat.screen.text == second
    # Recreate the app while retaining only SQLite, then use the existing button.
    chat.app = Application(chat.app.db, clock=lambda: datetime(2026, 9, 5, tzinfo=timezone.utc))
    texts = [first, second]
    for number in range(3, 8):
        await chat.click("Далее")
        assert f"{number}/7" in chat.screen.text
        assert len(chat.screen.text) < 1024
        texts.append(chat.screen.text)
    assert chat.action("Создать категории")
    assert not any("Далее" in label for row in chat.screen.buttons for label, _ in row)
    content = " ".join(texts).casefold()
    for topic in ("банк", "кэшбэк", "баланс", "csv", "перевод", "корректиров", "ключ", "часов", "удал", "доход", "расход", "архив"):
        assert topic in content


@pytest.mark.parametrize("kind,label", [("expense", "Расход"), ("income", "Доход")])
async def test_guide_category_creation_returns_main_menu(chat, kind, label):
    await last_page(chat)
    await chat.click("Создать категории")
    await chat.click(label)
    await chat.send(text="Моя категория")
    screen = await chat.send(text="мк, моя")
    for route in ("balance", "categories", "banks:0", "settings"):
        assert any(data == route for row in screen.buttons for _, data in row)
    async with chat.app.db.transaction() as conn:
        l = Ledger(conn)
        cats = await l.objects(1, "category")
        category = next(c for c in cats if c["name"] == "Моя категория")
        assert category["kind"] == kind
        assert "мк" in await l.aliases(1, "category", category["id"])
        assert await l.dialog(1) is None
        assert await l.objects(1, "bank") == []


async def test_restarting_guide_preserves_accounts_and_rejects_old_choice(chat):
    await last_page(chat)
    old = chat.action("Создать категории")
    async with chat.app.db.transaction() as conn:
        l = Ledger(conn)
        await l.create_bank(1, "БЦЦ", [], 1000000, "2026-09-05")
        await l.create_category(1, "Продукты", "expense", ["пр"])
        await l.record_text(1, "250 пр", datetime(2026, 9, 5).date())
    screen = await chat.send(text="/start")
    assert "Привет" in screen.text
    screen = await chat.send(callback=old)
    assert "устарел" in screen.text
    async with chat.app.db.transaction() as conn:
        assert await Ledger(conn).balance(1) == 975000
    await chat.send(text="/menu")
    await chat.send(callback="help")
    await chat.click("Пройти гайд")
    assert "1/7" in chat.screen.text


async def test_category_added_from_menu_also_returns_menu(chat):
    await chat.send(text="/menu")
    await chat.send(callback="newcat:expense")
    await chat.send(text="Транспорт")
    screen = await chat.send(text="-")
    assert any(data == "balance" for row in screen.buttons for _, data in row)
