from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
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
    allow_origin_regex=r"https://(.*\.shoplightspeed\.com|dashboardindustria\.com)",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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


class ProductSourcePayload(BaseModel):
    source_text: str = ""
    source_url: str = ""


class BatchQueuePayload(BaseModel):
    variant_ids: list[str] = []
    limit: int = 50
    force: bool = False


class BatchSubmitPayload(BaseModel):
    limit: int = 100
    variant_ids: list[str] = []


class BatchResetPayload(BaseModel):
    variant_ids: list[str]


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


def ensure_product_source_table() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS product_sources (
                Internal_Variant_ID TEXT PRIMARY KEY,
                source_text TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            )
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


def require_user(x_remote_user: Optional[str]) -> dict[str, str]:
    return current_user(x_remote_user)


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "None", "nan"}:
        return ""
    return text



def product_display_title(row: dict[str, Any]) -> str:
    return clean(row.get("FC_Title_Short")) or clean(row.get("FR_Title_Short")) or clean(row.get("US_Title_Short")) or product_id(row)


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


def product_internal_id(row: dict[str, Any]) -> str:
    return clean(row.get("Internal_ID"))


def matrix_product_ids(products: Optional[list[dict[str, Any]]] = None) -> set[str]:
    products = products or load_products()
    variants_by_product: dict[str, set[str]] = {}
    for row in products:
        internal_id = product_internal_id(row)
        variant_id = product_id(row)
        if internal_id and variant_id:
            variants_by_product.setdefault(internal_id, set()).add(variant_id)
    return {internal_id for internal_id, variants in variants_by_product.items() if len(variants) > 1}


def is_matrix_product(row: dict[str, Any], matrix_ids: Optional[set[str]] = None) -> bool:
    internal_id = product_internal_id(row)
    if not internal_id:
        return False
    return internal_id in (matrix_ids if matrix_ids is not None else matrix_product_ids())


def product_family_title(value: str, sku: str = "") -> str:
    text = clean(value).lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    text = re.sub(r"\s*-\s*[a-z0-9._/]+$", "", text).strip()
    if sku:
        text = re.sub(rf"\b{re.escape(sku.lower())}\b", "", text).strip()
    text = re.sub(
        r"\([^)]*(?:ml|oz|fl\.?\s*oz|g|gr|kg|l|litre|liter|un|po|in|mm|cm|w|v|inch|ounces?)[^)]*\)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*(?:ml|oz|fl\.?\s*oz|g|gr|kg|l|litre|liter|un|po|in|mm|cm|w|v|inch|ounces?)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\b\d+\s*[xX]\s*\d+(?:[.,]\d+)?\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"\s+", " ", text)


def product_family_key(row: dict[str, Any]) -> str:
    brand = clean(row.get("Brand")).lower()
    title = (
        clean(row.get("FC_Title_Short"))
        or clean(row.get("FR_Title_Short"))
        or clean(row.get("US_Title_Short"))
        or clean(row.get("Product_Title"))
    )
    family_title = product_family_title(title, clean(row.get("SKU")))
    if not brand or not family_title:
        return product_id(row)
    return f"{brand}|{family_title}"


def lightspeed_admin_url(row: dict[str, Any]) -> str:
    internal_id = product_internal_id(row)
    if not internal_id:
        return ""
    return LIGHTSPEED_ADMIN_PRODUCT_URL_TEMPLATE.format(
        product_id=internal_id,
        internal_id=internal_id,
        variant_id=product_id(row).replace("!ID:", ""),
    )


def batch_item_as_product(item: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    return {
        "Internal_Variant_ID": clean(item["Internal_Variant_ID"]),
        "Internal_ID": clean(item["Internal_ID"]),
        "Brand": clean(item["Brand"]),
        "FC_Title_Short": clean(item["Product_Title"]),
        "FR_Title_Short": clean(item["Product_Title"]),
        "US_Title_Short": clean(item["Product_Title"]),
        "FC_Title_Long": clean(item["Product_Title"]),
        "Product_Title": clean(item["Product_Title"]),
        "SKU": clean(item["SKU"]),
    }


def batch_catalog_state(
    row: Optional[dict[str, Any]],
    approvals: set[tuple[str, str]],
    processed_brands: set[str],
) -> str:
    if not row:
        return "Absent export"
    brand = clean(row.get("Brand"))
    if brand not in processed_brands:
        return "Marque ignorée"
    if is_ignored_by_approval(row, approvals):
        return "Ignoré"
    if is_masked_product(row):
        return "Masqué"
    return "Dans catalogue"


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



def product_source_info(variant_id: str) -> dict[str, str]:
    ensure_product_source_table()
    with connect() as conn:
        row = conn.execute(
            "SELECT source_text, source_url, updated_at FROM product_sources WHERE Internal_Variant_ID = ?",
            (variant_id,),
        ).fetchone()
    if not row:
        return {"source_text": "", "source_url": "", "updated_at": ""}
    return dict(row)


def product_source_ids(variant_ids: list[str]) -> set[str]:
    ids = [variant_id for variant_id in variant_ids if variant_id]
    if not ids:
        return set()
    ensure_product_source_table()
    placeholders = ",".join("?" for _ in ids)
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT Internal_Variant_ID
            FROM product_sources
            WHERE Internal_Variant_ID IN ({placeholders})
              AND (TRIM(COALESCE(source_text, '')) != '' OR TRIM(COALESCE(source_url, '')) != '')
            """,
            ids,
        ).fetchall()
    return {row["Internal_Variant_ID"] for row in rows}


def product_autofill_prompt(row: dict[str, Any]) -> list[dict[str, str]]:
    schema_keys = [
        "FR_Title_Short", "FR_Title_Long", "US_Title_Short", "US_Title_Long",
        "FC_Title_Short", "FC_Title_Long", "FR_Description_Short",
        "US_Description_Short", "FC_Description_Short", "FR_Description_Long",
        "US_Description_Long", "FC_Description_Long", "FR_URL", "US_URL",
        "FC_URL", "FR_Meta_Title", "FR_Meta_Description", "FR_Meta_Keywords",
        "FR_Google_Category", "US_Meta_Title", "US_Meta_Description",
        "US_Meta_Keywords", "US_Google_Category", "FC_Meta_Title",
        "FC_Meta_Description", "FC_Meta_Keywords", "FC_Google_Category",
    ]
    system_prompt = (
        "Tu es l'assistant catalogue Industria. Génère uniquement un objet JSON valide, "
        "sans markdown, sans texte avant ou après. Respecte exactement les champs attendus "
        "par Lightspeed. Les descriptions longues peuvent contenir du HTML valide. "
        "N'inclus pas Produits Associés dans le JSON. N'inclus jamais Tags, tags, filtres ou produits associés dans le JSON. "
        "N'invente jamais de bénéfices, technologies, ingrédients, matériaux, parfums, promesses ou modes d'utilisation. "
        "Les descriptions doivent s'appuyer seulement sur les infos produit/source fournies et sur le contenu déjà présent dans la fiche. "
        "Si les sources sont insuffisantes, laisse les champs de description vides plutôt que de produire une description générique."
    )
    extra_rules_path = PROJECT_DIR / "product_gpt_rules.md"
    extra_rules = extra_rules_path.read_text(encoding="utf-8") if extra_rules_path.exists() else ""
    product_context = dict(row)
    product_context["Industria_Is_Matrix_Product"] = is_matrix_product(row)
    product_context["Industria_Is_Set_Product"] = product_is_set(row)
    product_context["Industria_Matrix_Title_Rule"] = (
        "Ce produit fait partie d'une matrice Lightspeed: ne mets aucun format et aucun SKU/code produit "
        "dans les titres courts ou longs. Les variantes partagent la même fiche e-com."
        if product_context["Industria_Is_Matrix_Product"]
        else ""
    )
    product_context["Industria_Set_Content_Rule"] = (
        "Ce produit semble être un duo, trio, coffret, routine, ensemble, kit, bundle ou pack. "
        "Dans chaque description longue HTML, ajoute une section CONTENU DE L'ENSEMBLE / SET INCLUDES "
        "après la liste de bénéfices et avant UTILISATION / HOW TO USE, seulement si les items inclus "
        "sont clairement fournis par le titre ou les sources. N'invente jamais les items inclus."
        if product_context["Industria_Is_Set_Product"]
        else ""
    )
    product_context["Industria_Product_Source_Info"] = product_source_info(product_id(row))
    user_prompt = {
        "task": "Créer le JSON de remplissage Lightspeed pour ce produit.",
        "expected_keys": schema_keys,
        "product": product_context,
        "extra_rules": extra_rules,
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_prompt, ensure_ascii=False)},
    ]


def title_needs_sku_suffix(row: dict[str, Any]) -> bool:
    if is_matrix_product(row):
        return False
    brand = clean(row.get("Brand")).lower()
    text = " ".join(
        clean(row.get(column)).lower()
        for column in [
            "Brand",
            "FC_Title_Short",
            "FR_Title_Short",
            "US_Title_Short",
            "FC_Title_Long",
            "Product_Title",
            "Type de correction",
        ]
    )
    tool_keywords = [
        "fer plat",
        "séchoir",
        "sechoir",
        "fer à friser",
        "fer a friser",
        "fer à boucler",
        "fer a boucler",
        "fer professionnel",
        "fer céramique",
        "fer ceramique",
        "séchoir",
        "brosse chauffante",
        "brosse séchante",
        "brosse sechante",
        "styler",
        "air styler",
        "tondeuse",
        "flat iron",
        "hair straightener",
        "straightener",
        "hair dryer",
        "dryer",
        "blow dryer",
        "curling iron",
        "curling wand",
        "hot brush",
        "hot air brush",
        "clipper",
        "trimmer",
    ]
    return "dannyco" in brand or any(keyword in text for keyword in tool_keywords)


def product_is_set(row: dict[str, Any]) -> bool:
    text = " ".join(
        clean(row.get(column)).lower()
        for column in [
            "FC_Title_Short",
            "FR_Title_Short",
            "US_Title_Short",
            "FC_Title_Long",
            "Product_Title",
            "Type de correction",
        ]
    )
    set_keywords = [
        "duo",
        "trio",
        "coffret",
        "routine",
        "ensemble",
        "kit",
        "bundle",
        "pack",
        "set",
        "gift set",
        "value set",
    ]
    return any(keyword in text for keyword in set_keywords)


def remove_title_format(value: str) -> str:
    text = clean(value)
    # Matrices share one e-com product page, so titles should not include variant-specific sizes or codes.
    text = re.sub(r"\s*-\s*[A-Za-z0-9._/]+$", "", text).strip()
    text = re.sub(r"\s*\([^)]*(?:ml|oz|g|gr|kg|l|un|po|in|mm|cm|w|v|°|inch|ounces?)[^)]*\)\s*$", "", text, flags=re.IGNORECASE).strip()
    return text


def sanitize_gpt_json(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in list(cleaned):
        if key.lower() == "tags":
            cleaned.pop(key, None)

    title_keys = [
        "FR_Title_Short",
        "FR_Title_Long",
        "US_Title_Short",
        "US_Title_Long",
        "FC_Title_Short",
        "FC_Title_Long",
    ]
    if is_matrix_product(row):
        for key in title_keys:
            if clean(cleaned.get(key)):
                cleaned[key] = remove_title_format(cleaned[key])
        return cleaned

    sku = clean(row.get("SKU")) or clean(row.get("Internal_ID"))
    if not sku or not title_needs_sku_suffix(row):
        return cleaned

    sku_lower = sku.lower()
    for key in title_keys:
        value = clean(cleaned.get(key))
        if value and sku_lower not in value.lower():
            cleaned[key] = f"{value} - {sku}"

    return cleaned


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
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "-H", f"Authorization: Bearer {openai_api_key()}",
                f"https://api.openai.com/v1/files/{file_id}/content",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Lecture fichier OpenAI impossible: {exc}")

    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Lecture fichier OpenAI échouée: {result.stderr}")

    stripped = result.stdout.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = {}
        if "error" in payload:
            raise HTTPException(status_code=502, detail=f"Lecture fichier OpenAI refusée: {payload['error']}")

    return result.stdout


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
        latest_audit = conn.execute("SELECT * FROM audit_history ORDER BY Date DESC LIMIT 1").fetchone()

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
        "latest_audit": dict(latest_audit) if latest_audit else None,
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
    source_ids = product_source_ids([item["Internal_Variant_ID"] for item in page])
    for item in page:
        item["Infos produit"] = "Oui" if item["Internal_Variant_ID"] in source_ids else "Non"
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
            "source_info": product_source_info(variant_id),
        }
    raise HTTPException(status_code=404, detail="Produit introuvable")


@app.put("/api/products/{variant_id}/source-info")
def api_save_product_source_info(
    variant_id: str,
    payload: ProductSourcePayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    if not find_product(variant_id):
        raise HTTPException(status_code=404, detail="Produit introuvable")
    ensure_product_source_table()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO product_sources (Internal_Variant_ID, source_text, source_url, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(Internal_Variant_ID) DO UPDATE SET
                source_text = excluded.source_text,
                source_url = excluded.source_url,
                updated_at = excluded.updated_at
            """,
            (variant_id, payload.source_text.strip(), payload.source_url.strip(), now_text()),
        )
    return {"ok": True}


@app.get("/api/products/{variant_id}/autofill-json")
def api_product_autofill_json(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    row = find_product(variant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    return sanitize_gpt_json(row, call_openai_json(product_autofill_prompt(row)))


@app.get("/api/products/{variant_id}/batch-json")
def api_product_batch_json(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    with connect() as conn:
        item = conn.execute(
            """
            SELECT *
            FROM gpt_batch_items
            WHERE Internal_Variant_ID = ? AND status IN ('completed', 'approved')
            """,
            (variant_id,),
        ).fetchone()
    if not item or not item["result_json"]:
        raise HTTPException(status_code=404, detail="JSON batch introuvable pour ce produit")
    row = find_product(variant_id) or batch_item_as_product(item)
    return sanitize_gpt_json(row, json.loads(item["result_json"]))


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
    require_user(x_remote_user)
    ensure_batch_tables()
    approvals = load_approvals()
    processed_brands = load_processed_brands()
    products = load_products()
    matrix_ids = matrix_product_ids(products)
    candidates = []
    with connect() as conn:
        existing = {
            row["Internal_Variant_ID"]
            for row in conn.execute("SELECT Internal_Variant_ID FROM gpt_batch_items").fetchall()
        }
        existing_internal_ids = {
            row["Internal_ID"]
            for row in conn.execute("SELECT Internal_ID FROM gpt_batch_items WHERE Internal_ID != ''").fetchall()
        }
    seen_matrix_ids: set[str] = set()
    for row in products:
        internal_id = product_internal_id(row)
        if product_id(row) in existing:
            continue
        if is_matrix_product(row, matrix_ids):
            if internal_id in existing_internal_ids or internal_id in seen_matrix_ids:
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
        if is_matrix_product(row, matrix_ids):
            seen_matrix_ids.add(internal_id)
        candidates.append(batch_item_summary(row))
    return {"total": len(candidates), "items": candidates[: min(limit, 500)]}


@app.post("/api/gpt-batches/queue")
def api_gpt_batch_queue(
    payload: BatchQueuePayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    products = load_products()
    product_map = {product_id(row): row for row in products}
    matrix_ids = matrix_product_ids(products)
    force = bool(payload.force)
    selected_ids = payload.variant_ids
    if not selected_ids:
        approvals = load_approvals()
        processed_brands = load_processed_brands()
        seen_matrix_ids: set[str] = set()
        selected_ids = []
        for row in products:
            internal_id = product_internal_id(row)
            if is_matrix_product(row, matrix_ids):
                if internal_id in seen_matrix_ids:
                    continue
            if (
                clean(row.get("Brand")) in processed_brands
                and not is_ignored_by_approval(row, approvals)
                and not is_masked_product(row)
                and is_batch_correctable(row)
            ):
                if is_matrix_product(row, matrix_ids):
                    seen_matrix_ids.add(internal_id)
                selected_ids.append(product_id(row))
            if len(selected_ids) >= max(1, min(payload.limit, 500)):
                break

    filtered_ids = []
    seen_matrix_ids = set()
    for variant_id in selected_ids:
        row = product_map.get(variant_id)
        if not row:
            continue
        internal_id = product_internal_id(row)
        if is_matrix_product(row, matrix_ids):
            if internal_id in seen_matrix_ids:
                continue
            seen_matrix_ids.add(internal_id)
        filtered_ids.append(variant_id)
    selected_ids = filtered_ids

    created = 0
    now = now_text()
    with connect() as conn:
        existing_internal_ids = {
            row["Internal_ID"]
            for row in conn.execute("SELECT Internal_ID FROM gpt_batch_items WHERE Internal_ID != ''").fetchall()
        }
        for variant_id in selected_ids[:500]:
            row = product_map.get(variant_id)
            if not row:
                continue
            internal_id = product_internal_id(row)
            if is_matrix_product(row, matrix_ids) and internal_id in existing_internal_ids:
                continue
            if not force and is_masked_product(row):
                continue
            if not force and not is_batch_correctable(row):
                continue
            summary = batch_item_summary(row)
            result = conn.execute(
                """
                INSERT OR IGNORE INTO gpt_batch_items
                (Internal_Variant_ID, Internal_ID, Brand, Product_Title, SKU, status, force_submit, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    summary["Internal_Variant_ID"],
                    summary["Internal_ID"],
                    summary["Brand"],
                    summary["Product_Title"],
                    summary["SKU"],
                    1 if force else 0,
                    now,
                    now,
                ),
            )
            created += int(result.rowcount or 0)
            if not result.rowcount and force:
                reset_result = conn.execute(
                    """
                    UPDATE gpt_batch_items
                    SET status = 'pending',
                        batch_id = '',
                        custom_id = '',
                        request_json = '',
                        result_json = '',
                        error = '',
                        force_submit = 1,
                        updated_at = ?
                    WHERE Internal_Variant_ID = ?
                      AND status IN ('pending', 'submitted', 'error', 'completed', 'approved')
                    """,
                    (now, summary["Internal_Variant_ID"]),
                )
                created += int(reset_result.rowcount or 0)
            if result.rowcount and is_matrix_product(row, matrix_ids):
                existing_internal_ids.add(internal_id)
    return {"ok": True, "created": created, "queued": created}


@app.post("/api/gpt-batches/queue-and-submit")
def api_gpt_batch_queue_and_submit(
    payload: BatchQueuePayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    queue_result = api_gpt_batch_queue(
        BatchQueuePayload(variant_ids=payload.variant_ids, limit=payload.limit, force=True),
        x_remote_user=x_remote_user,
    )
    submit_result = api_gpt_batch_submit(
        BatchSubmitPayload(limit=max(1, min(len(payload.variant_ids) or payload.limit, 500)), variant_ids=payload.variant_ids),
        x_remote_user=x_remote_user,
    )
    return {"ok": True, "queued": queue_result.get("created", 0), **submit_result}


@app.post("/api/gpt-batches/reset")
def api_gpt_batch_reset_items(
    payload: BatchResetPayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    variant_ids = [str(item) for item in payload.variant_ids if str(item).strip()]
    if not variant_ids:
        return {"ok": True, "updated": 0}

    placeholders = ",".join("?" for _ in variant_ids)
    now = now_text()
    with connect() as conn:
        result = conn.execute(
            f"""
            UPDATE gpt_batch_items
            SET status = 'pending',
                batch_id = '',
                custom_id = '',
                request_json = '',
                result_json = '',
                error = '',
                force_submit = 1,
                updated_at = ?
            WHERE status IN ('submitted', 'error', 'completed', 'approved')
              AND Internal_Variant_ID IN ({placeholders})
            """,
            [now, *variant_ids],
        )
    return {"ok": True, "updated": int(result.rowcount or 0)}


@app.delete("/api/gpt-batches/pending")
def api_gpt_batch_clear_pending(
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    with connect() as conn:
        result = conn.execute("DELETE FROM gpt_batch_items WHERE status = 'pending'")
    return {"ok": True, "deleted": int(result.rowcount or 0)}


@app.get("/api/gpt-batches/items")
def api_gpt_batch_items(
    status: str = "pending",
    search: str = "",
    limit: int = 200,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
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
            SELECT
                Internal_Variant_ID,
                Internal_ID,
                Brand,
                Product_Title,
                SKU,
                status,
                batch_id,
                custom_id,
                error,
                force_submit,
                created_at,
                updated_at
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
    products_by_id = {product_id(row): row for row in load_products()}
    approvals = load_approvals()
    processed_brands = load_processed_brands()
    for item in items:
        row = products_by_id.get(item["Internal_Variant_ID"])
        item["Lightspeed_Admin_URL"] = lightspeed_admin_url(row or item)
        item["Catalogue_State"] = batch_catalog_state(row, approvals, processed_brands)
    return {"total": total, "items": items}


@app.post("/api/gpt-batches/submit")
def api_gpt_batch_submit(
    payload: BatchSubmitPayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    limit = max(1, min(payload.limit, 500))
    with connect() as conn:
        if payload.variant_ids:
            variant_ids = [str(item) for item in payload.variant_ids if str(item).strip()]
            placeholders = ",".join("?" for _ in variant_ids)
            rows = conn.execute(
                f"""
                SELECT *
                FROM gpt_batch_items
                WHERE status = 'pending'
                  AND Internal_Variant_ID IN ({placeholders})
                ORDER BY created_at
                LIMIT ?
                """,
                [*variant_ids, limit],
            ).fetchall()
        else:
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
    request_by_custom_id = {}
    variants_by_custom_id: dict[str, list[str]] = {}
    matrix_ids = matrix_product_ids()
    submitted_matrix_ids: set[str] = set()
    submitted_family_keys: set[str] = set()
    for item in rows:
        row = find_product(item["Internal_Variant_ID"])
        if not row:
            continue
        force = bool(item["force_submit"]) if "force_submit" in item.keys() else False
        internal_id = product_internal_id(row)
        if is_matrix_product(row, matrix_ids):
            if internal_id in submitted_matrix_ids:
                continue
        family_key = product_family_key(row)
        if not force and is_masked_product(row):
            continue
        if not force and not is_batch_correctable(row):
            continue
        family_custom_id = ""
        if not is_matrix_product(row, matrix_ids) and family_key in submitted_family_keys:
            for existing_custom_id, variant_ids in variants_by_custom_id.items():
                first_row = find_product(variant_ids[0])
                if first_row and product_family_key(first_row) == family_key:
                    family_custom_id = existing_custom_id
                    break
            if family_custom_id:
                variants_by_custom_id.setdefault(family_custom_id, []).append(str(item["Internal_Variant_ID"]))
                continue
        if is_matrix_product(row, matrix_ids):
            submitted_matrix_ids.add(internal_id)
        else:
            submitted_family_keys.add(family_key)
        custom_id = str(item["Internal_Variant_ID"])
        variants_by_custom_id.setdefault(custom_id, []).append(custom_id)
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
        request_by_custom_id[custom_id] = json.dumps(request_record, ensure_ascii=False)

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
        for custom_id, request_json in request_by_custom_id.items():
            for variant_id in variants_by_custom_id.get(custom_id, [custom_id]):
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
                    (batch_id, custom_id, request_json, now, variant_id),
                )

    submitted_items_count = sum(len(items) for items in variants_by_custom_id.values())
    return {"ok": True, "batch_id": batch_id, "count": submitted_items_count, "requests": len(request_by_custom_id)}


@app.post("/api/gpt-batches/sync")
def api_gpt_batch_sync(
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
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
                    raw_payload = json.loads(result_text)
                    linked_items = conn.execute(
                        """
                        SELECT Internal_Variant_ID
                        FROM gpt_batch_items
                        WHERE batch_id = ? AND custom_id = ?
                        """,
                        (local_batch["batch_id"], custom_id),
                    ).fetchall()
                    for linked_item in linked_items:
                        linked_variant_id = linked_item["Internal_Variant_ID"]
                        row = find_product(linked_variant_id) or find_product(custom_id) or {}
                        result_payload = sanitize_gpt_json(row, raw_payload)
                        result_json = json.dumps(result_payload, ensure_ascii=False, indent=2)
                        conn.execute(
                            """
                            UPDATE gpt_batch_items
                            SET status = 'completed',
                                result_json = ?,
                                error = '',
                                updated_at = ?
                            WHERE Internal_Variant_ID = ?
                            """,
                            (result_json, now, linked_variant_id),
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
    require_user(x_remote_user)
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


@app.post("/api/gpt-batches/items/{variant_id}/restore")
def api_gpt_batch_restore(
    variant_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_user(x_remote_user)
    ensure_batch_tables()
    with connect() as conn:
        result = conn.execute(
            """
            UPDATE gpt_batch_items
            SET status = 'completed',
                updated_at = ?
            WHERE Internal_Variant_ID = ? AND status = 'approved'
            """,
            (now_text(), variant_id),
        )
    return {"ok": True, "updated": result.rowcount}


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
