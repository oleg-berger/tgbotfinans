"""Financial rules. Caller owns the transaction; no Telegram dependencies."""
import csv
import io
import json
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .parsing import ValidationError, normalize, parse_entry, cashback, rate_value, valid_day, month_value


class UnknownWords(ValidationError):
    def __init__(self, words):
        self.words = words
        super().__init__("Неизвестные слова: " + ", ".join(words))


class Ledger:
    def __init__(self, conn):
        self.conn = conn

    async def rows(self, sql, params=()):
        return [dict(r) for r in await (await self.conn.execute(sql, params)).fetchall()]

    async def one(self, sql, params=()):
        rows = await self.rows(sql, params)
        return rows[0] if rows else None

    async def ensure_user(self, uid):
        await self.conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (uid,))
        for name, kind, system in [("Прочее", "expense", "other"), ("Кэшбэк", "income", "cashback")]:
            if not await self.one("SELECT id FROM categories WHERE user_id=? AND system=?", (uid, system)):
                await self.create_category(uid, name, kind, [], system=system)

    async def user(self, uid):
        user = await self.one("SELECT * FROM users WHERE id=?", (uid,))
        if not user:
            raise ValidationError("Сначала отправьте /start.")
        return user

    async def set_timezone(self, uid, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("Неизвестный часовой пояс. Пример: Asia/Qyzylorda или Europe/Moscow.") from None
        await self.conn.execute("UPDATE users SET timezone=? WHERE id=?", (value, uid))

    async def set_reminder(self, uid, value):
        await self.conn.execute("UPDATE users SET reminder_time=? WHERE id=?", (value, uid))

    async def complete_guide(self, uid):
        await self.conn.execute("UPDATE users SET guide_completed=1 WHERE id=?", (uid,))

    async def mark_cashback_intro(self, uid, month):
        month_value(month)
        await self.conn.execute("UPDATE users SET cashback_intro_shown=1 WHERE id=?", (uid,))
        # The welcome offer replaces the catch-up reminder for this month.
        await self.conn.execute("INSERT INTO reminders(user_id,month,delivered) VALUES(?,?,1) ON CONFLICT(user_id,month) DO UPDATE SET delivered=1", (uid, month))

    async def object(self, uid, kind, oid, active=False):
        table = {"bank": "banks", "category": "categories"}.get(kind)
        if not table:
            raise ValidationError("Неизвестный объект.")
        row = await self.one(f"SELECT * FROM {table} WHERE user_id=? AND id=?", (uid, oid))
        if not row or (active and row["archived"]):
            raise ValidationError("Объект недоступен или находится в архиве.")
        return row

    async def objects(self, uid, kind, *, include_archived=False):
        table = {"bank": "banks", "category": "categories"}[kind]
        return await self.rows(f"SELECT * FROM {table} WHERE user_id=?" + ("" if include_archived else " AND archived=0") + " ORDER BY id", (uid,))

    async def check_aliases(self, uid, words, kind=None, oid=None):
        cleaned = list(dict.fromkeys(normalize(w) for w in words))
        for word in cleaned:
            if not word or len(word) > 48 or "\n" in word or word.startswith("/") or word.replace(".", "").isdigit():
                raise ValidationError("Ключ должен содержать буквы и быть не длиннее 48 символов.")
            existing = await self.one("SELECT * FROM aliases WHERE user_id=? AND word=?", (uid, word))
            if existing and (kind is None or existing[kind + "_id"] != oid):
                raise ValidationError(f"Ключ «{word}» уже занят.")
        return cleaned

    async def add_aliases(self, uid, kind, oid, words):
        await self.object(uid, kind, oid, active=True)
        cleaned = await self.check_aliases(uid, words, kind, oid)
        for word in cleaned:
            await self.conn.execute(f"INSERT OR IGNORE INTO aliases(user_id,word,{kind}_id) VALUES(?,?,?)", (uid, word, oid))

    async def aliases(self, uid, kind, oid):
        await self.object(uid, kind, oid)
        return [r["word"] for r in await self.rows(f"SELECT word FROM aliases WHERE user_id=? AND {kind}_id=? ORDER BY word", (uid, oid))]

    async def remove_alias(self, uid, kind, oid, word):
        obj = await self.object(uid, kind, oid, active=True)
        if normalize(word) == normalize(obj["name"]):
            raise ValidationError("Название используется как ключ; для его изменения переименуйте объект.")
        await self.conn.execute(f"DELETE FROM aliases WHERE user_id=? AND {kind}_id=? AND word=?", (uid, oid, normalize(word)))

    async def create_category(self, uid, name, kind, aliases, system=None):
        if kind not in ("expense", "income"):
            raise ValidationError("Выберите доход или расход.")
        name = self.clean_name(name)
        words = await self.check_aliases(uid, [name, *aliases])
        cur = await self.conn.execute("INSERT INTO categories(user_id,name,kind,system) VALUES(?,?,?,?)", (uid, name, kind, system))
        await self.add_aliases(uid, "category", cur.lastrowid, words)
        return cur.lastrowid

    @staticmethod
    def clean_name(name):
        name = " ".join(name.split())
        if not name or len(name) > 48:
            raise ValidationError("Название должно содержать от 1 до 48 символов.")
        return name

    async def create_bank(self, uid, name, aliases, opening, day):
        name = self.clean_name(name)
        words = await self.check_aliases(uid, [name, *aliases])
        valid_day(day)
        self.check_amount(opening, signed=True)
        cur = await self.conn.execute("INSERT INTO banks(user_id,name) VALUES(?,?)", (uid, name))
        bid = cur.lastrowid
        await self.add_aliases(uid, "bank", bid, words)
        await self.insert_op(uid, "opening", opening, day, bid)
        user = await self.user(uid)
        if not user["default_bank"]:
            await self.set_default(uid, bid)
        return bid

    async def rename(self, uid, kind, oid, name):
        obj = await self.object(uid, kind, oid, active=True)
        name = self.clean_name(name)
        await self.check_aliases(uid, [name], kind, oid)
        await self.conn.execute(f"DELETE FROM aliases WHERE user_id=? AND {kind}_id=? AND word=?", (uid, oid, normalize(obj["name"])))
        table = {"bank": "banks", "category": "categories"}[kind]
        await self.conn.execute(f"UPDATE {table} SET name=? WHERE id=? AND user_id=?", (name, oid, uid))
        await self.add_aliases(uid, kind, oid, [name])

    async def set_default(self, uid, bid):
        await self.object(uid, "bank", bid, active=True)
        await self.conn.execute("UPDATE users SET default_bank=? WHERE id=?", (bid, uid))

    async def archive(self, uid, kind, oid):
        obj = await self.object(uid, kind, oid)
        if kind == "category" and obj["system"]:
            raise ValidationError("Системную категорию нельзя архивировать.")
        table = {"bank": "banks", "category": "categories"}[kind]
        await self.conn.execute(f"UPDATE {table} SET archived=1 WHERE id=? AND user_id=?", (oid, uid))
        await self.conn.execute(f"DELETE FROM aliases WHERE user_id=? AND {kind}_id=?", (uid, oid))
        if kind == "bank":
            await self.conn.execute("UPDATE users SET default_bank=NULL WHERE id=? AND default_bank=?", (uid, oid))

    @staticmethod
    def check_amount(amount, signed=False):
        if type(amount) is not int or abs(amount) > 99_999_999_999_999 or (not signed and amount <= 0):
            raise ValidationError("Недопустимая сумма.")

    async def insert_op(self, uid, kind, amount, day, bank, category=None, target=None, rate=None, payout_month=None):
        valid_day(day)
        self.check_amount(amount, signed=kind in ("opening", "adjustment"))
        cur = await self.conn.execute("INSERT INTO operations(user_id,kind,amount,day,bank_id,category_id,target_bank,rate,cashback,payout_month) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid, kind, amount, day, bank, category, target, rate, cashback(amount, rate) if kind == "expense" else 0, payout_month))
        return await self.operation(uid, cur.lastrowid)

    async def operation(self, uid, oid):
        op = await self.one("SELECT o.*, b.name bank_name, c.name category_name, t.name target_name FROM operations o JOIN banks b ON b.id=o.bank_id LEFT JOIN categories c ON c.id=o.category_id LEFT JOIN banks t ON t.id=o.target_bank WHERE o.user_id=? AND o.id=? AND o.deleted=0", (uid, oid))
        if not op:
            raise ValidationError("Операция недоступна или удалена.")
        return op

    async def resolve(self, uid, words):
        aliases = await self.rows("SELECT * FROM aliases WHERE user_id=?", (uid,))
        mapping = {a["word"]: a for a in aliases}
        banks, cats, unknown = set(), set(), []
        i = 0
        while i < len(words):
            match = None
            for end in range(len(words), i, -1):
                key = " ".join(words[i:end])
                if key in mapping:
                    match = mapping[key]
                    i = end
                    break
            if match:
                (banks if match["bank_id"] else cats).add(match["bank_id"] or match["category_id"])
            else:
                unknown.append(words[i])
                i += 1
        if unknown:
            raise UnknownWords(list(dict.fromkeys(unknown)))
        if len(banks) > 1 or len(cats) > 1:
            raise ValidationError("В одной записи укажите только один банк и одну категорию.")
        return next(iter(banks), None), next(iter(cats), None)

    async def record_text(self, uid, text, today: date):
        entry = parse_entry(text, today)
        bank, cat = await self.resolve(uid, entry.words)
        bank = bank or (await self.user(uid))["default_bank"]
        if not bank:
            raise ValidationError("Сначала выберите банк по умолчанию в разделе «Банки».")
        if not cat:
            cat = (await self.one("SELECT id FROM categories WHERE user_id=? AND system='other'", (uid,)))["id"]
        return await self.record(uid, entry.amount, entry.day, bank, cat)

    async def record(self, uid, amount, day, bank, cat):
        await self.object(uid, "bank", bank, active=True)
        category = await self.object(uid, "category", cat, active=True)
        rate = await self.get_rate(uid, bank, cat, day[:7]) if category["kind"] == "expense" else None
        return await self.insert_op(uid, category["kind"], amount, day, bank, cat, rate=rate)

    async def balance(self, uid, bid=None):
        if bid is not None:
            await self.object(uid, "bank", bid)
        ops = await self.rows("SELECT * FROM operations WHERE user_id=? AND deleted=0", (uid,))
        total = 0
        for op in ops:
            if bid is None or op["bank_id"] == bid:
                total += -op["amount"] if op["kind"] in ("expense", "transfer") else op["amount"]
            if op["kind"] == "transfer" and (bid is None or op["target_bank"] == bid):
                total += op["amount"]
        return total

    async def balances(self, uid):
        # Aggregate integers in Python: Cloudflare SQL cursors cross JavaScript's
        # number boundary, whereas a bank total may exceed 2**53.
        totals = {}
        for op in await self.rows("SELECT bank_id,target_bank,kind,amount FROM operations WHERE user_id=? AND deleted=0", (uid,)):
            bid = op["bank_id"]
            totals[bid] = totals.get(bid, 0) + (-op["amount"] if op["kind"] in ("expense", "transfer") else op["amount"])
            if op["kind"] == "transfer":
                target = op["target_bank"]
                totals[target] = totals.get(target, 0) + op["amount"]
        return totals

    async def transfer(self, uid, source, target, amount, day):
        await self.object(uid, "bank", source, active=True)
        await self.object(uid, "bank", target, active=True)
        if source == target:
            raise ValidationError("Выберите другой банк для перевода.")
        return await self.insert_op(uid, "transfer", amount, day, source, target=target)

    async def adjust(self, uid, bank, actual, day):
        await self.object(uid, "bank", bank, active=True)
        self.check_amount(actual, signed=True)
        difference = actual - await self.balance(uid, bank)
        return await self.insert_op(uid, "adjustment", difference, day, bank)

    async def edit_operation(self, uid, oid, **changes):
        op = await self.operation(uid, oid)
        if set(changes) - {"amount", "day", "bank_id", "category_id"}:
            raise ValidationError("Недопустимое поле.")
        updated = op | changes
        valid_day(updated["day"])
        self.check_amount(updated["amount"], signed=op["kind"] in ("opening", "adjustment"))
        if op["payout_month"] and ("bank_id" in changes or "category_id" in changes):
            raise ValidationError("У выплаты можно менять сумму и дату. Для другого банка удалите выплату и создайте заново.")
        if "bank_id" in changes:
            if op["kind"] not in ("income", "expense"):
                raise ValidationError("Для смены банка этой операции удалите ее и создайте заново.")
            await self.object(uid, "bank", updated["bank_id"], active=True)
        if "category_id" in changes:
            if op["kind"] not in ("income", "expense"):
                raise ValidationError("У этой операции нет категории.")
            cat = await self.object(uid, "category", updated["category_id"], active=True)
            updated["kind"] = cat["kind"]
        reprice = any(updated[k] != op[k] for k in ("bank_id", "category_id")) or updated["day"][:7] != op["day"][:7]
        rate = op["rate"]
        if updated["kind"] != "expense":
            rate = None
        elif reprice:
            rate = await self.get_rate(uid, updated["bank_id"], updated["category_id"], updated["day"][:7])
        await self.conn.execute("UPDATE operations SET amount=?,day=?,bank_id=?,category_id=?,kind=?,rate=?,cashback=? WHERE user_id=? AND id=?", (updated["amount"], updated["day"], updated["bank_id"], updated["category_id"], updated["kind"], rate, cashback(updated["amount"], rate), uid, oid))
        return await self.operation(uid, oid)

    async def delete_operation(self, uid, oid):
        await self.operation(uid, oid)
        await self.conn.execute("UPDATE operations SET deleted=1 WHERE user_id=? AND id=?", (uid, oid))

    async def get_rate(self, uid, bank, cat, month):
        row = await self.one("SELECT rate FROM rates WHERE user_id=? AND bank_id=? AND category_id=? AND month=?", (uid, bank, cat, month))
        return row["rate"] if row else None

    async def set_rate(self, uid, bank, cat, month, rate, recalculate):
        await self.object(uid, "bank", bank, active=True)
        category = await self.object(uid, "category", cat, active=True)
        if category["kind"] != "expense":
            raise ValidationError("Кэшбэк настраивается только для расходов.")
        month_value(month)
        rate = rate_value(rate)
        await self.conn.execute("INSERT INTO rates VALUES(?,?,?,?,?) ON CONFLICT(user_id,bank_id,category_id,month) DO UPDATE SET rate=excluded.rate", (uid, bank, cat, month, rate))
        if recalculate:
            ops = await self.rows("SELECT id,amount FROM operations WHERE user_id=? AND bank_id=? AND category_id=? AND substr(day,1,7)=? AND kind='expense' AND deleted=0", (uid, bank, cat, month))
            for op in ops:
                await self.conn.execute("UPDATE operations SET rate=?,cashback=? WHERE id=? AND user_id=?", (rate, cashback(op["amount"], rate), op["id"], uid))

    async def copy_rates(self, uid, source_month, target_month, recalculate):
        month_value(source_month)
        month_value(target_month)
        rows = await self.rows("SELECT r.* FROM rates r JOIN banks b ON b.id=r.bank_id JOIN categories c ON c.id=r.category_id WHERE r.user_id=? AND r.month=? AND b.archived=0 AND c.archived=0", (uid, source_month))
        for r in rows:
            await self.set_rate(uid, r["bank_id"], r["category_id"], target_month, r["rate"], recalculate)
        return len(rows)

    async def payout(self, uid, bank, month, amount, day):
        await self.object(uid, "bank", bank, active=True)
        month_value(month)
        existing = await self.one("SELECT id FROM operations WHERE user_id=? AND bank_id=? AND payout_month=? AND deleted=0", (uid, bank, month))
        if existing:
            return await self.operation(uid, existing["id"])
        cat = await self.one("SELECT id FROM categories WHERE user_id=? AND system='cashback'", (uid,))
        return await self.insert_op(uid, "income", amount, day, bank, cat["id"], payout_month=month)

    async def operations(self, uid, month=None, category=None, *, kind=None):
        if month:
            month_value(month)
        if category:
            await self.object(uid, "category", category)
        if kind is not None and kind not in ("income", "expense", "opening", "transfer", "adjustment"):
            raise ValidationError("Неизвестный тип операции.")
        return await self.rows("SELECT o.*,b.name bank_name,c.name category_name,t.name target_name FROM operations o JOIN banks b ON b.id=o.bank_id LEFT JOIN categories c ON c.id=o.category_id LEFT JOIN banks t ON t.id=o.target_bank WHERE o.user_id=? AND o.deleted=0" + (" AND substr(o.day,1,7)=?" if month else "") + (" AND o.category_id=?" if category else "") + (" AND o.kind=?" if kind else "") + " ORDER BY o.day DESC,o.id DESC", (uid, *([month] if month else []), *([category] if category else []), *([kind] if kind else [])))

    async def summary(self, uid, month=None):
        ops = await self.operations(uid, month)
        return {"income": sum(o["amount"] for o in ops if o["kind"] in ("income", "opening")), "expense": sum(o["amount"] for o in ops if o["kind"] == "expense"), "budget": await self.balance(uid)}

    async def export_csv(self, uid, month):
        out = io.StringIO(newline="")
        writer = csv.writer(out, delimiter=";")
        writer.writerow(["Дата", "Тип", "Сумма KZT", "Банк", "Категория", "Ожидаемый кэшбэк KZT", "Процент", "Банк назначения", "Месяц выплаты"])
        names = {"income": "Доход", "expense": "Расход", "transfer": "Перевод", "opening": "Начальный остаток", "adjustment": "Корректировка"}
        def safe(value):
            value = str(value or "")
            return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
        def decimal(value):
            return f"{value // 100}.{value % 100:02d}" if value >= 0 else "-" + decimal(-value)
        for o in await self.operations(uid, month):
            writer.writerow([o["day"], names[o["kind"]], decimal(o["amount"]).replace(".", ","), safe(o["bank_name"]), safe(o["category_name"]), decimal(o["cashback"]).replace(".", ","), o["rate"] or "", safe(o["target_name"]), o["payout_month"] or ""])
        return out.getvalue().encode("utf-8-sig")

    async def dialog(self, uid):
        row = await self.one("SELECT payload FROM dialogs WHERE user_id=?", (uid,))
        return json.loads(row["payload"]) if row else None

    async def save_dialog(self, uid, payload):
        if payload is None:
            await self.conn.execute("DELETE FROM dialogs WHERE user_id=?", (uid,))
        else:
            await self.conn.execute("INSERT INTO dialogs VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload", (uid, json.dumps(payload, ensure_ascii=False)))
