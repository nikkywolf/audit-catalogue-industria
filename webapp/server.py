from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DB_PATH = PROJECT_DIR / "industria_catalogue.db"

IGNORED_WHEN_APPROVED_ERRORS = {
    "Produit invisible — vérifier si volontaire",
    "Produit invisible - vérifier si volontaire",
}

LIGHTSPEED_ADMIN_PRODUCT_URL_TEMPLATE = os.getenv(
    "LIGHTSPEED_ADMIN_PRODUCT_URL_TEMPLATE",
    "https://industria-coiffure-641699.shoplightspeed.com/admin/products/{product_id}",
)

USERS = {
    "vero": {"name": "Vero", "role": "admin"},
    "nathalie": {"name": "Nathalie", "role": "editor"},
    "virginy": {"name": "Virginy", "role": "editor"},
}

app = FastAPI(title="Industria Catalogue Audit")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.shoplightspeed\.com",
    allow_credentials=False,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def load_dotenv() -> None:
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


class ApprovalPayload(BaseModel):
    variant_id: str
    error_type: str = ""
    error: str


class TodoPayload(BaseModel):
    tache: str
    description: str = ""
    assigne: str = ""
    date_echeance: str = ""


class TodoPatchPayload(BaseModel):
    description: Optional[str] = None
    statut: Optional[str] = None
    date_echeance: Optional[str] = None


class BrandPayload(BaseModel):
    brand: str


class BatchQueuePayload(BaseModel):
    variant_ids: list[str] = []
    limit: int = 50


class BatchSubmitPayload(BaseModel):
    limit: int = 100


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_batch_tables() -> None:
    with connect() as conn:
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


def current_user(x_remote_user: Optional[str] = Header(default=None)) -> dict[str, str]:
    username = (x_remote_user or "vero").lower()
    if username not in USERS:
        username = "vero"
    return {"username": username, **USERS[username]}


def require_admin(x_remote_user: Optional[str]) -> dict[str, str]:
    user = current_user(x_remote_user)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Accès admin requis")
    return user


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "None", "nan"}:
        return ""
    return text


def split_error_list(value: Any) -> list[str]:
    text = clean(value)
    if not text:
        return []
    return [item.strip() for item in text.split("|") if item.strip()]


def row_errors(row: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for error in split_error_list(row.get("Erreurs critiques")):
        errors.append({"type": "Critique", "error": error})
    for error in split_error_list(row.get("Erreurs majeures")):
        errors.append({"type": "Majeure", "error": error})
    for error in split_error_list(row.get("Erreurs mineures")):
        errors.append({"type": "Mineure", "error": error})
    for error in split_error_list(row.get("Alertes catalogue")):
        errors.append({"type": "Catalogue", "error": error})
    return errors


def load_approvals() -> set[tuple[str, str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT Internal_Variant_ID, Erreur FROM approvals"
        ).fetchall()
    return {(str(row["Internal_Variant_ID"]), str(row["Erreur"])) for row in rows}


def load_processed_brands() -> set[str]:
    with connect() as conn:
        rows = conn.execute("SELECT Brand FROM brand_settings").fetchall()
    return {str(row["Brand"]) for row in rows}


def load_json_payload_table(table: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(f"SELECT payload FROM {table}").fetchall()
    records: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def load_products() -> list[dict[str, Any]]:
    return load_json_payload_table("catalogue_report")


def load_brand_summary() -> list[dict[str, Any]]:
    return load_json_payload_table("brand_summary")


def product_count_by_brand(products: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in products:
        brand = clean(row.get("Brand"))
        if brand:
            counts[brand] = counts.get(brand, 0) + 1
    return counts


def product_id(row: dict[str, Any]) -> str:
    return str(row.get("Internal_Variant_ID", ""))


def find_product(variant_id: str) -> Optional[dict[str, Any]]:
    for row in load_products():
        if product_id(row) == variant_id:
            return row
    return None


def lightspeed_admin_url(row: dict[str, Any]) -> str:
    product_internal_id = clean(row.get("Internal_ID"))
    if not product_internal_id:
        return ""
    return LIGHTSPEED_ADMIN_PRODUCT_URL_TEMPLATE.format(
        product_id=product_internal_id,
        internal_id=product_internal_id,
        variant_id=product_id(row).replace("!ID:", ""),
    )


def is_ignored_by_approval(row: dict[str, Any], approvals: set[tuple[str, str]]) -> bool:
    variant_id = product_id(row)
    return any((variant_id, error) in approvals for error in IGNORED_WHEN_APPROVED_ERRORS)


def summarize_product(row: dict[str, Any], approvals: set[tuple[str, str]]) -> dict[str, Any]:
    variant_id = product_id(row)
    errors = row_errors(row)
    approved_count = sum(1 for item in errors if (variant_id, item["error"]) in approvals)
    remaining_count = len(errors) - approved_count
    return {
        "Internal_Variant_ID": variant_id,
        "Internal_ID": clean(row.get("Internal_ID")),
        "Brand": clean(row.get("Brand")),
        "FC_Title_Short": clean(row.get("FC_Title_Short")),
        "SKU": clean(row.get("SKU")),
        "UPC": clean(row.get("UPC")),
        "Score": row.get("Score", ""),
        "Priorité": clean(row.get("Priorité")),
        "Type de correction": clean(row.get("Type de correction")),
        "Alertes catalogue": clean(row.get("Alertes catalogue")),
        "Lightspeed_Admin_URL": lightspeed_admin_url(row),
        "Erreurs restantes": remaining_count,
        "Erreurs approuvées": approved_count,
    }


def product_autofill_prompt(row: dict[str, Any]) -> list[dict[str, str]]:
    schema_keys = [
        "FR_Title_Short", "FR_Title_Long", "US_Title_Short", "US_Title_Long",
        "FC_Title_Short", "FC_Title_Long", "FR_Description_Short",
        "US_Description_Short", "FC_Description_Short", "FR_Description_Long",
        "US_Description_Long", "FC_Description_Long", "FR_URL", "US_URL",
        "FC_URL", "FR_Meta_Title", "FR_Meta_Description", "FR_Meta_Keywords",
        "FR_Google_Category", "US_Meta_Title", "US_Meta_Description",
        "US_Meta_Keywords", "US_Google_Category", "FC_Meta_Title",
        "FC_Meta_Description", "FC_Meta_Keywords", "FC_Google_Category", "Tags",
    ]
    system_prompt = (
        "Tu es l'assistant catalogue Industria. Génère uniquement un objet JSON valide, "
        "sans markdown, sans texte avant ou après. Respecte exactement les champs attendus "
        "par Lightspeed. Les descriptions longues peuvent contenir du HTML valide. "
        "N'inclus pas Produits Associés dans le JSON. Mets les tags dans la clé Tags si des tags sont pertinents."
    )
    extra_rules_path = PROJECT_DIR / "product_gpt_rules.md"
    extra_rules = extra_rules_path.read_text(encoding="utf-8") if extra_rules_path.exists() else ""
    user_prompt = {
        "task": "Créer le JSON de remplissage Lightspeed pour ce produit.",
        "expected_keys": schema_keys,
        "product": row,
        "extra_rules": extra_rules,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
    ]


def openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="OPENAI_API_KEY manquante. Ajoute ta clé OpenAI dans .env ou sur le serveur.",
        )
    return api_key


def openai_request(path: str, payload: Optional[dict[str, Any]] = None, method: str = "POST") -> dict[str, Any]:
    args = [
        "curl",
        "-sS",
        "-X", method,
        f"https://api.openai.com{path}",
        "-H", f"Authorization: Bearer {openai_api_key()}",
        "-H", "Content-Type: application/json",
    ]
    if payload is not None:
        args += ["-d", json.dumps(payload, ensure_ascii=False)]

    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Requête OpenAI impossible: {exc}")

    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Requête OpenAI échouée: {result.stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"Réponse OpenAI invalide: {result.stdout[:500]}")

    if "error" in data:
        raise HTTPException(status_code=502, detail=f"OpenAI refusé: {data['error']}")

    return data


def call_openai_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    openai_api_key()

    payload = {
        "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    data = openai_request("/v1/chat/completions", payload)
    content = data["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Réponse OpenAI invalide: {exc}")


def is_batch_correctable(row: dict[str, Any]) -> bool:
    text = " | ".join(
        clean(row.get(column))
        for column in [
            "Type de correction",
            "Erreurs critiques",
            "Erreurs majeures",
            "Erreurs mineures",
        ]
    ).lower()
    patterns = [
        "titre",
        "html",
        "h2",
        "description",
        "meta",
        "seo",
        "url",
    ]
    return any(pattern in text for pattern in patterns)


def is_masked_product(row: dict[str, Any]) -> bool:
    visible = clean(row.get("Visible")).upper()
    alerts = clean(row.get("Alertes catalogue")).lower()
    return visible == "N" or "produit invisible" in alerts


def batch_item_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Internal_Variant_ID": product_id(row),
        "Internal_ID": clean(row.get("Internal_ID")),
        "Brand": clean(row.get("Brand")),
        "Product_Title": clean(row.get("FC_Title_Short")),
        "SKU": clean(row.get("SKU")),
        "Priorité": clean(row.get("Priorité")),
        "Type de correction": clean(row.get("Type de correction")),
        "Lightspeed_Admin_URL": lightspeed_admin_url(row),
    }


def openai_file_upload_jsonl(path: Path) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "-X", "POST",
                "https://api.openai.com/v1/files",
                "-H", f"Authorization: Bearer {openai_api_key()}",
                "-F", "purpose=batch",
                "-F", f"file=@{path}",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Upload OpenAI impossible: {exc}")

    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Upload OpenAI échoué: {result.stderr}")

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail=f"Réponse OpenAI invalide: {result.stdout[:500]}")

    if "error" in payload:
        raise HTTPException(status_code=502, detail=f"Upload OpenAI refusé: {payload['error']}")

    return payload


def openai_file_content(file_id: str) -> str:
    request = Request(
        f"https://api.openai.com/v1/files/{file_id}/content",
        headers={"Authorization": f"Bearer {openai_api_key()}"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=120) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=502, detail=f"Lecture fichier OpenAI refusée: {body_text}")


def product_matches_search(row: dict[str, Any], search: str) -> bool:
    if not search:
        return True
    haystack = " ".join(str(value) for value in row.values()).lower()
    return search.lower() in haystack


def filter_products(
    products: list[dict[str, Any]],
    approvals: set[tuple[str, str]],
    processed_brands: set[str],
    search: str = "",
    brand: str = "",
    priority: str = "",
    correction: str = "",
    approval: str = "pending",
) -> list[dict[str, Any]]:
    result = []
    for row in products:
        row_brand = clean(row.get("Brand"))
        if processed_brands and row_brand not in processed_brands:
            continue
        if is_ignored_by_approval(row, approvals):
            continue
        if brand and row_brand != brand:
            continue
        if priority and clean(row.get("Priorité")) != priority:
            continue
        if correction and correction not in clean(row.get("Type de correction")):
            continue
        if not product_matches_search(row, search):
            continue
        summary = summarize_product(row, approvals)
        if approval == "pending" and summary["Erreurs restantes"] <= 0:
            continue
        if approval == "approved" and not (
            summary["Erreurs restantes"] == 0 and summary["Erreurs approuvées"] > 0
        ):
            continue
        result.append(summary)
    return result


@app.get("/")
def index() -> FileResponse:
    return FileResponse(APP_DIR / "static" / "index.html")


@app.get("/api/me")
def api_me(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    return current_user(x_remote_user)


@app.get("/api/bootstrap")
def api_bootstrap():
    products = load_products()
    approvals = load_approvals()
    processed_brands = load_processed_brands()
    all_brands = sorted({clean(row.get("Brand")) for row in products if clean(row.get("Brand"))})
    active_products = [
        row for row in products
        if clean(row.get("Brand")) in processed_brands and not is_ignored_by_approval(row, approvals)
    ]
    ignored_count = sum(
        1 for row in products
        if clean(row.get("Brand")) not in processed_brands or is_ignored_by_approval(row, approvals)
    )
    priorities = sorted({clean(row.get("Priorité")) for row in active_products if clean(row.get("Priorité"))})
    correction_types = sorted({
        item.strip()
        for row in active_products
        for item in clean(row.get("Type de correction")).split("|")
        if item.strip()
    })
    with connect() as conn:
        latest_sync = conn.execute(
            "SELECT source, status, started_at, finished_at, message FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        history = [dict(row) for row in conn.execute("SELECT * FROM audit_history ORDER BY Date").fetchall()]

    return {
        "metrics": {
            "products": len(active_products),
            "conformes": sum(1 for row in active_products if clean(row.get("Priorité")) == "Conforme"),
            "action_required": sum(1 for row in active_products if clean(row.get("Priorité")) == "Action requise"),
            "critical": sum(1 for row in active_products if clean(row.get("Priorité")) == "Critique"),
            "approved_errors": sum(summarize_product(row, approvals)["Erreurs approuvées"] for row in active_products),
            "ecom_products": len(products),
            "ignored_products": ignored_count,
        },
        "brands": all_brands,
        "processed_brands": sorted(processed_brands),
        "priorities": priorities,
        "correction_types": correction_types,
        "brand_summary": load_brand_summary(),
        "history": history,
        "latest_sync": dict(latest_sync) if latest_sync else None,
    }


@app.get("/api/products")
def api_products(
    search: str = "",
    brand: str = "",
    priority: str = "",
    correction: str = "",
    approval: str = "pending",
    limit: int = 100,
    offset: int = 0,
):
    products = load_products()
    approvals = load_approvals()
    processed_brands = load_processed_brands()
    filtered = filter_products(
        products,
        approvals,
        processed_brands,
        search=search,
        brand=brand,
        priority=priority,
        correction=correction,
        approval=approval,
    )
    page = filtered[offset: offset + min(limit, 500)]
    return {"total": len(filtered), "items": page}


@app.get("/api/products/{variant_id}")
def api_product_detail(variant_id: str):
    approvals = load_approvals()
    row = find_product(variant_id)
    if row:
        errors = row_errors(row)
        return {
            "product": summarize_product(row, approvals),
            "unresolved": [
                item for item in errors
                if (variant_id, item["error"]) not in approvals
            ],
            "approved": [
                item for item in errors
                if (variant_id, item["error"]) in approvals
            ],
        }
    raise HTTPException(status_code=404, detail="Produit introuvable")


@app.get("/api/products/{variant_id}/autofill-json")
def api_product_autofill_json(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    row = find_product(variant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return call_openai_json(product_autofill_prompt(row))


@app.get("/api/products/{variant_id}/batch-json")
def api_product_batch_json(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    with connect() as conn:
        item = conn.execute(
            """
            SELECT result_json
            FROM gpt_batch_items
            WHERE Internal_Variant_ID = ? AND status IN ('completed', 'approved')
            """,
            (variant_id,),
        ).fetchone()
    if not item or not item["result_json"]:
        raise HTTPException(status_code=404, detail="JSON batch introuvable pour ce produit")
    return json.loads(item["result_json"])


@app.post("/api/approvals")
def api_approve(payload: ApprovalPayload):
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO approvals
            (Internal_Variant_ID, Type, Erreur, Date)
            VALUES (?, ?, ?, ?)
            """,
            (
                payload.variant_id,
                payload.error_type,
                payload.error,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    return {"ok": True}


@app.delete("/api/approvals")
def api_remove_approval(payload: ApprovalPayload):
    with connect() as conn:
        conn.execute(
            "DELETE FROM approvals WHERE Internal_Variant_ID = ? AND Erreur = ?",
            (payload.variant_id, payload.error),
        )
    return {"ok": True}


@app.get("/api/ignored")
def api_ignored(search: str = "", limit: int = 100):
    approvals = load_approvals()
    ignored = []
    for row in load_products():
        if not is_ignored_by_approval(row, approvals):
            continue
        if not product_matches_search(row, search):
            continue
        ignored.append(summarize_product(row, approvals))
    return {"total": len(ignored), "items": ignored[: min(limit, 500)]}


@app.post("/api/ignored/{variant_id}/restore")
def api_restore_ignored(variant_id: str):
    with connect() as conn:
        for error in IGNORED_WHEN_APPROVED_ERRORS:
            conn.execute(
                "DELETE FROM approvals WHERE Internal_Variant_ID = ? AND Erreur = ?",
                (variant_id, error),
            )
    return {"ok": True}


@app.get("/api/brands")
def api_brands(search: str = ""):
    products = load_products()
    processed_brands = load_processed_brands()
    counts = product_count_by_brand(products)
    query = search.lower().strip()

    active = []
    ignored = []
    for brand in sorted(counts):
        if query and query not in brand.lower():
            continue
        item = {"Brand": brand, "Produits": counts[brand]}
        if brand in processed_brands:
            active.append(item)
        else:
            ignored.append(item)
    return {
        "active": active,
        "ignored": ignored,
        "active_total": len([brand for brand in counts if brand in processed_brands]),
        "ignored_total": len([brand for brand in counts if brand not in processed_brands]),
    }


@app.post("/api/brands/ignore")
def api_ignore_brand(payload: BrandPayload):
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status_code=400, detail="Marque manquante")
    with connect() as conn:
        conn.execute("DELETE FROM brand_settings WHERE Brand = ?", (brand,))
    return {"ok": True}


@app.post("/api/brands/restore")
def api_restore_brand(payload: BrandPayload):
    brand = payload.brand.strip()
    if not brand:
        raise HTTPException(status_code=400, detail="Marque manquante")
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO brand_settings (Brand) VALUES (?)", (brand,))
    return {"ok": True}


@app.get("/api/gpt-batches/candidates")
def api_gpt_batch_candidates(
    search: str = "",
    limit: int = 200,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    approvals = load_approvals()
    processed_brands = load_processed_brands()
    candidates = []
    with connect() as conn:
        existing = {
            row["Internal_Variant_ID"]
            for row in conn.execute("SELECT Internal_Variant_ID FROM gpt_batch_items").fetchall()
        }
    for row in load_products():
        if product_id(row) in existing:
            continue
        if clean(row.get("Brand")) not in processed_brands:
            continue
        if is_ignored_by_approval(row, approvals):
            continue
        if is_masked_product(row):
            continue
        if not product_matches_search(row, search):
            continue
        if not is_batch_correctable(row):
            continue
        candidates.append(batch_item_summary(row))
    return {"total": len(candidates), "items": candidates[: min(limit, 500)]}


@app.post("/api/gpt-batches/queue")
def api_gpt_batch_queue(
    payload: BatchQueuePayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    products = load_products()
    product_map = {product_id(row): row for row in products}
    selected_ids = payload.variant_ids
    if not selected_ids:
        approvals = load_approvals()
        processed_brands = load_processed_brands()
        selected_ids = [
            product_id(row)
            for row in products
            if clean(row.get("Brand")) in processed_brands
            and not is_ignored_by_approval(row, approvals)
            and not is_masked_product(row)
            and is_batch_correctable(row)
        ][: max(1, min(payload.limit, 500))]

    created = 0
    now = now_text()
    with connect() as conn:
        for variant_id in selected_ids[:500]:
            row = product_map.get(variant_id)
            if not row:
                continue
            if is_masked_product(row):
                continue
            if not is_batch_correctable(row):
                continue
            summary = batch_item_summary(row)
            result = conn.execute(
                """
                INSERT OR IGNORE INTO gpt_batch_items
                (Internal_Variant_ID, Internal_ID, Brand, Product_Title, SKU, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    summary["Internal_Variant_ID"],
                    summary["Internal_ID"],
                    summary["Brand"],
                    summary["Product_Title"],
                    summary["SKU"],
                    now,
                    now,
                ),
            )
            created += int(result.rowcount or 0)
    return {"ok": True, "created": created}


@app.get("/api/gpt-batches/items")
def api_gpt_batch_items(
    status: str = "pending",
    search: str = "",
    limit: int = 200,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    params: list[Any] = [status]
    where = "status = ?"
    if search:
        where += " AND (Brand LIKE ? OR Product_Title LIKE ? OR SKU LIKE ?)"
        term = f"%{search}%"
        params.extend([term, term, term])
    params.append(min(limit, 500))
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM gpt_batch_items
            WHERE {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM gpt_batch_items WHERE {where}",
            params[:-1],
        ).fetchone()[0]
    items = [dict(row) for row in rows]
    for item in items:
        row = find_product(item["Internal_Variant_ID"])
        item["Lightspeed_Admin_URL"] = lightspeed_admin_url(row) if row else ""
    return {"total": total, "items": items}


@app.post("/api/gpt-batches/submit")
def api_gpt_batch_submit(
    payload: BatchSubmitPayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    limit = max(1, min(payload.limit, 500))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM gpt_batch_items
            WHERE status = 'pending'
            ORDER BY created_at
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    if not rows:
        return {"ok": True, "message": "Aucun produit en attente."}

    batch_lines = []
    now = now_text()
    request_by_variant = {}
    for item in rows:
        row = find_product(item["Internal_Variant_ID"])
        if not row:
            continue
        if is_masked_product(row):
            continue
        if not is_batch_correctable(row):
            continue
        custom_id = str(item["Internal_Variant_ID"])
        request_body = {
            "model": os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            "messages": product_autofill_prompt(row),
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        request_record = {
            "custom_id": custom_id,
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": request_body,
        }
        batch_lines.append(json.dumps(request_record, ensure_ascii=False))
        request_by_variant[custom_id] = json.dumps(request_record, ensure_ascii=False)

    if not batch_lines:
        return {"ok": False, "message": "Aucun produit valide à envoyer."}

    batch_dir = PROJECT_DIR / "openai_batches"
    batch_dir.mkdir(exist_ok=True)
    input_path = batch_dir / f"industria_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    input_path.write_text("\n".join(batch_lines) + "\n", encoding="utf-8")

    uploaded = openai_file_upload_jsonl(input_path)
    batch = openai_request(
        "/v1/batches",
        {
            "input_file_id": uploaded["id"],
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
    )
    batch_id = batch["id"]

    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO gpt_batches
            (batch_id, status, input_file_id, output_file_id, error_file_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                batch.get("status", "submitted"),
                uploaded["id"],
                batch.get("output_file_id") or "",
                batch.get("error_file_id") or "",
                now,
                now,
            ),
        )
        for custom_id, request_json in request_by_variant.items():
            conn.execute(
                """
                UPDATE gpt_batch_items
                SET status = 'submitted',
                    batch_id = ?,
                    custom_id = ?,
                    request_json = ?,
                    updated_at = ?
                WHERE Internal_Variant_ID = ?
                """,
                (batch_id, custom_id, request_json, now, custom_id),
            )

    return {"ok": True, "batch_id": batch_id, "count": len(request_by_variant)}


@app.post("/api/gpt-batches/sync")
def api_gpt_batch_sync(
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    synced = 0
    now = now_text()
    with connect() as conn:
        batches = conn.execute(
            "SELECT * FROM gpt_batches WHERE status NOT IN ('completed_synced', 'failed', 'cancelled')"
        ).fetchall()

    for local_batch in batches:
        batch = openai_request(f"/v1/batches/{local_batch['batch_id']}", None, method="GET")
        status = batch.get("status", "")
        output_file_id = batch.get("output_file_id") or ""
        error_file_id = batch.get("error_file_id") or ""
        with connect() as conn:
            conn.execute(
                """
                UPDATE gpt_batches
                SET status = ?, output_file_id = ?, error_file_id = ?, updated_at = ?
                WHERE batch_id = ?
                """,
                (status, output_file_id, error_file_id, now, local_batch["batch_id"]),
            )

        if status != "completed" or not output_file_id:
            continue

        content = openai_file_content(output_file_id)
        with connect() as conn:
            for line in content.splitlines():
                if not line.strip():
                    continue
                record = json.loads(line)
                custom_id = record.get("custom_id", "")
                response = record.get("response") or {}
                body = response.get("body") or {}
                try:
                    result_text = body["choices"][0]["message"]["content"]
                    result_json = json.dumps(json.loads(result_text), ensure_ascii=False, indent=2)
                    conn.execute(
                        """
                        UPDATE gpt_batch_items
                        SET status = 'completed',
                            result_json = ?,
                            error = '',
                            updated_at = ?
                        WHERE Internal_Variant_ID = ?
                        """,
                        (result_json, now, custom_id),
                    )
                    synced += 1
                except Exception as exc:
                    conn.execute(
                        """
                        UPDATE gpt_batch_items
                        SET status = 'error',
                            error = ?,
                            updated_at = ?
                        WHERE Internal_Variant_ID = ?
                        """,
                        (str(exc), now, custom_id),
                    )
            conn.execute(
                "UPDATE gpt_batches SET status = 'completed_synced', updated_at = ? WHERE batch_id = ?",
                (now, local_batch["batch_id"]),
            )

    return {"ok": True, "synced": synced}


@app.post("/api/gpt-batches/items/{variant_id}/approve")
def api_gpt_batch_approve(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_batch_tables()
    with connect() as conn:
        conn.execute(
            """
            UPDATE gpt_batch_items
            SET status = 'approved',
                updated_at = ?
            WHERE Internal_Variant_ID = ? AND status = 'completed'
            """,
            (now_text(), variant_id),
        )
    return {"ok": True}


@app.get("/api/todos")
def api_todos():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM todos ORDER BY ID").fetchall()
    return {"items": [dict(row) for row in rows]}


@app.post("/api/todos")
def api_create_todo(payload: TodoPayload, request: Request):
    username = request.headers.get("X-Remote-User", "vero").lower()
    display_name = USERS.get(username, USERS["vero"])["name"]
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(ID), 0) + 1 AS next_id FROM todos").fetchone()
        next_id = int(row["next_id"])
        conn.execute(
            """
            INSERT INTO todos
            (ID, Tache, Description, Assigne, Statut, Priorite, Date_echeance,
             Cree_par, Date_creation, Date_completion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_id,
                payload.tache.strip(),
                payload.description.strip(),
                payload.assigne,
                "À faire",
                "",
                payload.date_echeance,
                display_name,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "",
            ),
        )
    return {"ok": True, "id": next_id}


@app.patch("/api/todos/{todo_id}")
def api_update_todo(todo_id: int, payload: TodoPatchPayload):
    updates = {}
    if payload.description is not None:
        updates["Description"] = payload.description
    if payload.date_echeance is not None:
        updates["Date_echeance"] = payload.date_echeance
    if payload.statut is not None:
        updates["Statut"] = payload.statut
        if payload.statut == "Terminé":
            updates["Date_completion"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if not updates:
        return {"ok": True}

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    values = list(updates.values()) + [todo_id]
    with connect() as conn:
        conn.execute(f"UPDATE todos SET {set_clause} WHERE ID = ?", values)
    return {"ok": True}


@app.delete("/api/todos/{todo_id}")
def api_delete_todo(todo_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM todos WHERE ID = ?", (todo_id,))
    return {"ok": True}
