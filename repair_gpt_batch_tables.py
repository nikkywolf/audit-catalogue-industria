from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "industria_catalogue.db"
SKIP_TABLES = {"gpt_batch_items", "gpt_batches"}


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


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


def sqlite_objects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND name NOT LIKE 'sqlite_%'
        ORDER BY
          CASE type
            WHEN 'table' THEN 0
            WHEN 'index' THEN 1
            WHEN 'trigger' THEN 2
            WHEN 'view' THEN 3
            ELSE 4
          END,
          name
        """
    ).fetchall()


def copy_table(src: sqlite3.Connection, dst: sqlite3.Connection, table: str) -> int:
    columns = [row["name"] for row in src.execute(f"PRAGMA table_info({quote_identifier(table)})")]
    if not columns:
        return 0
    column_sql = ", ".join(quote_identifier(column) for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    select_sql = f"SELECT {column_sql} FROM {quote_identifier(table)}"
    insert_sql = f"INSERT INTO {quote_identifier(table)} ({column_sql}) VALUES ({placeholders})"

    copied = 0
    cursor = src.execute(select_sql)
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        dst.executemany(insert_sql, rows)
        copied += len(rows)
    return copied


def rebuild_without_gpt_tables() -> None:
    suffix = f"before-gpt-rebuild-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    backup_file(DB_PATH, suffix)
    backup_file(DB_PATH.with_name(f"{DB_PATH.name}-wal"), suffix)
    backup_file(DB_PATH.with_name(f"{DB_PATH.name}-shm"), suffix)

    rebuilt_path = DB_PATH.with_name(f"{DB_PATH.name}.rebuilt-{suffix}")
    if rebuilt_path.exists():
        rebuilt_path.unlink()

    src = sqlite3.connect(DB_PATH, timeout=30)
    src.row_factory = sqlite3.Row
    dst = sqlite3.connect(rebuilt_path)
    dst.row_factory = sqlite3.Row

    try:
        objects = sqlite_objects(src)
        table_names = {
            row["name"]
            for row in objects
            if row["type"] == "table" and row["name"] not in SKIP_TABLES
        }

        for row in objects:
            if row["type"] == "table" and row["name"] in table_names:
                dst.execute(row["sql"])

        copied_counts: list[tuple[str, int]] = []
        for table in sorted(table_names):
            copied_counts.append((table, copy_table(src, dst, table)))

        for row in objects:
            if row["type"] == "index" and row["tbl_name"] in table_names:
                dst.execute(row["sql"])
            if row["type"] == "trigger" and row["tbl_name"] in table_names:
                dst.execute(row["sql"])
            if row["type"] == "view" and row["name"] not in SKIP_TABLES:
                dst.execute(row["sql"])

        ensure_batch_tables(dst)
        dst.execute("PRAGMA user_version = 1")
        dst.commit()

        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"Nouvelle base invalide: {check}")

        src.close()
        dst.close()

        os.replace(rebuilt_path, DB_PATH)
        for suffix_name in ("-wal", "-shm"):
            sidecar = DB_PATH.with_name(f"{DB_PATH.name}{suffix_name}")
            if sidecar.exists():
                sidecar.unlink()

        print("Base reconstruite sans les anciennes tables GPT.")
        for table, count in copied_counts:
            if table in {"ecom_products", "ignored_products", "brand_settings", "approvals", "todos", "catalogue_report"}:
                print(f"{table}: {count}")
        print(f"Sauvegarde créée avec suffixe: {suffix}")
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass


if __name__ == "__main__":
    rebuild_without_gpt_tables()
