"""
Shared SQLite connection helper for the storage package.

Backed by SQLite on a mounted volume (see docker-compose.yml) so state
survives container restarts/redeploys.
"""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from autosend.config import settings

DB_PATH = Path(settings.database_path)

@contextmanager
def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

