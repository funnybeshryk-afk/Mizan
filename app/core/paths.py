from __future__ import annotations

import sys
from pathlib import Path


def get_base_dir() -> Path:
    """Directory the app's mutable, persistent files (data/mizan.db, .env,
    logs/) live next to.

    - Frozen (PyInstaller .exe): the directory containing the .exe itself.
      Deliberately NOT sys._MEIPASS - that's a fresh temp extraction folder
      PyInstaller creates on every launch and deletes on exit, so treating
      it as the data directory would silently reset the database (and any
      other on-disk state) on every single run.
    - Dev mode (`python main.py`): the project root (the repo directory
      containing app/, main.py, requirements.txt, ...).
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


BASE_DIR = get_base_dir()
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "mizan.db"
LOGS_DIR = BASE_DIR / "logs"
ENV_PATH = BASE_DIR / ".env"
