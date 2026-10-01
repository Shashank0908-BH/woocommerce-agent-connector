"""Against the real WooCommerce store from dev/setup.sh (WooCommerce 11, fictional data).

Run: ./dev/setup.sh && uv run pytest -m live
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from mcp import Client

from woo_connector import server
from woo_connector.auth import Credentials, oauth1_params
from woo_connector.client import WooClient, WooError
from woo_connector.connect import CallbackListener, authorize_url
from woo_connector.tools import WooTools

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "dev"
pytestmark = pytest.mark.live


def _env() -> dict[str, str]:
    path = DEV / ".env"
    if not path.exists():
        pytest.skip("dev store not set up; run ./dev/setup.sh")
    env = dict(line.split("=", 1) for line in path.read_text().split() if "=" in line)
    try:
        httpx.get(env["WOO_BASE_URL"] + "/wp-json/", timeout=3)
    except httpx.HTTPError:
        pytest.skip("dev store not running")
    return env


ENV = _env() if (DEV / ".env").exists() else {}
BASE = ENV.get("WOO_BASE_URL", "http://localhost:8088")
READ = Credentials(ENV["WOO_CONSUMER_KEY"], ENV["WOO_CONSUMER_SECRET"]) if ENV else None
RW = Credentials(ENV["WOO_RW_CONSUMER_KEY"], ENV["WOO_RW_CONSUMER_SECRET"]) if ENV else None


def sql(statement: str) -> str:
    return subprocess.run(
        ["docker", "compose", "-f", str(DEV / "docker-compose.yml"), "exec", "-T", "db",
         "mariadb", "-uwordpress", "-pwordpress", "wordpress", "-N", "-e", statement],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def set_host_rate_limit(rule: str | None) -> None:
    sql("DELETE FROM wp_options WHERE option_name='connector_dev_rate_limit'")
    if rule:
        sql(f"INSERT INTO wp_options (option_name, option_value, autoload) VALUES ('connector_dev_rate_limit', '{rule}', 'off')")


@pytest.fixture
def host_rate_limit():
    yield set_host_rate_limit
    set_host_rate_limit(None)


def read_client(**kw) -> WooClient:
    return WooClient(BASE, READ, **kw)


# --- the MCP surface, end to end --------------------------------------------------

async def test_every_tool_over_mcp():
    server.set_tools(WooTools(read_client(), pii="masked"))
    try:
        async with Client(server.mcp) as mcp_client:
            listed = await mcp_client.list_tools()
            names = {t.name for t in listed.tools}
            assert names == {"store_overview", "list_orders", "get_order", "search_orders", "list_products",
                             "get_product", "search_products", "inventory_report"}
            assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in listed.tools)

            async def call(name, **args):
                res = await mcp_client.call_tool(name, args)
                assert not res.is_error, res.content
                return res.structured_content

            overview = await call("store_overview")
            assert overview["order_count"] == 240 and overview["currency"] == "INR" and overview["auth_mode"] == "oauth1"

            orders = await call("list_orders", status=["processing", "on-hold"], page_size=5)
            assert len(orders["orders"]) == 5 and {o["status"] for o in orders["orders"]} <= {"processing", "on-hold"}
            order = await call("get_order", order_id=orders["orders"][0]["id"], include_notes=True)
            assert order["items"] and "@example.test" in order["customer"]["email"] and "***" in order["customer"]["email"]

            found = await call("search_orders", query="Meera Pillai", limit=50)
            assert found["orders"] and all(o["customer"]["name"] == "Meera Pillai" for o in found["orders"])

            products = await call("list_products", page_size=100)
            assert products["total"] == 21
            mug = await call("get_product", sku="MUG-KULHAD")
            assert {v["sku"] for v in mug["variations"]} == {"MUG-KULHAD-SMALL", "MUG-KULHAD-LARGE"}
            by_sku = await call("search_products", query="TEA-005")
            assert by_sku["products"][0]["sku"] == "TEA-005"

            inv = await call("inventory_report")
            assert inv["products_scanned"] == 22 and inv["out_of_stock"] >= 3
            assert any(i["sku"] == "MUG-KULHAD-SMALL" and i["level"] == "low" for i in inv["items"])

            missing = await mcp_client.call_tool("get_order", {"order_id": 999999})
            assert missing.is_error
            text = missing.content[0].text  # "Error executing tool get_order: {...}"
            assert json.loads(text[text.index("{"):])["error"] == "not_found"

            bad = await mcp_client.call_tool("list_orders", {"status": ["shipped"]})
            assert bad.is_error  # rejected by the schema before reaching the store
    finally:
        await server.get_tools().client.aclose()
        server.set_tools(None)


# --- auth --------------------------------------------------------------------------

async def test_basic_auth_path_when_the_store_sees_https():
    # The WordPress image trusts X-Forwarded-Proto, the same as a store behind a TLS proxy.
    async with WooClient(BASE, READ, auth_mode="basic", allow_insecure_basic=True,
                         extra_headers={"X-Forwarded-Proto": "https"}) as c:
        page = await c.get_page("orders", {"per_page": 1})
    assert page.total == 240


@pytest.mark.parametrize("creds", [
    Credentials("ck_" + "0" * 40, "cs_" + "0" * 40),     # unknown key
    Credentials(ENV.get("WOO_CONSUMER_KEY", "ck_x"), "cs_" + "1" * 40),  # right key, wrong secret
])
async def test_bad_keys_fail_fast_as_auth_failed(creds):
    async with WooClient(BASE, creds) as c:
        start = time.monotonic()
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert err.value.code == "auth_failed" and c.stats["requests"] == 1 and time.monotonic() - start < 5


async def test_store_rejects_a_replayed_signature():
    url = f"{BASE}/wp-json/wc/v3/orders"
    signed = oauth1_params("GET", url, {"per_page": "1"}, READ)
    async with httpx.AsyncClient() as http:
        first = await http.get(url, params=signed)
        replay = await http.get(url, params=signed)
    assert first.status_code == 200 and replay.status_code == 401 and "nonce" in replay.text.lower()


async def test_revoked_key_reports_auth_failed():
    c = read_client()
    tools = WooTools(c)
    assert (await tools.store_overview())["order_count"] == 240
    truncated = READ.consumer_key[-7:]
    saved = sql(f"SELECT consumer_secret FROM wp_woocommerce_api_keys WHERE truncated_key='{truncated}'")
    sql(f"UPDATE wp_woocommerce_api_keys SET consumer_secret='cs_revoked' WHERE truncated_key='{truncated}'")
    try:
        with pytest.raises(WooError) as err:
            await tools.store_overview()
        assert err.value.code == "auth_failed" and "reconnect" in str(err.value)
    finally:
        sql(f"UPDATE wp_woocommerce_api_keys SET consumer_secret='{saved}' WHERE truncated_key='{truncated}'")
        await c.aclose()


async def test_connector_only_ever_sends_get_even_with_a_read_write_key():
    seen: list[str] = []

    class Recorder(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request):
            seen.append(request.method)
            return await super().handle_async_request(request)

    async with WooClient(BASE, RW, transport=Recorder()) as c:
        t = WooTools(c)
        await t.store_overview()
        orders = await t.list_orders(page_size=3)
        await t.get_order(orders["orders"][0]["id"], include_notes=True)
        await t.search_orders("Kabir")
        await t.get_product(sku="MUG-KULHAD")
        await t.inventory_report(only_low_stock=False)
    assert seen and set(seen) == {"GET"}


# --- pagination ----------------------------------------------------------------------

async def test_walking_every_page_sees_each_order_once_while_new_orders_arrive():
    async with read_client() as c, httpx.AsyncClient(auth=(RW.consumer_key, RW.consumer_secret),
                                                     headers={"X-Forwarded-Proto": "https"}) as writer:
        t = WooTools(c)
        seen: list[int] = []
        created: list[int] = []
        cursor = None
        while True:
            page = await t.list_orders(page_size=50, cursor=cursor)
            seen += [o["id"] for o in page["orders"]]
            # A customer checks out while the agent is paging. Without the pinned cursor
            # this shifts every later page by one and the agent sees a duplicate.
            resp = await writer.post(f"{BASE}/wp-json/wc/v3/orders", json={
                "status": "processing", "line_items": [{"product_id": 10, "quantity": 1}]})
            assert resp.status_code == 201, resp.text
            created.append(resp.json()["id"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        try:
            assert len(seen) == len(set(seen)) == 240
            assert not set(created) & set(seen)
        finally:
            for oid in created:
                await writer.delete(f"{BASE}/wp-json/wc/v3/orders/{oid}", params={"force": "true"})


# --- rate limits -----------------------------------------------------------------------

async def test_agent_burst_into_a_rate_limited_host_all_succeed(host_rate_limit):
    host_rate_limit("10/2")  # 5 req/s per key, like a cautious host
    async with read_client(rate=20, burst=20, max_concurrency=8) as c:  # client deliberately too eager
        t = WooTools(c)
        start = time.monotonic()
        results = await asyncio.gather(*(t.list_orders(page_size=5) for _ in range(40)))
        elapsed = time.monotonic() - start
    assert all(len(r["orders"]) == 5 for r in results)
    assert c.stats["rate_limited"] > 0, "host limit never triggered; test is not testing anything"
    # 10 per 2s fixed window: 40 requests need at least 4 windows, so 3+ window lengths
    # (a fixed window allows 20 across one boundary). A hammering client would take far longer.
    assert 3 < elapsed < 30, elapsed


async def test_client_tuned_under_the_host_limit_never_sees_a_429(host_rate_limit):
    host_rate_limit("10/2")
    await asyncio.sleep(2.1)  # let the previous test's window expire
    # 4/s with a burst of 1 is at most 9 per 2s window, under the host's 10.
    async with read_client(rate=4, burst=1, max_concurrency=4) as c:
        t = WooTools(c)
        await asyncio.gather(*(t.list_orders(page_size=5) for _ in range(20)))
    assert c.stats["rate_limited"] == 0


# --- the approve-access flow -----------------------------------------------------------

def _admin_password() -> str:
    return (DEV / ".admin").read_text().split("=", 1)[1].strip()


@pytest.mark.parametrize("approve", [True, False])
def test_connect_flow_through_the_real_approve_screen(approve):
    from playwright.sync_api import sync_playwright

    before = int(sql("SELECT COUNT(*) FROM wp_woocommerce_api_keys"))
    with CallbackListener(host="127.0.0.1", expected_scope="read") as listener:
        callback = f"https://host.docker.internal:{listener.port}{listener.path}"
        url = authorize_url(BASE, app_name="Agent Studio test", scope="read", state=listener.state,
                            return_url=f"https://localhost:{listener.port}{listener.path}/return", callback_url=callback)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(ignore_https_errors=True)
            page.goto(url)
            page.fill("#username", "admin")
            page.fill("#password", _admin_password())
            page.click("button.wc-auth-login-button")
            page.wait_for_selector("text=Agent Studio test")
            assert "View" in page.content()  # read scope: the screen lists view permissions only
            page.click("a.wc-auth-approve" if approve else "a.wc-auth-deny")
            page.wait_for_load_state()
            browser.close()
        result = listener.wait(10)
    if approve:
        assert result is not None and result.permissions == "read"
        async def check():
            async with WooClient(BASE, result.creds) as c:
                return await WooTools(c).store_overview()
        assert asyncio.run(check())["order_count"] == 240
        assert int(sql("SELECT COUNT(*) FROM wp_woocommerce_api_keys")) == before + 1
        sql(f"DELETE FROM wp_woocommerce_api_keys WHERE truncated_key='{result.creds.consumer_key[-7:]}'")
    else:
        assert result is None and listener.denied
        assert int(sql("SELECT COUNT(*) FROM wp_woocommerce_api_keys")) == before


# --- real transports ----------------------------------------------------------------------

def _server_env(**extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WOO_")}
    env.update(WOO_BASE_URL=BASE, WOO_CONSUMER_KEY=READ.consumer_key, WOO_CONSUMER_SECRET=READ.consumer_secret, **extra)
    return env


async def test_stdio_subprocess_like_an_agent_host_would_launch_it():
    import sys

    from mcp import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "woo_connector.cli", "serve"],
                                   env=_server_env(WOO_PII_MODE="none"))
    async with Client(params) as c:
        res = await c.call_tool("search_orders", {"query": "Zoya", "limit": 3})
    assert not res.is_error and res.structured_content["orders"]
    assert all(o["customer"] is None for o in res.structured_content["orders"])  # PII mode none honoured


def test_http_mode_refuses_requests_without_the_token():
    import socket
    import sys

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    token = "t" * 32
    proc = subprocess.Popen([sys.executable, "-m", "woo_connector.cli", "serve", "--http", "--port", str(port)],
                            env=_server_env(WOO_MCP_TOKEN=token), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
        headers = {"Accept": "application/json, text/event-stream"}
        for _ in range(50):
            try:
                httpx.get(url, timeout=0.5)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        assert httpx.post(url, json=init, headers=headers).status_code == 401
        assert httpx.post(url, json=init, headers={**headers, "Authorization": "Bearer wrong"}).status_code == 401
        ok = httpx.post(url, json=init, headers={**headers, "Authorization": f"Bearer {token}"})
        assert ok.status_code == 200, ok.text
    finally:
        proc.terminate()
        proc.wait(5)


def test_http_mode_will_not_start_without_a_token():
    import sys

    out = subprocess.run([sys.executable, "-m", "woo_connector.cli", "serve", "--http"], env=_server_env(),
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 1 and "WOO_MCP_TOKEN" in out.stderr
