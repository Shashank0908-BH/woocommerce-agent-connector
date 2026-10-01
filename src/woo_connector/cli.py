"""woo-connector connect | check | serve | tools-spec"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import sys
import webbrowser

from .auth import AuthConfigError, normalize_store_url, save_credentials


def cmd_connect(args: argparse.Namespace) -> int:
    from .client import WooClient
    from .connect import CallbackListener, authorize_url
    from .tools import WooTools

    store = normalize_store_url(args.store_url)
    host, _, port = args.listen.rpartition(":")
    with CallbackListener(host or "127.0.0.1", int(port or 0), expected_scope=args.scope) as listener:
        base = args.public_callback_url or f"https://{args.callback_host}:{listener.port}"
        callback = base.rstrip("/") + listener.path
        url = authorize_url(store, app_name=args.app_name, scope=args.scope, state=listener.state,
                            return_url=callback + "/return", callback_url=callback)
        print(f"Open this link, log in to the store and approve access:\n\n  {url}\n", file=sys.stderr)
        if not args.no_browser:
            webbrowser.open(url)
        result = listener.wait(args.timeout)
        if listener.denied:
            print("The store owner denied access.", file=sys.stderr)
            return 2
        if result is None:
            extra = f" Rejected callbacks: {listener.rejections}" if listener.rejections else ""
            print(f"No keys arrived within {args.timeout:.0f}s.{extra}", file=sys.stderr)
            return 3
    path = save_credentials(store, result.creds, result.permissions)
    print(f"Saved {result.permissions} key ...{result.creds.consumer_key[-4:]} for {store} to {path}", file=sys.stderr)

    async def verify() -> dict:
        async with WooClient(store, result.creds) as client:
            return await WooTools(client).store_overview()

    print(json.dumps(asyncio.run(verify()), indent=2))
    return 0


def cmd_check(_: argparse.Namespace) -> int:
    from .server import get_tools

    async def run() -> dict:
        tools = get_tools()
        try:
            return await tools.store_overview()
        finally:
            await tools.client.aclose()

    print(json.dumps(asyncio.run(run()), indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import get_tools, mcp

    get_tools()  # fail fast on bad config, before a client connects
    if not args.http:
        mcp.run("stdio")
        return 0
    token = os.environ.get("WOO_MCP_TOKEN", "")
    if len(token) < 24:
        raise AuthConfigError("HTTP mode exposes store data on a port; set WOO_MCP_TOKEN to a random string of 24+ chars")
    import uvicorn

    app = mcp.streamable_http_app(host=args.host)
    uvicorn.run(BearerAuth(app, token), host=args.host, port=args.port, log_level="warning")
    return 0


class BearerAuth:
    """Minimal ASGI guard: every HTTP request needs Authorization: Bearer <WOO_MCP_TOKEN>."""

    def __init__(self, app, token: str):
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(given, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def cmd_tools_spec(args: argparse.Namespace) -> int:
    from .server import mcp

    tools = asyncio.run(mcp.list_tools())
    spec = {
        "server": "woocommerce",
        "transport": ["stdio", "streamable-http"],
        "tools": [t.model_dump(mode="json", exclude_none=True, by_alias=True) for t in tools],
    }
    text = json.dumps(spec, indent=2) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="woo-connector", description="Read-only WooCommerce connector for agents (MCP).")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("connect", help="get API keys through the store's approve-access screen")
    c.add_argument("store_url")
    c.add_argument("--app-name", default="Agent Studio")
    c.add_argument("--scope", default="read", choices=["read", "read_write"])
    c.add_argument("--listen", default="127.0.0.1:0", help="host:port for the local https callback listener")
    c.add_argument("--callback-host", default="localhost",
                   help="host the store should post keys to (host.docker.internal for the dev store)")
    c.add_argument("--public-callback-url", help="public https URL that forwards to the listener, e.g. a tunnel")
    c.add_argument("--timeout", type=float, default=300)
    c.add_argument("--no-browser", action="store_true")
    c.set_defaults(func=cmd_connect)

    sub.add_parser("check", help="call the store with the configured keys").set_defaults(func=cmd_check)

    s = sub.add_parser("serve", help="run the MCP server (stdio by default)")
    s.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio; needs WOO_MCP_TOKEN")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    t = sub.add_parser("tools-spec", help="print the MCP tool specification as JSON")
    t.add_argument("--out")
    t.set_defaults(func=cmd_tools_spec)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs full request URLs at INFO; with OAuth 1.0a those carry the consumer key.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        return args.func(args)
    except AuthConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
