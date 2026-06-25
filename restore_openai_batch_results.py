from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "industria_catalogue.db"


def load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def openai_api_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY manquante dans l'environnement ou .env")
    return key


def curl_json(path: str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "-H",
            f"Authorization: Bearer {openai_api_key()}",
            f"https://api.openai.com{path}",
        ],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    payload = json.loads(result.stdout)
    if "error" in payload:
        raise RuntimeError(str(payload["error"]))
    return payload


def curl_text(path: str) -> str:
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "-H",
            f"Authorization: Bearer {openai_api_key()}",
            f"https://api.openai.com{path}",
        ],
        cwd=BASE_DIR,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout


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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(gpt_batch_items)").fetchall()}
    if "force_submit" not in columns:
        conn.execute("ALTER TABLE gpt_batch_items ADD COLUMN force_submit INTEGER NOT NULL DEFAULT 0")


def product_lookup(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for row in conn.execute("SELECT payload FROM catalogue_report").fetchall():
        payload = json.loads(row["payload"])
        variant_id = str(payload.get("Internal_Variant_ID", ""))
        if variant_id:
            lookup[variant_id] = payload
    return lookup


def parse_result_json(record: dict[str, Any]) -> str:
    body = (record.get("response") or {}).get("body") or {}
    content = body["choices"][0]["message"]["content"]
    return json.dumps(json.loads(content), ensure_ascii=False, indent=2)


def restore_batch(conn: sqlite3.Connection, batch: dict[str, Any], products: dict[str, dict[str, Any]]) -> int:
    output_file_id = batch.get("output_file_id") or ""
    if not output_file_id:
        return 0

    content = curl_text(f"/v1/files/{output_file_id}/content")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    restored = 0

    conn.execute(
        """
        INSERT OR REPLACE INTO gpt_batches
        (batch_id, status, input_file_id, output_file_id, error_file_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            batch.get("id", ""),
            "completed_synced",
            batch.get("input_file_id") or "",
            output_file_id,
            batch.get("error_file_id") or "",
            now,
            now,
        ),
    )

    for line in content.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        variant_id = str(record.get("custom_id", ""))
        if not variant_id:
            continue
        product = products.get(variant_id, {})
        try:
            result_json = parse_result_json(record)
            error = ""
            status = "completed"
        except Exception as exc:
            result_json = ""
            error = str(exc)
            status = "error"

        conn.execute(
            """
            INSERT INTO gpt_batch_items
            (Internal_Variant_ID, Internal_ID, Brand, Product_Title, SKU, status,
             batch_id, custom_id, result_json, error, force_submit, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(Internal_Variant_ID) DO UPDATE SET
                status = excluded.status,
                batch_id = excluded.batch_id,
                custom_id = excluded.custom_id,
                result_json = excluded.result_json,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                variant_id,
                str(product.get("Internal_ID", "")),
                str(product.get("Brand", "")),
                str(product.get("FC_Title_Short", "")),
                str(product.get("SKU", "")),
                status,
                batch.get("id", ""),
                variant_id,
                result_json,
                error,
                now,
                now,
            ),
        )
        restored += 1

    return restored


def main() -> None:
    load_dotenv()
    openai_api_key()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_batch_tables(conn)
    products = product_lookup(conn)

    batches = curl_json("/v1/batches?limit=100").get("data", [])
    total = 0
    for batch in batches:
        if batch.get("status") != "completed" or not batch.get("output_file_id"):
            continue
        restored = restore_batch(conn, batch, products)
        if restored:
            print(f"{batch.get('id')}: {restored} résultat(s) restauré(s)")
            total += restored

    conn.commit()
    print(f"Total restauré: {total}")


if __name__ == "__main__":
    main()
