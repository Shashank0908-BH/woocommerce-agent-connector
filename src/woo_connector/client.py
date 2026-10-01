"""HTTP client for the WooCommerce REST API (wc/v3), read-only.

Three things sit between a tool call and the store:

1. A token bucket shared by every call in the process, so twenty tool calls an
   agent fires in parallel become a steady stream the host will accept.
2. A cooldown: when the store answers 429, the whole client pauses for the
   Retry-After the store asked for, not just the request that got it. Without
   this, the other nineteen calls walk into the same wall.
3. Retries with capped exponential backoff and full jitter, for 429, 502, 503,
   504, timeouts and dropped connections, inside a fixed time budget per call,
   so an agent gets an answer or a clear error, never a hang.

Only GET exists here. Every request is safe to retry because nothing writes.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .auth import Credentials, basic_header, choose_mode, oauth1_params

log = logging.getLogger("woo_connector")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
USER_AGENT = "woo-connector/0.1 (+agent-studio)"


class WooError(Exception):
    """An error an agent can act on. `code` is stable; `message` is for people."""

    def __init__(self, code: str, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": str(self), "status": self.status, "retryable": self.retryable}


@dataclass
class RateLimiter:
    """Token bucket plus a shared cooldown, safe across concurrent coroutines."""

    rate: float = 5.0  # tokens per second
    burst: int = 10
    _tokens: float = field(init=False)
    _updated: float = field(init=False)
    _cooldown_until: float = field(init=False, default=0.0)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._updated = time.monotonic()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._cooldown_until:
                    wait = self._cooldown_until - now
                else:
                    self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate)
                    self._updated = now
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    wait = (1 - self._tokens) / self.rate
            await asyncio.sleep(wait)

    def cool_down(self, seconds: float) -> None:
        """Called on a 429: nobody in this process sends until `seconds` pass."""
        until = time.monotonic() + seconds
        if until > self._cooldown_until:
            self._cooldown_until = until
            self._tokens = 0.0
            self._updated = until  # refill starts after the pause, not before it


@dataclass
class Page:
    items: list[dict[str, Any]]
    total: int | None
    total_pages: int | None


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(value)
        return max(0.0, parsed.timestamp() - time.time()) if parsed else None


class WooClient:
    def __init__(
        self,
        base_url: str,
        creds: Credentials,
        *,
        auth_mode: str = "auto",
        allow_insecure_basic: bool = False,
        rate: float = 5.0,
        burst: int = 10,
        max_concurrency: int = 4,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_cap: float = 20.0,
        call_budget: float = 60.0,
        timeout: float = 20.0,
        extra_headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.creds = creds
        self.auth_mode = choose_mode(self.base_url, auth_mode, allow_insecure_basic)
        self.limiter = RateLimiter(rate=rate, burst=burst)
        self._sem = asyncio.Semaphore(max_concurrency)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.call_budget = call_budget
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **(extra_headers or {})}
        if self.auth_mode == "basic":
            headers["Authorization"] = basic_header(creds)
        self._http = httpx.AsyncClient(timeout=timeout, headers=headers, transport=transport, follow_redirects=False)
        self.stats = {"requests": 0, "retries": 0, "rate_limited": 0}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "WooClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def _url(self, path: str) -> str:
        return f"{self.base_url}/wp-json/wc/v3/{path.lstrip('/')}"

    async def get(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        """One logical read. The budget covers everything: waiting for a token,
        waiting out a 429, every retry. Past it the agent gets `busy`, not a hang."""
        clean = {k: _param(v) for k, v in (params or {}).items() if v is not None}
        deadline = time.monotonic() + self.call_budget
        try:
            async with asyncio.timeout(self.call_budget):
                return await self._get(path, clean, deadline)
        except TimeoutError:
            raise WooError("busy", f"no answer within {self.call_budget:.0f}s: queued behind other calls or the store "
                           "is slow; retry shortly", retryable=True) from None

    async def _get(self, path: str, clean: dict[str, str], deadline: float) -> tuple[Any, httpx.Headers]:
        url = self._url(path)
        attempt = 0
        while True:
            await self.limiter.acquire()
            # OAuth nonces are single use, so sign each attempt afresh.
            query = oauth1_params("GET", url, clean, self.creds) if self.auth_mode == "oauth1" else clean
            self.stats["requests"] += 1
            resp: httpx.Response | None = None
            try:
                async with self._sem:
                    resp = await self._http.get(url, params=query)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last = WooError("store_unreachable", f"could not reach the store: {exc!r}", retryable=True)
            else:
                if 300 <= resp.status_code < 400:
                    # Not followed: a redirect would drop the auth header or break the signature.
                    raise WooError("redirected", f"store redirected to {resp.headers.get('location')}; "
                                   "set the store URL to that address", status=resp.status_code)
                if resp.status_code < 400:
                    try:
                        return _json(resp), resp.headers
                    except WooError as exc:
                        # JSON cut off mid-body is a proxy or a dying PHP worker; worth another go.
                        # An HTML page in place of JSON is a firewall; retrying will not change it.
                        if "json" not in resp.headers.get("content-type", ""):
                            raise
                        last = exc
                elif resp.status_code not in RETRYABLE_STATUS:
                    raise _error_from(resp)
                else:
                    last = _error_from(resp)

            attempt += 1
            retry_after = None
            if resp is not None and resp.status_code == 429:
                self.stats["rate_limited"] += 1
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                self.limiter.cool_down(retry_after if retry_after is not None else self.backoff_base * 2)
            delay = retry_after if retry_after is not None else random.uniform(
                0, min(self.backoff_cap, self.backoff_base * 2 ** attempt)
            )
            if attempt > self.max_retries or time.monotonic() + delay > deadline:
                raise last
            self.stats["retries"] += 1
            log.info("retry GET %s in %.2fs (attempt %d: %s)", path, delay, attempt, last.code)
            await asyncio.sleep(delay)

    async def get_page(self, path: str, params: dict[str, Any] | None = None) -> Page:
        data, headers = await self.get(path, params)
        if not isinstance(data, list):
            raise WooError("unexpected_response", f"expected a list from {path}, got {type(data).__name__}")
        return Page(data, _int(headers.get("X-WP-Total")), _int(headers.get("X-WP-TotalPages")))


def _param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError):
        # Security plugins and CDNs answer with HTML challenge pages; say so.
        snippet = resp.text[:120].replace("\n", " ")
        raise WooError("unexpected_response", f"store returned non-JSON ({resp.headers.get('content-type')}): {snippet}",
                       status=resp.status_code) from None


def _error_from(resp: httpx.Response) -> WooError:
    status = resp.status_code
    try:
        body = resp.json()
        wc_code, message = body.get("code", ""), body.get("message", "")
    except (json.JSONDecodeError, ValueError, AttributeError):
        wc_code, message = "", resp.text[:200]
    if status == 401:
        return WooError("auth_failed", f"store rejected the API key ({wc_code}): {message}. The key may be revoked; reconnect.",
                        status=status)
    if status == 403:
        return WooError("forbidden", f"the API key lacks permission for this ({wc_code}): {message}", status=status)
    if status == 404:
        if wc_code == "rest_no_route":
            return WooError("api_unavailable", "WooCommerce REST API not found; is WooCommerce active and are permalinks on?",
                            status=status)
        return WooError("not_found", message or "not found", status=status)
    if status == 400:
        return WooError("bad_request", message or "the store rejected the request parameters", status=status)
    if status == 429:
        return WooError("rate_limited", "store kept rate-limiting after retries; try again later", status=status, retryable=True)
    if status in RETRYABLE_STATUS:
        return WooError("store_error", f"store returned {status} after retries", status=status, retryable=True)
    return WooError("http_error", f"store returned {status}: {message}", status=status)
