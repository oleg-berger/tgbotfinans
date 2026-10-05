import argparse
import asyncio
import logging
from .config import Config


def main():
    parser = argparse.ArgumentParser(description="Telegram finance bot")
    parser.add_argument("--check", action="store_true", help="Validate .env without contacting Telegram")
    args = parser.parse_args()
    try:
        config = Config.load()
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    if args.check:
        print("Configuration OK. Telegram connection has not been checked.")
        return
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("financebot").setLevel(logging.INFO)
    # Avoid upstream exception messages (may include URLs or user input).
    logging.getLogger("aiogram").disabled = True
    logging.getLogger("aiogram.dispatcher").disabled = True
    logging.getLogger("aiogram.event").disabled = True
    from .runtime import run
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        parser.exit(1, f"Startup failed ({type(exc).__name__}). Check configuration and connectivity.\n")


if __name__ == "__main__":
    main()
