from __future__ import annotations

import hashlib
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lightspeed_client import LightspeedClient, LightspeedConfig


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def printable_profile(value: str) -> str:
    has_space = any(char.isspace() for char in value)
    ascii_only = all(ord(char) < 128 for char in value)
    return (
        f"len={len(value)}, sha256_12={fingerprint(value)}, "
        f"ascii={'oui' if ascii_only else 'non'}, "
        f"espaces={'oui' if has_space else 'non'}"
    )


def try_url(client: LightspeedClient, base_url: str, endpoint: str) -> tuple[int, str]:
    endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    url = f"{base_url.rstrip('/')}{endpoint}?{urlencode({'limit': 1, 'offset': 0})}"
    request = Request(url, headers=client._headers(), method="GET")

    try:
        with urlopen(request, timeout=30) as response:
            response.read()
            return response.status, "OK"
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:220]
        return exc.code, body.replace("\n", " ")
    except Exception as exc:
        return 0, str(exc)


def main() -> None:
    config = LightspeedConfig.from_env()
    client = LightspeedClient(config)
    reversed_client = LightspeedClient(
        LightspeedConfig(
            base_url=config.base_url,
            products_endpoint=config.products_endpoint,
            api_key=config.api_secret,
            api_secret=config.api_key,
            bearer_token="",
            page_size=config.page_size,
        )
    )

    print("Configuration chargée:")
    print(f"- URL API: {config.base_url}")
    print(f"- Endpoint: {config.products_endpoint}")
    print(f"- Authentification: {'Bearer token' if config.bearer_token else 'key/secret'}")
    print(f"- Clé API présente: {'oui' if config.api_key else 'non'}")
    print(f"- Secret présent: {'oui' if config.api_secret else 'non'}")
    print(f"- Profil clé API: {printable_profile(config.api_key)}")
    print(f"- Profil secret: {printable_profile(config.api_secret)}")
    print(f"- Empreinte paire: {fingerprint(config.api_key + ':' + config.api_secret)}")
    print("")

    candidates = [
        (config.base_url, config.products_endpoint),
        (config.base_url, "/shop.json"),
        (config.base_url, "/en/shop.json"),
        (config.base_url, "/orders.json"),
        (config.base_url, "/en/orders.json"),
        ("https://api.webshopapp.com/en", "/products.json"),
        ("https://api.webshopapp.com/en", "/shop.json"),
        ("https://api.shoplightspeed.com/en", "/products.json"),
        ("https://api.webshopapp.com/fr", "/products.json"),
        ("https://api.shoplightspeed.com/fr", "/products.json"),
    ]

    seen = set()
    print("Test des variantes eCom:")
    for base_url, endpoint in candidates:
        key = (base_url, endpoint)
        if key in seen:
            continue
        seen.add(key)

        status, message = try_url(client, base_url, endpoint)
        print(f"- {base_url}{endpoint}: HTTP {status} - {message}")

    print("")

    print("Test inversion clé/secret:")
    status, message = try_url(
        reversed_client,
        "https://api.webshopapp.com/en",
        "/products.json",
    )
    print(f"- secret/key inversés: HTTP {status} - {message}")
    print("")

    try:
        payload = client._get_json(config.products_endpoint, {"limit": 1, "offset": 0})
    except RuntimeError as exc:
        print(exc)
        print("")
        print("Conclusion probable:")
        print("- 401 Unauthorized: clé/secret refusés ou clé sans accès API eCom.")
        print("- 403 Forbidden: endpoint ou permissions refusés.")
        print("- Si tes clés viennent de Lightspeed Retail/R-Series, ce n'est pas la même API.")
        return
    except HTTPError as exc:
        print(f"HTTP {exc.code}: {exc.reason}")
        return

    print("Connexion Lightspeed réussie.")
    print(f"Type de réponse: {type(payload).__name__}")


if __name__ == "__main__":
    main()
