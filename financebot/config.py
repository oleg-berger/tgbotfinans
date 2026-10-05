from dataclasses import dataclass
import os
from pathlib import Path
import re
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    allowed_users: set[int]
    database: Path
    backups: Path

    @classmethod
    def load(cls, env_file=".env"):
        load_dotenv(env_file, override=False)
        token = os.environ.get("BOT_TOKEN", "").strip()
        ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Укажите BOT_TOKEN от BotFather в .env.")
        if not re.fullmatch(r"[1-9]\d*(?:\s*,\s*[1-9]\d*)*", ids):
            raise ValueError("ALLOWED_USER_IDS должен содержать Telegram ID через запятую.")
        return cls(token, {int(value) for value in ids.split(",")}, Path(os.environ.get("DATABASE_PATH", "data/finance.sqlite3")).resolve(), Path(os.environ.get("BACKUP_DIR", "backups")).resolve())
