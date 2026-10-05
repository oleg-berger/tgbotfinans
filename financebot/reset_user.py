"""Offline reset of one account. Preview by default, backup before deletion."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import uuid

from dotenv import load_dotenv

from .database import MIGRATIONS
from .instance import InstanceLock


# Child records must be removed before their referenced banks and categories.
USER_TABLES = (
    "events", "dialogs", "daily_reminders", "reminders", "rates",
    "operations", "aliases", "banks", "categories",
)


def reset_user(database, backup_dir, uid, *, confirm=False):
    database = Path(database).resolve()
    if type(uid) is not int or not 0 < uid < 2**63:
        raise ValueError("Укажите положительный числовой Telegram ID.")
    if not database.is_file():
        raise ValueError("База не найдена. Проверьте DATABASE_PATH в .env.")
    with InstanceLock(database.with_suffix(".lock")):
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
            if version != MIGRATIONS[-1][0]:
                raise ValueError("Версия базы не совпадает с приложением. Сначала обновите базу обычным запуском бота, затем остановите его.")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables != set(USER_TABLES) | {"users", "schema_version"}:
                raise ValueError("Структура базы изменилась. Сброс отменен до проверки новых таблиц.")
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone():
                raise ValueError("Этот Telegram ID не найден в выбранной базе. Ничего не удалено.")
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (uid,)).fetchone()[0] for table in USER_TABLES}
            counts["users"] = 1
            if not confirm:
                return {"counts": counts, "backup": None}

            backup_dir = Path(backup_dir).resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backup_dir / f"before-reset-{uid}-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
            # The app's OS lock prevents the bot from writing until this operation ends.
            with closing(sqlite3.connect(backup)) as target:
                conn.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Проверка резервной копии не прошла. Данные не удалены.")

            try:
                conn.execute("BEGIN IMMEDIATE")
                for table in USER_TABLES:
                    conn.execute(f"DELETE FROM {table} WHERE user_id=?", (uid,))
                conn.execute("DELETE FROM users WHERE id=?", (uid,))
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Нарушены связи в базе. Сброс отменен.")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return {"counts": counts, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description="Сброс данных одного Telegram ID. Бот должен быть остановлен.")
    parser.add_argument("telegram_id", type=int)
    parser.add_argument("--confirm", action="store_true", help="Создать резервную копию и удалить данные выбранного ID")
    args = parser.parse_args()
    # No bot token or Telegram connection is required for offline maintenance.
    load_dotenv(Path.cwd() / ".env", override=False)
    database = Path(os.environ.get("DATABASE_PATH", "data/finance.sqlite3")).resolve()
    backup_dir = Path(os.environ.get("BACKUP_DIR", "backups")).resolve()
    print(f"База: {database}\nTelegram ID: {args.telegram_id}")
    try:
        result = reset_user(database, backup_dir, args.telegram_id, confirm=args.confirm)
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Сброс не выполнен: {exc}\n")
    for table, count in result["counts"].items():
        print(f"{table}: {count}")
    if result["backup"]:
        print(f"Сброс выполнен. Резервная копия: {result['backup']}")
        print("Очистите старую переписку с ботом, затем запустите бота и отправьте /start.")
        print("Список разрешенных ID в .env не изменен.")
    else:
        print("Это предварительный просмотр. Ничего не удалено.")
        print("Для сброса повторите команду с --confirm.")


if __name__ == "__main__":
    main()
