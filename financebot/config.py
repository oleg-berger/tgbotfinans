from dataclasses import dataclass
import os
from pathlib import Path
import re
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    database: Path
    backups: Path

    @classmethod
    def load(cls, env_file=".env"):
        load_dotenv(env_file, override=False)
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Укажите BOT_TOKEN от BotFather в .env.")
        return cls(token, Path(os.environ.get("DATABASE_PATH", "data/finance.sqlite3")).resolve(), Path(os.environ.get("BACKUP_DIR", "backups")).resolve())
