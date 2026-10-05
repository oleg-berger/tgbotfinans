import asyncio
from datetime import datetime, timezone
import pytest
from financebot.database import Database
from financebot.application import Application
from financebot.ledger import Ledger

NOW = datetime(2026, 9, 5, 6, tzinfo=timezone.utc)


def action(screen, label):
    return next(data for row in screen.buttons for text, data in row if label in text)


@pytest.fixture
async def app(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.initialize()
    return Application(db, clock=lambda: NOW)


async def send(app, text, uid=1, key=None):
    send.count += 1
    return await app.handle(uid, key or f"m:{send.count}", text=text)


send.count = 0


async def click(app, screen, label, uid=1):
    send.count += 1
    return await app.handle(uid, f"c:{send.count}", callback=action(screen, label))


async def onboard(app):
    await send(app, "/start")
    await app.handle(1, f"bank-setup:{send.count}", callback="newbank")
    await send(app, "БЦЦ")
    await send(app, "бц")
    s = await send(app, "10000")
    if any(text == "Да" for row in s.buttons for text, _ in row):
        return await click(app, s, "Да")
    return s


async def test_onboarding_restart_unknown_training_and_duplicate(app):
    await send(app, "/start")
    await app.handle(1, "bank-setup-restart", callback="newbank")
    await send(app, "БЦЦ")
    app = Application(app.db, clock=lambda: NOW)
    await send(app, "бц")
    s = await send(app, "10000")
    assert not any(text == "Да" for row in s.buttons for text, _ in row)
    s = await send(app, "250 пр бцц", key="purchase")
    assert "пр" in s.text
    s = await click(app, s, "Создать категорию")
    s = await click(app, s, "Расход")
    await send(app, "Продукты")
    s = await send(app, "еда")
    assert "9 750,00" in s.text
    again = await send(app, "250 пр бцц", key="purchase")
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        assert await l.balance(1) == 975000
        assert len([o for o in await l.operations(1) if o["kind"] == "expense"]) == 1
    s = await send(app, "250 пр бцц", key="purchase2")
    assert "9 500,00" in s.text


async def test_public_access_and_duplicate_parallel_messages(app):
    welcome = await send(app, "/start", uid=99)
    assert welcome.buttons
    await onboard(app)
    await asyncio.gather(*(send(app, "250", key="same") for _ in range(3)))
    async with app.db.transaction() as conn:
        assert await Ledger(conn).balance(1) == 975000
        ledger = Ledger(conn)
        assert await ledger.one("SELECT * FROM users WHERE id=99") is not None
        assert await ledger.balance(99) == 0
        assert await ledger.operations(99) == []
        assert await ledger.objects(99, "bank") == []


async def test_cashback_buttons_confirm_and_stale_callback(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        cat = await l.create_category(1, "Продукты", "expense", ["пр"])
        bank = (await l.objects(1, "bank"))[0]["id"]
    s = await app.handle(1, "rate1", callback=f"rate:2026-09:{bank}:{cat}")
    s = await send(app, "3")
    old = action(s, "Пересчитать")
    await click(app, s, "Пересчитать")
    s = await send(app, "250 пр")
    assert "7,50" in s.text and "3%" in s.text
    s = await app.handle(1, "pay1", callback=f"payout:2026-09:{bank}")
    s = await send(app, "7.50")
    s = await send(app, "05.09.2026")
    confirm = action(s, "Подтвердить")
    await click(app, s, "Подтвердить")
    await app.handle(1, "pay-again", callback=confirm)
    stale = await app.handle(1, "old", callback=old)
    assert "устарел" in stale.text.lower()
    async with app.db.transaction() as conn:
        assert await Ledger(conn).balance(1) == 975750


async def test_operation_edit_delete_reports_and_csv(app):
    await onboard(app)
    s = await send(app, "250")
    s = await click(app, s, "Операция")
    s = await click(app, s, "Сумма")
    s = await send(app, "500")
    assert "500,00" in s.text
    s = await app.handle(1, "report", callback="stats:2026-09")
    assert "500,00" in s.text
    s = await click(app, s, "CSV")
    assert s.document and s.filename.endswith(".csv")
    async with app.db.transaction() as conn:
        ops = await Ledger(conn).operations(1)
        oid = next(o["id"] for o in ops if o["kind"] == "expense")
    s = await app.handle(1, "open", callback=f"op:{oid}")
    s = await click(app, s, "Удалить")
    s = await click(app, s, "Подтвердить")
    async with app.db.transaction() as conn:
        assert await Ledger(conn).balance(1) == 1000000


async def test_initial_balance_in_statistics_and_income_report(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        second = await l.create_bank(1, "Kaspi", [], 500000, "2026-09-05")
        first = (await l.objects(1, "bank"))[0]["id"]
        salary = await l.create_category(1, "Зарплата", "income", ["зп"])
        await l.record(1, 100000, "2026-09-05", first, salary)
        await l.transfer(1, first, second, 10000, "2026-09-05")
        await l.archive(1, "bank", second)
    await send(app, "10000")
    screen = await app.handle(1, "opening-stats", callback="stats:2026-09")
    assert screen.text.count("Доходы: 16 000,00 ₸") == 2
    assert screen.text.count("Расходы: 10 000,00 ₸") == 2
    assert "Разница за месяц: 6 000,00 ₸" in screen.text
    screen = await click(app, screen, "Категории")
    screen = await click(app, screen, "Доходы")
    assert action(screen, "Начальный остаток: 15 000,00 ₸") == "ops:2026-09:opening:0"
    assert action(screen, "Зарплата: 1 000,00 ₸")
    screen = await click(app, screen, "Начальный остаток")
    assert "<b>Начальный остаток · 2026-09</b>" in screen.text
    entries = [label for row in screen.buttons for label, route in row if route.startswith("op:")]
    assert len(entries) == 2
    assert all("Начальный остаток" in label for label in entries)
    assert any("Kaspi" in label for label in entries)
    assert any("БЦЦ" in label for label in entries)
    screen = await click(app, screen, "БЦЦ")
    assert "Начальный остаток · 10 000,00 ₸" in screen.text


async def test_error_rolls_back_and_allows_retry(app):
    await onboard(app)
    await app.handle(1, "add", callback="newcat:expense")
    await send(app, "Еда")
    bad = await send(app, "бцц")
    assert "занят" in bad.text
    await send(app, "еда2")
    async with app.db.transaction() as conn:
        cats = await Ledger(conn).objects(1, "category")
        assert len([c for c in cats if c["name"] == "Еда"]) == 1


async def test_many_aliases_keep_object_and_pages_accessible(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        bank = (await l.objects(1, "bank"))[0]["id"]
        await l.add_aliases(1, "bank", bank, [f"ключ{i:03d}" + "x" * 35 for i in range(110)])
    s = await app.handle(1, "large-bank", callback=f"obj:bank:{bank}")
    assert len(s.text) < 4096
    s = await click(app, s, "Ключевые слова")
    assert len(s.text) < 4096
    s = await click(app, s, "→")
    assert len(s.text) < 4096


async def test_transfer_adjustment_and_copy_through_dialogs(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        bank = (await l.objects(1, "bank"))[0]["id"]
        second = await l.create_bank(1, "Kaspi", [], 0, "2026-09-05")
        cat = await l.create_category(1, "Еда", "expense", ["еда"])
        await l.set_rate(1, bank, cat, "2026-08", "3", False)
    s = await app.handle(1, "transfer", callback=f"transfer:{bank}")
    s = await click(app, s, "Kaspi")
    s = await send(app, "100")
    await click(app, s, "Подтвердить")
    s = await app.handle(1, "adjust", callback=f"adjust:{second}")
    s = await send(app, "80")
    await click(app, s, "Подтвердить")
    s = await send(app, "250 еда")
    assert "не настроен" in s.text
    s = await app.handle(1, "copy", callback="copy:2026-09")
    await click(app, s, "Пересчитать")
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        assert await l.balance(1) == 973000
        assert await l.balance(1, second) == 8000
        assert (await l.summary(1, "2026-09"))["expense"] == 25000
        expense = next(o for o in await l.operations(1) if o["kind"] == "expense")
        assert expense["cashback"] == 750


async def test_no_default_prompts_and_archive_blocks_new_keys(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        bank = (await l.objects(1, "bank"))[0]["id"]
        await conn.execute("UPDATE users SET default_bank=NULL WHERE id=1")
    s = await send(app, "250")
    s = await click(app, s, "БЦЦ")
    assert "9 750,00" in s.text
    s = await app.handle(1, "arc", callback=f"archive:bank:{bank}")
    await click(app, s, "Подтвердить")
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        assert await l.balance(1) == 975000
        assert (await l.user(1))["default_bank"] is None
        assert await l.aliases(1, "bank", bank) == []


async def test_negative_adjustment_receipt_has_single_sign(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        bid = (await Ledger(conn).objects(1, "bank"))[0]["id"]
    s = await app.handle(1, "adjust-neg", callback=f"adjust:{bid}")
    s = await send(app, "9980")
    s = await click(app, s, "Подтвердить")
    assert "−20,00" in s.text and "+-" not in s.text


async def test_first_bank_skips_default_question_and_becomes_default(app):
    s = await onboard(app)
    assert "БЦЦ" in s.text
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        bank = (await l.objects(1, "bank"))[0]
        assert (await l.user(1))["default_bank"] == bank["id"]


async def test_second_bank_no_answer_keeps_existing_default(app):
    await onboard(app)
    await app.handle(1, "bank2", callback="newbank")
    await send(app, "Kaspi")
    await send(app, "-")
    s = await send(app, "500")
    assert any(text == "Нет" for row in s.buttons for text, _ in row)
    await click(app, s, "Нет")
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        banks = await l.objects(1, "bank")
        assert (await l.user(1))["default_bank"] == banks[0]["id"]


async def test_unknown_word_new_bank_flow_keeps_default(app):
    await onboard(app)
    s = await send(app, "250 kaspi")
    s = await click(app, s, "Создать банк")
    await send(app, "Kaspi")
    await send(app, "-")
    s = await send(app, "100")
    s = await click(app, s, "Нет")
    assert "-150,00" in s.text
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        first = (await l.objects(1, "bank"))[0]
        assert (await l.user(1))["default_bank"] == first["id"]


async def test_operations_list_escapes_object_names(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        cid = await l.create_category(1, "Х<р>ен & co", "expense", ["хр"])
    await send(app, "250 хр")
    s = await app.handle(1, "ops", callback="ops:all:0:0")
    labels = [text for row in s.buttons for text, _ in row]
    assert any("Х<р>ен & co" in label for label in labels)
    s = await app.handle(1, "ops-cat", callback=f"ops:all:{cid}:0")
    assert "Х&lt;р&gt;ен &amp; co" in s.text


async def test_daily_reminder_settings_flow(app):
    await onboard(app)
    s = await app.handle(1, "c:settings", callback="settings")
    s = await click(app, s, "Напоминание о записях")
    assert "выключено" in s.text
    s = await click(app, s, "21:00")
    assert "21:00" in s.text
    s = await click(app, s, "Напоминание о записях")
    s = await click(app, s, "Свое время")
    s = await send(app, "25:70")
    assert "ЧЧ:ММ" in s.text
    s = await send(app, "19:45")
    assert "19:45" in s.text
    s = await click(app, s, "Напоминание о записях")
    s = await click(app, s, "Выключить")
    assert "выключено" in s.text
    async with app.db.transaction() as conn:
        assert (await Ledger(conn).user(1))["reminder_time"] is None


async def test_cashback_paynow_transfers_expected_without_questions(app):
    await onboard(app)
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        cat = await l.create_category(1, "Продукты", "expense", ["пр"])
        bank = (await l.objects(1, "bank"))[0]["id"]
        await l.set_rate(1, bank, cat, "2026-09", "3", False)
    empty = await app.handle(1, "paynow-empty", callback=f"paynow:2026-08:{bank}")
    assert "нет ожидаемого кэшбэка" in empty.text
    await send(app, "250 пр")
    s = await app.handle(1, "paynow", callback=f"paynow:2026-09:{bank}")
    assert "7,50" in s.text and "Кэшбэк" in s.text
    async with app.db.transaction() as conn:
        assert await Ledger(conn).balance(1) == 975750
    again = await app.handle(1, "paynow-2", callback=f"paynow:2026-09:{bank}")
    async with app.db.transaction() as conn:
        l = Ledger(conn)
        payouts = await l.rows("SELECT id FROM operations WHERE user_id=1 AND payout_month='2026-09' AND deleted=0")
        assert len(payouts) == 1
        assert await l.balance(1) == 975750


async def test_slash_commands_open_pages_and_cancel_dialog(app):
    await onboard(app)
    expected = {
        "/balance": "баланс",
        "/balances": "Остатки банков",
        "/stats": "Статистика",
        "/ops": "Операции",
        "/cashback": "Кэшбэк",
        "/categories": "Категории",
        "/banks": "Банки",
        "/settings": "Настройки",
    }
    for command, marker in expected.items():
        s = await send(app, command)
        assert marker in s.text, command
    s = await send(app, "/guide")
    assert "гайд" in s.text.lower()
    # Command inside an active dialog cancels it instead of being consumed as input.
    s = await app.handle(1, "c:newbank", callback="newbank")
    s = await send(app, "/stats")
    assert "Статистика" in s.text
    async with app.db.transaction() as conn:
        assert await Ledger(conn).dialog(1) is None
