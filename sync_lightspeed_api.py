from __future__ import annotations

from datetime import datetime

from database import connect, init_db, replace_lightspeed_products
from lightspeed_client import LightspeedClient


def start_sync_run(source: str) -> int:
    init_db()

    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO sync_runs (source, status, started_at)
            VALUES (?, ?, ?)
            """,
            (source, "running", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cursor.lastrowid)


def finish_sync_run(run_id: int, status: str, message: str = "") -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE sync_runs
            SET status = ?, finished_at = ?, message = ?
            WHERE id = ?
            """,
            (
                status,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                message,
                run_id,
            ),
        )


def sync_products() -> int:
    run_id = start_sync_run("lightspeed_api")

    try:
        client = LightspeedClient()
        products = list(client.iter_products())
        replace_lightspeed_products(products)
        finish_sync_run(run_id, "success", f"{len(products)} produits synchronisés.")
        return len(products)
    except Exception as exc:
        finish_sync_run(run_id, "error", str(exc))
        raise


if __name__ == "__main__":
    count = sync_products()
    print(f"Synchronisation Lightspeed terminée: {count} produits.")
