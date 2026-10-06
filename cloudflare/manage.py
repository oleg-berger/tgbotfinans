"""Explicit administration; secrets are prompted, never read from project .env."""
import argparse
import asyncio
from contextlib import closing
import getpass
import json
from pathlib import Path
import re
import sqlite3
import sys
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from financebot.schema import MIGRATIONS
from financebot.cloud_database import Connection
from financebot.snapshots import TABLES, MAX_IMPORT_BYTES, import_snapshot

COMMANDS = (
    ("start", "Начать"), ("menu", "Главное меню"), ("balance", "Баланс"),
    ("stats", "Статистика"), ("ops", "Операции"), ("cashback", "Кэшбэк"),
    ("categories", "Категории"), ("banks", "Банки"), ("settings", "Настройки"),
    ("guide", "Гайд по боту"), ("cancel", "Отменить ввод"), ("help", "Помощь"),
)


def request(url, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode()
    if len(body) > MAX_IMPORT_BYTES:
        raise ValueError("Запрос превышает 20 МиБ.")
    call = Request(url, body, {"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urlopen(call, timeout=120) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        # urllib exceptions include URLs; Telegram URLs contain the token.
        raise ValueError(f"Сервер отклонил запрос: HTTP {exc.code}.") from None
    except URLError:
        raise ValueError("Не удалось подключиться к серверу.") from None


def write_snapshot(path, snapshot):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite the user's only copy accidentally.
    with target.open("x", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False)
    print(f"Снимок сохранен: {target.resolve()}")


def export_sqlite(source, destination):
    path = Path(source).resolve()
    if not path.is_file():
        raise ValueError("Файл SQLite не найден.")
    # Backup API produces a consistent snapshot including committed WAL data.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as live:
        with closing(sqlite3.connect(":memory:")) as copy:
            live.backup(copy)
            copy.row_factory = sqlite3.Row
            if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Проверка целостности SQLite не прошла.")
            version = copy.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if type(version) is not int or not 1 <= version <= MIGRATIONS[-1][0]:
                raise ValueError("Версия исходной базы не поддерживается.")
            # Upgrade the consistent in-memory copy, never the original file.
            for pending, script in MIGRATIONS:
                if pending > version:
                    copy.executescript(script)
                    copy.execute("INSERT INTO schema_version VALUES(?)", (pending,))
            version = MIGRATIONS[-1][0]
            names = {row[0] for row in copy.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if names != set(TABLES) | {"schema_version"}:
                raise ValueError("Набор таблиц SQLite отличается от ожидаемого.")
            tables = {table: [dict(row) for row in copy.execute(f"SELECT * FROM {table}")] for table in TABLES}
    snapshot = {"format": "financebot-snapshot-v1", "schema_version": version, "tables": tables}
    if len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_IMPORT_BYTES - 1024:
        raise ValueError("Снимок превышает лимит переноса 20 МиБ.")
    write_snapshot(destination, snapshot)


def snapshot_to_sqlite(source, destination):
    text = Path(source).read_text(encoding="utf-8")
    if len(text.encode()) > MAX_IMPORT_BYTES:
        raise ValueError("Снимок превышает лимит переноса.")
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Claim a new path exclusively; an existing working database is never opened.
    with target.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(target)) as native:
            native.row_factory = sqlite3.Row
            native.execute("PRAGMA foreign_keys=ON")
            native.execute("BEGIN IMMEDIATE")
            native.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
            for version, script in MIGRATIONS:
                for statement in script.split(";"):
                    if statement.strip():
                        native.execute(statement)
                native.execute("INSERT INTO schema_version VALUES(?)", (version,))
            class SQL:
                def exec(self, statement, *params):
                    rows = [dict(row) for row in native.execute(statement, params).fetchall()]
                    return SimpleNamespace(toArray=lambda: rows)
            asyncio.run(import_snapshot(Connection(SQL()), text))
            if native.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Проверка целостности SQLite не прошла.")
            native.commit()
    except BaseException:
        target.unlink()
        raise
    print(f"Создана отдельная SQLite-база: {target.resolve()}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="HTTPS адрес Worker (без /webhook)")
    commands = parser.add_subparsers(dest="action", required=True)
    local = commands.add_parser("export-sqlite", help="Снимок существующей локальной базы без изменения оригинала")
    local.add_argument("source")
    local.add_argument("output")
    convert = commands.add_parser("snapshot-to-sqlite", help="Создать новую SQLite-базу из облачного снимка")
    convert.add_argument("source")
    convert.add_argument("output")
    commands.add_parser("configure", help="Установить webhook и прежнее меню Telegram")
    commands.add_parser("telegram-status")
    commands.add_parser("probe-webhook", help="Проверить обработку пустого webhook без финансового события")
    commands.add_parser("disconnect", help="Удалить webhook для возврата к polling")
    export = commands.add_parser("export")
    export.add_argument("output")
    commands.add_parser("backup")
    commands.add_parser("backups")
    download = commands.add_parser("download")
    download.add_argument("name")
    download.add_argument("output")
    load = commands.add_parser("import")
    load.add_argument("source")
    restore = commands.add_parser("restore")
    restore.add_argument("name")
    restore.add_argument("--confirm", action="store_true", required=True)
    reset = commands.add_parser("reset-user")
    reset.add_argument("uid", type=int)
    reset.add_argument("--confirm", action="store_true", help="Без этого флага только предварительный просмотр")
    args = parser.parse_args(argv)
    if args.action == "export-sqlite":
        export_sqlite(args.source, args.output)
        return
    if args.action == "snapshot-to-sqlite":
        snapshot_to_sqlite(args.source, args.output)
        return
    if args.action in ("configure", "telegram-status", "disconnect"):
        token = getpass.getpass("BOT_TOKEN: ").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Некорректный формат токена.")
        def telegram(method, payload):
            result = request(f"https://api.telegram.org/bot{token}/{method}", payload)
            if not result.get("ok"):
                raise ValueError("Telegram отклонил запрос.")
            return result["result"]
        if args.action == "telegram-status":
            info = telegram("getWebhookInfo", {})
            status = {key: info.get(key) for key in ("url", "pending_update_count", "last_error_date", "max_connections", "allowed_updates")}
            message = info.get("last_error_message")
            if message:
                # Error details help distinguish authentication and runtime failures.
                message = str(message).replace(token, "[TOKEN]")
                message = re.sub(r"https?://\S+", "[URL]", message)
                status["last_error_message"] = message
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return
        if args.action == "disconnect":
            telegram("deleteWebhook", {"drop_pending_updates": False})
            print("Webhook удален. Теперь можно запустить один polling-экземпляр.")
            return
        parsed = urlparse(args.url or "")
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Укажите --url с HTTPS адресом Worker без пути.")
        telegram("setMyCommands", {"commands": [{"command": key, "description": label} for key, label in COMMANDS]})
        telegram("setWebhook", {"url": args.url.rstrip("/") + "/webhook", "secret_token": "", "max_connections": 1, "allowed_updates": ["message", "callback_query"], "drop_pending_updates": False})
        print("Webhook и меню команд настроены. Отправьте боту /start.")
        return
    parsed = urlparse(args.url or "")
    if not parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment or not (parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        raise ValueError("Укажите --url с HTTPS адресом Worker без пути.")
    if args.action == "probe-webhook":
        call = Request(args.url.rstrip("/") + "/webhook", b"{}", {
            "Content-Type": "application/json",
        }, method="POST")
        try:
            with urlopen(call, timeout=30) as response:
                print(f"HTTP {response.status}: Worker обработал пустой запрос без секрета.")
        except HTTPError as exc:
            if exc.code == 403:
                raise ValueError("HTTP 403: проверьте, опубликована ли версия frontend без проверки WEBHOOK_SECRET.") from None
            if exc.code == 503:
                raise ValueError("HTTP 503: ошибка внутри Worker/core. Нужны логи.") from None
            raise ValueError(f"Worker ответил HTTP {exc.code}.") from None
        except URLError:
            raise ValueError("Не удалось подключиться к Worker.") from None
        return
    secret = getpass.getpass("ADMIN_SECRET: ").strip()
    if not secret:
        raise ValueError("ADMIN_SECRET не может быть пустым.")
    payload = {}
    if args.action == "import":
        source = Path(args.source)
        if source.stat().st_size > MAX_IMPORT_BYTES - 1024:
            raise ValueError("Снимок превышает лимит переноса.")
        payload = {"snapshot": json.loads(source.read_text(encoding="utf-8"))}
    elif args.action in ("download", "restore"):
        payload = {"name": args.name, "confirm": getattr(args, "confirm", False)}
    elif args.action == "reset-user":
        payload = {"uid": args.uid, "confirm": args.confirm}
    result = request(args.url.rstrip("/") + "/admin/" + args.action, payload, {"Authorization": "Bearer " + secret})
    if args.action in ("export", "download"):
        write_snapshot(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1)
