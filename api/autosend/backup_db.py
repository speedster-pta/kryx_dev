"""
Safe SQLite backup for Shofar Online.

Runs INSIDE the container via:
    docker compose exec -T kryx python -m autosend.backup_db

Uses sqlite3's built-in online backup API (safe against concurrent writers,
unlike a raw file copy which can grab a mid-transaction / WAL-inconsistent
snapshot). Output is gzip-compressed in place and named with a UTC timestamp.

Exits non-zero on any failure so cron/log monitoring can catch it.
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path("/data/autosend.db")
BACKUP_DIR = Path("/data/backups")


def run_backup() -> Path:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Source database not found at {DB_PATH}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_path = BACKUP_DIR / f"shofar_{timestamp}.db"
    gz_path = raw_path.with_suffix(raw_path.suffix + ".gz")

    # Online backup API — safe with a live app writing to the DB concurrently.
    src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(str(raw_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    # Quick integrity check on the backup before we trust it.
    check_conn = sqlite3.connect(str(raw_path))
    try:
        result = check_conn.execute("PRAGMA integrity_check;").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    finally:
        check_conn.close()

    with open(raw_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    raw_path.unlink()

    return gz_path


def main() -> int:
    try:
        gz_path = run_backup()
    except Exception as exc:  # noqa: BLE001 - want any failure loud in cron logs
        print(f"[backup_db] FAILED: {exc}", file=sys.stderr)
        return 1

    size_kb = gz_path.stat().st_size / 1024
    print(f"[backup_db] OK: {gz_path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
