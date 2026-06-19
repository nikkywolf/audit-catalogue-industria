from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent


def load_dotenv() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key.startswith("LIGHTSPEED_"):
            os.environ[key] = value


@dataclass
class LightspeedConfig:
    base_url: str
    products_endpoint: str
    api_key: str
    api_secret: str = ""
    bearer_token: str = ""
    page_size: int = 100

    @classmethod
    def from_env(cls) -> "LightspeedConfig":
        load_dotenv()

        base_url = os.environ.get("LIGHTSPEED_API_BASE_URL", "").rstrip("/")
        api_key = os.environ.get("LIGHTSPEED_API_KEY", "")
        api_secret = os.environ.get("LIGHTSPEED_API_SECRET", "")
        bearer_token = os.environ.get("LIGHTSPEED_API_TOKEN", "")
        products_endpoint = os.environ.get("LIGHTSPEED_PRODUCTS_ENDPOINT", "/products.json")
        page_size = int(os.environ.get("LIGHTSPEED_PAGE_SIZE", "100"))

        missing = []
        if not base_url:
            missing.append("LIGHTSPEED_API_BASE_URL")
        if not api_key and not bearer_token:
            missing.append("LIGHTSPEED_API_KEY ou LIGHTSPEED_API_TOKEN")

        if missing:
            raise RuntimeError(
                "Configuration Lightspeed manquante: " + ", ".join(missing)
            )

        return cls(
            base_url=base_url,
            products_endpoint=products_endpoint,
            api_key=api_key,
            api_secret=api_secret,
            bearer_token=bearer_token,
            page_size=page_size,
        )


class LightspeedClient:
    def __init__(self, config: LightspeedConfig | None = None):
        self.config = config or LightspeedConfig.from_env()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "IndustriaCatalogueAudit/1.0",
        }

        if self.config.bearer_token:
            headers["Authorization"] = f"Bearer {self.config.bearer_token}"
            return headers

        credentials = f"{self.config.api_key}:{self.config.api_secret}".encode("utf-8")
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode("ascii")
        return headers

    def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        query = f"?{urlencode(params or {})}" if params else ""
        url = f"{self.config.base_url}{endpoint}{query}"

        request = Request(url, headers=self._headers(), method="GET")
        try:
            with urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            auth_mode = "Bearer token" if self.config.bearer_token else "Basic key/secret"
            raise RuntimeError(
                "Lightspeed refuse la requête.\n"
                f"HTTP {exc.code} {exc.reason}\n"
                f"URL appelée: {url}\n"
                f"Authentification: {auth_mode}\n"
                f"Réponse: {body}\n\n"
                "À vérifier: le domaine API, l'endpoint produits, le type de compte "
                "Lightspeed utilisé et les permissions de la clé API."
            ) from exc

    @staticmethod
    def _extract_products(payload: dict) -> list[dict]:
        for key in ("products", "Product", "Items", "Item", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                nested = value.get("product") or value.get("item") or value.get("data")
                if isinstance(nested, list):
                    return nested
                return [value]
        return []

    def iter_products(self):
        offset = 0

        while True:
            payload = self._get_json(
                self.config.products_endpoint,
                {
                    "limit": self.config.page_size,
                    "offset": offset,
                },
            )
            products = self._extract_products(payload)

            if not products:
                break

            for product in products:
                yield product

            if len(products) < self.config.page_size:
                break

            offset += self.config.page_size
