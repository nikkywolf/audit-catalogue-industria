from __future__ import annotations

import json
import hashlib
import base64
from html.parser import HTMLParser
from io import BytesIO
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urljoin, urlparse, urldefrag
from urllib.request import Request as UrlRequest, urlopen
import uuid

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
DB_PATH = PROJECT_DIR / "industria_catalogue.db"
IMAGE_DATA_DIR = PROJECT_DIR / "data" / "images"
IMAGE_UPLOAD_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}

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


class ImageDiscoverPayload(BaseModel):
    source_url: str
    max_images: int = 8


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


def ensure_image_tables() -> None:
    IMAGE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS image_sources (
                id TEXT PRIMARY KEY,
                brand TEXT NOT NULL,
                domain TEXT NOT NULL,
                source_type TEXT DEFAULT '',
                priority INTEGER DEFAULT 100,
                is_allowed INTEGER NOT NULL DEFAULT 1,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS image_search_runs (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                query_json TEXT DEFAULT '',
                started_at TEXT NOT NULL,
                completed_at TEXT DEFAULT '',
                status TEXT NOT NULL,
                candidates_found INTEGER NOT NULL DEFAULT 0,
                candidates_downloaded INTEGER NOT NULL DEFAULT 0,
                errors_json TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS product_image_candidates (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                variant_id TEXT DEFAULT '',
                source_url TEXT DEFAULT '',
                source_domain TEXT DEFAULT '',
                source_type TEXT DEFAULT 'manual_upload',
                original_filename TEXT DEFAULT '',
                original_path TEXT DEFAULT '',
                file_hash TEXT DEFAULT '',
                perceptual_hash TEXT DEFAULT '',
                width INTEGER DEFAULT 0,
                height INTEGER DEFAULT 0,
                file_size_kb REAL DEFAULT 0,
                mime_type TEXT DEFAULT '',
                http_status INTEGER DEFAULT 0,
                download_status TEXT NOT NULL DEFAULT 'saved',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_images (
                id TEXT PRIMARY KEY,
                product_id TEXT NOT NULL,
                variant_id TEXT DEFAULT '',
                candidate_id TEXT DEFAULT '',
                original_filename TEXT DEFAULT '',
                original_path TEXT DEFAULT '',
                processed_filename TEXT DEFAULT '',
                processed_path TEXT DEFAULT '',
                image_role TEXT DEFAULT 'validation_requise',
                is_primary_candidate INTEGER NOT NULL DEFAULT 0,
                is_selected_primary INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'validation_requise',
                quality_report_json TEXT DEFAULT '',
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


def require_user(x_remote_user: Optional[str]) -> dict[str, str]:
    return current_user(x_remote_user)


def clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "None", "nan"}:
        return ""
    return text


def safe_folder_segment(value: Any, fallback: str) -> str:
    text = clean(value) or fallback
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.replace("/", " ").replace("\\", " ")
    text = re.sub(r"[<>:\"|?*\x00-\x1f]", " ", text)
    text = re.sub(r"[^A-Za-z0-9._() -]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text or fallback


def seo_slug(value: Any, fallback: str = "image") -> str:
    text = clean(value) or fallback
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or fallback


def product_display_title(row: dict[str, Any]) -> str:
    return clean(row.get("FC_Title_Short")) or clean(row.get("FR_Title_Short")) or clean(row.get("US_Title_Short")) or product_id(row)


def product_image_product_id(row: dict[str, Any]) -> str:
    return product_internal_id(row) or product_id(row).replace("!ID:", "") or product_id(row)


def product_group_name(row: dict[str, Any]) -> str:
    title = product_display_title(row)
    brand = clean(row.get("Brand"))
    if brand and title.lower().startswith(brand.lower()):
        title = title[len(brand):].lstrip(" -|")
    if "|" in title:
        group = title.split("|", 1)[0]
    elif " - " in title:
        group = title.split(" - ", 1)[0]
    else:
        group = ""
    return clean(group) or "sans-gamme"


def product_folder_name(row: dict[str, Any]) -> str:
    title = product_display_title(row)
    brand = clean(row.get("Brand"))
    if brand and title.lower().startswith(brand.lower()):
        title = title[len(brand):].lstrip(" -|")
    if "|" in title:
        title = title.split("|", 1)[1]
    title = clean(title) or product_image_product_id(row)
    return title


def image_zip_folder_parts(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        safe_folder_segment(clean(row.get("Brand")), "sans-marque"),
        safe_folder_segment(product_group_name(row), "sans-gamme"),
        safe_folder_segment(product_folder_name(row), product_image_product_id(row)),
    )


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


def image_counts_by_product() -> dict[str, dict[str, int]]:
    ensure_image_tables()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                product_id,
                COUNT(*) AS total_images,
                SUM(CASE WHEN is_selected_primary = 1 THEN 1 ELSE 0 END) AS primary_images,
                SUM(CASE WHEN status = 'validation_requise' THEN 1 ELSE 0 END) AS validation_required
            FROM product_images
            GROUP BY product_id
            """
        ).fetchall()
    return {
        row["product_id"]: {
            "total_images": int(row["total_images"] or 0),
            "primary_images": int(row["primary_images"] or 0),
            "validation_required": int(row["validation_required"] or 0),
        }
        for row in rows
    }


def product_image_status(row: dict[str, Any], counts: dict[str, int], processed_brands: set[str]) -> str:
    if clean(row.get("Brand")) not in processed_brands:
        return "Ignoré par marque"
    if counts.get("total_images", 0) <= 0:
        return "Manquante"
    if counts.get("validation_required", 0) > 0:
        return "Validation requise"
    if counts.get("primary_images", 0) <= 0:
        return "Photo principale manquante"
    return "Conforme"


def summarize_image_product(row: dict[str, Any], counts_by_product: dict[str, dict[str, int]], processed_brands: set[str]) -> dict[str, Any]:
    product_key = product_image_product_id(row)
    counts = counts_by_product.get(product_key, {"total_images": 0, "primary_images": 0, "validation_required": 0})
    return {
        "Internal_Variant_ID": product_id(row),
        "Internal_ID": product_internal_id(row),
        "Product_ID": product_key,
        "Brand": clean(row.get("Brand")),
        "Gamme": product_group_name(row),
        "Produit": product_display_title(row),
        "SKU": clean(row.get("SKU")),
        "Statut image": product_image_status(row, counts, processed_brands),
        "Images": counts.get("total_images", 0),
        "Principale": counts.get("primary_images", 0),
        "Validation": counts.get("validation_required", 0),
        "Lightspeed_Admin_URL": lightspeed_admin_url(row),
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


@app.get("/api/images/products")
def api_image_products(
    search: str = "",
    brand: str = "",
    status: str = "",
    limit: int = 100,
    offset: int = 0,
):
    ensure_image_tables()
    products = load_products()
    processed_brands = load_processed_brands()
    counts_by_product = image_counts_by_product()

    rows = []
    query = search.lower().strip()
    for row in products:
        if clean(row.get("Brand")) not in processed_brands:
            continue
        summary = summarize_image_product(row, counts_by_product, processed_brands)
        if brand and summary["Brand"] != brand:
            continue
        if status and summary["Statut image"] != status:
            continue
        if query:
            haystack = " ".join(
                [
                    summary["Brand"],
                    summary["Gamme"],
                    summary["Produit"],
                    summary["Product_ID"],
                    summary["Internal_Variant_ID"],
                    summary["SKU"],
                ]
            ).lower()
            if query not in haystack:
                continue
        rows.append(summary)

    return {"total": len(rows), "items": rows[offset: offset + min(limit, 500)]}


@app.get("/api/images/metrics")
def api_image_metrics():
    ensure_image_tables()
    products = load_products()
    processed_brands = load_processed_brands()
    counts_by_product = image_counts_by_product()
    summaries = [summarize_image_product(row, counts_by_product, processed_brands) for row in products]
    included = [item for item in summaries if item["Statut image"] != "Ignoré par marque"]
    return {
        "total_analysable": len(included),
        "sans_image": sum(1 for item in included if item["Statut image"] == "Manquante"),
        "sans_principale": sum(1 for item in included if item["Statut image"] == "Photo principale manquante"),
        "validation_requise": sum(1 for item in included if item["Statut image"] == "Validation requise"),
        "conformes": sum(1 for item in included if item["Statut image"] == "Conforme"),
        "marques_ignorees_exclues": len(summaries) - len(included),
    }


def image_file_extension(upload: UploadFile) -> str:
    mime = clean(upload.content_type).lower()
    if mime not in IMAGE_UPLOAD_MIME_TYPES:
        raise HTTPException(status_code=400, detail=f"Format image non supporté: {upload.content_type or upload.filename}")
    extension = Path(upload.filename or "").suffix.lower()
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if extension == ".jpeg" else extension
    guessed = mimetypes.guess_extension(mime) or ".jpg"
    return ".jpg" if guessed == ".jpe" else guessed


def save_upload_bytes(target: Path, data: bytes) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


class ManufacturerImageParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.images: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._current_link = ""
        self._current_link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        attr = {key.lower(): value or "" for key, value in attrs}
        if tag == "a":
            href = clean(attr.get("href"))
            if href:
                self._current_link = urljoin(self.base_url, href)
                self._current_link_text = []
        if tag in {"img", "source"}:
            for key in ["src", "data-src", "data-original", "data-image", "data-zoom-image"]:
                self.add_image(attr.get(key))
            self.add_srcset(attr.get("srcset") or attr.get("data-srcset"))
        if tag == "meta":
            name = (attr.get("property") or attr.get("name") or "").lower()
            if name in {"og:image", "og:image:secure_url", "twitter:image", "twitter:image:src"}:
                self.add_image(attr.get("content"))
        if tag == "link":
            rel = attr.get("rel", "").lower()
            if "image_src" in rel:
                self.add_image(attr.get("href"))

    def handle_data(self, data: str):
        if self._current_link:
            self._current_link_text.append(data)

    def handle_endtag(self, tag: str):
        if tag == "a" and self._current_link:
            self.links.append((self._current_link, " ".join(self._current_link_text)))
            self._current_link = ""
            self._current_link_text = []

    def add_image(self, value: Optional[str]):
        url = clean(value)
        if not url:
            return
        full_url = urljoin(self.base_url, url)
        full_url = urldefrag(full_url)[0]
        parsed = urlparse(full_url)
        if parsed.scheme in {"http", "https"}:
            self.images.append(full_url)

    def add_srcset(self, value: Optional[str]):
        srcset = clean(value)
        if not srcset:
            return
        candidates = []
        for part in srcset.split(","):
            bits = part.strip().split()
            if not bits:
                continue
            weight = 0
            if len(bits) > 1:
                number = re.sub(r"[^0-9.]", "", bits[-1])
                try:
                    weight = int(float(number))
                except ValueError:
                    weight = 0
            candidates.append((weight, bits[0]))
        for _, url in sorted(candidates, reverse=True):
            self.add_image(url)


def normalized_manufacturer_url(source_url: str) -> str:
    url = clean(source_url)
    if not url:
        raise HTTPException(status_code=400, detail="URL fabricant manquante")
    if not re.match(r"^https?://", url, re.I):
        url = f"https://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="URL fabricant invalide")
    return url


def fetch_url(url: str, max_bytes: int = 8_000_000) -> tuple[bytes, str, int]:
    request = UrlRequest(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 IndustriaAudit/1.0",
            "Accept": "text/html,image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise HTTPException(status_code=413, detail="Fichier trop lourd")
            return data, response.headers.get("content-type", ""), int(response.status)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Impossible de lire l'URL fabricant: {exc}")


def parse_manufacturer_page(url: str) -> ManufacturerImageParser:
    data, content_type, _ = fetch_url(url, max_bytes=4_000_000)
    if "text/html" not in content_type.lower():
        raise HTTPException(status_code=400, detail="L'URL fournie ne semble pas être une page web")
    parser = ManufacturerImageParser(url)
    parser.feed(data.decode("utf-8", errors="ignore"))
    return parser


def product_search_tokens(row: dict[str, Any]) -> list[str]:
    raw = " ".join([
        clean(row.get("Brand")),
        product_display_title(row),
        clean(row.get("SKU")),
        clean(row.get("UPC")),
    ])
    tokens = re.findall(r"[a-zA-Z0-9À-ÿ]{3,}", raw.lower())
    blocked = {"avec", "pour", "the", "and", "les", "des", "une", "produit", "professional"}
    return [token for token in dict.fromkeys(tokens) if token not in blocked][:16]


def score_manufacturer_link(row: dict[str, Any], url: str, text: str) -> int:
    haystack = f"{url} {text}".lower()
    score = 0
    for token in product_search_tokens(row):
        if token in haystack:
            score += 2 if len(token) >= 5 else 1
    if any(word in haystack for word in ["/product", "/products", "/produit", "/shop", "/collections"]):
        score += 2
    if any(word in haystack for word in ["blog", "article", "privacy", "account", "cart", "contact"]):
        score -= 5
    return score


def manufacturer_image_urls(row: dict[str, Any], source_url: str, max_pages: int = 5) -> tuple[list[str], list[str]]:
    base_url = normalized_manufacturer_url(source_url)
    parsed_base = urlparse(base_url)
    pages = [base_url]
    try:
        base_parser = parse_manufacturer_page(base_url)
    except HTTPException:
        raise
    same_domain_links = []
    for href, text in base_parser.links:
        parsed = urlparse(href)
        if parsed.netloc == parsed_base.netloc:
            score = score_manufacturer_link(row, href, text)
            if score > 0:
                same_domain_links.append((score, href))
    for _, href in sorted(same_domain_links, reverse=True):
        if href not in pages:
            pages.append(href)
        if len(pages) >= max_pages:
            break

    images = list(base_parser.images)
    visited_pages = [base_url]
    for page_url in pages[1:]:
        try:
            parser = parse_manufacturer_page(page_url)
        except HTTPException:
            continue
        visited_pages.append(page_url)
        images.extend(parser.images)

    filtered = []
    seen = set()
    for image_url in images:
        lowered = image_url.lower()
        if any(skip in lowered for skip in [".svg", ".gif", "logo", "icon", "sprite", "placeholder"]):
            continue
        if image_url in seen:
            continue
        seen.add(image_url)
        filtered.append(image_url)
    return filtered, visited_pages


def downloaded_image_extension(content_type: str, source_url: str) -> str:
    mime = content_type.split(";")[0].strip().lower()
    if mime in {"image/jpeg", "image/jpg"}:
        return ".jpg"
    if mime == "image/png":
        return ".png"
    if mime == "image/webp":
        return ".webp"
    extension = Path(urlparse(source_url).path).suffix.lower()
    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return ".jpg" if extension == ".jpeg" else extension
    return ".jpg"


def save_discovered_image(row: dict[str, Any], source_url: str, source_page: str) -> bool:
    try:
        data, content_type, http_status = fetch_url(source_url)
    except HTTPException:
        return False
    if not content_type.lower().split(";")[0].startswith("image/"):
        return False
    try:
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
    except Exception:
        return False
    if max(width, height) < 500 or min(width, height) < 300:
        return False

    product_key = product_image_product_id(row)
    variant_id = product_id(row)
    file_hash = hashlib.sha256(data).hexdigest()
    now = now_text()
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM product_image_candidates WHERE product_id = ? AND file_hash = ? LIMIT 1",
            (product_key, file_hash),
        ).fetchone()
        if exists:
            return False
        extension = downloaded_image_extension(content_type, source_url)
        candidate_id = str(uuid.uuid4())
        image_id = str(uuid.uuid4())
        base_name = seo_slug(f"{clean(row.get('Brand'))} {product_display_title(row)}", product_key)
        filename = f"{base_name}-fabricant-{image_id[:8]}{extension}"
        original_path = IMAGE_DATA_DIR / "originals" / product_key / filename
        candidate_path = IMAGE_DATA_DIR / "candidates" / product_key / filename
        save_upload_bytes(original_path, data)
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original_path, candidate_path)
        source_domain = urlparse(source_url).netloc
        size_kb = round(len(data) / 1024, 2)
        conn.execute(
            """
            INSERT INTO product_image_candidates
            (id, product_id, variant_id, source_url, source_domain, source_type, original_filename,
             original_path, file_hash, width, height, file_size_kb, mime_type, http_status,
             download_status, created_at)
            VALUES (?, ?, ?, ?, ?, 'manufacturer_site', ?, ?, ?, ?, ?, ?, ?, ?, 'saved', ?)
            """,
            (
                candidate_id,
                product_key,
                variant_id,
                source_url,
                source_domain,
                filename,
                str(candidate_path),
                file_hash,
                width,
                height,
                size_kb,
                content_type,
                http_status,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO product_images
            (id, product_id, variant_id, candidate_id, original_filename, original_path,
             image_role, status, quality_report_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'validation_requise', 'validation_requise', ?, ?, ?)
            """,
            (
                image_id,
                product_key,
                variant_id,
                candidate_id,
                filename,
                str(original_path),
                json.dumps({"source_page": source_page, "source_url": source_url}, ensure_ascii=False),
                now,
                now,
            ),
        )
    return True


def find_product_by_image_product_id(product_key: str) -> Optional[dict[str, Any]]:
    for row in load_products():
        if product_image_product_id(row) == product_key:
            return row
    return None


def image_data_url(path: Path, mime_type: str = "") -> str:
    data = path.read_bytes()
    mime = mime_type or mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def analyze_image_with_gpt(row: dict[str, Any], image: dict[str, Any]) -> dict[str, Any]:
    source_path = Path(clean(image.get("original_path")))
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Image originale introuvable")
    prompt = {
        "task": "Analyse cette image produit e-commerce Industria et retourne uniquement du JSON.",
        "product": {
            "brand": clean(row.get("Brand")),
            "title": product_display_title(row),
            "product_id": product_image_product_id(row),
            "sku": clean(row.get("SKU")),
        },
        "expected_schema": {
            "image_role": "principal|lifestyle|texture|dos|ingredients|infographie|packaging|avant_apres|application|routine|resultat|gamme|autre|validation_requise",
            "confidence": "0-100",
            "plp_suitable": "boolean",
            "single_product": "boolean",
            "white_background": "boolean",
            "complete_product": "boolean",
            "official_looking": "boolean",
            "blurry": "boolean",
            "label_readable": "boolean",
            "contains_person": "boolean",
            "contains_decor": "boolean",
            "multiple_products": "boolean",
            "right_product": "boolean",
            "reasons": "array of strings",
            "warnings": "array of strings",
        },
        "rules": [
            "Ne modifie jamais l'image.",
            "Ne génère pas de nouveau visuel.",
            "Recommande principal seulement si l'image convient comme photo PLP.",
            "Si incertain, utilise validation_requise.",
        ],
    }
    payload = {
        "model": os.getenv("OPENAI_VISION_MODEL", os.getenv("OPENAI_MODEL", "gpt-4.1-mini")),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                    {"type": "image_url", "image_url": {"url": image_data_url(source_path)}},
                ],
            }
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    data = openai_request("/v1/chat/completions", payload)
    content = data["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail=f"Analyse GPT Vision invalide: {exc}")


def image_role_suffix(role: str) -> str:
    mapping = {
        "principal": "principal",
        "lifestyle": "lifestyle",
        "texture": "texture",
        "dos": "dos",
        "ingredients": "ingredients",
        "infographie": "infographie",
        "packaging": "packaging",
        "avant_apres": "avant-apres",
        "application": "application",
        "routine": "routine",
        "resultat": "resultat",
        "gamme": "gamme",
    }
    return mapping.get(clean(role), "validation")


def processed_image_filename(row: dict[str, Any], role: str, extension: str = ".jpg", index: int = 1) -> str:
    base = seo_slug(f"{clean(row.get('Brand'))} {product_display_title(row)}", product_image_product_id(row))
    suffix = image_role_suffix(role)
    duplicate_suffix = "" if index <= 1 else f"-{index}"
    return f"{base}-{suffix}{duplicate_suffix}{extension}"


def save_jpeg_under_target(image, path: Path, target_kb: int = 100, min_quality: int = 62) -> tuple[int, float]:
    path.parent.mkdir(parents=True, exist_ok=True)
    best_data = b""
    best_quality = 88
    for quality in range(88, min_quality - 1, -4):
        buffer = BytesIO()
        image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
        data = buffer.getvalue()
        best_data = data
        best_quality = quality
        if len(data) <= target_kb * 1024:
            break
    path.write_bytes(best_data)
    return best_quality, round(len(best_data) / 1024, 2)


def process_image_file(row: dict[str, Any], image: dict[str, Any], index: int = 1) -> dict[str, Any]:
    try:
        from PIL import Image, ImageOps
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Pillow n'est pas disponible: {exc}")

    source_path = Path(clean(image.get("original_path")))
    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Image originale introuvable")
    role = clean(image.get("image_role")) or "validation_requise"
    product_key = product_image_product_id(row)
    filename = processed_image_filename(row, role, index=index)
    target_path = IMAGE_DATA_DIR / "processed" / product_key / filename

    with Image.open(source_path) as raw:
        raw = ImageOps.exif_transpose(raw).convert("RGBA")
        if role == "principal" or image.get("is_selected_primary"):
            canvas = Image.new("RGB", (1080, 1080), "#FFFFFF")
            raw.thumbnail((920, 920), Image.LANCZOS)
            composed = Image.new("RGBA", raw.size, "#FFFFFF")
            composed.alpha_composite(raw)
            rgb = composed.convert("RGB")
            x = (1080 - rgb.width) // 2
            y = (1080 - rgb.height) // 2
            canvas.paste(rgb, (x, y))
            quality, size_kb = save_jpeg_under_target(canvas, target_path, target_kb=100)
            width, height = canvas.size
        else:
            raw.thumbnail((1080, 1080), Image.LANCZOS)
            canvas = Image.new("RGB", raw.size, "#FFFFFF")
            canvas.paste(raw.convert("RGB"), (0, 0))
            quality, size_kb = save_jpeg_under_target(canvas, target_path, target_kb=280, min_quality=68)
            width, height = canvas.size

    return {
        "processed_filename": filename,
        "processed_path": str(target_path),
        "width": width,
        "height": height,
        "size_kb": size_kb,
        "format": "jpg",
        "quality": quality,
        "role": role,
        "status": "processed",
        "warnings": [] if size_kb <= (100 if role == "principal" else 280) else ["poids trop élevé"],
    }


@app.post("/api/images/products/{variant_id}/upload")
async def api_image_upload(
    variant_id: str,
    files: list[UploadFile] = File(...),
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_image_tables()
    row = find_product(variant_id)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    product_key = product_image_product_id(row)
    now = now_text()
    saved = 0
    with connect() as conn:
        for upload in files:
            extension = image_file_extension(upload)
            data = await upload.read()
            if not data:
                continue
            image_id = str(uuid.uuid4())
            base_name = seo_slug(f"{clean(row.get('Brand'))} {product_display_title(row)}", product_key)
            filename = f"{base_name}-{image_id[:8]}{extension}"
            original_path = IMAGE_DATA_DIR / "originals" / product_key / filename
            candidate_path = IMAGE_DATA_DIR / "candidates" / product_key / filename
            file_hash = save_upload_bytes(original_path, data)
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original_path, candidate_path)
            size_kb = round(len(data) / 1024, 2)
            candidate_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO product_image_candidates
                (id, product_id, variant_id, source_type, original_filename, original_path,
                 file_hash, file_size_kb, mime_type, download_status, created_at)
                VALUES (?, ?, ?, 'manual_upload', ?, ?, ?, ?, ?, 'saved', ?)
                """,
                (candidate_id, product_key, variant_id, upload.filename or filename, str(candidate_path), file_hash, size_kb, upload.content_type or "", now),
            )
            conn.execute(
                """
                INSERT INTO product_images
                (id, product_id, variant_id, candidate_id, original_filename, original_path,
                 image_role, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'validation_requise', 'validation_requise', ?, ?)
                """,
                (image_id, product_key, variant_id, candidate_id, filename, str(original_path), now, now),
            )
            saved += 1
    return {"ok": True, "saved": saved}


@app.post("/api/images/products/{product_key}/discover")
def api_image_discover_product(
    product_key: str,
    payload: ImageDiscoverPayload,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_image_tables()
    row = find_product_by_image_product_id(product_key)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")

    max_images = max(1, min(payload.max_images, 20))
    image_urls, visited_pages = manufacturer_image_urls(row, payload.source_url)
    downloaded = 0
    for image_url in image_urls:
        if save_discovered_image(row, image_url, visited_pages[0]):
            downloaded += 1
        if downloaded >= max_images:
            break
    return {
        "ok": True,
        "downloaded": downloaded,
        "found": len(image_urls),
        "pages": visited_pages,
    }


@app.post("/api/images/products/{product_key}/analyze")
def api_image_analyze_product(
    product_key: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_image_tables()
    openai_api_key()
    row = find_product_by_image_product_id(product_key)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    now = now_text()
    analyzed = 0
    with connect() as conn:
        images = [dict(item) for item in conn.execute(
            """
            SELECT *
            FROM product_images
            WHERE product_id = ?
              AND original_path != ''
            """,
            (product_key,),
        ).fetchall()]
        best_primary_id = ""
        best_primary_confidence = 0.0
        for image in images:
            analysis = analyze_image_with_gpt(row, image)
            role = clean(analysis.get("image_role")) or "validation_requise"
            try:
                confidence = float(str(analysis.get("confidence") or 0).replace("%", "").strip())
            except ValueError:
                confidence = 0.0
            is_primary = 1 if role == "principal" and confidence >= 75 and analysis.get("plp_suitable") else 0
            if is_primary and confidence > best_primary_confidence:
                best_primary_id = image["id"]
                best_primary_confidence = confidence
            status = "validation_requise" if role in {"validation_requise", "autre"} or confidence < 65 else "analyse_ok"
            conn.execute(
                """
                UPDATE product_images
                SET image_role = ?,
                    is_primary_candidate = ?,
                    is_selected_primary = 0,
                    status = ?,
                    quality_report_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (role, is_primary, status, json.dumps(analysis, ensure_ascii=False), now, image["id"]),
            )
            analyzed += 1
        if best_primary_id:
            conn.execute(
                "UPDATE product_images SET is_selected_primary = 1 WHERE id = ?",
                (best_primary_id,),
            )
    return {"ok": True, "analyzed": analyzed}


@app.post("/api/images/products/{product_key}/process")
def api_image_process_product(
    product_key: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_image_tables()
    row = find_product_by_image_product_id(product_key)
    if not row:
        raise HTTPException(status_code=404, detail="Produit introuvable")
    now = now_text()
    processed = 0
    with connect() as conn:
        images = [dict(item) for item in conn.execute(
            """
            SELECT *
            FROM product_images
            WHERE product_id = ?
              AND original_path != ''
            """,
            (product_key,),
        ).fetchall()]
        role_counts: dict[str, int] = {}
        for image in images:
            role = clean(image.get("image_role")) or "validation_requise"
            role_counts[role] = role_counts.get(role, 0) + 1
            report = process_image_file(row, image, index=role_counts[role])
            merged_report = {}
            if clean(image.get("quality_report_json")):
                try:
                    merged_report = json.loads(image["quality_report_json"])
                except json.JSONDecodeError:
                    merged_report = {}
            merged_report["processing"] = report
            conn.execute(
                """
                UPDATE product_images
                SET processed_filename = ?,
                    processed_path = ?,
                    status = ?,
                    quality_report_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    report["processed_filename"],
                    report["processed_path"],
                    "processed",
                    json.dumps(merged_report, ensure_ascii=False),
                    now,
                    image["id"],
                ),
            )
            processed += 1
    return {"ok": True, "processed": processed}


@app.post("/api/images/{image_id}/primary")
def api_image_select_primary(
    image_id: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    ensure_image_tables()
    now = now_text()
    with connect() as conn:
        image = conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            raise HTTPException(status_code=404, detail="Image introuvable")
        conn.execute(
            "UPDATE product_images SET is_selected_primary = 0 WHERE product_id = ?",
            (image["product_id"],),
        )
        conn.execute(
            """
            UPDATE product_images
            SET is_selected_primary = 1,
                image_role = 'principal',
                status = 'validation_requise',
                updated_at = ?
            WHERE id = ?
            """,
            (now, image_id),
        )
    return {"ok": True}


def image_rows_for_export(scope: str, product_id_value: str = "", variant_id: str = "", brand: str = "", gamme: str = "") -> list[dict[str, Any]]:
    ensure_image_tables()
    products = load_products()
    products_by_product_id = {product_image_product_id(row): row for row in products}
    products_by_variant_id = {product_id(row): row for row in products}
    where = []
    params: list[Any] = []
    if scope == "product":
        if variant_id:
            where.append("variant_id = ?")
            params.append(variant_id)
        elif product_id_value:
            where.append("product_id = ?")
            params.append(product_id_value)
        else:
            raise HTTPException(status_code=400, detail="Produit manquant")
    elif scope == "brand":
        if not brand:
            raise HTTPException(status_code=400, detail="Marque manquante")
    elif scope == "gamme":
        if not brand or not gamme:
            raise HTTPException(status_code=400, detail="Marque et gamme requises")
    elif scope != "all":
        raise HTTPException(status_code=400, detail="Scope ZIP invalide")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    with connect() as conn:
        image_rows = [dict(row) for row in conn.execute(f"SELECT * FROM product_images {where_sql}", params).fetchall()]

    export_rows = []
    for image in image_rows:
        row = products_by_variant_id.get(image["variant_id"]) or products_by_product_id.get(image["product_id"])
        if not row:
            continue
        if scope == "brand" and clean(row.get("Brand")) != brand:
            continue
        if scope == "gamme" and (clean(row.get("Brand")) != brand or product_group_name(row) != gamme):
            continue
        export_rows.append({"image": image, "product": row})
    return export_rows


def create_images_zip(scope: str, product_id_value: str = "", variant_id: str = "", brand: str = "", gamme: str = "") -> Path:
    rows = image_rows_for_export(scope, product_id_value=product_id_value, variant_id=variant_id, brand=brand, gamme=gamme)
    if not rows:
        raise HTTPException(status_code=404, detail="Aucune image à exporter")
    tmp_dir = Path("/tmp") / "industria-image-zips"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_dir / f"industria-images-{scope}-{uuid.uuid4().hex[:10]}.zip"
    used_product_paths: dict[tuple[str, str, str], str] = {}
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in rows:
            image = item["image"]
            row = item["product"]
            source_path = Path(clean(image.get("processed_path")) or clean(image.get("original_path")))
            if not source_path.exists():
                continue
            brand_part, group_part, product_part = image_zip_folder_parts(row)
            key = (brand_part, group_part, product_part)
            existing_product_id = used_product_paths.get(key)
            current_product_id = product_image_product_id(row)
            if existing_product_id and existing_product_id != current_product_id:
                product_part = safe_folder_segment(f"{product_part} {current_product_id}", current_product_id)
            used_product_paths[key] = current_product_id
            filename = clean(image.get("processed_filename")) or source_path.name
            archive.write(source_path, f"{brand_part}/{group_part}/{product_part}/{filename}")
    return zip_path


@app.get("/api/images/export.zip")
def api_images_export_zip(
    scope: str = "product",
    product_id: str = "",
    variant_id: str = "",
    brand: str = "",
    gamme: str = "",
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    zip_path = create_images_zip(scope, product_id_value=product_id, variant_id=variant_id, brand=brand, gamme=gamme)
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


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
