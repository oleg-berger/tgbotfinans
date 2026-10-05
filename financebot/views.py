"""Telegram screens; values are escaped for HTML at the display boundary."""
from html import escape as e
from .parsing import fmt, shift_month, month_value
from .screens import Screen, buttons, pages


KINDS = {"income": "Доход", "expense": "Расход", "opening": "Начальный остаток", "transfer": "Перевод", "adjustment": "Корректировка"}


class Views:
    def __init__(self, ledger, uid, today):
        self.l, self.uid, self.today = ledger, uid, today

    @property
    def month(self):
        return self.today.strftime("%Y-%m")

    def menu(self):
        return Screen("<b>Учет финансов</b>", buttons([
            ("💰 Баланс", "balance"), ("📊 Статистика", f"stats:{self.month}"),
            ("🧾 Операции", "ops:all:0:0"), ("🪙 Кэшбэк", f"cash:{self.month}:0"),
            ("🗂 Категории", "categories"), ("🏦 Банки", "banks:0"), ("⚙️ Настройки", "settings")]))

    def back(self, target="menu"):
        return [("← Назад", target)]

    def month_nav(self, month, prefix, suffix=""):
        return [("← Месяц", f"{prefix}:{shift_month(month, -1)}{suffix}"), ("Месяц →", f"{prefix}:{shift_month(month, 1)}{suffix}")]

    async def receipt(self, op):
        sign = "−" if op["kind"] in ("expense", "transfer") or op["amount"] < 0 else "+"
        date_text = ".".join(reversed(op["day"].split("-")))
        text = f"💳 <b>{sign}{fmt(abs(op['amount']))}</b>\n<b>{e(op['category_name'] or KINDS[op['kind']])} · {e(op['bank_name'])}</b>\n🗓️ {date_text}\n📈 Остаток в {e(op['bank_name'])}: <b>{fmt(await self.l.balance(self.uid, op['bank_id']))}</b>"
        rows = []
        if op["kind"] == "expense":
            if op["rate"] is None:
                text += "\n🪙 Кэшбэк не настроен"
                rows.append([("Настроить кэшбэк", f"rate:{op['day'][:7]}:{op['bank_id']}:{op['category_id']}")])
            else:
                text += f"\n🪙 Ожидаемый кэшбэк: <b>{fmt(op['cashback'])} · {e(op['rate'])}%</b>"
        rows += [[("🧾 Операция", f"op:{op['id']}")], [("☰ Меню", "menu")]]
        return Screen(text, rows)

    async def view(self, route):
        p = route.split(":")
        cmd = p[0]
        if cmd == "menu":
            return self.menu()
        if cmd == "balance":
            return Screen(f"💰 <b>Общий баланс: {fmt(await self.l.balance(self.uid))}</b>", buttons([("🏦 Баланс каждого банка", "balances:0"), ("🗂 Отчет по категориям", f"report:{self.month}:expense:0:balance"), ("📊 Статистика", f"stats:{self.month}"), ("☰ Меню", "menu")]))
        if cmd == "balances":
            banks = await self.l.objects(self.uid, "bank", include_archived=True)
            totals = await self.l.balances(self.uid)
            items = [(f"{'📁 ' if b['archived'] else ''}{b['name']}: {fmt(totals.get(b['id'], 0))}", f"obj:bank:{b['id']}") for b in banks]
            return Screen("🏦 <b>Остатки банков</b>\nАрхивные банки тоже входят в общий баланс.", pages(items, int(p[1]), "balances") + [self.back("balance")])
        if cmd == "stats":
            month = month_value(p[1])
            s, total = await self.l.summary(self.uid, month), await self.l.summary(self.uid)
            return Screen(f"📊 <b>Статистика · {month}</b>\n💰 Бюджет сейчас: <b>{fmt(s['budget'])}</b>\n\nДоходы: {fmt(s['income'])}\nРасходы: {fmt(s['expense'])}\nРазница за месяц: {fmt(s['income'] - s['expense'])}\n\nЗа все время:\nДоходы: {fmt(total['income'])}\nРасходы: {fmt(total['expense'])}", [self.month_nav(month, "stats"), *buttons([("🗂 Категории", f"report:{month}:expense:0"), ("🧾 Операции месяца", f"ops:{month}:0:0"), ("📄 CSV", f"csv:{month}"), ("☰ Меню", "menu")])])
        if cmd == "csv":
            return Screen("Экспорт операций готов.", [self.back(f"stats:{p[1]}")], await self.l.export_csv(self.uid, p[1]), f"operations-{p[1]}.csv")
        if cmd == "report":
            month, kind, page = month_value(p[1]), p[2], int(p[3])
            src = p[4] if len(p) > 4 else "stats"
            back = "balance" if src == "balance" else f"stats:{month}"
            ops = await self.l.operations(self.uid, month)
            totals = {}
            for op in ops:
                if op["kind"] == kind or (kind == "income" and op["kind"] == "opening"):
                    key = ("opening", KINDS["opening"]) if op["kind"] == "opening" else (op["category_id"], op["category_name"])
                    totals[key] = totals.get(key, 0) + op["amount"]
            items = [(f"{name}: {fmt(value)}", f"ops:{month}:{cid}:0") for (cid, name), value in sorted(totals.items(), key=lambda x: -x[1])]
            page = max(0, min(page, max(0, (len(items) - 1) // 8)))
            rows = buttons(items[page * 8:(page + 1) * 8])
            nav = []
            if page:
                nav.append(("←", f"report:{month}:{kind}:{page - 1}:{src}"))
            if (page + 1) * 8 < len(items):
                nav.append(("→", f"report:{month}:{kind}:{page + 1}:{src}"))
            if nav:
                rows.append(nav)
            return Screen(f"🗂 <b>Категории · {month}</b>" + ("\nПока нет операций." if not items else ""), [[("− Расходы", f"report:{month}:expense:0:{src}"), ("+ Доходы", f"report:{month}:income:0:{src}")], *rows, self.back(back)])
        if cmd == "ops":
            month, cat, page = p[1], "opening" if p[2] == "opening" else int(p[2]), int(p[3])
            opening = cat == "opening"
            ops = await self.l.operations(self.uid, None if month == "all" else month, None if opening else cat or None, kind="opening" if opening else None)
            title = KINDS["opening"] if opening else ("Операции" if not cat else (await self.l.object(self.uid, "category", cat))["name"])
            items = [(f"{op['day']} · {KINDS[op['kind']]} {fmt(op['amount'])} · {op['category_name'] or op['bank_name']}", f"op:{op['id']}") for op in ops]
            return Screen(f"🧾 <b>{e(title)} · {'все время' if month == 'all' else month}</b>" + ("\nНет операций." if not items else ""), pages(items, page, f"ops:{month}:{cat}") + [self.back("menu" if month == "all" else f"stats:{month}")])
        if cmd == "op":
            op = await self.l.operation(self.uid, int(p[1]))
            text = f"🧾 <b>{KINDS[op['kind']]} · {fmt(op['amount'])}</b>\n{op['day']}\nБанк: {e(op['bank_name'])}"
            if op["category_name"]:
                text += f"\nКатегория: {e(op['category_name'])}"
            if op["target_name"]:
                text += f"\nБанк назначения: {e(op['target_name'])}"
            if op["kind"] == "expense":
                text += f"\nОжидаемый кэшбэк: {fmt(op['cashback'])}"
            if op["payout_month"]:
                text += f"\nВыплата кэшбэка за {op['payout_month']}"
            items = [("✏️ Сумма", f"edit:{op['id']}:amount"), ("🗓️ Дата", f"edit:{op['id']}:day")]
            if op["kind"] in ("income", "expense") and not op["payout_month"]:
                items += [("🏦 Банк", f"edit:{op['id']}:bank_id"), ("🗂 Категория", f"edit:{op['id']}:category_id")]
            items += [("🗑 Удалить", f"delete:{op['id']}"), ("← Назад", f"ops:{op['day'][:7]}:0:0")]
            return Screen(text, buttons(items))
        if cmd == "categories":
            return Screen("🗂 <b>Категории</b>", buttons([("− Расходы", "catlist:expense:0"), ("+ Доходы", "catlist:income:0"), ("📁 Архив", "archived:category:0"), ("☰ Меню", "menu")]))
        if cmd == "catlist":
            kind, page = p[1], int(p[2])
            cats = [c for c in await self.l.objects(self.uid, "category") if c["kind"] == kind]
            return Screen("🗂 " + ("Расходы" if kind == "expense" else "Доходы"), pages([(c["name"], f"obj:category:{c['id']}") for c in cats], page, f"catlist:{kind}") + [[("+ Добавить категорию", f"newcat:{kind}")], self.back("categories")])
        if cmd == "banks":
            banks = await self.l.objects(self.uid, "bank")
            default = (await self.l.user(self.uid))["default_bank"]
            return Screen("🏦 <b>Банки</b>\n⭐ — банк по умолчанию", pages([(b["name"] + (" ⭐" if b["id"] == default else ""), f"obj:bank:{b['id']}") for b in banks], int(p[1]), "banks") + buttons([("+ Добавить банк", "newbank"), ("📁 Архив", "archived:bank:0"), ("☰ Меню", "menu")]))
        if cmd == "archived":
            items = [(o["name"], f"obj:{p[1]}:{o['id']}") for o in await self.l.objects(self.uid, p[1], include_archived=True) if o["archived"]]
            return Screen("📁 <b>Архив</b>", pages(items, int(p[2]), f"archived:{p[1]}") + [self.back("banks:0" if p[1] == "bank" else "categories")])
        if cmd == "aliases":
            kind, oid, page = p[1], int(p[2]), int(p[3])
            obj = await self.l.object(self.uid, kind, oid)
            aliases = await self.l.aliases(self.uid, kind, oid)
            page = max(0, min(page, max(0, (len(aliases) - 1) // 8)))
            text = f"✨ <b>Ключи · {e(obj['name'])}</b>\n" + "\n".join(e(w) for w in aliases[page * 8:(page + 1) * 8])
            nav = []
            if page:
                nav.append(("←", f"aliases:{kind}:{oid}:{page - 1}"))
            if (page + 1) * 8 < len(aliases):
                nav.append(("→", f"aliases:{kind}:{oid}:{page + 1}"))
            return Screen(text, ([nav] if nav else []) + buttons([("Добавить ключи", f"aliasadd:{kind}:{oid}"), ("Убрать ключ", f"aliasremove:{kind}:{oid}"), ("← Назад", f"obj:{kind}:{oid}")]))
        if cmd == "obj":
            kind, oid = p[1], int(p[2])
            obj = await self.l.object(self.uid, kind, oid)
            text = f"<b>{e(obj['name'])}</b>"
            if kind == "bank":
                text += f"\nОстаток: {fmt(await self.l.balance(self.uid, oid))}"
            aliases = await self.l.aliases(self.uid, kind, oid)
            text += "\nКлючи: " + e(", ".join(aliases[:8]) or "нет")
            if len(aliases) > 8:
                text += f" … (всего {len(aliases)})"
            items = []
            if not obj["archived"]:
                if kind == "bank":
                    items = [("✏️ Переименовать", f"rename:{kind}:{oid}"), ("✨ Ключевые слова", f"aliases:{kind}:{oid}:0"), ("⭐ По умолчанию", f"default:{oid}"), ("↔️ Перевод", f"transfer:{oid}"), ("🪙 Кэшбэк", f"cashbank:{self.month}:{oid}:0")]
                else:
                    items = [("✏️ Переименовать", f"rename:{kind}:{oid}"), ("✨ Все ключи", f"aliases:{kind}:{oid}:0"), ("✨ Добавить ключи", f"aliasadd:{kind}:{oid}"), ("Убрать ключ", f"aliasremove:{kind}:{oid}")]
                    if not obj["system"]:
                        items += [("📁 В архив", f"archive:{kind}:{oid}")]
            else:
                text += "\n📁 В архиве. История сохранена."
            items += [("← Назад", "banks:0" if kind == "bank" else "categories")]
            return Screen(text, buttons(items))
        if cmd == "cash":
            month, page = month_value(p[1]), int(p[2])
            banks = await self.l.objects(self.uid, "bank", include_archived=True)
            return Screen(f"🪙 <b>Кэшбэк · {month}</b>\nВыберите банк: ставки по категориям, ожидаемый кэшбэк и фактическая выплата.", [self.month_nav(month, "cash", ":0"), *pages([(b["name"], f"cashbank:{month}:{b['id']}:0") for b in banks], page, f"cash:{month}"), *buttons([("Копировать ставки прошлого месяца", f"copy:{month}"), ("☰ Меню", "menu")])])
        if cmd == "cashbank":
            month, bid, page = month_value(p[1]), int(p[2]), int(p[3])
            bank = await self.l.object(self.uid, "bank", bid)
            rates = await self.l.rows("SELECT r.*,c.name FROM rates r JOIN categories c ON c.id=r.category_id WHERE r.user_id=? AND r.bank_id=? AND r.month=? ORDER BY c.name", (self.uid, bid, month))
            ops = await self.l.operations(self.uid, month)
            expected = sum(o["cashback"] for o in ops if o["bank_id"] == bid)
            payout = await self.l.one("SELECT id,amount FROM operations WHERE user_id=? AND bank_id=? AND payout_month=? AND deleted=0", (self.uid, bid, month))
            text = f"🪙 <b>{e(bank['name'])} · {month}</b>\nОжидаемый кэшбэк: {fmt(expected)}\nВыплачено: {fmt(payout['amount']) if payout else 'не подтверждено'}"
            if not rates:
                text += "\nСтавки не настроены."
            items = [(f"{r['name']}: {r['rate']}%", f"rate:{month}:{bid}:{r['category_id']}") for r in rates]
            rows = pages(items, page, f"cashbank:{month}:{bid}")
            if not bank["archived"]:
                rows.append([("+ Настроить категорию", f"ratepick:{month}:{bid}")])
                rows.append([("Изменить выплату" if payout else "💳 Перевести на счет", f"op:{payout['id']}" if payout else f"paynow:{month}:{bid}")])
            rows.append(self.back(f"cash:{month}:0"))
            return Screen(text, rows)
        if cmd == "settings":
            user = await self.l.user(self.uid)
            reminder = user["reminder_time"] or "выключено"
            return Screen(f"⚙️ <b>Настройки</b>\nЧасовой пояс: {e(user['timezone'])}\nВалюта: KZT\nПериоды: календарные месяцы\nНапоминание о кэшбэке: 1-го числа, 09:00\nЕжедневная запись трат: {reminder}\nВаш Telegram ID: <code>{self.uid}</code>", buttons([("⏰ Напоминание о записях", "reminder"), ("🕐 Часовой пояс", "timezone"), ("📖 Помощь", "help"), ("☰ Меню", "menu")]))
        if cmd == "reminder":
            user = await self.l.user(self.uid)
            current = user["reminder_time"] or "выключено"
            return Screen(f"⏰ <b>Ежедневное напоминание</b>\nСейчас: {current}\nБот напомнит записать траты дня в выбранное время по вашему часовому поясу.", buttons([("20:00", "remset:2000"), ("21:00", "remset:2100"), ("22:00", "remset:2200"), ("✏️ Свое время", "remcustom"), ("Выключить", "remset:off")]) + [self.back("settings")])
        if cmd == "help":
            return Screen("<b>Быстрая запись</b>\n<code>250 пр бцц</code> — расход по ключам.\n<code>15000 зп</code> — доход, если «зп» привязан к категории доходов.\n<code>250</code> — расход в «Прочее», банк по умолчанию.\nДата второй строкой: <code>04.09.2026</code>.\n\nКлючи задаются через запятую; названия тоже работают как ключи.\n/start — приветствие и гайд, /menu — меню, /cancel — отмена диалога.\nБыстрые переходы: /balance, /stats, /ops, /cashback, /categories, /banks, /settings.\n\nПереводы и сверка остатка: Банки → нужный банк.\nКэшбэк сначала ожидаемый; полученную сумму подтвердите в разделе Кэшбэк.\nАрхивирование сохраняет историю и остаток.\nСверка записывает разницу: последующее изменение старых операций изменит текущий остаток.", [[("📖 Пройти гайд по боту", "guide")], self.back("settings")])
        return None
