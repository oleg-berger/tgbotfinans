"""Stage only reviewed code and guide assets, never .env or financial data."""
from pathlib import Path
import argparse
from importlib.metadata import distribution
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "cloudflare" / "src"
MODULES = (
    "__init__", "application", "schema", "ledger", "parsing", "screens",
    "views", "dialogs", "guide", "reminders", "cloud_database",
    "cloud_transport", "cloud_service", "snapshots",
)


def build():
    TARGET.mkdir(parents=True, exist_ok=True)
    package = TARGET / "financebot"
    package.mkdir(exist_ok=True)
    for module in MODULES:
        destination = package / f"{module}.py"
        temporary = destination.with_suffix(".next")
        shutil.copyfile(ROOT / "financebot" / f"{module}.py", temporary)
        temporary.replace(destination)
    for path in package.glob("*.py"):
        if path.stem not in MODULES:
            path.unlink()
    shutil.copyfile(ROOT / "cloudflare" / "entry.py", TARGET / "worker.next")
    (TARGET / "worker.next").replace(TARGET / "worker.py")
    assets = ROOT / "cloudflare" / "assets" / "guide"
    assets.mkdir(parents=True, exist_ok=True)
    for page in range(1, 8):
        filename = f"page-{page:02d}.png"
        shutil.copyfile(ROOT / "financebot" / "assets" / "guide" / filename, assets / filename)
    return TARGET


def vendor():
    target = ROOT / "cloudflare" / "python_modules"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir()
    for name in ("workers-runtime-sdk", "tzdata"):
        package = distribution(name)
        for item in package.files or ():
            if ".." in item.parts or "__pycache__" in item.parts or item.suffix == ".pyc":
                continue
            source = Path(package.locate_file(item))
            if source.is_file():
                destination = target / item
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor", action="store_true", help="Stage pinned SDK and timezone packages from this Python environment")
    if parser.parse_args().vendor:
        vendor()
    print(f"Worker sources prepared: {build()}")
