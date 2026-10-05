from datetime import date
import pytest

from financebot.database import Database
from financebot.ledger import Ledger
from financebot.parsing import parse_entry, money, rate_value, ValidationError


@pytest.fixture
async def ledger(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    async with db.transaction() as conn:
        yield Ledger(conn)


async def setup(l):
    await l.ensure_user(1)
    bank = await l.create_bank(1, "БЦЦ", ["бц"], 1_000_000, "2026-09-01")
    cat = await l.create_category(1, "Продукты", "expense", ["пр", "еда"])
    return bank, cat


def test_parser_money_dates_and_unknown_tokens():
    p = parse_entry("250,50 ПР бцц\n04.09.2026", date(2026, 9, 5))
    assert (p.amount, p.words, p.day) == (25050, ["пр", "бцц"], "2026-09-04")
    assert parse_entry("250", date(2026, 9, 5)).words == []
    assert money("-10,01", signed=True) == -1001


def test_whole_percent_is_readable_not_scientific_notation():
    assert rate_value("100") == "100"
    assert rate_value("10.00") == "10"


@pytest.mark.parametrize("value", ["0", "-1", "1.001", "NaN", "250 пр\n31.02.2026", "250 пр\n06.09.2026", "1\n01.09.2026\nx"])
def test_invalid_input_is_rejected(value):
    with pytest.raises(ValidationError):
        parse_entry(value, date(2026, 9, 5))


async def test_purchase_default_and_income(ledger):
    b, c = await setup(ledger)
    await ledger.set_rate(1, b, c, "2026-09", "3", False)
    op = await ledger.record_text(1, "250 пр бцц", date(2026, 9, 5))
    assert op["cashback"] == 750
    assert await ledger.balance(1, b) == 975000
    other = await ledger.record_text(1, "50", date(2026, 9, 5))
    assert other["kind"] == "expense"
    salary = await ledger.create_category(1, "Зарплата", "income", ["зп"])
    inc = await ledger.record_text(1, "15000 зп", date(2026, 9, 5))
    assert inc["category_id"] == salary
    assert await ledger.balance(1, b) == 2470000


async def test_unknown_and_collision_cannot_silently_spend(ledger):
    await setup(ledger)
    from financebot.ledger import UnknownWords
    with pytest.raises(UnknownWords) as e:
        await ledger.record_text(1, "250 новое", date(2026, 9, 5))
    assert e.value.words == ["новое"]
    assert (await ledger.summary(1, "2026-09"))["expense"] == 0
    with pytest.raises(ValidationError):
        await ledger.create_category(1, "Еда", "expense", ["бцц"])


async def test_transfer_adjustment_and_delete(ledger):
    b, _ = await setup(ledger)
    b2 = await ledger.create_bank(1, "Kaspi", [], 0, "2026-09-01")
    transfer = await ledger.transfer(1, b, b2, 10000, "2026-09-05")
    assert await ledger.balance(1, b2) == 10000
    assert await ledger.balance(1) == 1000000
    await ledger.adjust(1, b2, 9000, "2026-09-05")
    assert await ledger.balance(1) == 999000
    assert (await ledger.summary(1, "2026-09"))["income"] == 0
    await ledger.delete_operation(1, transfer["id"])
    assert await ledger.balance(1, b2) == -1000


async def test_rate_modes_edits_and_payout(ledger):
    b, c = await setup(ledger)
    await ledger.set_rate(1, b, c, "2026-09", "3", False)
    op = await ledger.record_text(1, "250 пр", date(2026, 9, 5))
    await ledger.set_rate(1, b, c, "2026-09", "5", False)
    op = await ledger.edit_operation(1, op["id"], amount=50000)
    assert op["cashback"] == 1500
    pay = await ledger.payout(1, b, "2026-09", 1200, "2026-09-05")
    duplicate = await ledger.payout(1, b, "2026-09", 1200, "2026-09-05")
    assert duplicate["id"] == pay["id"]
    await ledger.set_rate(1, b, c, "2026-09", "5", True)
    assert (await ledger.operation(1, op["id"]))["cashback"] == 2500
    assert (await ledger.operation(1, pay["id"]))["amount"] == 1200
    assert await ledger.balance(1) == 951200
    await ledger.edit_operation(1, pay["id"], amount=1300)
    assert await ledger.balance(1) == 951300
    await ledger.delete_operation(1, pay["id"])
    assert await ledger.balance(1) == 950000
    await ledger.delete_operation(1, op["id"])
    assert await ledger.balance(1) == 1000000


async def test_isolation_archive_history_csv(ledger):
    b, c = await setup(ledger)
    await ledger.ensure_user(2)
    op = await ledger.record_text(1, "250 пр", date(2026, 9, 5))
    for action in [ledger.operation(2, op["id"]), ledger.delete_operation(2, op["id"]), ledger.set_default(2, b), ledger.archive(2, "bank", b)]:
        with pytest.raises(ValidationError):
            await action
    await ledger.archive(1, "category", c)
    assert (await ledger.summary(1, "2026-09"))["expense"] == 25000
    csv = (await ledger.export_csv(1, "2026-09")).decode("utf-8-sig")
    assert "Продукты" in csv and "250,00" in csv and "БЦЦ" in csv
    assert "Продукты" not in (await ledger.export_csv(2, "2026-09")).decode("utf-8-sig")


async def test_month_change_reprices_and_rounds(ledger):
    b, c = await setup(ledger)
    await ledger.set_rate(1, b, c, "2026-08", "3", False)
    op = await ledger.record_text(1, "0.50 пр\n31.08.2026", date(2026, 9, 5))
    assert op["cashback"] == 2
    op = await ledger.edit_operation(1, op["id"], day="2026-09-01")
    assert op["cashback"] == 0 and op["rate"] is None
    assert (await ledger.summary(1, "2026-08"))["expense"] == 0
    assert (await ledger.summary(1, "2026-09"))["expense"] == 50


async def test_edit_bank_category_and_income_reverses_correct_balances(ledger):
    b, c = await setup(ledger)
    second = await ledger.create_bank(1, "Kaspi", [], 0, "2026-09-01")
    income = await ledger.create_category(1, "Зарплата", "income", ["зп"])
    await ledger.set_rate(1, b, c, "2026-09", "3", False)
    await ledger.set_rate(1, second, c, "2026-09", "5", False)
    op = await ledger.record_text(1, "250 пр", date(2026, 9, 5))
    op = await ledger.edit_operation(1, op["id"], bank_id=second)
    assert await ledger.balance(1, b) == 1000000
    assert await ledger.balance(1, second) == -25000
    assert op["cashback"] == 1250
    op = await ledger.edit_operation(1, op["id"], category_id=income)
    assert op["kind"] == "income" and op["cashback"] == 0
    assert await ledger.balance(1, second) == 25000
    assert (await ledger.summary(1, "2026-09"))["expense"] == 0


async def test_alias_rename_remove_and_multiword_name(ledger):
    b, _ = await setup(ledger)
    await ledger.rename(1, "bank", b, "Банк ЦентрКредит")
    op = await ledger.record_text(1, "250 ПР БАНК ЦЕНТРКРЕДИТ", date(2026, 9, 5))
    assert op["bank_id"] == b
    await ledger.add_aliases(1, "bank", b, ["bcc"])
    await ledger.remove_alias(1, "bank", b, "bcc")
    assert "bcc" not in await ledger.aliases(1, "bank", b)
    assert "бцц" not in await ledger.aliases(1, "bank", b)


async def test_csv_signed_correction_and_formula_protection(ledger):
    b, _ = await setup(ledger)
    await ledger.rename(1, "bank", b, "=Bank")
    await ledger.adjust(1, b, 999950, "2026-09-05")
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO((await ledger.export_csv(1, "2026-09")).decode("utf-8-sig")), delimiter=";"))
    assert rows[0]["Сумма KZT"] == "-0,50"
    assert rows[0]["Банк"] == "'=Bank"


async def test_balances_match_per_bank_balance(ledger):
    b, c = await setup(ledger)
    b2 = await ledger.create_bank(1, "Kaspi", [], 500, "2026-09-01")
    await ledger.record_text(1, "250 пр бцц", date(2026, 9, 5))
    transfer = await ledger.transfer(1, b, b2, 10000, "2026-09-05")
    totals = await ledger.balances(1)
    assert totals[b] == await ledger.balance(1, b) == 965000
    assert totals[b2] == await ledger.balance(1, b2) == 10500
    assert sum(totals.values()) == await ledger.balance(1)
    await ledger.delete_operation(1, transfer["id"])
    await ledger.archive(1, "bank", b2)
    totals = await ledger.balances(1)
    assert totals[b2] == await ledger.balance(1, b2) == 500
