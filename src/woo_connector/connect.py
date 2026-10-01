"""Getting keys without the merchant copying secrets by hand.

WooCommerce has its own authorisation endpoint, /wc-auth/v1/authorize. It is
not OAuth 2.0 but it plays the same part:

1. The merchant opens the authorise URL, logs in to their store, sees the app
   name and the scope ("read"), and clicks Approve.
2. The store creates a key pair and POSTs it as JSON to our callback_url. The
   secret travels server to server; it never appears in a browser URL.
3. The browser is sent to return_url with success=1 or success=0.

`user_id` is echoed back untouched in both, so it doubles as the state value:
a random token per attempt, checked on arrival, which stops a forged callback
from planting someone else's keys. The store insists the callback is https.

In production the callback is an Agent Studio endpoint. For a laptop, this
module runs the listener itself with a throwaway self-signed certificate.
"""

from __future__ import annotations

import datetime as dt
import hmac
import ipaddress
import json
import secrets
import ssl
import tempfile
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .auth import AuthConfigError, Credentials

MAX_BODY = 16 * 1024


@dataclass
class ConnectResult:
    creds: Credentials
    permissions: str
    key_id: int | None


def authorize_url(store_url: str, *, app_name: str, scope: str, state: str, return_url: str, callback_url: str) -> str:
    if scope not in ("read", "write", "read_write"):
        raise ValueError("scope must be read, write or read_write")
    if not callback_url.startswith("https://"):
        raise ValueError("WooCommerce only delivers keys to an https callback_url")
    query = urlencode({
        "app_name": app_name, "scope": scope, "user_id": state, "return_url": return_url, "callback_url": callback_url,
    })
    return f"{store_url.rstrip('/')}/wc-auth/v1/authorize?{query}"


def self_signed_cert(directory: Path, hosts: list[str]) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "woo-connector local callback")])
    sans: list[x509.GeneralName] = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    key_path.chmod(0o600)
    return cert_path, key_path


class CallbackListener:
    """HTTPS listener for one connect attempt. Accepts exactly one valid callback."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, path: str = "/woo/callback",
                 expected_scope: str = "read", cert_hosts: list[str] | None = None):
        self.state = secrets.token_urlsafe(24)
        self.path = path
        self.expected_scope = expected_scope
        self.result: ConnectResult | None = None
        self.denied = False
        self.rejections: list[str] = []
        self._done = threading.Event()
        self._tmp = tempfile.TemporaryDirectory(prefix="woo-connect-")
        cert, key = self_signed_cert(Path(self._tmp.name), cert_hosts or ["localhost", "127.0.0.1", "host.docker.internal"])
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:  # keep secrets out of stderr
                pass

            def _reply(self, code: int, body: str, ctype: str = "application/json") -> None:
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802
                if urlsplit(self.path).path != listener.path:
                    return self._reply(404, '{"error":"not_found"}')
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY:
                    return self._reply(400, '{"error":"bad_length"}')
                try:
                    payload = json.loads(self.rfile.read(length))
                    reason = listener._accept(payload)
                except (json.JSONDecodeError, UnicodeDecodeError, TypeError, AttributeError):
                    reason = "malformed json"
                if reason:
                    listener.rejections.append(reason)
                    return self._reply(400, json.dumps({"error": "rejected", "reason": reason}))
                self._reply(200, '{"ok":true}')

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                if parts.path != listener.path + "/return":
                    return self._reply(404, '{"error":"not_found"}')
                q = parse_qs(parts.query)
                if not hmac.compare_digest(q.get("user_id", [""])[0], listener.state):
                    return self._reply(400, "Unknown connect attempt.", "text/plain")
                if q.get("success", ["0"])[0] != "1":
                    listener.denied = True
                    listener._done.set()
                    return self._reply(200, "Access was denied. You can close this tab.", "text/plain")
                self._reply(200, "Connected. You can close this tab.", "text/plain")

        self.server = ThreadingHTTPServer((host, port), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _accept(self, payload: dict) -> str | None:
        if self.result is not None:
            return "already connected"
        if not hmac.compare_digest(str(payload.get("user_id", "")), self.state):
            return "state mismatch"
        try:
            creds = Credentials(str(payload["consumer_key"]), str(payload["consumer_secret"]))
        except (KeyError, AuthConfigError) as exc:
            return f"bad keys: {exc}"
        perms = str(payload.get("key_permissions", ""))
        if perms != self.expected_scope:
            return f"asked for {self.expected_scope} keys, store sent {perms or 'none'}"
        key_id = payload.get("key_id")
        self.result = ConnectResult(creds, perms, int(key_id) if str(key_id).isdigit() else None)
        self._done.set()
        return None

    def start(self) -> "CallbackListener":
        self._thread.start()
        return self

    def wait(self, timeout: float) -> ConnectResult | None:
        self._done.wait(timeout)
        return self.result

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def __enter__(self) -> "CallbackListener":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()
