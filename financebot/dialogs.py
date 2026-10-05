"""Persistent, nonce-protected conversations and input handling."""
import re
import uuid
from html import escape as e

from .ledger import UnknownWords
from .guide import GUIDE_PAGES, welcome_screen, guide_screen
from .parsing import ValidationError, money, input_day, rate_value, parse_entry, fmt, shift_month, month_value
from .screens import Screen, buttons, pages
from .views import Views


def alias_input(text):
    return [] if text.strip() == "-" else [s.strip() for s in text.split(",") if s.strip()]


class Session(Views):
    async def begin(self, step, **data):
        state = {"step": step, "token": uuid.uuid4().hex[:12], "data": data}
        await self.l.save_dialog(self.uid, state)
        return await self.prompt(state)

    async def advance(self, state, step, **changes):
        return await self.begin(step, **(state["data"] | changes))

    async def prompt(self, state):
        step, data, token = state["step"], state["data"], state["token"]
        if step == "welcome":
            return welcome_screen(token)
        if step == "guide":
            return guide_screen(data["page"], token)
        def choice(label, value):
            return (label, f"d:{token}:{value}")
        texts = {
            "bank_name": "🏦 Введите название банка.",
            "bank_aliases": "✨ Введите ключи банка через запятую (например: бц, bcc). Или «-», чтобы пропустить.",
            "bank_opening": "💰 Введите текущий остаток банка. Можно 0 или отрицательную сумму.",
            "bank_default": "Сделать этот банк банком по умолчанию?",
            "cat_name": "🗂 Введите название категории.",
            "cat_aliases": "✨ Введите ключи категории через запятую. Или «-», чтобы пропустить.",
            "unknown": f"Неизвестное слово: <b>{e(data.get('word', ''))}</b>. К чему его привязать? Операция еще не записана.",
            "unknown_kind": "Новая категория — доход или расход?",
            "category_kind": "🗂 Создадим категорию. Что будем учитывать: расходы или доходы?",
            "unknown_bind": "Выберите, к чему привязать слово.",
            "entry_bank": "Выберите банк по умолчанию для этой и следующих записей.",
            "rename": "Введите новое название.",
            "aliasadd": "Введите новые ключи через запятую.",
            "aliasremove": "Введите ключ, который нужно убрать.",
            "timezone": "Введите часовой пояс, например Asia/Qyzylorda или Europe/Moscow.",
            "reminder_time": "Введите время ежедневного напоминания в формате ЧЧ:ММ, например 21:30.",
            "transfer_target": "Выберите банк назначения.",
            "transfer_amount": "Введите сумму перевода.",
            "adjust_amount": "Введите фактический остаток банка. Бот запишет разницу как корректировку.",
            "ratepick": "Выберите категорию расходов.",
            "rate_value": "Введите процент кэшбэка от 0 до 100.",
            "rate_confirm": "Пересчитать уже внесенные покупки за выбранный месяц? Подтвержденные выплаты не изменятся.",
            "copy_confirm": "Скопировать ставки предыдущего месяца? Совпадающие ставки будут заменены. Пересчитать покупки за выбранный месяц?",
            "payout_amount": "Введите фактически полученную сумму кэшбэка.",
            "payout_day": "Введите дату зачисления: ДД.ММ.ГГГГ.",
            "edit_amount": "Введите новую сумму. Для корректировки и начального остатка можно 0 или отрицательное число.",
            "edit_day": "Введите новую дату: ДД.ММ.ГГГГ.",
            "edit_bank_id": "Выберите новый банк.",
            "edit_category_id": "Выберите новую категорию (она определяет доход или расход).",
            "confirm": data.get("question", "Подтвердить действие?"),
        }
        text = texts.get(step, "Выберите действие.")
        rows = []
        if step == "bank_default":
            rows = [[choice("Да", "yes"), choice("Нет", "no")]]
        elif step == "unknown":
            rows = buttons([choice("Привязать к категории", "category"), choice("Привязать к банку", "bank"), choice("Создать категорию", "newcat"), choice("Создать банк", "newbank")])
        elif step in ("unknown_kind", "category_kind"):
            rows = [[choice("− Расход", "expense"), choice("+ Доход", "income")]]
        elif step in ("rate_confirm", "copy_confirm"):
            rows = buttons([choice("Пересчитать", "yes"), choice("Только для новых записей", "no")])
        elif step == "confirm":
            rows = [[choice("Подтвердить", "yes")]]
        elif step in ("unknown_bind", "entry_bank", "transfer_target", "ratepick", "edit_bank_id", "edit_category_id"):
            kind = data.get("kind") if step == "unknown_bind" else ("category" if step in ("ratepick", "edit_category_id") else "bank")
            objs = await self.l.objects(self.uid, kind)
            if step == "ratepick":
                objs = [o for o in objs if o["kind"] == "expense"]
            if step == "transfer_target":
                objs = [o for o in objs if o["id"] != data["bank"]]
            items = [choice(o["name"], str(o["id"])) for o in objs]
            rows = pages(items, data.get("page", 0), f"d:{token}:page")
            if not objs:
                text += "\nСписок пуст. Создайте банк или категорию через меню."
        rows.append([("Отмена / Меню", "menu")])
        return Screen(text, rows)

    async def text(self, text):
        command = text.split("@")[0].lower()
        if command in ("/start", "/menu", "/cancel"):
            await self.l.save_dialog(self.uid, None)
            if command == "/start":
                return await self.begin("welcome")
            return self.menu()
        if command == "/help":
            return await self.view("help")
        pages = {
            "/balance": "balance",
            "/balances": "balances:0",
            "/stats": f"stats:{self.month}",
            "/ops": "ops:all:0:0",
            "/cashback": f"cash:{self.month}:0",
            "/categories": "categories",
            "/banks": "banks:0",
            "/settings": "settings",
        }
        if command == "/guide":
            await self.l.save_dialog(self.uid, None)
            return await self.begin("welcome")
        if command in pages:
            await self.l.save_dialog(self.uid, None)
            return await self.view(pages[command])
        state = await self.l.dialog(self.uid)
        if state:
            return await self.consume_text(state, text)
        if not await self.l.objects(self.uid, "bank"):
            return await self.begin("bank_name")
        return await self.record_pending(text, self.today.isoformat())

    async def record_pending(self, text, today):
        from datetime import date
        try:
            entry = parse_entry(text, date.fromisoformat(today))
            bank, _ = await self.l.resolve(self.uid, entry.words)
            if not bank and not (await self.l.user(self.uid))["default_bank"]:
                return await self.begin("entry_bank", pending=text, original_today=today)
            op = await self.l.record_text(self.uid, text, date.fromisoformat(today))
        except UnknownWords as exc:
            return await self.begin("unknown", word=exc.words[0], pending=text, original_today=today)
        await self.l.save_dialog(self.uid, None)
        return await self.receipt(op)

    async def finish_creation(self, data, kind, oid):
        if data.get("pending"):
            await self.l.add_aliases(self.uid, kind, oid, [data["word"]])
            screen = await self.record_pending(data["pending"], data["original_today"])
            if kind == "bank" and not await self.l.dialog(self.uid):
                intro = await self.cashback_intro(oid)
                if intro:
                    screen.text += "\n\n" + intro.text
                    screen.buttons += intro.buttons
            return screen
        await self.l.save_dialog(self.uid, None)
        if kind == "category":
            obj = await self.l.object(self.uid, kind, oid)
            screen = self.menu()
            screen.text = f"✅ Категория «{e(obj['name'])}» создана.\n\n" + screen.text
            if not await self.l.objects(self.uid, "bank"):
                screen.text += "\n\n🏦 Перед первой записью добавь банк и его текущий остаток в разделе «Банки»."
            return screen
        if kind == "bank":
            intro = await self.cashback_intro(oid)
            if intro:
                return intro
        return await self.view(f"obj:{kind}:{oid}")

    async def cashback_intro(self, bank):
        user = await self.l.user(self.uid)
        if not user["guide_completed"] or user["cashback_intro_shown"]:
            return None
        await self.l.object(self.uid, "bank", bank, active=True)
        await self.l.mark_cashback_intro(self.uid, self.month)
        return Screen("✅ Банк готов!\n\n🪙 Теперь настрой первый кэшбэк: выбери категорию расходов и укажи процент на этот месяц. Можно начать с «Прочее» или создать свою категорию через меню.\n\nПервого числа каждого следующего месяца я напомню обновить ставки.", buttons([
            ("🪙 Настроить первый кэшбэк", f"ratepick:{self.month}:{bank}"),
            ("🗂 Категории", "categories"), ("Позже / Меню", "menu")]))

    async def consume_text(self, state, text):
        step, d = state["step"], state["data"]
        if step in ("bank_name", "cat_name"):
            name = self.l.clean_name(text)
            await self.l.check_aliases(self.uid, [name])
            return await self.advance(state, "bank_aliases" if step == "bank_name" else "cat_aliases", name=name)
        if step == "bank_aliases":
            aliases = alias_input(text)
            await self.l.check_aliases(self.uid, [d["name"], *aliases, *([d["word"]] if d.get("pending") else [])])
            return await self.advance(state, "bank_opening", aliases=aliases)
        if step == "bank_opening":
            opening = money(text, signed=True)
            if not await self.l.objects(self.uid, "bank"):
                oid = await self.l.create_bank(self.uid, d["name"], d["aliases"] + ([d["word"]] if d.get("pending") else []), opening, self.today.isoformat())
                return await self.finish_creation(d, "bank", oid)
            return await self.advance(state, "bank_default", opening=opening)
        if step == "cat_aliases":
            aliases = alias_input(text) + ([d["word"]] if d.get("pending") else [])
            oid = await self.l.create_category(self.uid, d["name"], d["category_kind"], aliases)
            return await self.finish_creation(d, "category", oid)
        if step in ("rename", "aliasadd", "aliasremove"):
            if step == "rename":
                await self.l.rename(self.uid, d["kind"], d["oid"], text)
            elif step == "aliasadd":
                await self.l.add_aliases(self.uid, d["kind"], d["oid"], alias_input(text))
            else:
                await self.l.remove_alias(self.uid, d["kind"], d["oid"], text)
            await self.l.save_dialog(self.uid, None)
            return await self.view(f"obj:{d['kind']}:{d['oid']}")
        if step == "timezone":
            await self.l.set_timezone(self.uid, text.strip())
            await self.l.save_dialog(self.uid, None)
            return await self.view("settings")
        if step == "reminder_time":
            value = text.strip()
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
                raise ValidationError("Введите время в формате ЧЧ:ММ, например 21:30.")
            await self.l.set_reminder(self.uid, value)
            await self.l.save_dialog(self.uid, None)
            return await self.view("settings")
        if step == "transfer_amount":
            amount = money(text)
            source = await self.l.object(self.uid, "bank", d["bank"], active=True)
            target = await self.l.object(self.uid, "bank", d["target"], active=True)
            return await self.advance(state, "confirm", operation="transfer", amount=amount, question=f"Перевести {fmt(amount)} из {e(source['name'])} в {e(target['name'])}?")
        if step == "adjust_amount":
            amount = money(text, signed=True)
            difference = amount - await self.l.balance(self.uid, d["bank"])
            return await self.advance(state, "confirm", operation="adjust", amount=amount, question=f"Установить остаток {fmt(amount)}? Корректировка: {fmt(difference)}.")
        if step == "rate_value":
            return await self.advance(state, "rate_confirm", rate=rate_value(text))
        if step == "payout_amount":
            return await self.advance(state, "payout_day", amount=money(text))
        if step == "payout_day":
            day = input_day(text, self.today)
            return await self.advance(state, "confirm", operation="payout", day=day, question=f"Зачислить {fmt(d['amount'])} кэшбэка за {d['month']}? Дата: {day}.")
        if step in ("edit_amount", "edit_day"):
            op = await self.l.operation(self.uid, d["oid"])
            field = step.removeprefix("edit_")
            value = money(text, signed=op["kind"] in ("opening", "adjustment")) if field == "amount" else input_day(text, self.today)
            await self.l.edit_operation(self.uid, d["oid"], **{field: value})
            await self.l.save_dialog(self.uid, None)
            return await self.view(f"op:{d['oid']}")
        raise ValidationError("Выберите вариант кнопкой или нажмите «Отмена / Меню».")

    async def callback(self, route):
        p = route.split(":")
        cmd = p[0]
        if cmd == "guide":
            return await self.begin("guide", page=0)
        if cmd == "d":
            state = await self.l.dialog(self.uid)
            if not state or state["token"] != p[1]:
                raise ValidationError("Этот выбор устарел. Откройте нужный раздел заново.")
            if p[2] == "page":
                if state["step"] not in ("unknown_bind", "entry_bank", "transfer_target", "ratepick", "edit_bank_id", "edit_category_id"):
                    raise ValidationError("Недоступное действие.")
                state["data"]["page"] = max(0, int(p[3]))
                await self.l.save_dialog(self.uid, state)
                return await self.prompt(state)
            return await self.consume_choice(state, p[2])
        if cmd == "newbank":
            return await self.begin("bank_name")
        if cmd == "newcat":
            if p[1] not in ("expense", "income"):
                raise ValidationError("Выберите тип категории.")
            return await self.begin("cat_name", category_kind=p[1])
        if cmd in ("rename", "aliasadd", "aliasremove", "archive"):
            kind, oid = p[1], int(p[2])
            obj = await self.l.object(self.uid, kind, oid, active=True)
            if cmd == "archive":
                return await self.begin("confirm", operation="archive", kind=kind, oid=oid, question=f"Переместить «{e(obj['name'])}» в архив? История и остаток сохранятся.")
            return await self.begin(cmd, kind=kind, oid=oid)
        if cmd in ("default", "transfer", "adjust"):
            bid = int(p[1])
            bank = await self.l.object(self.uid, "bank", bid, active=True)
            if cmd == "default":
                return await self.begin("confirm", operation="default", bank=bid, question=f"Использовать {e(bank['name'])} по умолчанию?")
            return await self.begin("transfer_target" if cmd == "transfer" else "adjust_amount", bank=bid)
        if cmd in ("rate", "ratepick", "payout"):
            month, bank = month_value(p[1]), int(p[2])
            await self.l.object(self.uid, "bank", bank, active=True)
            if cmd == "rate":
                cat = await self.l.object(self.uid, "category", int(p[3]), active=True)
                if cat["kind"] != "expense":
                    raise ValidationError("Выберите категорию расходов.")
                return await self.begin("rate_value", month=month, bank=bank, cat=int(p[3]))
            return await self.begin("ratepick" if cmd == "ratepick" else "payout_amount", month=month, bank=bank)
        if cmd == "paynow":
            month, bank = month_value(p[1]), int(p[2])
            await self.l.object(self.uid, "bank", bank, active=True)
            ops = await self.l.operations(self.uid, month)
            expected = sum(o["cashback"] for o in ops if o["bank_id"] == bank)
            if expected <= 0:
                raise ValidationError("За этот месяц нет ожидаемого кэшбэка для перевода.")
            op = await self.l.payout(self.uid, bank, month, expected, self.today.isoformat())
            await self.l.save_dialog(self.uid, None)
            return await self.receipt(op)
        if cmd == "copy":
            return await self.begin("copy_confirm", month=month_value(p[1]))
        if cmd == "edit":
            oid, field = int(p[1]), p[2]
            op = await self.l.operation(self.uid, oid)
            allowed = ["amount", "day"]
            if op["kind"] in ("income", "expense") and not op["payout_month"]:
                allowed += ["bank_id", "category_id"]
            if field not in allowed:
                raise ValidationError("Поле недоступно для изменения.")
            return await self.begin("edit_" + field, oid=oid)
        if cmd == "delete":
            op = await self.l.operation(self.uid, int(p[1]))
            return await self.begin("confirm", operation="delete", oid=op["id"], question=f"Удалить операцию {fmt(op['amount'])} от {op['day']}? Остаток будет пересчитан.")
        if cmd == "timezone":
            return await self.begin("timezone")
        if cmd == "remset":
            value = None if p[1] == "off" else f"{p[1][:2]}:{p[1][2:]}"
            await self.l.set_reminder(self.uid, value)
            await self.l.save_dialog(self.uid, None)
            return await self.view("settings")
        if cmd == "remcustom":
            return await self.begin("reminder_time")
        screen = await self.view(route)
        if screen:
            await self.l.save_dialog(self.uid, None)
            return screen
        raise ValidationError("Кнопка недоступна. Откройте меню.")

    async def consume_choice(self, state, value):
        step, d = state["step"], state["data"]
        if step == "welcome" and value == "guide":
            return await self.begin("guide", page=0)
        if step == "guide":
            page = d["page"]
            if value == "back":
                return await self.begin("guide", page=page - 1) if page else await self.begin("welcome")
            if value == "next" and page < len(GUIDE_PAGES) - 1:
                return await self.begin("guide", page=page + 1)
            if value in ("finish", "categories") and page == len(GUIDE_PAGES) - 1:
                # Accept the former final-page action for dialogs saved before the update.
                await self.l.complete_guide(self.uid)
                banks = await self.l.objects(self.uid, "bank")
                if not banks:
                    return await self.begin("bank_name")
                await self.l.save_dialog(self.uid, None)
                return await self.cashback_intro(banks[0]["id"]) or self.menu()
        if step == "bank_default" and value in ("yes", "no"):
            oid = await self.l.create_bank(self.uid, d["name"], d["aliases"] + ([d["word"]] if d.get("pending") else []), d["opening"], self.today.isoformat())
            if value == "yes":
                await self.l.set_default(self.uid, oid)
            return await self.finish_creation(d, "bank", oid)
        if step == "unknown":
            if value in ("bank", "category"):
                return await self.advance(state, "unknown_bind", kind=value)
            if value == "newcat":
                return await self.advance(state, "unknown_kind")
            if value == "newbank":
                return await self.advance(state, "bank_name")
        if step in ("unknown_kind", "category_kind") and value in ("expense", "income"):
            return await self.advance(state, "cat_name", category_kind=value)
        if step in ("unknown_bind", "entry_bank", "transfer_target", "ratepick", "edit_bank_id", "edit_category_id"):
            if not value.isdigit():
                raise ValidationError("Выберите объект из списка.")
            oid = int(value)
            if step == "unknown_bind":
                await self.l.add_aliases(self.uid, d["kind"], oid, [d["word"]])
                return await self.record_pending(d["pending"], d["original_today"])
            if step == "entry_bank":
                await self.l.set_default(self.uid, oid)
                return await self.record_pending(d["pending"], d["original_today"])
            if step == "transfer_target":
                await self.l.object(self.uid, "bank", oid, active=True)
                if oid == d["bank"]:
                    raise ValidationError("Выберите другой банк.")
                return await self.advance(state, "transfer_amount", target=oid)
            if step == "ratepick":
                cat = await self.l.object(self.uid, "category", oid, active=True)
                if cat["kind"] != "expense":
                    raise ValidationError("Нужна категория расходов.")
                return await self.advance(state, "rate_value", cat=oid)
            field = step.removeprefix("edit_")
            await self.l.edit_operation(self.uid, d["oid"], **{field: oid})
            await self.l.save_dialog(self.uid, None)
            return await self.view(f"op:{d['oid']}")
        if step in ("rate_confirm", "copy_confirm") and value in ("yes", "no"):
            if step == "rate_confirm":
                await self.l.set_rate(self.uid, d["bank"], d["cat"], d["month"], d["rate"], value == "yes")
                route = f"cashbank:{d['month']}:{d['bank']}:0"
            else:
                count = await self.l.copy_rates(self.uid, shift_month(d["month"], -1), d["month"], value == "yes")
                route = f"cash:{d['month']}:0"
            await self.l.save_dialog(self.uid, None)
            screen = await self.view(route)
            if step == "copy_confirm":
                screen.text = f"Скопировано ставок: {count}.\n" + screen.text
            return screen
        if step == "confirm" and value == "yes":
            operation = d["operation"]
            op = None
            if operation == "transfer":
                op = await self.l.transfer(self.uid, d["bank"], d["target"], d["amount"], self.today.isoformat())
            elif operation == "adjust":
                op = await self.l.adjust(self.uid, d["bank"], d["amount"], self.today.isoformat())
            elif operation == "payout":
                op = await self.l.payout(self.uid, d["bank"], d["month"], d["amount"], d["day"])
            elif operation == "delete":
                await self.l.delete_operation(self.uid, d["oid"])
            elif operation == "archive":
                await self.l.archive(self.uid, d["kind"], d["oid"])
            elif operation == "default":
                await self.l.set_default(self.uid, d["bank"])
            else:
                raise ValidationError("Недоступное действие.")
            await self.l.save_dialog(self.uid, None)
            if op:
                return await self.receipt(op)
            screen = self.menu()
            screen.text = "Готово.\n" + screen.text
            return screen
        raise ValidationError("Этот выбор недоступен. Используйте кнопки текущего диалога.")
