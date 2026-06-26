from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "industria_catalogue.db"


def backup_file(path: Path, suffix: str) -> None:
    if path.exists():
        shutil.copy2(path, path.with_name(f"{path.name}.{suffix}"))


def ensure_batch_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS gpt_batch_items (
            Internal_Variant_ID TEXT PRIMARY KEY,
            Internal_ID TEXT DEFAULT '',
            Brand TEXT DEFAULT '',
            Product_Title TEXT DEFAULT '',
            SKU TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            batch_id TEXT DEFAULT '',
            custom_id TEXT DEFAULT '',
            request_json TEXT DEFAULT '',
            result_json TEXT DEFAULT '',
            error TEXT DEFAULT '',
            force_submit INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gpt_batches (
            batch_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            input_file_id TEXT DEFAULT '',
            output_file_id TEXT DEFAULT '',
            error_file_id TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def repair() -> None:
    suffix = f"before-gpt-repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_file(DB_PATH, suffix)
    backup_file(DB_PATH.with_name(f"{DB_PATH.name}-wal"), suffix)
    backup_file(DB_PATH.with_name(f"{DB_PATH.name}-shm"), suffix)

    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS gpt_batch_items;
            DROP TABLE IF EXISTS gpt_batches;
            """
        )
        ensure_batch_tables(conn)
        conn.commit()
    finally:
        conn.close()

    print("Tables GPT réparées.")
    print(f"Sauvegarde créée avec suffixe: {suffix}")


if __name__ == "__main__":
    repair()
