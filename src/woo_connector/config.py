"""Settings come from environment variables; keys can also come from the
credential file written by `woo-connector connect`."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .auth import AuthConfigError, Credentials, load_credentials, normalize_store_url


@dataclass(frozen=True)
class Settings:
    store_url: str
    creds: Credentials
    auth_mode: str = "auto"
    pii_mode: str = "masked"
    rate: float = 5.0
    burst: int = 10
    max_concurrency: int = 4
    allow_insecure_basic: bool = False


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def from_env() -> Settings:
    url = os.environ.get("WOO_BASE_URL", "").strip()
    if not url:
        raise AuthConfigError("set WOO_BASE_URL to the store address, e.g. https://shop.example.com")
    url = normalize_store_url(url)
    key, secret = os.environ.get("WOO_CONSUMER_KEY"), os.environ.get("WOO_CONSUMER_SECRET")
    if key and secret:
        creds = Credentials(key.strip(), secret.strip())
    else:
        stored = load_credentials(url)
        if not stored:
            raise AuthConfigError(f"no keys for {url}: run `woo-connector connect {url}` or set WOO_CONSUMER_KEY/SECRET")
        creds = stored[0]
    pii = os.environ.get("WOO_PII_MODE", "masked")
    if pii not in ("masked", "full", "none"):
        raise AuthConfigError("WOO_PII_MODE must be masked, full or none")
    try:
        return Settings(
            store_url=url,
            creds=creds,
            auth_mode=os.environ.get("WOO_AUTH_MODE", "auto"),
            pii_mode=pii,
            rate=float(os.environ.get("WOO_RATE", "5")),
            burst=int(os.environ.get("WOO_BURST", "10")),
            max_concurrency=int(os.environ.get("WOO_MAX_CONCURRENCY", "4")),
            allow_insecure_basic=_flag("WOO_ALLOW_INSECURE_BASIC"),
        )
    except ValueError as exc:
        raise AuthConfigError(f"bad numeric setting: {exc}") from None
