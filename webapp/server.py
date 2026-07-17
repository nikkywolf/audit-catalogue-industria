from __future__ import annotations

import base64
import json
import hashlib
import html
import logging
import os
import re
import sqlite3
import subprocess
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen
import uuid

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
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
logger = logging.getLogger("uvicorn.error")
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


def update_dotenv(values: dict[str, str]) -> None:
    env_path = PROJECT_DIR / ".env"
    existing_lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(values)
    output_lines = []

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            output_lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            output_lines.append(f"{key}={remaining.pop(key)}")
        else:
            output_lines.append(line)

    for key, value in remaining.items():
        output_lines.append(f"{key}={value}")

    env_path.write_text("\n".join(output_lines).rstrip() + "\n", encoding="utf-8")
    for key, value in values.items():
        os.environ[key] = value


class ApprovalPayload(BaseModel):
    variant_id: str
    error_type: str = ""
    error: str


class BulkApprovalPayload(BaseModel):
    variant_ids: list[str]


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


def ensure_integration_tables() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                provider TEXT NOT NULL,
                state_hash TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS integration_tokens (
                provider TEXT PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                token_type TEXT DEFAULT '',
                scope TEXT DEFAULT '',
                expires_at INTEGER DEFAULT 0,
                refresh_expires_at INTEGER DEFAULT 0,
                account_id TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            );
            """
        )


def ensure_ecom_api_tables() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ecom_api_products (
                Internal_Variant_ID TEXT PRIMARY KEY,
                Internal_ID TEXT NOT NULL,
                Brand TEXT DEFAULT '',
                Product_Title TEXT DEFAULT '',
                SKU TEXT DEFAULT '',
                UPC TEXT DEFAULT '',
                Visible TEXT DEFAULT '',
                mapped_payload TEXT NOT NULL,
                product_payload TEXT NOT NULL,
                variant_payload TEXT NOT NULL,
                enriched_at TEXT DEFAULT '',
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ecom_api_sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                products_seen INTEGER NOT NULL DEFAULT 0,
                variants_saved INTEGER NOT NULL DEFAULT 0,
                start_offset INTEGER NOT NULL DEFAULT 0,
                next_offset INTEGER NOT NULL DEFAULT 0,
                message TEXT DEFAULT ''
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(ecom_api_sync_runs)").fetchall()}
        if "start_offset" not in columns:
            conn.execute("ALTER TABLE ecom_api_sync_runs ADD COLUMN start_offset INTEGER NOT NULL DEFAULT 0")
        if "next_offset" not in columns:
            conn.execute("ALTER TABLE ecom_api_sync_runs ADD COLUMN next_offset INTEGER NOT NULL DEFAULT 0")
        product_columns = {row["name"] for row in conn.execute("PRAGMA table_info(ecom_api_products)").fetchall()}
        if "enriched_at" not in product_columns:
            conn.execute("ALTER TABLE ecom_api_products ADD COLUMN enriched_at TEXT DEFAULT ''")


def hash_oauth_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def save_oauth_state(provider: str, state: str, ttl_seconds: int = 600) -> None:
    ensure_integration_tables()
    now = int(time.time())
    with connect() as conn:
        conn.execute(
            "DELETE FROM oauth_states WHERE provider = ? AND (expires_at < ? OR used_at > 0)",
            (provider, now),
        )
        conn.execute(
            """
            INSERT INTO oauth_states (provider, state_hash, created_at, expires_at, used_at)
            VALUES (?, ?, ?, ?, 0)
            """,
            (provider, hash_oauth_state(state), now, now + ttl_seconds),
        )


def consume_oauth_state(provider: str, state: str) -> bool:
    ensure_integration_tables()
    now = int(time.time())
    state_hash = hash_oauth_state(state)
    with connect() as conn:
        row = conn.execute(
            """
            SELECT state_hash
            FROM oauth_states
            WHERE provider = ?
              AND state_hash = ?
              AND expires_at >= ?
              AND used_at = 0
            """,
            (provider, state_hash, now),
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE oauth_states SET used_at = ? WHERE provider = ? AND state_hash = ?",
            (now, provider, state_hash),
        )
    return True


def save_integration_token(provider: str, token: dict[str, Any]) -> None:
    ensure_integration_tables()
    now = int(time.time())
    expires_at = now + max(0, int(token.get("expires_in") or 0))
    refresh_expires_at = now + max(0, int(token.get("refresh_expires_in") or 0))
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO integration_tokens
            (provider, access_token, refresh_token, token_type, scope, expires_at, refresh_expires_at, account_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_type = excluded.token_type,
                scope = excluded.scope,
                expires_at = excluded.expires_at,
                refresh_expires_at = excluded.refresh_expires_at,
                account_id = excluded.account_id,
                updated_at = excluded.updated_at
            """,
            (
                provider,
                str(token.get("access_token") or ""),
                str(token.get("refresh_token") or ""),
                str(token.get("token_type") or "bearer"),
                str(token.get("scope") or ""),
                expires_at,
                refresh_expires_at,
                str(token.get("account_id") or token.get("accountID") or ""),
                now_text(),
            ),
        )


def load_integration_token(provider: str) -> Optional[sqlite3.Row]:
    ensure_integration_tables()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM integration_tokens WHERE provider = ?",
            (provider,),
        ).fetchone()



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


LIGHTSPEED_RSERIES_PROVIDER = "lightspeed_rseries"
LIGHTSPEED_RSERIES_AUTHORIZE_URL = "https://cloud.lightspeedapp.com/auth/oauth/authorize"
LIGHTSPEED_RSERIES_TOKEN_URL = "https://cloud.lightspeedapp.com/auth/oauth/token"
LIGHTSPEED_RSERIES_REDIRECT_URI = "https://dashboardindustria.com/lightspeed/callback"
SENSITIVE_OAUTH_FIELDS = ("client_secret", "access_token", "refresh_token", "code")
SENSITIVE_ECOM_FIELDS = ("LIGHTSPEED_ECOM_API_SECRET", "api_secret", "secret", "password")


class LightspeedTokenExchangeError(Exception):
    def __init__(self, status_code: int, response_body: str, endpoint: str, parameter_names: list[str]):
        self.status_code = status_code
        self.response_body = response_body
        self.endpoint = endpoint
        self.parameter_names = parameter_names
        super().__init__("Lightspeed token exchange failed")


def mask_sensitive_text(text: str) -> str:
    masked = text
    for field in SENSITIVE_OAUTH_FIELDS:
        masked = re.sub(
            rf'("{field}"\s*:\s*")[^"]+(")',
            rf'\1***\2',
            masked,
            flags=re.IGNORECASE,
        )
        masked = re.sub(
            rf"({field}=)[^&\s]+",
            rf"\1***",
            masked,
            flags=re.IGNORECASE,
        )
    return masked[:1000]


def mask_sensitive_text_full(text: str) -> str:
    masked = text
    for field in (*SENSITIVE_OAUTH_FIELDS, *SENSITIVE_ECOM_FIELDS):
        masked = re.sub(
            rf'("{field}"\s*:\s*")[^"]+(")',
            rf'\1***\2',
            masked,
            flags=re.IGNORECASE,
        )
        masked = re.sub(
            rf"({field}=)[^&\s]+",
            rf"\1***",
            masked,
            flags=re.IGNORECASE,
        )
    return masked


def normalize_lightspeed_redirect_uri(value: str) -> str:
    cleaned = value.strip().strip('"').strip("'").strip()
    if cleaned.endswith("/"):
        cleaned = cleaned.rstrip("/")
    return cleaned


def lightspeed_retail_config() -> dict[str, str]:
    redirect_uri = normalize_lightspeed_redirect_uri(os.environ.get("LIGHTSPEED_REDIRECT_URI", ""))
    if redirect_uri != LIGHTSPEED_RSERIES_REDIRECT_URI:
        logger.warning(
            "LIGHTSPEED_REDIRECT_URI normalized mismatch; using required callback. configured_repr=%r configured_length=%s required_length=%s",
            redirect_uri,
            len(redirect_uri),
            len(LIGHTSPEED_RSERIES_REDIRECT_URI),
        )
        redirect_uri = LIGHTSPEED_RSERIES_REDIRECT_URI
    logger.info("Lightspeed R-Series redirect_uri repr=%r length=%s", redirect_uri, len(redirect_uri))
    return {
        "client_id": os.environ.get("LIGHTSPEED_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("LIGHTSPEED_CLIENT_SECRET", "").strip(),
        "redirect_uri": redirect_uri,
        "scope": "employee:register employee:inventory",
    }


def lightspeed_retail_connected() -> bool:
    token = load_integration_token(LIGHTSPEED_RSERIES_PROVIDER)
    return bool(token and token["access_token"] and token["refresh_token"])


def lightspeed_retail_auth_url() -> str:
    config = lightspeed_retail_config()
    if not config["client_id"] or not config["client_secret"] or not config["redirect_uri"]:
        raise HTTPException(status_code=400, detail="Configuration Lightspeed Retail incomplète")

    state = secrets.token_urlsafe(24)
    save_oauth_state(LIGHTSPEED_RSERIES_PROVIDER, state)
    params = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "scope": config["scope"],
        "state": state,
    }
    parts = urlsplit(LIGHTSPEED_RSERIES_AUTHORIZE_URL)
    auth_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))
    masked_params = {**params, "client_id": "***", "state": "***"}
    masked_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(masked_params), parts.fragment))
    logger.info("Lightspeed R-Series authorization URL generated: %s", masked_url)
    return auth_url

def request_lightspeed_retail_token(payload: dict[str, str]) -> dict[str, Any]:
    config = lightspeed_retail_config()
    fields = {
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        **payload,
    }
    body = urlencode(fields).encode("utf-8")
    request = UrlRequest(
        LIGHTSPEED_RSERIES_TOKEN_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "IndustriaCatalogueAudit/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = mask_sensitive_text(exc.read().decode("utf-8", errors="replace"))
        parameter_names = sorted(fields.keys())
        logger.warning(
            "Lightspeed R-Series OAuth token request failed: endpoint=%s status=%s params=%s body=%s",
            LIGHTSPEED_RSERIES_TOKEN_URL,
            exc.code,
            ",".join(parameter_names),
            detail,
        )
        raise LightspeedTokenExchangeError(
            status_code=exc.code,
            response_body=detail,
            endpoint=LIGHTSPEED_RSERIES_TOKEN_URL,
            parameter_names=parameter_names,
        ) from exc


def exchange_lightspeed_retail_code(code: str) -> dict[str, Any]:
    config = lightspeed_retail_config()
    return request_lightspeed_retail_token(
        {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": config["redirect_uri"],
        }
    )


def refresh_lightspeed_retail_token() -> dict[str, Any]:
    token = load_integration_token(LIGHTSPEED_RSERIES_PROVIDER)
    if not token or not token["refresh_token"]:
        raise HTTPException(status_code=400, detail="Lightspeed Retail n'est pas connecté.")
    refreshed = request_lightspeed_retail_token(
        {
            "refresh_token": token["refresh_token"],
            "grant_type": "refresh_token",
        }
    )
    if not refreshed.get("refresh_token"):
        refreshed["refresh_token"] = token["refresh_token"]
    if not refreshed.get("account_id") and token["account_id"]:
        refreshed["account_id"] = token["account_id"]
    save_integration_token(LIGHTSPEED_RSERIES_PROVIDER, refreshed)
    return refreshed


def get_valid_lightspeed_retail_token() -> str:
    token = load_integration_token(LIGHTSPEED_RSERIES_PROVIDER)
    if not token or not token["access_token"]:
        raise HTTPException(status_code=400, detail="Lightspeed Retail n'est pas connecté.")
    if token["expires_at"] and int(token["expires_at"]) <= int(time.time()) + 60:
        refreshed = refresh_lightspeed_retail_token()
        return str(refreshed.get("access_token") or "")
    return str(token["access_token"])


def lightspeed_rseries_get(path: str, params: Optional[dict[str, Any]] = None, retry: bool = True) -> dict[str, Any]:
    endpoint = path if path.startswith("/") else f"/{path}"
    query = f"?{urlencode(params or {})}" if params else ""
    url = f"https://api.lightspeedapp.com/API/V3{endpoint}{query}"
    token = get_valid_lightspeed_retail_token()
    request = UrlRequest(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IndustriaCatalogueAudit/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = mask_sensitive_text(exc.read().decode("utf-8", errors="replace"))
        if exc.code == 401 and retry:
            logger.info("Lightspeed R-Series token expired during API call; refreshing token.")
            refresh_lightspeed_retail_token()
            return lightspeed_rseries_get(path, params=params, retry=False)
        logger.warning("Lightspeed R-Series API GET failed: endpoint=%s status=%s body=%s", endpoint, exc.code, body)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Lightspeed R-Series a refusé la requête.",
                "endpoint": endpoint,
                "status": exc.code,
                "body": body,
            },
        ) from exc


def lightspeed_rseries_get_raw(path: str, params: Optional[dict[str, Any]] = None, retry: bool = True) -> str:
    endpoint = path if path.startswith("/") else f"/{path}"
    query = f"?{urlencode(params or {})}" if params else ""
    url = f"https://api.lightspeedapp.com/API/V3{endpoint}{query}"
    token = get_valid_lightspeed_retail_token()
    request = UrlRequest(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "IndustriaCatalogueAudit/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        body = mask_sensitive_text(exc.read().decode("utf-8", errors="replace"))
        if exc.code == 401 and retry:
            logger.info("Lightspeed R-Series token expired during raw API call; refreshing token.")
            refresh_lightspeed_retail_token()
            return lightspeed_rseries_get_raw(path, params=params, retry=False)
        logger.warning("Lightspeed R-Series raw API GET failed: endpoint=%s status=%s body=%s", endpoint, exc.code, body)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Lightspeed R-Series a refusé la requête.",
                "endpoint": endpoint,
                "status": exc.code,
                "body": body,
            },
        ) from exc


def lightspeed_ecom_config() -> dict[str, str]:
    api_key = os.environ.get("LIGHTSPEED_ECOM_API_KEY", "").strip()
    api_secret = os.environ.get("LIGHTSPEED_ECOM_API_SECRET", "").strip()
    base_url = (
        os.environ.get("LIGHTSPEED_ECOM_API_BASE_URL")
        or os.environ.get("LIGHTSPEED_API_BASE_URL")
        or "https://api.shoplightspeed.com"
    ).strip().rstrip("/")
    language = (
        os.environ.get("LIGHTSPEED_ECOM_LANGUAGE")
        or os.environ.get("LIGHTSPEED_API_LANGUAGE")
        or ""
    ).strip().strip("/")

    missing = []
    if not api_key:
        missing.append("LIGHTSPEED_ECOM_API_KEY")
    if not api_secret:
        missing.append("LIGHTSPEED_ECOM_API_SECRET")
    if missing:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Configuration Lightspeed eCom manquante.",
                "missing": missing,
            },
        )

    language = language or "fc"
    if base_url.endswith(f"/{language}"):
        products_endpoint = "/products.json"
    else:
        products_endpoint = f"/{language}/products.json"

    return {
        "api_key": api_key,
        "api_secret": api_secret,
        "base_url": base_url,
        "products_endpoint": products_endpoint,
    }


def lightspeed_ecom_candidate_endpoints() -> list[str]:
    config = lightspeed_ecom_config()
    configured = config["products_endpoint"]
    candidates = [configured]
    for language in ("fc", "fr", "en"):
        endpoint = f"/{language}/products.json"
        if endpoint not in candidates:
            candidates.append(endpoint)
    return candidates


def lightspeed_ecom_get_raw(endpoint: str, params: Optional[dict[str, Any]] = None) -> tuple[str, str]:
    config = lightspeed_ecom_config()
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        parsed = urlsplit(endpoint)
        endpoint = parsed.path
        if parsed.query:
            endpoint = f"{endpoint}?{parsed.query}"
    endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    query = f"?{urlencode(params or {})}" if params else ""
    url = f"{config['base_url']}{endpoint}{query}"
    credentials = f"{config['api_key']}:{config['api_secret']}".encode("utf-8")
    request = UrlRequest(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            "User-Agent": "IndustriaCatalogueAudit/1.0",
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=60) as response:
            return url, response.read().decode("utf-8")
    except HTTPError as exc:
        body = mask_sensitive_text_full(exc.read().decode("utf-8", errors="replace"))
        safe_url = re.sub(r"([?&](?:key|secret|api_key|api_secret)=)[^&]+", r"\1***", url, flags=re.IGNORECASE)
        logger.warning("Lightspeed eCom API GET failed: endpoint=%s status=%s body=%s", endpoint, exc.code, body[:1000])
        raise HTTPException(
            status_code=502,
            detail={
                "message": "Lightspeed eCom a refusé la requête.",
                "endpoint": safe_url,
                "status": exc.code,
                "body": body[:2000],
            },
        ) from exc


def lightspeed_ecom_get_json(endpoint: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    _, raw_body = lightspeed_ecom_get_raw(endpoint, params)
    payload = json.loads(raw_body)
    return payload if isinstance(payload, dict) else {}


def lightspeed_ecom_get_products_test_raw() -> tuple[str, str]:
    last_error: HTTPException | None = None
    for endpoint in lightspeed_ecom_candidate_endpoints():
        try:
            return lightspeed_ecom_get_raw(endpoint, {"limit": 10, "offset": 0})
        except HTTPException as exc:
            last_error = exc
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            body = str(detail.get("body", ""))
            if "Unknown or inactive language" not in body:
                raise
    if last_error:
        raise last_error
    raise HTTPException(status_code=502, detail="Aucun endpoint eCom testé.")


def lightspeed_ecom_get_products_raw(limit: int, offset: int) -> tuple[str, str]:
    last_error: HTTPException | None = None
    for endpoint in lightspeed_ecom_candidate_endpoints():
        try:
            return lightspeed_ecom_get_raw(endpoint, {"limit": limit, "offset": offset})
        except HTTPException as exc:
            last_error = exc
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            body = str(detail.get("body", ""))
            if "Unknown or inactive language" not in body:
                raise
    if last_error:
        raise last_error
    raise HTTPException(status_code=502, detail="Aucun endpoint eCom testé.")


def resource_link(value: Any) -> str:
    if isinstance(value, dict):
        link = value.get("link")
        if link:
            return clean(link)
        resource = value.get("resource")
        if isinstance(resource, dict):
            return clean(resource.get("link"))
    return ""


def extract_ecom_products(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("products", "Product", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = value.get("product") or value.get("item") or value.get("data")
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, dict)]
                return [value]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def nested_count(value: Any, child_key: str = "") -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if child_key and isinstance(value.get(child_key), list):
            return len(value[child_key])
        for nested in value.values():
            if isinstance(nested, list):
                return len(nested)
    return 0


def first_nested_dict(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(value, dict):
        for key in keys:
            nested = value.get(key)
            if isinstance(nested, dict):
                return nested
        return value
    return {}


def collection_from_payload(payload: dict[str, Any], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = value.get(key[:-1]) or value.get(key) or value.get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            return [value]
    return []


def get_ecom_resource_collection(resource: Any, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    link = resource_link(resource)
    if not link:
        return []
    try:
        return collection_from_payload(lightspeed_ecom_get_json(link), keys)
    except (HTTPException, json.JSONDecodeError) as exc:
        logger.warning("Lightspeed eCom linked resource failed: link=%s error=%s", link, exc)
        return []


def get_ecom_resource_record(resource: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    link = resource_link(resource)
    if not link:
        return first_nested_dict(resource, keys)
    try:
        payload = lightspeed_ecom_get_json(link)
    except (HTTPException, json.JSONDecodeError) as exc:
        logger.warning("Lightspeed eCom linked record failed: link=%s error=%s", link, exc)
        return {}
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return payload


def simplify_ecom_product(product: dict[str, Any], expand_links: bool = False) -> dict[str, Any]:
    brand = product.get("brand") or product.get("Brand")
    brand_dict = get_ecom_resource_record(brand, ("brand", "Brand")) if expand_links else first_nested_dict(brand, ("brand", "Brand"))
    variants = product.get("variants") or product.get("Variants") or product.get("variant")
    images = product.get("images") or product.get("Images") or product.get("image")
    variant_rows = get_ecom_resource_collection(variants, ("variants", "variant", "Variant")) if expand_links else []
    image_rows = get_ecom_resource_collection(images, ("productImages", "productImage", "images", "image", "Image")) if expand_links else []
    variant_dict = variant_rows[0] if variant_rows else first_nested_dict(variants, ("variant", "Variant"))
    return {
        "product_id": clean(product.get("id") or product.get("productID") or product.get("product_id")),
        "title": clean(product.get("title") or product.get("fulltitle") or product.get("name")),
        "visibility": clean(product.get("isVisible") or product.get("visible") or product.get("visibility")),
        "brand": clean(brand_dict.get("title") or brand_dict.get("name") or brand_dict.get("id")),
        "url": clean(product.get("url") or product.get("urlPath") or product.get("fullUrl")),
        "price": clean(product.get("priceIncl") or product.get("priceExcl") or product.get("price") or variant_dict.get("priceIncl") or variant_dict.get("price")),
        "sku": clean(product.get("sku") or product.get("articleCode") or variant_dict.get("sku") or variant_dict.get("articleCode")),
        "ean_upc": clean(product.get("ean") or product.get("upc") or product.get("ean13") or variant_dict.get("ean") or variant_dict.get("upc")),
        "variants_count": len(variant_rows) if expand_links else nested_count(variants, "variant"),
        "images_count": len(image_rows) if expand_links else nested_count(images, "image"),
        "updated_at": clean(product.get("updatedAt") or product.get("updated_at") or product.get("modifiedAt")),
    }


def ecom_product_context(product: dict[str, Any]) -> dict[str, Any]:
    brand = product.get("brand") or product.get("Brand")
    variants = product.get("variants") or product.get("Variants") or product.get("variant")
    images = product.get("images") or product.get("Images") or product.get("image")
    categories = product.get("categories")
    attributes = product.get("attributes")
    metafields = product.get("metafields")
    tags = product.get("tags")
    return {
        "brand": get_ecom_resource_record(brand, ("brand", "Brand")),
        "variants": get_ecom_resource_collection(variants, ("variants", "variant", "Variant")),
        "images": get_ecom_resource_collection(images, ("productImages", "productImage", "images", "image", "Image")),
        "categories": get_ecom_resource_collection(categories, ("categoriesProducts", "categoriesProduct")),
        "attributes": get_ecom_resource_collection(attributes, ("productAttributes", "productAttribute")),
        "metafields": get_ecom_resource_collection(metafields, ("productMetafields", "productMetafield")),
        "tags": get_ecom_resource_collection(tags, ("tagsProducts", "tagsProduct")),
    }


def image_url_from_ecom_image(image: dict[str, Any]) -> str:
    return clean(
        image.get("src")
        or image.get("url")
        or image.get("link")
        or image.get("thumb")
        or image.get("original")
    )


def ecom_metafield_value(metafields: list[dict[str, Any]], *keys: str) -> str:
    wanted = {key.lower() for key in keys}
    for field in metafields:
        key = clean(field.get("key")).lower()
        if key in wanted:
            return clean(field.get("value"))
    return ""


def ecom_category_titles(categories_products: list[dict[str, Any]]) -> list[str]:
    titles: list[str] = []
    for item in categories_products:
        category = item.get("category")
        record = get_ecom_resource_record(category, ("category", "Category"))
        title = clean(record.get("title") or record.get("fulltitle") or record.get("name") or record.get("id"))
        if title:
            titles.append(title)
    return titles


def ecom_attribute_values(attributes: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in attributes:
        parts = [
            clean(item.get("title") or item.get("name") or item.get("key")),
            clean(item.get("value") or item.get("valueTitle") or item.get("attributeValue")),
        ]
        text = ": ".join(part for part in parts if part)
        if text:
            values.append(text)
    return values


def ecom_tag_values(tags: list[dict[str, Any]]) -> list[str]:
    values: list[str] = []
    for item in tags:
        tag = item.get("tag")
        record = get_ecom_resource_record(tag, ("tag", "Tag")) if isinstance(tag, dict) else {}
        text = clean(record.get("title") or record.get("name") or item.get("title") or item.get("name") or item.get("id"))
        if text:
            values.append(text)
    return values


def ecom_audit_candidate_rows(product: dict[str, Any], context: dict[str, Any]) -> list[dict[str, str]]:
    product_id_value = clean(product.get("id") or product.get("productID") or product.get("product_id"))
    brand = context.get("brand") or {}
    variants = context.get("variants") or [{}]
    images = context.get("images") or []
    metafields = context.get("metafields") or []
    category_titles = ecom_category_titles(context.get("categories") or [])
    filter_values = ecom_attribute_values(context.get("attributes") or []) + ecom_tag_values(context.get("tags") or [])
    image_urls = [url for image in images if (url := image_url_from_ecom_image(image))]
    brand_value = clean(brand.get("title") or brand.get("name") or brand.get("id"))
    meta_title_fc = ecom_metafield_value(metafields, "meta_title_fc", "meta_title_fr") or clean(product.get("title"))
    meta_description_fc = ecom_metafield_value(metafields, "meta_description_fc", "meta_description_fr")
    meta_keywords_fc = ecom_metafield_value(metafields, "meta_keywords_fc", "meta_keywords_fr")
    google_category_fc = ecom_metafield_value(metafields, "google_product_category_fc", "google_product_category_fr")
    rows: list[dict[str, str]] = []
    for variant in variants:
        variant_id = clean(variant.get("id") or variant.get("variantID") or variant.get("productVariantID")) or product_id_value
        rows.append(
            {
                "Internal_ID": product_id_value,
                "Internal_Variant_ID": variant_id,
                "Brand": brand_value,
                "FC_Title_Short": clean(product.get("title")),
                "US_Title_Short": clean(product.get("title")),
                "SKU": clean(variant.get("sku") or variant.get("articleCode") or product.get("sku")),
                "UPC": clean(variant.get("ean") or variant.get("upc") or variant.get("ean13") or product.get("ean")),
                "Visible": "Y" if str(product.get("isVisible")).lower() == "true" or clean(product.get("visibility")).lower() == "visible" else "N",
                "Stock_Disable_Sold_Out": clean(variant.get("stockTracking") or variant.get("stockDisableSoldOut") or product.get("stockDisableSoldOut")),
                "Images": " | ".join(image_urls),
                "FC_Description_Short": clean(product.get("description")),
                "FC_Description_Long": clean(product.get("content")),
                "US_Description_Short": "",
                "US_Description_Long": "",
                "URL": clean(product.get("url")),
                "FC_Meta_Title": meta_title_fc,
                "FC_Meta_Description": meta_description_fc,
                "FC_Meta_Keywords": meta_keywords_fc,
                "FC_Google_Category": google_category_fc,
                "Categories": " | ".join(category_titles),
                "Filtres": " | ".join(filter_values),
                "Updated_At": clean(product.get("updatedAt") or product.get("updated_at") or product.get("modifiedAt")),
            }
        )
    return rows


def ecom_product_datetime(product: dict[str, Any]) -> Optional[datetime]:
    value = clean(product.get("updatedAt") or product.get("updated_at") or product.get("modifiedAt") or product.get("createdAt"))
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def quick_ecom_audit_row(product: dict[str, Any]) -> dict[str, str]:
    product_id_value = clean(product.get("id") or product.get("productID") or product.get("product_id"))
    return {
        "Internal_ID": product_id_value,
        "Internal_Variant_ID": product_id_value,
        "Brand": "",
        "FC_Title_Short": clean(product.get("title")),
        "US_Title_Short": clean(product.get("title")),
        "SKU": "",
        "UPC": "",
        "Visible": "Y" if str(product.get("isVisible")).lower() == "true" or clean(product.get("visibility")).lower() == "visible" else "N",
        "Images": "",
        "FC_Description_Short": clean(product.get("description")),
        "FC_Description_Long": clean(product.get("content")),
        "URL": clean(product.get("url")),
        "Updated_At": clean(product.get("updatedAt") or product.get("updated_at") or product.get("modifiedAt") or product.get("createdAt")),
        "needs_enrichment": "Y",
    }


def save_quick_ecom_product(conn: sqlite3.Connection, product: dict[str, Any], synced_at: str) -> bool:
    row = quick_ecom_audit_row(product)
    product_id_value = clean(row.get("Internal_ID"))
    if not product_id_value:
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO ecom_api_products
        (Internal_Variant_ID, Internal_ID, Brand, Product_Title, SKU, UPC, Visible,
         mapped_payload, product_payload, variant_payload, enriched_at, synced_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT enriched_at FROM ecom_api_products WHERE Internal_Variant_ID = ?), ''), ?)
        """,
        (
            product_id_value,
            product_id_value,
            clean(row.get("Brand")),
            clean(row.get("FC_Title_Short")),
            clean(row.get("SKU")),
            clean(row.get("UPC")),
            clean(row.get("Visible")),
            json.dumps(row, ensure_ascii=False),
            json.dumps(product, ensure_ascii=False),
            "{}",
            product_id_value,
            synced_at,
        ),
    )
    return True


def fetch_recent_ecom_products(hours: int = 24, scan_limit: int = 100) -> tuple[str, list[dict[str, Any]]]:
    hours = max(1, min(int(hours), 720))
    scan_limit = max(1, min(int(scan_limit), 250))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    url, raw_body = lightspeed_ecom_get_products_raw(scan_limit, 0)
    payload = json.loads(raw_body)
    products = extract_ecom_products(payload)
    recent = []
    for product in products:
        product_date = ecom_product_datetime(product)
        if product_date and product_date >= cutoff:
            recent.append(product)
    return url, recent


def save_recent_ecom_products(hours: int = 24, scan_limit: int = 100) -> dict[str, Any]:
    ensure_ecom_api_tables()
    url, products = fetch_recent_ecom_products(hours, scan_limit)
    synced_at = now_text()
    saved = 0
    with connect() as conn:
        for product in products:
            if save_quick_ecom_product(conn, product, synced_at):
                saved += 1
    return {
        "ok": True,
        "url": url,
        "recent_found": len(products),
        "saved": saved,
        "message": f"{len(products)} produit(s) récent(s) trouvé(s), {saved} sauvegardé(s) dans la table eCom API.",
    }


def sync_ecom_api_products(limit: int = 100) -> dict[str, Any]:
    ensure_ecom_api_tables()
    limit = max(1, min(int(limit), 200))
    page_size = min(10, limit)
    started_at = now_text()
    with connect() as conn:
        latest = conn.execute(
            """
            SELECT next_offset
            FROM ecom_api_sync_runs
            WHERE status IN ('success', 'paused')
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        start_offset = int(latest["next_offset"]) if latest else 0
        cursor = conn.execute(
            """
            INSERT INTO ecom_api_sync_runs (status, started_at, start_offset, next_offset, message)
            VALUES ('running', ?, ?, ?, '')
            """,
            (started_at, start_offset, start_offset),
        )
        run_id = int(cursor.lastrowid)

    products_seen = 0
    variants_saved = 0
    offset = start_offset
    try:
        while products_seen < limit:
            batch_limit = min(page_size, limit - products_seen)
            try:
                _, raw_body = lightspeed_ecom_get_products_raw(batch_limit, offset)
            except HTTPException as exc:
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                if detail.get("status") == 429 or "Too many requests" in str(detail.get("body", "")):
                    message = (
                        f"Pause limite Lightspeed à offset {offset}. "
                        "Relance la synchronisation plus tard pour reprendre."
                    )
                    with connect() as conn:
                        conn.execute(
                            """
                            UPDATE ecom_api_sync_runs
                            SET status = 'paused', finished_at = ?, products_seen = ?, variants_saved = ?,
                                next_offset = ?, message = ?
                            WHERE id = ?
                            """,
                            (now_text(), products_seen, variants_saved, offset, message, run_id),
                        )
                    return {
                        "ok": True,
                        "paused": True,
                        "run_id": run_id,
                        "products_seen": products_seen,
                        "variants_saved": variants_saved,
                        "next_offset": offset,
                        "message": message,
                    }
                raise
            payload = json.loads(raw_body)
            products = extract_ecom_products(payload)
            if not products:
                break

            with connect() as conn:
                for product in products:
                    if save_quick_ecom_product(conn, product, started_at):
                        variants_saved += 1
            products_seen += len(products)
            offset += len(products)
            with connect() as conn:
                conn.execute(
                    """
                    UPDATE ecom_api_sync_runs
                    SET products_seen = ?, variants_saved = ?, next_offset = ?
                    WHERE id = ?
                    """,
                    (products_seen, variants_saved, offset, run_id),
                )
            if len(products) < batch_limit:
                break
            time.sleep(1.5)

        message = (
            f"Sync rapide: {products_seen} produit(s) lus, {variants_saved} ligne(s) sauvegardée(s). "
            f"Prochain offset: {offset}."
        )
        with connect() as conn:
            conn.execute(
                """
                UPDATE ecom_api_sync_runs
                SET status = 'success', finished_at = ?, products_seen = ?, variants_saved = ?, next_offset = ?, message = ?
                WHERE id = ?
                """,
                (now_text(), products_seen, variants_saved, offset, message, run_id),
            )
        return {
            "ok": True,
            "run_id": run_id,
            "products_seen": products_seen,
            "variants_saved": variants_saved,
            "next_offset": offset,
            "message": message,
        }
    except Exception as exc:
        message = mask_sensitive_text_full(str(exc))[:1000]
        with connect() as conn:
            conn.execute(
                """
                UPDATE ecom_api_sync_runs
                SET status = 'error', finished_at = ?, products_seen = ?, variants_saved = ?, next_offset = ?, message = ?
                WHERE id = ?
                """,
                (now_text(), products_seen, variants_saved, offset, message, run_id),
            )
        raise


def enrich_ecom_api_products(limit: int = 10) -> dict[str, Any]:
    ensure_ecom_api_tables()
    limit = max(1, min(int(limit), 25))
    enriched_at = now_text()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT Internal_ID, product_payload
            FROM ecom_api_products
            WHERE COALESCE(enriched_at, '') = ''
            ORDER BY synced_at, Internal_ID
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    products_seen = 0
    variants_saved = 0
    for row in rows:
        try:
            product = json.loads(row["product_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(product, dict):
            continue
        context = ecom_product_context(product)
        mapped_rows = ecom_audit_candidate_rows(product, context)
        variants_by_id = {
            clean(variant.get("id") or variant.get("variantID") or variant.get("productVariantID")): variant
            for variant in context.get("variants", [])
            if isinstance(variant, dict)
        }
        product_id_value = clean(product.get("id") or row["Internal_ID"])
        with connect() as conn:
            conn.execute("DELETE FROM ecom_api_products WHERE Internal_Variant_ID = ?", (product_id_value,))
            for mapped in mapped_rows:
                variant_id = clean(mapped.get("Internal_Variant_ID"))
                if not variant_id:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO ecom_api_products
                    (Internal_Variant_ID, Internal_ID, Brand, Product_Title, SKU, UPC, Visible,
                     mapped_payload, product_payload, variant_payload, enriched_at, synced_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        variant_id,
                        clean(mapped.get("Internal_ID")),
                        clean(mapped.get("Brand")),
                        clean(mapped.get("FC_Title_Short")),
                        clean(mapped.get("SKU")),
                        clean(mapped.get("UPC")),
                        clean(mapped.get("Visible")),
                        json.dumps(mapped, ensure_ascii=False),
                        json.dumps(product, ensure_ascii=False),
                        json.dumps(variants_by_id.get(variant_id, {}), ensure_ascii=False),
                        enriched_at,
                        enriched_at,
                    ),
                )
                variants_saved += 1
        products_seen += 1
        time.sleep(1.5)
    return {
        "ok": True,
        "products_enriched": products_seen,
        "variants_saved": variants_saved,
        "message": f"{products_seen} produit(s) enrichi(s), {variants_saved} variante(s) sauvegardée(s).",
    }


def ecom_mapping_status(row: dict[str, str], field: str, source: str, required: bool = True) -> dict[str, str]:
    value = clean(row.get(field))
    if value:
        status = "OK"
    elif required:
        status = "Manquant"
    else:
        status = "À confirmer"
    return {
        "field": field,
        "value": value,
        "source": source,
        "status": status,
    }


ECOM_AUDIT_MAPPING_SOURCES = {
    "Internal_ID": "product.id",
    "Internal_Variant_ID": "variants[].id",
    "Brand": "brand.link -> brand.title/name",
    "FC_Title_Short": "product.title",
    "US_Title_Short": "à confirmer: eCom FC seulement dans ce test",
    "SKU": "variants[].sku/articleCode",
    "UPC": "variants[].ean/upc",
    "Visible": "product.isVisible / product.visibility",
    "Stock_Disable_Sold_Out": "variants/product stock fields",
    "Images": "images.link -> images[].src/url/thumb",
    "FC_Description_Short": "product.description",
    "FC_Description_Long": "product.content",
    "US_Description_Short": "à confirmer: autre langue/API",
    "US_Description_Long": "à confirmer: autre langue/API",
    "URL": "product.url",
    "FC_Meta_Title": "productMetafields meta_title_fc/fr ou product.title",
    "FC_Meta_Description": "productMetafields meta_description_fc/fr",
    "FC_Meta_Keywords": "productMetafields meta_keywords_fc/fr",
    "FC_Google_Category": "productMetafields google_product_category_fc/fr",
    "Categories": "categoriesProducts -> category.link -> category.title",
    "Filtres": "productAttributes + tagsProducts",
    "Updated_At": "product.updatedAt",
}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def extract_collection(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if isinstance(value, dict):
        nested = value.get(key)
        if nested is not None:
            value = nested
    return [item for item in as_list(value) if isinstance(item, dict)]


def extract_first_record(payload: dict[str, Any], key: str) -> dict[str, Any]:
    records = extract_collection(payload, key)
    return records[0] if records else {}


def resolve_lightspeed_account_id() -> str:
    token = load_integration_token(LIGHTSPEED_RSERIES_PROVIDER)
    if token and token["account_id"]:
        return str(token["account_id"])

    payload = lightspeed_rseries_get("/Account.json")
    accounts = extract_collection(payload, "Account")
    if not accounts:
        raise HTTPException(status_code=502, detail="Aucun compte Lightspeed R-Series retourné par l'API.")
    account_id = str(accounts[0].get("accountID") or accounts[0].get("id") or "")
    if not account_id:
        raise HTTPException(status_code=502, detail="Lightspeed n'a pas retourné de accountID.")

    if token:
        save_integration_token(
            LIGHTSPEED_RSERIES_PROVIDER,
            {
                "access_token": token["access_token"],
                "refresh_token": token["refresh_token"],
                "token_type": token["token_type"],
                "scope": token["scope"],
                "expires_in": max(0, int(token["expires_at"] or 0) - int(time.time())),
                "refresh_expires_in": max(0, int(token["refresh_expires_at"] or 0) - int(time.time())),
                "account_id": account_id,
            },
        )
    return account_id


def normalize_shop(row: dict[str, Any]) -> dict[str, str]:
    return {
        "shopID": clean(row.get("shopID") or row.get("id")),
        "name": clean(row.get("name")),
        "address": clean(row.get("address1") or row.get("address") or row.get("street1")),
        "city": clean(row.get("city")),
        "province": clean(row.get("state") or row.get("province")),
        "postal_code": clean(row.get("zip") or row.get("postalCode") or row.get("postal_code")),
    }


def get_lightspeed_shops() -> list[dict[str, str]]:
    account_id = resolve_lightspeed_account_id()
    payload = lightspeed_rseries_get(f"/Account/{account_id}/Shop.json")
    return [normalize_shop(row) for row in extract_collection(payload, "Shop")]


def normalize_price_level(row: dict[str, Any]) -> dict[str, str]:
    return {
        "priceLevelID": clean(row.get("priceLevelID") or row.get("id")),
        "name": clean(row.get("name")),
        "archived": clean(row.get("archived")),
        "type": clean(row.get("type")),
        "Calculation": clean(row.get("Calculation") or row.get("calculation")),
    }


def get_lightspeed_price_levels() -> list[dict[str, str]]:
    account_id = resolve_lightspeed_account_id()
    payload = lightspeed_rseries_get(f"/Account/{account_id}/PriceLevel.json")
    return [normalize_price_level(row) for row in extract_collection(payload, "PriceLevel")]


def simplify_item_price_rows(item: dict[str, Any]) -> list[dict[str, str]]:
    item_prices = item.get("Prices") or item.get("ItemPrices") or {}
    rows = item_prices.get("ItemPrice") if isinstance(item_prices, dict) else item_prices
    return [
        {
            "amount": clean(row.get("amount")),
            "useType": clean(row.get("useType")),
            "useTypeID": clean(row.get("useTypeID")),
        }
        for row in as_list(rows)
        if isinstance(row, dict)
    ]


def get_default_price_from_item(item: dict[str, Any]) -> str:
    for row in simplify_item_price_rows(item):
        if row["useType"].lower() == "default" or row["useTypeID"] == "1":
            return row["amount"]
    return ""


def simplify_item_shop_rows(item: dict[str, Any]) -> list[dict[str, str]]:
    item_shops = item.get("ItemShops") or {}
    rows = item_shops.get("ItemShop") if isinstance(item_shops, dict) else item_shops
    return [
        {
            "shopID": clean(row.get("shopID")),
            "qoh": clean(row.get("qoh")),
            "sellable": clean(row.get("sellable")),
        }
        for row in as_list(rows)
        if isinstance(row, dict)
    ]


def summarize_simple_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "itemID": clean(item.get("itemID")),
        "description": clean(item.get("description")),
        "customSku": clean(item.get("customSku")),
        "upc": clean(item.get("upc")),
        "defaultCost": clean(item.get("defaultCost")),
        "archived": clean(item.get("archived")),
        "deleted": clean(item.get("deleted")),
    }


def summarize_related_item(item: dict[str, Any]) -> dict[str, Any]:
    summary = summarize_simple_item(item)
    summary["ItemPrices"] = simplify_item_price_rows(item)
    summary["ItemShops"] = simplify_item_shop_rows(item)
    return summary


def get_lightspeed_simple_items(limit: int = 10) -> list[dict[str, str]]:
    account_id = resolve_lightspeed_account_id()
    payload = lightspeed_rseries_get(
        f"/Account/{account_id}/Item.json",
        params={"limit": max(1, min(limit, 10))},
    )
    return [summarize_simple_item(item) for item in extract_collection(payload, "Item")[:10]]


def get_lightspeed_items_with_inventory_relations(limit: int = 10) -> list[dict[str, Any]]:
    account_id = resolve_lightspeed_account_id()
    payload = lightspeed_rseries_get(
        f"/Account/{account_id}/Item.json",
        params={
            "limit": max(1, min(limit, 10)),
            "load_relations": json.dumps(["ItemPrices", "ItemShops"]),
        },
    )
    items = extract_collection(payload, "Item")[:10]
    return [summarize_related_item(item) for item in items]


def find_lightspeed_item_id_by_upc(upc: str) -> str:
    account_id = resolve_lightspeed_account_id()
    payload = lightspeed_rseries_get(
        f"/Account/{account_id}/Item.json",
        params={"upc": upc, "limit": 1},
    )
    item = extract_first_record(payload, "Item")
    return clean(item.get("itemID"))


def get_lightspeed_item_detail(item_id: str) -> dict[str, Any]:
    account_id = resolve_lightspeed_account_id()
    return lightspeed_rseries_get(
        f"/Account/{account_id}/Item/{item_id}.json",
        params={"load_relations": json.dumps(["ItemShops"])},
    )


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


def load_gpt_batch_product_keys() -> tuple[set[str], set[str]]:
    ensure_batch_tables()
    with connect() as conn:
        rows = conn.execute(
            "SELECT Internal_Variant_ID, Internal_ID FROM gpt_batch_items"
        ).fetchall()
    variant_ids = {str(row["Internal_Variant_ID"]) for row in rows if row["Internal_Variant_ID"]}
    internal_ids = {str(row["Internal_ID"]) for row in rows if row["Internal_ID"]}
    return variant_ids, internal_ids


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


def effective_priority(row: dict[str, Any], approvals: set[tuple[str, str]]) -> str:
    summary = summarize_product(row, approvals)
    if summary["Erreurs restantes"] == 0:
        return "Conforme"
    return clean(row.get("Priorité"))


def adjusted_brand_summary(products: list[dict[str, Any]], approvals: set[tuple[str, str]]) -> list[dict[str, Any]]:
    by_brand: dict[str, dict[str, Any]] = {}
    for row in products:
        brand = clean(row.get("Brand"))
        if not brand:
            continue
        item = by_brand.setdefault(
            brand,
            {
                "Brand": brand,
                "Produits": 0,
                "Score_total": 0.0,
                "Conformes": 0,
                "A_surveillance": 0,
                "Action_requise": 0,
                "Critiques": 0,
            },
        )
        item["Produits"] += 1
        try:
            item["Score_total"] += float(row.get("Score") or 0)
        except (TypeError, ValueError):
            pass
        priority = effective_priority(row, approvals)
        if priority == "Conforme":
            item["Conformes"] += 1
        elif priority == "À surveiller":
            item["A_surveillance"] += 1
        elif priority == "Action requise":
            item["Action_requise"] += 1
        elif priority == "Critique":
            item["Critiques"] += 1

    rows = []
    for item in by_brand.values():
        products_count = item["Produits"] or 1
        score = item.pop("Score_total", 0.0)
        item["Score_moyen"] = round(score / products_count, 1)
        item["% conformes"] = round(item["Conformes"] / products_count * 100, 1)
        item["% critiques"] = round(item["Critiques"] / products_count * 100, 1)
        rows.append(item)
    return sorted(rows, key=lambda item: (item["Critiques"], item["Action_requise"], item["Produits"]), reverse=True)


def filter_products(
    products: list[dict[str, Any]],
    approvals: set[tuple[str, str]],
    processed_brands: set[str],
    excluded_variant_ids: Optional[set[str]] = None,
    excluded_internal_ids: Optional[set[str]] = None,
    search: str = "",
    brand: str = "",
    priority: str = "",
    correction: str = "",
    approval: str = "pending",
) -> list[dict[str, Any]]:
    excluded_variant_ids = excluded_variant_ids or set()
    excluded_internal_ids = excluded_internal_ids or set()
    result = []
    for row in products:
        if product_id(row) in excluded_variant_ids:
            continue
        internal_id = product_internal_id(row)
        if internal_id and internal_id in excluded_internal_ids:
            continue
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


@app.get("/api/integrations")
def api_integrations(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    retail_config = lightspeed_retail_config()
    retail_token = load_integration_token(LIGHTSPEED_RSERIES_PROVIDER)
    retail_missing = [
        label
        for label, value in (
            ("LIGHTSPEED_CLIENT_ID", retail_config["client_id"]),
            ("LIGHTSPEED_CLIENT_SECRET", retail_config["client_secret"]),
            ("LIGHTSPEED_REDIRECT_URI", retail_config["redirect_uri"]),
        )
        if not value
    ]
    ecom_connected = bool(
        os.environ.get("LIGHTSPEED_API_TOKEN")
        or (os.environ.get("LIGHTSPEED_API_KEY") and os.environ.get("LIGHTSPEED_API_SECRET"))
    )
    return {
        "items": [
            {
                "id": "lightspeed_retail",
                "name": "Lightspeed Retail POS R-Series",
                "connected": lightspeed_retail_connected(),
                "configured": not retail_missing,
                "missing": retail_missing,
                "domain_prefix": "",
                "scope": retail_token["scope"] if retail_token else retail_config["scope"],
                "expires": datetime.fromtimestamp(int(retail_token["expires_at"])).strftime("%Y-%m-%d %H:%M:%S") if retail_token and retail_token["expires_at"] else "",
                "connect_url": "/lightspeed/connect",
            },
            {
                "id": "lightspeed_ecom",
                "name": "Lightspeed eCom",
                "connected": ecom_connected,
                "configured": ecom_connected,
                "missing": [],
            },
            {"id": "google_merchant", "name": "Google Merchant Center", "connected": False, "configured": False, "missing": []},
            {"id": "google_ads", "name": "Google Ads", "connected": False, "configured": False, "missing": []},
        ]
    }


@app.get("/lightspeed/connect")
def lightspeed_connect(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    return RedirectResponse(lightspeed_retail_auth_url())


@app.post("/api/integrations/lightspeed/refresh")
def api_lightspeed_refresh(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    refresh_lightspeed_retail_token()
    return {"ok": True, "connected": True}


@app.get("/lightspeed/callback")
def lightspeed_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    error_uri: str = "",
):
    callback_debug = {
        "error": error or "",
        "error_description": error_description or "",
        "error_uri": error_uri or "",
        "state_present": bool(state),
        "code_present": bool(code),
    }
    logger.info("Lightspeed R-Series OAuth callback received: %s", callback_debug)
    if error:
        return HTMLResponse(
            f"""
            <h1>Connexion Lightspeed refusée</h1>
            <p><strong>error:</strong> {html.escape(error)}</p>
            <p><strong>error_description:</strong> {html.escape(error_description or "")}</p>
            <p><strong>error_uri:</strong> {html.escape(error_uri or "")}</p>
            <p><strong>state reçu:</strong> {"oui" if state else "non"}</p>
            <p><strong>code reçu:</strong> {"oui" if code else "non"}</p>
            """,
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            f"""
            <h1>Connexion Lightspeed incomplète</h1>
            <p>Code OAuth manquant.</p>
            <p><strong>error:</strong> {html.escape(error or "")}</p>
            <p><strong>error_description:</strong> {html.escape(error_description or "")}</p>
            <p><strong>error_uri:</strong> {html.escape(error_uri or "")}</p>
            <p><strong>state reçu:</strong> {"oui" if state else "non"}</p>
            <p><strong>code reçu:</strong> non</p>
            """,
            status_code=400,
        )
    if not state or not consume_oauth_state(LIGHTSPEED_RSERIES_PROVIDER, state):
        logger.warning("Lightspeed R-Series OAuth callback rejected because state was invalid or expired.")
        return HTMLResponse("<h1>Connexion Lightspeed refusée</h1><p>Validation de sécurité invalide.</p>", status_code=400)

    try:
        token = exchange_lightspeed_retail_code(code)
    except LightspeedTokenExchangeError as exc:
        return HTMLResponse(
            f"""
            <h1>Échec de l'échange du code Lightspeed</h1>
            <p>Le code OAuth est présent, mais Lightspeed a refusé l'échange contre les jetons.</p>
            <p><strong>Endpoint:</strong> {html.escape(exc.endpoint)}</p>
            <p><strong>Statut HTTP:</strong> {exc.status_code}</p>
            <p><strong>Paramètres envoyés:</strong> {html.escape(", ".join(exc.parameter_names))}</p>
            <pre style="white-space: pre-wrap;">{html.escape(exc.response_body)}</pre>
            """,
            status_code=502,
        )
    save_integration_token(LIGHTSPEED_RSERIES_PROVIDER, token)
    return HTMLResponse(
        """
        <!doctype html>
        <html lang="fr">
          <head><meta charset="utf-8"><title>Lightspeed connecté</title></head>
          <body style="font-family: system-ui; padding: 32px;">
            <h1>Lightspeed Retail POS R-Series est connecté.</h1>
            <p>Tu peux retourner au Catalogue Audit.</p>
            <p><a href="/">Retour au dashboard</a></p>
          </body>
        </html>
        """
    )


def diagnostic_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="fr">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <title>{html.escape(title)}</title>
            <style>
              body {{ font-family: system-ui, sans-serif; margin: 32px; background: #0e1117; color: #f4f6fb; }}
              a {{ color: #67d4c7; }}
              table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
              th, td {{ border: 1px solid #2b313d; padding: 8px 10px; text-align: left; vertical-align: top; }}
              th {{ background: #1b202b; }}
              select, button {{ padding: 8px 10px; margin-right: 8px; }}
              .error {{ border: 1px solid #ff7c7c; padding: 12px; color: #ffb3b3; white-space: pre-wrap; }}
              .muted {{ color: #9da5b4; }}
            </style>
          </head>
          <body>
            <h1>{html.escape(title)}</h1>
            {body}
          </body>
        </html>
        """
    )


def api_error_html(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        text = json.dumps(detail, ensure_ascii=False, indent=2)
    else:
        text = str(detail)
    return f'<div class="error">{html.escape(text)}</div>'


@app.get("/lightspeed/stores")
def lightspeed_stores_page(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    try:
        shops = get_lightspeed_shops()
    except HTTPException as exc:
        return diagnostic_page("Lightspeed R-Series - Succursales", api_error_html(exc))

    rows = "".join(
        f"""
        <tr>
          <td>{html.escape(shop["shopID"])}</td>
          <td>{html.escape(shop["name"])}</td>
          <td>{html.escape(shop["address"])}</td>
          <td>{html.escape(shop["city"])}</td>
          <td>{html.escape(shop["province"])}</td>
          <td>{html.escape(shop["postal_code"])}</td>
        </tr>
        """
        for shop in shops
    ) or '<tr><td colspan="6" class="muted">Aucune succursale retournée par Lightspeed.</td></tr>'
    options = "".join(
        f'<option value="{html.escape(shop["shopID"])}">{html.escape(shop["name"] or shop["shopID"])}</option>'
        for shop in shops
    )
    body = f"""
      <p class="muted">Diagnostic lecture seule. Aucun envoi à Google, aucune écriture Lightspeed.</p>
      <table>
        <thead>
          <tr><th>ID R-Series</th><th>Nom</th><th>Adresse</th><th>Ville</th><th>Province</th><th>Code postal</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <h2>Test inventaire par succursale</h2>
      <form action="/lightspeed/items-test" method="get">
        <select name="shop_id" required>{options}</select>
        <button type="submit">Récupérer 10 articles</button>
      </form>
    """
    return diagnostic_page("Lightspeed R-Series - Succursales", body)


@app.get("/lightspeed/price-levels")
def lightspeed_price_levels_page(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    try:
        price_levels = get_lightspeed_price_levels()
    except HTTPException as exc:
        return diagnostic_page("Lightspeed R-Series - Niveaux de prix", api_error_html(exc))

    rows = "".join(
        f"""
        <tr>
          <td>{html.escape(row["priceLevelID"])}</td>
          <td>{html.escape(row["name"])}</td>
          <td>{html.escape(row["archived"])}</td>
          <td>{html.escape(row["type"])}</td>
          <td>{html.escape(row["Calculation"])}</td>
        </tr>
        """
        for row in price_levels
    ) or '<tr><td colspan="5" class="muted">Aucun niveau de prix retourné par Lightspeed.</td></tr>'
    body = f"""
      <p class="muted">Diagnostic lecture seule. Aucun envoi à Google, aucune écriture Lightspeed.</p>
      <p><a href="/lightspeed/stores">Succursales</a> | <a href="/lightspeed/items-test">Test inventaire</a></p>
      <table>
        <thead>
          <tr><th>priceLevelID</th><th>name</th><th>archived</th><th>type</th><th>Calculation</th></tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
    """
    return diagnostic_page("Lightspeed R-Series - Niveaux de prix", body)


@app.get("/lightspeed/items-test")
def lightspeed_items_test_page(
    shop_id: str = "",
    item_id: str = "",
    upc: str = "",
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    try:
        simple_items = get_lightspeed_simple_items(limit=10)
    except HTTPException as exc:
        return diagnostic_page("Lightspeed R-Series - Test inventaire", api_error_html(exc))

    simple_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(item["itemID"])}</td>
          <td>{html.escape(item["description"])}</td>
          <td>{html.escape(item["customSku"])}</td>
          <td>{html.escape(item["upc"])}</td>
          <td>{html.escape(item["defaultCost"])}</td>
          <td>{html.escape(item["archived"])}</td>
          <td>{html.escape(item["deleted"])}</td>
        </tr>
        """
        for item in simple_items
    ) or '<tr><td colspan="7" class="muted">Aucun article retourné par Lightspeed.</td></tr>'

    detail_html = ""
    search_value = clean(item_id) or clean(upc)
    if search_value:
        try:
            resolved_item_id = clean(item_id)
            if not resolved_item_id and upc:
                resolved_item_id = find_lightspeed_item_id_by_upc(clean(upc))
            if not resolved_item_id:
                raise HTTPException(status_code=404, detail=f"Aucun item trouvé pour UPC {upc}.")
            raw_payload = get_lightspeed_item_detail(resolved_item_id)
            raw_item = extract_first_record(raw_payload, "Item")
            item_prices = simplify_item_price_rows(raw_item)
            item_shops = simplify_item_shop_rows(raw_item)
            default_price = get_default_price_from_item(raw_item)
            selected_shop = clean(shop_id)
            selected_stock = next((row for row in item_shops if row["shopID"] == selected_shop), {}) if selected_shop else {}
            price_rows = "".join(
                f"<tr><td>{html.escape(row['amount'])}</td><td>{html.escape(row['useType'])}</td><td>{html.escape(row['useTypeID'])}</td></tr>"
                for row in item_prices
            ) or '<tr><td colspan="3" class="muted">Aucun prix retourné.</td></tr>'
            shop_rows = "".join(
                f"<tr><td>{html.escape(row['shopID'])}</td><td>{html.escape(row['qoh'])}</td><td>{html.escape(row['sellable'])}</td></tr>"
                for row in item_shops
            ) or '<tr><td colspan="3" class="muted">Aucun ItemShops retourné.</td></tr>'
            selected_stock_html = ""
            if selected_shop:
                selected_stock_html = f"""
                  <h3>Stock succursale sélectionnée</h3>
                  <table>
                    <thead><tr><th>shopID</th><th>qoh</th><th>sellable</th></tr></thead>
                    <tbody>
                      <tr>
                        <td>{html.escape(selected_shop)}</td>
                        <td>{html.escape(clean(selected_stock.get("qoh")))}</td>
                        <td>{html.escape(clean(selected_stock.get("sellable")))}</td>
                      </tr>
                    </tbody>
                  </table>
                """
            detail_html = f"""
              <h2>Produit précis</h2>
              <p class="muted">GET /Account/accountID/Item/{html.escape(resolved_item_id)}.json avec load_relations=ItemShops</p>
              <h3>Valeurs retenues pour Google Merchant local</h3>
              <table>
                <thead><tr><th>itemID</th><th>description</th><th>UPC</th><th>SKU</th><th>Prix Default</th></tr></thead>
                <tbody>
                  <tr>
                    <td>{html.escape(resolved_item_id)}</td>
                    <td>{html.escape(clean(raw_item.get("description")))}</td>
                    <td>{html.escape(clean(raw_item.get("upc")))}</td>
                    <td>{html.escape(clean(raw_item.get("customSku")))}</td>
                    <td>{html.escape(default_price)}</td>
                  </tr>
                </tbody>
              </table>
              {selected_stock_html}
              <h3>JSON brut complet masqué</h3>
              <pre style="white-space: pre-wrap;">{html.escape(mask_sensitive_text(json.dumps(raw_payload, ensure_ascii=False, indent=2)))}</pre>
              <h3>Prices</h3>
              <table>
                <thead><tr><th>amount</th><th>useType</th><th>useTypeID</th></tr></thead>
                <tbody>{price_rows}</tbody>
              </table>
              <h3>ItemShops</h3>
              <table>
                <thead><tr><th>shopID</th><th>qoh</th><th>sellable</th></tr></thead>
                <tbody>{shop_rows}</tbody>
              </table>
            """
        except HTTPException as exc:
            detail_html = f"<h2>Produit précis</h2>{api_error_html(exc)}"

    body = f"""
      <p><a href="/lightspeed/stores">Succursales</a> | <a href="/lightspeed/price-levels">Niveaux de prix</a></p>
      <p class="muted">Diagnostic lecture seule. Succursale sélectionnée : {html.escape(shop_id)}. Aucune écriture Lightspeed, aucun envoi Google.</p>
      <form action="/lightspeed/items-test" method="get">
        <input name="shop_id" value="{html.escape(shop_id)}" placeholder="shopID optionnel" />
        <input name="item_id" value="{html.escape(item_id)}" placeholder="itemID exact" />
        <input name="upc" value="{html.escape(upc)}" placeholder="UPC exact" />
        <button type="submit">Tester produit précis</button>
      </form>
      <h2>Étape 1 - Requête simple sans relation</h2>
      <p class="muted">GET /Account/accountID/Item.json?limit=10</p>
      <table>
        <thead>
          <tr><th>itemID</th><th>description</th><th>customSku</th><th>upc</th><th>defaultCost</th><th>archived</th><th>deleted</th></tr>
        </thead>
        <tbody>{simple_rows}</tbody>
      </table>

      {detail_html}
    """
    return diagnostic_page("Lightspeed R-Series - Test inventaire", body)


@app.get("/lightspeed/item-raw/{itemID}")
def lightspeed_item_raw_page(
    itemID: str,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    account_id = resolve_lightspeed_account_id()
    raw_body = lightspeed_rseries_get_raw(f"/Account/{account_id}/Item/{itemID}.json")
    return Response(
        content=mask_sensitive_text(raw_body),
        media_type="application/json; charset=utf-8",
    )


@app.get("/catalogue/ecom-test")
def catalogue_ecom_test_page(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    try:
        url, raw_body = lightspeed_ecom_get_products_test_raw()
        payload = json.loads(raw_body)
        products = extract_ecom_products(payload)[:10]
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom C-Series - Test catalogue", api_error_html(exc))
    except json.JSONDecodeError as exc:
        return diagnostic_page(
            "Lightspeed eCom C-Series - Test catalogue",
            f"""
            <div class="error">Réponse eCom non JSON: {html.escape(str(exc))}</div>
            <h2>Réponse brute</h2>
            <pre style="white-space: pre-wrap;">{html.escape(mask_sensitive_text_full(raw_body))}</pre>
            """,
        )

    simplified = [simplify_ecom_product(product, expand_links=True) for product in products]
    rows = "".join(
        f"""
        <tr>
          <td>{html.escape(row["product_id"])}</td>
          <td>{html.escape(row["title"])}</td>
          <td>{html.escape(row["visibility"])}</td>
          <td>{html.escape(row["brand"])}</td>
          <td>{html.escape(row["url"])}</td>
          <td>{html.escape(row["price"])}</td>
          <td>{html.escape(row["sku"])}</td>
          <td>{html.escape(row["ean_upc"])}</td>
          <td>{row["variants_count"]}</td>
          <td>{row["images_count"]}</td>
          <td>{html.escape(row["updated_at"])}</td>
        </tr>
        """
        for row in simplified
    ) or '<tr><td colspan="11" class="muted">Aucun produit retourné par Lightspeed eCom.</td></tr>'
    body = f"""
      <p class="muted">Diagnostic eCom en lecture seule. Aucun changement local, aucun envoi vers Lightspeed.</p>
      <p class="muted">Endpoint appelé : {html.escape(url)}</p>
      <h2>Version simplifiée</h2>
      <table>
        <thead>
          <tr>
            <th>product ID</th>
            <th>titre</th>
            <th>visibilité</th>
            <th>marque</th>
            <th>URL</th>
            <th>prix</th>
            <th>SKU</th>
            <th>EAN/UPC</th>
            <th>variantes</th>
            <th>images</th>
            <th>modifié</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <h2>Réponse brute</h2>
      <pre style="white-space: pre-wrap;">{html.escape(mask_sensitive_text_full(raw_body))}</pre>
    """
    return diagnostic_page("Lightspeed eCom C-Series - Test catalogue", body)


@app.get("/catalogue/ecom-map-test")
def catalogue_ecom_map_test_page(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    try:
        url, raw_body = lightspeed_ecom_get_products_test_raw()
        payload = json.loads(raw_body)
        products = extract_ecom_products(payload)[:10]
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom C-Series - Mapping audit", api_error_html(exc))
    except json.JSONDecodeError as exc:
        return diagnostic_page(
            "Lightspeed eCom C-Series - Mapping audit",
            f'<div class="error">Réponse eCom non JSON: {html.escape(str(exc))}</div>',
        )

    candidate_rows: list[dict[str, str]] = []
    status_rows: list[dict[str, str]] = []
    required_fields = {
        "Internal_ID",
        "Internal_Variant_ID",
        "Brand",
        "FC_Title_Short",
        "US_Title_Short",
        "SKU",
        "UPC",
        "Visible",
        "Images",
        "FC_Description_Short",
        "FC_Description_Long",
        "FC_Meta_Title",
        "FC_Meta_Description",
        "FC_Meta_Keywords",
        "FC_Google_Category",
        "Categories",
    }
    for product in products:
        context = ecom_product_context(product)
        rows = ecom_audit_candidate_rows(product, context)
        candidate_rows.extend(rows[:3])
        sample = rows[0] if rows else {}
        for field, source in ECOM_AUDIT_MAPPING_SOURCES.items():
            status_rows.append(ecom_mapping_status(sample, field, source, required=field in required_fields))

    mapping_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(row["field"])}</td>
          <td>{html.escape(row["status"])}</td>
          <td>{html.escape(row["source"])}</td>
          <td>{html.escape(row["value"][:220])}</td>
        </tr>
        """
        for row in status_rows
    ) or '<tr><td colspan="4" class="muted">Aucun mapping à afficher.</td></tr>'

    candidate_fields = [
        "Internal_ID",
        "Internal_Variant_ID",
        "Brand",
        "FC_Title_Short",
        "US_Title_Short",
        "SKU",
        "UPC",
        "Visible",
        "Images",
        "FC_Description_Short",
        "FC_Description_Long",
        "FC_Meta_Title",
        "FC_Meta_Description",
        "FC_Meta_Keywords",
        "FC_Google_Category",
        "Categories",
        "Filtres",
    ]
    candidate_html_rows = "".join(
        "<tr>" + "".join(f"<td>{html.escape(clean(row.get(field))[:180])}</td>" for field in candidate_fields) + "</tr>"
        for row in candidate_rows[:20]
    ) or f'<tr><td colspan="{len(candidate_fields)}" class="muted">Aucune ligne candidate.</td></tr>'
    body = f"""
      <p class="muted">Diagnostic lecture seule. Aucun changement SQLite, aucun remplacement CSV.</p>
      <p class="muted">Endpoint produits : {html.escape(url)}</p>
      <h2>Statut des champs audit</h2>
      <table>
        <thead><tr><th>Champ audit</th><th>Statut</th><th>Source API</th><th>Exemple valeur</th></tr></thead>
        <tbody>{mapping_rows}</tbody>
      </table>
      <h2>Lignes candidates au format audit</h2>
      <table>
        <thead><tr>{"".join(f"<th>{html.escape(field)}</th>" for field in candidate_fields)}</tr></thead>
        <tbody>{candidate_html_rows}</tbody>
      </table>
    """
    return diagnostic_page("Lightspeed eCom C-Series - Mapping audit", body)


def ecom_api_sync_status() -> dict[str, Any]:
    ensure_ecom_api_tables()
    with connect() as conn:
        product_count = int(conn.execute("SELECT COUNT(*) FROM ecom_api_products").fetchone()[0])
        enriched_count = int(conn.execute("SELECT COUNT(*) FROM ecom_api_products WHERE COALESCE(enriched_at, '') != ''").fetchone()[0])
        pending_enrichment_count = int(conn.execute("SELECT COUNT(*) FROM ecom_api_products WHERE COALESCE(enriched_at, '') = ''").fetchone()[0])
        latest = conn.execute(
            """
            SELECT *
            FROM ecom_api_sync_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT Internal_ID, Internal_Variant_ID, Brand, Product_Title, SKU, UPC, Visible, synced_at
            FROM ecom_api_products
            ORDER BY synced_at DESC, Brand, Product_Title
            LIMIT 25
            """
        ).fetchall()
    return {
        "product_count": product_count,
        "enriched_count": enriched_count,
        "pending_enrichment_count": pending_enrichment_count,
        "latest": dict(latest) if latest else None,
        "rows": [dict(row) for row in rows],
    }


@app.get("/catalogue/ecom-api-sync")
def catalogue_ecom_api_sync_page(x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User")):
    require_admin(x_remote_user)
    status = ecom_api_sync_status()
    latest = status["latest"] or {}
    latest_html = (
        f"""
        <table>
          <tbody>
            <tr><th>Statut</th><td>{html.escape(clean(latest.get("status")))}</td></tr>
            <tr><th>Début</th><td>{html.escape(clean(latest.get("started_at")))}</td></tr>
            <tr><th>Fin</th><td>{html.escape(clean(latest.get("finished_at")))}</td></tr>
            <tr><th>Offset départ</th><td>{html.escape(clean(latest.get("start_offset")))}</td></tr>
            <tr><th>Prochain offset</th><td>{html.escape(clean(latest.get("next_offset")))}</td></tr>
            <tr><th>Produits lus</th><td>{html.escape(clean(latest.get("products_seen")))}</td></tr>
            <tr><th>Variantes sauvegardées</th><td>{html.escape(clean(latest.get("variants_saved")))}</td></tr>
            <tr><th>Message</th><td>{html.escape(clean(latest.get("message")))}</td></tr>
          </tbody>
        </table>
        """
        if latest
        else '<p class="muted">Aucune synchronisation eCom API lancée.</p>'
    )
    rows_html = "".join(
        f"""
        <tr>
          <td>{html.escape(clean(row["Internal_ID"]))}</td>
          <td>{html.escape(clean(row["Internal_Variant_ID"]))}</td>
          <td>{html.escape(clean(row["Brand"]))}</td>
          <td>{html.escape(clean(row["Product_Title"]))}</td>
          <td>{html.escape(clean(row["SKU"]))}</td>
          <td>{html.escape(clean(row["UPC"]))}</td>
          <td>{html.escape(clean(row["Visible"]))}</td>
          <td>{html.escape(clean(row["synced_at"]))}</td>
        </tr>
        """
        for row in status["rows"]
    ) or '<tr><td colspan="8" class="muted">Aucune ligne eCom API sauvegardée.</td></tr>'
    body = f"""
      <p class="muted">Synchronisation manuelle vers une table séparée. Ne modifie pas l'audit CSV actuel.</p>
      <p><a href="/catalogue/ecom-recent">Voir les produits récents eCom</a></p>
      <p><strong>Lignes eCom API sauvegardées :</strong> {status["product_count"]}</p>
      <p><strong>Enrichies :</strong> {status["enriched_count"]} | <strong>À enrichir :</strong> {status["pending_enrichment_count"]}</p>
      <form action="/catalogue/ecom-api-sync" method="post">
        <label>Nombre de produits à lire :
          <input name="limit" type="number" min="1" max="200" value="100" />
        </label>
        <button type="submit">Sync rapide eCom API</button>
      </form>
      <form action="/catalogue/ecom-api-enrich" method="post" style="margin-top: 12px;">
        <label>Produits à enrichir :
          <input name="limit" type="number" min="1" max="25" value="10" />
        </label>
        <button type="submit">Enrichir données manquantes</button>
      </form>
      <h2>Dernière synchronisation</h2>
      {latest_html}
      <h2>Dernières lignes sauvegardées</h2>
      <table>
        <thead>
          <tr><th>Internal_ID</th><th>Internal_Variant_ID</th><th>Brand</th><th>Produit</th><th>SKU</th><th>UPC</th><th>Visible</th><th>Sync</th></tr>
        </thead>
        <tbody>{rows_html}</tbody>
      </table>
    """
    return diagnostic_page("Lightspeed eCom API - Sync séparée", body)


@app.post("/catalogue/ecom-api-sync")
async def catalogue_ecom_api_sync_submit(
    request: Request,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    body_bytes = await request.body()
    form = parse_qs(body_bytes.decode("utf-8", errors="replace"))
    limit = int(clean((form.get("limit") or ["100"])[0]) or 100)
    try:
        result = sync_ecom_api_products(limit)
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom API - Sync séparée", api_error_html(exc))
    except Exception as exc:
        return diagnostic_page(
            "Lightspeed eCom API - Sync séparée",
            f'<div class="error">{html.escape(mask_sensitive_text_full(str(exc))[:2000])}</div>',
        )
    body = f"""
      <p><strong>{html.escape(result["message"])}</strong></p>
      <p><a href="/catalogue/ecom-api-sync">Retour au statut eCom API</a></p>
      <p><a href="/catalogue/ecom-map-test">Voir le mapping</a></p>
    """
    return diagnostic_page("Lightspeed eCom API - Sync terminée", body)


@app.post("/catalogue/ecom-api-enrich")
async def catalogue_ecom_api_enrich_submit(
    request: Request,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    body_bytes = await request.body()
    form = parse_qs(body_bytes.decode("utf-8", errors="replace"))
    limit = int(clean((form.get("limit") or ["10"])[0]) or 10)
    try:
        result = enrich_ecom_api_products(limit)
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom API - Enrichissement", api_error_html(exc))
    except Exception as exc:
        return diagnostic_page(
            "Lightspeed eCom API - Enrichissement",
            f'<div class="error">{html.escape(mask_sensitive_text_full(str(exc))[:2000])}</div>',
        )
    body = f"""
      <p><strong>{html.escape(result["message"])}</strong></p>
      <p><a href="/catalogue/ecom-api-sync">Retour au statut eCom API</a></p>
      <p><a href="/catalogue/ecom-map-test">Voir le mapping</a></p>
    """
    return diagnostic_page("Lightspeed eCom API - Enrichissement terminé", body)


@app.get("/catalogue/ecom-recent")
def catalogue_ecom_recent_page(
    hours: int = 24,
    scan_limit: int = 100,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    try:
        url, products = fetch_recent_ecom_products(hours, scan_limit)
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom API - Produits récents", api_error_html(exc))
    except Exception as exc:
        return diagnostic_page(
            "Lightspeed eCom API - Produits récents",
            f'<div class="error">{html.escape(mask_sensitive_text_full(str(exc))[:2000])}</div>',
        )
    rows = "".join(
        f"""
        <tr>
          <td>{html.escape(clean(product.get("id")))}</td>
          <td>{html.escape(clean(product.get("title")))}</td>
          <td>{html.escape(clean(product.get("visibility") or product.get("isVisible")))}</td>
          <td>{html.escape(clean(product.get("createdAt")))}</td>
          <td>{html.escape(clean(product.get("updatedAt")))}</td>
          <td>{html.escape(clean(product.get("url")))}</td>
        </tr>
        """
        for product in products
    ) or '<tr><td colspan="6" class="muted">Aucun produit récent trouvé dans le lot scanné.</td></tr>'
    body = f"""
      <p class="muted">Diagnostic léger. Utilise seulement la liste produits eCom, sans enrichissement lourd.</p>
      <p class="muted">Endpoint : {html.escape(url)}</p>
      <form action="/catalogue/ecom-recent" method="get">
        <label>Heures :
          <input name="hours" type="number" min="1" max="720" value="{hours}" />
        </label>
        <label>Produits à scanner :
          <input name="scan_limit" type="number" min="1" max="250" value="{scan_limit}" />
        </label>
        <button type="submit">Voir les récents</button>
      </form>
      <form action="/catalogue/ecom-recent/save" method="post" style="margin-top: 12px;">
        <input type="hidden" name="hours" value="{hours}" />
        <input type="hidden" name="scan_limit" value="{scan_limit}" />
        <button type="submit">Sauvegarder ces récents</button>
      </form>
      <h2>Produits récents trouvés : {len(products)}</h2>
      <table>
        <thead><tr><th>ID</th><th>Titre</th><th>Visibilité</th><th>Créé</th><th>Modifié</th><th>URL</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    """
    return diagnostic_page("Lightspeed eCom API - Produits récents", body)


@app.post("/catalogue/ecom-recent/save")
async def catalogue_ecom_recent_save(
    request: Request,
    x_remote_user: Optional[str] = Header(default=None, alias="X-Remote-User"),
):
    require_admin(x_remote_user)
    body_bytes = await request.body()
    form = parse_qs(body_bytes.decode("utf-8", errors="replace"))
    hours = int(clean((form.get("hours") or ["24"])[0]) or 24)
    scan_limit = int(clean((form.get("scan_limit") or ["100"])[0]) or 100)
    try:
        result = save_recent_ecom_products(hours, scan_limit)
    except HTTPException as exc:
        return diagnostic_page("Lightspeed eCom API - Produits récents", api_error_html(exc))
    except Exception as exc:
        return diagnostic_page(
            "Lightspeed eCom API - Produits récents",
            f'<div class="error">{html.escape(mask_sensitive_text_full(str(exc))[:2000])}</div>',
        )
    body = f"""
      <p><strong>{html.escape(result["message"])}</strong></p>
      <p><a href="/catalogue/ecom-recent?hours={hours}&scan_limit={scan_limit}">Retour aux produits récents</a></p>
      <p><a href="/catalogue/ecom-api-sync">Voir la table eCom API</a></p>
    """
    return diagnostic_page("Lightspeed eCom API - Produits récents sauvegardés", body)


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
            "conformes": sum(1 for row in active_products if effective_priority(row, approvals) == "Conforme"),
            "action_required": sum(1 for row in active_products if effective_priority(row, approvals) == "Action requise"),
            "critical": sum(1 for row in active_products if effective_priority(row, approvals) == "Critique"),
            "approved_errors": sum(summarize_product(row, approvals)["Erreurs approuvées"] for row in active_products),
            "ecom_products": len(products),
            "ignored_products": ignored_count,
        },
        "brands": all_brands,
        "processed_brands": sorted(processed_brands),
        "priorities": priorities,
        "correction_types": correction_types,
        "brand_summary": adjusted_brand_summary(active_products, approvals),
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
    gpt_variant_ids, gpt_internal_ids = load_gpt_batch_product_keys()
    filtered = filter_products(
        products,
        approvals,
        processed_brands,
        excluded_variant_ids=gpt_variant_ids,
        excluded_internal_ids=gpt_internal_ids,
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


@app.post("/api/approvals/bulk")
def api_approve_all_selected(payload: BulkApprovalPayload):
    variant_ids = [clean(item) for item in payload.variant_ids if clean(item)]
    if not variant_ids:
        return {"ok": True, "approved": 0, "products": 0}

    products_by_id = {product_id(row): row for row in load_products()}
    approvals = load_approvals()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    approved = 0
    touched_products = 0
    with connect() as conn:
        for variant_id in variant_ids:
            row = products_by_id.get(variant_id)
            if not row:
                continue
            product_approved = 0
            for item in row_errors(row):
                error = item["error"]
                if (variant_id, error) in approvals:
                    continue
                conn.execute(
                    """
                    INSERT OR REPLACE INTO approvals
                    (Internal_Variant_ID, Type, Erreur, Date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (variant_id, item["type"], error, now),
                )
                approvals.add((variant_id, error))
                approved += 1
                product_approved += 1
            if product_approved:
                touched_products += 1
    return {"ok": True, "approved": approved, "products": touched_products}


@app.post("/api/ignored/bulk")
def api_ignore_selected_products(payload: BulkApprovalPayload):
    variant_ids = [clean(item) for item in payload.variant_ids if clean(item)]
    if not variant_ids:
        return {"ok": True, "ignored": 0}

    products_by_id = {product_id(row): row for row in load_products()}
    ignore_error = "Produit invisible - vérifier si volontaire"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ignored = 0
    with connect() as conn:
        for variant_id in variant_ids:
            if variant_id not in products_by_id:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO approvals
                (Internal_Variant_ID, Type, Erreur, Date)
                VALUES (?, ?, ?, ?)
                """,
                (variant_id, "Catalogue", ignore_error, now),
            )
            ignored += 1
    return {"ok": True, "ignored": ignored}


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
