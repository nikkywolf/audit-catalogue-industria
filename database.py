from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "industria_catalogue.db"


APPROVAL_COLUMNS = ["Internal_Variant_ID", "Type", "Erreur", "Date"]
TODO_COLUMNS = [
    "ID",
    "Tache",
    "Description",
    "Assigne",
    "Statut",
    "Priorite",
    "Date_echeance",
    "Cree_par",
    "Date_creation",
    "Date_completion",
]
HISTORY_COLUMNS = ["Date", "Produits", "Conformes", "Action_requise", "Critiques"]
BRAND_SETTINGS_COLUMNS = ["Brand"]
AUDIT_SYNC_TABLES = ["catalogue_report", "brand_summary", "audit_history"]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS catalogue_report (
                Internal_Variant_ID TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS brand_summary (
                Brand TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                Internal_Variant_ID TEXT NOT NULL,
                Type TEXT NOT NULL,
                Erreur TEXT NOT NULL,
                Date TEXT NOT NULL,
                PRIMARY KEY (Internal_Variant_ID, Erreur)
            );

            CREATE TABLE IF NOT EXISTS todos (
                ID INTEGER PRIMARY KEY,
                Tache TEXT NOT NULL,
                Description TEXT DEFAULT '',
                Assigne TEXT DEFAULT '',
                Statut TEXT DEFAULT 'À faire',
                Priorite TEXT DEFAULT '',
                Date_echeance TEXT DEFAULT '',
                Cree_par TEXT DEFAULT '',
                Date_creation TEXT DEFAULT '',
                Date_completion TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS brand_settings (
                Brand TEXT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS audit_history (
                Date TEXT PRIMARY KEY,
                Produits INTEGER NOT NULL,
                Conformes INTEGER NOT NULL,
                Action_requise INTEGER NOT NULL,
                Critiques INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS lightspeed_products (
                source_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                message TEXT DEFAULT ''
            );
            """
        )


def _rows_to_df(rows: Iterable[sqlite3.Row], columns: list[str] | None = None) -> pd.DataFrame:
    data = [dict(row) for row in rows]
    if not data:
        return pd.DataFrame(columns=columns or [])
    return pd.DataFrame(data)


def _json_table_to_df(table: str, fallback_columns: list[str] | None = None) -> pd.DataFrame:
    init_db()
    with connect() as conn:
        rows = conn.execute(f"SELECT payload FROM {table}").fetchall()
    records = [json.loads(row["payload"]) for row in rows]
    if not records:
        return pd.DataFrame(columns=fallback_columns or [])
    return pd.DataFrame(records)


def table_count(table: str) -> int:
    init_db()
    with connect() as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def replace_report(report_df: pd.DataFrame, summary_df: pd.DataFrame) -> None:
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with connect() as conn:
        conn.execute("DELETE FROM catalogue_report")
        conn.execute("DELETE FROM brand_summary")

        for _, row in report_df.fillna("").iterrows():
            variant_id = str(row.get("Internal_Variant_ID", "")).strip()
            if not variant_id:
                variant_id = str(row.name)
            conn.execute(
                """
                INSERT OR REPLACE INTO catalogue_report
                (Internal_Variant_ID, payload, updated_at)
                VALUES (?, ?, ?)
                """,
                (variant_id, json.dumps(row.to_dict(), ensure_ascii=False), now),
            )

        for _, row in summary_df.fillna("").iterrows():
            brand = str(row.get("Brand", "")).strip()
            if not brand:
                brand = str(row.name)
            conn.execute(
                """
                INSERT OR REPLACE INTO brand_summary
                (Brand, payload, updated_at)
                VALUES (?, ?, ?)
                """,
                (brand, json.dumps(row.to_dict(), ensure_ascii=False), now),
            )


def load_report() -> pd.DataFrame:
    return _json_table_to_df("catalogue_report")


def load_brand_summary() -> pd.DataFrame:
    return _json_table_to_df("brand_summary")


def load_approvals() -> pd.DataFrame:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT Internal_Variant_ID, Type, Erreur, Date
            FROM approvals
            ORDER BY Date DESC
            """
        ).fetchall()
    return _rows_to_df(rows, APPROVAL_COLUMNS)


def approve_error(variant_id: str, error_type: str, error: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO approvals
            (Internal_Variant_ID, Type, Erreur, Date)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(variant_id),
                str(error_type),
                str(error),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def remove_approval(variant_id: str, error: str) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            DELETE FROM approvals
            WHERE Internal_Variant_ID = ? AND Erreur = ?
            """,
            (str(variant_id), str(error)),
        )


def load_todos() -> pd.DataFrame:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM todos ORDER BY ID").fetchall()
    df = _rows_to_df(rows, TODO_COLUMNS)
    for column in TODO_COLUMNS:
        if column not in df.columns:
            df[column] = ""
    return df[TODO_COLUMNS]


def save_todos(todos_df: pd.DataFrame) -> None:
    init_db()
    clean_df = todos_df.copy()
    for column in TODO_COLUMNS:
        if column not in clean_df.columns:
            clean_df[column] = ""
    clean_df = clean_df[TODO_COLUMNS].fillna("")

    with connect() as conn:
        conn.execute("DELETE FROM todos")
        for _, row in clean_df.iterrows():
            conn.execute(
                """
                INSERT OR REPLACE INTO todos
                (ID, Tache, Description, Assigne, Statut, Priorite, Date_echeance,
                 Cree_par, Date_creation, Date_completion)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(row[column] for column in TODO_COLUMNS),
            )


def load_processed_brands(all_brands: list[str]) -> list[str]:
    init_db()
    with connect() as conn:
        rows = conn.execute("SELECT Brand FROM brand_settings ORDER BY Brand").fetchall()
    saved_brands = [row["Brand"] for row in rows]
    return [brand for brand in saved_brands if brand in all_brands] or all_brands


def save_processed_brands(processed_brands: list[str]) -> None:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM brand_settings")
        for brand in processed_brands:
            conn.execute(
                "INSERT OR REPLACE INTO brand_settings (Brand) VALUES (?)",
                (str(brand),),
            )


def load_history() -> pd.DataFrame:
    init_db()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT Date, Produits, Conformes, Action_requise, Critiques
            FROM audit_history
            ORDER BY Date
            """
        ).fetchall()
    return _rows_to_df(rows, HISTORY_COLUMNS)


def add_history_row(row: dict) -> None:
    init_db()
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO audit_history
            (Date, Produits, Conformes, Action_requise, Critiques)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                row["Date"],
                int(row["Produits"]),
                int(row["Conformes"]),
                int(row["Action_requise"]),
                int(row["Critiques"]),
            ),
        )


def replace_lightspeed_products(products: list[dict], source_key: str = "id") -> None:
    init_db()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect() as conn:
        for product in products:
            source_id = str(product.get(source_key) or product.get("itemID") or product.get("id"))
            if not source_id:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO lightspeed_products
                (source_id, payload, synced_at)
                VALUES (?, ?, ?)
                """,
                (source_id, json.dumps(product, ensure_ascii=False), now),
            )


def latest_sync_run() -> dict | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT source, status, started_at, finished_at, message
            FROM sync_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row else None


def import_legacy_files() -> None:
    init_db()

    report_file = BASE_DIR / "rapport_qualite_catalogue.xlsx"
    if report_file.exists() and load_report().empty:
        report_df = pd.read_excel(report_file, sheet_name="Tous les produits")
        summary_df = pd.read_excel(report_file, sheet_name="Résumé par marque")
        replace_report(report_df, summary_df)

    approvals_file = BASE_DIR / "approbations_erreurs.csv"
    if approvals_file.exists() and load_approvals().empty:
        approvals_df = pd.read_csv(approvals_file).fillna("")
        for _, row in approvals_df.iterrows():
            approve_error(row["Internal_Variant_ID"], row["Type"], row["Erreur"])

    todos_file = BASE_DIR / "todo_list.csv"
    if todos_file.exists() and load_todos().empty:
        save_todos(pd.read_csv(todos_file))

    brand_file = BASE_DIR / "brand_settings.csv"
    if brand_file.exists():
        settings_df = pd.read_csv(brand_file)
        if "Brand" in settings_df.columns:
            with connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM brand_settings").fetchone()[0]
            if count == 0:
                save_processed_brands(settings_df["Brand"].dropna().astype(str).tolist())

    history_file = BASE_DIR / "historique_audit.csv"
    if history_file.exists() and load_history().empty:
        history_df = pd.read_csv(history_file).fillna("")
        for _, row in history_df.iterrows():
            add_history_row(row.to_dict())


def import_from_sqlite(source_db: Path) -> None:
    init_db()
    if not source_db.exists():
        return

    tables = [
        "catalogue_report",
        "brand_summary",
        "approvals",
        "todos",
        "brand_settings",
        "audit_history",
        "lightspeed_products",
        "sync_runs",
    ]

    with connect() as conn:
        conn.execute("ATTACH DATABASE ? AS source_db", (str(source_db),))

        for table in tables:
            source_exists = conn.execute(
                """
                SELECT COUNT(*)
                FROM source_db.sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table,),
            ).fetchone()[0]

            if not source_exists:
                continue

            destination_count = conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]

            if destination_count > 0:
                continue

            source_columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA source_db.table_info({table})")
            ]
            destination_columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            columns = [col for col in destination_columns if col in source_columns]

            if not columns:
                continue

            column_list = ", ".join(columns)
            conn.execute(
                f"""
                INSERT INTO {table} ({column_list})
                SELECT {column_list}
                FROM source_db.{table}
                """
            )

        conn.commit()
        conn.execute("DETACH DATABASE source_db")


def export_audit_sync_db(target_db: Path) -> None:
    init_db()
    target_db = Path(target_db)
    if target_db.exists():
        target_db.unlink()

    with connect() as conn:
        conn.execute("ATTACH DATABASE ? AS sync_db", (str(target_db),))
        conn.executescript(
            """
            CREATE TABLE sync_db.catalogue_report (
                Internal_Variant_ID TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE sync_db.brand_summary (
                Brand TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE sync_db.audit_history (
                Date TEXT PRIMARY KEY,
                Produits INTEGER NOT NULL,
                Conformes INTEGER NOT NULL,
                Action_requise INTEGER NOT NULL,
                Critiques INTEGER NOT NULL
            );

            INSERT INTO sync_db.catalogue_report
            SELECT * FROM main.catalogue_report;

            INSERT INTO sync_db.brand_summary
            SELECT * FROM main.brand_summary;

            INSERT INTO sync_db.audit_history
            SELECT * FROM main.audit_history;
            """
        )
        conn.commit()
        conn.execute("DETACH DATABASE sync_db")


def import_audit_sync_db(source_db: Path) -> None:
    init_db()
    source_db = Path(source_db)
    if not source_db.exists():
        raise FileNotFoundError(f"Base de synchronisation introuvable: {source_db}")

    with connect() as conn:
        conn.execute("ATTACH DATABASE ? AS sync_db", (str(source_db),))

        for table in AUDIT_SYNC_TABLES:
            source_exists = conn.execute(
                """
                SELECT COUNT(*)
                FROM sync_db.sqlite_master
                WHERE type = 'table' AND name = ?
                """,
                (table,),
            ).fetchone()[0]
            if not source_exists:
                continue

            if table in {"catalogue_report", "brand_summary"}:
                conn.execute(f"DELETE FROM main.{table}")

            source_columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA sync_db.table_info({table})")
            ]
            destination_columns = [
                row["name"]
                for row in conn.execute(f"PRAGMA main.table_info({table})")
            ]
            columns = [col for col in destination_columns if col in source_columns]
            if not columns:
                continue

            column_list = ", ".join(columns)
            conn.execute(
                f"""
                INSERT OR REPLACE INTO main.{table} ({column_list})
                SELECT {column_list}
                FROM sync_db.{table}
                """
            )

        conn.commit()
        conn.execute("DETACH DATABASE sync_db")
