"""Input validation and exact monetary arithmetic (integer tiyn)."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import re


class ValidationError(ValueError):
    pass


def normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def money(value: str, *, signed: bool = False) -> int:
    value = value.strip().replace(",", ".")
    pattern = r"-?\d{1,12}(?:\.\d{1,2})?" if signed else r"\d{1,12}(?:\.\d{1,2})?"
    if not re.fullmatch(pattern, value):
        raise ValidationError("Введите сумму цифрами, максимум два знака после запятой.")
    result = int(Decimal(value) * 100)
    if not signed and result <= 0:
        raise ValidationError("Сумма должна быть больше нуля.")
    return result


def rate_value(value: str) -> str:
    value = value.strip().replace(",", ".").removesuffix("%").strip()
    if not re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?", value) or not 0 <= Decimal(value) <= 100:
        raise ValidationError("Введите процент от 0 до 100, максимум два знака после запятой.")
    return format(Decimal(value).normalize(), "f")


def cashback(amount: int, rate: str | None) -> int:
    return int((Decimal(amount) * Decimal(rate or "0") / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt(amount: int) -> str:
    return f"{Decimal(amount) / 100:,.2f}".replace(",", " ").replace(".", ",") + " ₸"


def valid_day(value: str, today: date | None = None) -> str:
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise ValidationError("Неверная дата.") from None
    if today and d > today:
        raise ValidationError("Дата не может быть в будущем.")
    return d.isoformat()


def input_day(value: str, today: date) -> str:
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value.strip()):
        raise ValidationError("Введите дату в формате ДД.ММ.ГГГГ.")
    d, m, y = value.strip().split(".")
    return valid_day(f"{y}-{m}-{d}", today)


def month_value(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValidationError("Неверный месяц.")
    valid_day(value + "-01")
    return value


def shift_month(value: str, delta: int) -> str:
    year, month = map(int, month_value(value).split("-"))
    n = year * 12 + month - 1 + delta
    return month_value(f"{n // 12:04d}-{n % 12 + 1:02d}")


@dataclass
class Entry:
    amount: int
    words: list[str]
    day: str


def parse_entry(text: str, today: date) -> Entry:
    lines = text.strip().splitlines()
    if not lines or len(lines) > 2 or not lines[0].split():
        raise ValidationError("Пример: 250 пр бцц. Дата — второй строкой: 05.09.2026.")
    amount, *words = lines[0].split()
    return Entry(money(amount), [normalize(w) for w in words], input_day(lines[1], today) if len(lines) == 2 else today.isoformat())
