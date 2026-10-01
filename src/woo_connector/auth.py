"""How requests to a WooCommerce store are authenticated, and where keys live.

WooCommerce REST keys are a consumer key and secret pair. The store accepts
them two ways, and which one is legal depends on the scheme:

* HTTPS: HTTP Basic, key as username and secret as password.
* HTTP: one-legged OAuth 1.0a. The secret never travels; each request carries
  an HMAC signature over the method, URL and every query parameter, plus a
  timestamp and a nonce the store remembers for 15 minutes.

Basic over plain HTTP is refused here even though it would be easy to send,
because it puts the secret on the wire in clear text.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


class AuthConfigError(Exception):
    """Credentials are missing, malformed, or unsafe to use as configured."""


@dataclass(frozen=True)
class Credentials:
    consumer_key: str
    consumer_secret: str

    def __post_init__(self) -> None:
        if not self.consumer_key.startswith("ck_") or not self.consumer_secret.startswith("cs_"):
            raise AuthConfigError("WooCommerce keys look like ck_... and cs_...; got something else")

    def __repr__(self) -> str:  # never print a secret by accident
        return f"Credentials(consumer_key='...{self.consumer_key[-4:]}', consumer_secret='***')"


def _enc(value: str) -> str:
    """RFC 3986 percent-encoding, the only encoding OAuth 1.0a accepts."""
    return quote(value, safe="~-._")


def oauth1_params(
    method: str,
    url: str,
    params: dict[str, str],
    creds: Credentials,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """Return params plus the oauth_* fields and signature for one request.

    Mirrors WC_REST_Authentication::check_oauth_signature: the base URL has no
    query string, parameters are sorted by key, and the HMAC key is the secret
    followed by '&' (no token secret in one-legged OAuth).
    """
    signed = {k: str(v) for k, v in params.items()}
    signed.update(
        oauth_consumer_key=creds.consumer_key,
        oauth_nonce=nonce or secrets.token_hex(16),
        oauth_signature_method="HMAC-SHA256",
        oauth_timestamp=str(timestamp if timestamp is not None else int(time.time())),
    )
    parts = urlsplit(url)
    base_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    normalized = "&".join(f"{_enc(k)}={_enc(v)}" for k, v in sorted(signed.items()))
    base_string = "&".join([method.upper(), _enc(base_url), _enc(normalized)])
    digest = hmac.new(f"{creds.consumer_secret}&".encode(), base_string.encode(), hashlib.sha256).digest()
    signed["oauth_signature"] = base64.b64encode(digest).decode()
    return signed


def basic_header(creds: Credentials) -> str:
    token = base64.b64encode(f"{creds.consumer_key}:{creds.consumer_secret}".encode()).decode()
    return f"Basic {token}"


def choose_mode(base_url: str, requested: str = "auto", allow_insecure_basic: bool = False) -> str:
    """auto picks basic for https and oauth1 for http."""
    scheme = urlsplit(base_url).scheme
    if scheme not in ("http", "https"):
        raise AuthConfigError(f"store URL must start with http:// or https://, got {base_url!r}")
    mode = requested if requested != "auto" else ("basic" if scheme == "https" else "oauth1")
    if mode not in ("basic", "oauth1"):
        raise AuthConfigError(f"auth mode must be auto, basic or oauth1, got {requested!r}")
    if mode == "basic" and scheme == "http" and not allow_insecure_basic:
        raise AuthConfigError("refusing to send the consumer secret over plain http; use oauth1 or https")
    return mode


# --- credential store -------------------------------------------------------
# One JSON file, mode 0600, keyed by store URL, so one machine can hold keys for
# several merchants. Environment variables always win over the file.

def default_store_path() -> Path:
    root = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(root) / "woo-connector" / "credentials.json"


def normalize_store_url(url: str) -> str:
    return url.strip().rstrip("/")


def save_credentials(store_url: str, creds: Credentials, permissions: str, path: Path | None = None) -> Path:
    path = path or default_store_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _read_store(path)
    data[normalize_store_url(store_url)] = {
        "consumer_key": creds.consumer_key,
        "consumer_secret": creds.consumer_secret,
        "permissions": permissions,
        "saved_at": int(time.time()),
    }
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    return path


def load_credentials(store_url: str, path: Path | None = None) -> tuple[Credentials, str] | None:
    path = path or default_store_path()
    entry = _read_store(path).get(normalize_store_url(store_url))
    if not entry:
        return None
    return Credentials(entry["consumer_key"], entry["consumer_secret"]), entry.get("permissions", "unknown")


def _read_store(path: Path) -> dict:
    if not path.exists():
        return {}
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise AuthConfigError(f"{path} is readable by other users (mode {oct(mode)}); run: chmod 600 {path}")
    return json.loads(path.read_text() or "{}")
