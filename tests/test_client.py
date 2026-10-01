"""Client behaviour against a scripted fake store (respx)."""

import asyncio
import time

import httpx
import pytest
import respx

from woo_connector.auth import Credentials
from woo_connector.client import RateLimiter, WooClient, WooError

BASE = "https://shop.test"
URL = f"{BASE}/wp-json/wc/v3/orders"
CREDS = Credentials("ck_test", "cs_test")


def client(**kw) -> WooClient:
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("rate", 1000)
    kw.setdefault("burst", 1000)
    return WooClient(BASE, CREDS, **kw)


@respx.mock
async def test_basic_auth_on_https_and_paging_headers():
    route = respx.get(URL).respond(200, json=[{"id": 1}], headers={"X-WP-Total": "41", "X-WP-TotalPages": "3"})
    async with client() as c:
        page = await c.get_page("orders", {"per_page": 20, "status": ["processing", "on-hold"], "x": None})
    req = route.calls.last.request
    assert req.headers["authorization"].startswith("Basic ")
    assert req.url.params["status"] == "processing,on-hold" and "x" not in req.url.params
    assert (page.total, page.total_pages) == (41, 3)


@respx.mock
async def test_oauth1_on_http_signs_every_attempt_with_a_new_nonce():
    url = "http://shop.test/wp-json/wc/v3/orders"
    route = respx.get(url).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=[])])
    async with WooClient("http://shop.test", CREDS, backoff_base=0.01) as c:
        await c.get("orders")
    nonces = [call.request.url.params["oauth_nonce"] for call in route.calls]
    assert len(nonces) == 2 and nonces[0] != nonces[1]
    assert "authorization" not in route.calls.last.request.headers


@respx.mock
async def test_retries_transient_errors_then_succeeds():
    route = respx.get(URL).mock(side_effect=[httpx.Response(502), httpx.Response(503), httpx.ConnectError("boom"),
                                             httpx.Response(200, json=[{"id": 7}])])
    async with client() as c:
        data, _ = await c.get("orders")
    assert data == [{"id": 7}] and route.call_count == 4 and c.stats["retries"] == 3


@respx.mock
async def test_gives_up_after_max_retries_with_retryable_error():
    respx.get(URL).respond(503)
    async with client(max_retries=3) as c:
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert err.value.code == "store_error" and err.value.retryable and c.stats["requests"] == 4


@respx.mock
async def test_429_honours_retry_after_and_pauses_everyone():
    calls = []

    def handler(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json=[])

    respx.get(URL).mock(side_effect=handler)
    async with client(max_concurrency=8) as c:
        start = time.monotonic()
        # First call trips the 429; the others start just after and must wait out the same pause.
        first = asyncio.create_task(c.get("orders"))
        await asyncio.sleep(0.05)
        await asyncio.gather(first, *(c.get("orders") for _ in range(5)))
    assert all(t - start >= 0.95 for t in calls[1:]), "a request went out during the cooldown"
    assert c.stats["rate_limited"] == 1


@respx.mock
async def test_http_date_retry_after_is_understood():
    future = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})  # in the past -> 0s
    respx.get(URL).mock(side_effect=[future, httpx.Response(200, json=[])])
    async with client() as c:
        await c.get("orders")


@pytest.mark.parametrize("status,body,code", [
    (401, {"code": "woocommerce_rest_authentication_error", "message": "Consumer key is invalid."}, "auth_failed"),
    (403, {"code": "woocommerce_rest_cannot_view", "message": "no"}, "forbidden"),
    (404, {"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}, "not_found"),
    (404, {"code": "rest_no_route", "message": "No route"}, "api_unavailable"),
    (400, {"code": "rest_invalid_param", "message": "Invalid parameter(s): after"}, "bad_request"),
])
@respx.mock
async def test_client_errors_are_mapped_and_not_retried(status, body, code):
    route = respx.get(URL).respond(status, json=body)
    async with client() as c:
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert err.value.code == code and route.call_count == 1 and not err.value.retryable


@respx.mock
async def test_html_challenge_page_is_reported_not_crashed_on():
    respx.get(URL).respond(200, text="<html>Checking your browser...</html>", headers={"content-type": "text/html"})
    async with client() as c:
        with pytest.raises(WooError, match="non-JSON") as err:
            await c.get("orders")
    assert err.value.code == "unexpected_response"


@respx.mock
async def test_redirect_is_not_followed():
    respx.get(URL).respond(301, headers={"location": "https://www.shop.test/wp-json/wc/v3/orders"})
    async with client() as c:
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert err.value.code == "redirected" and "www.shop.test" in str(err.value)


@respx.mock
async def test_call_budget_caps_total_time():
    respx.get(URL).respond(429, headers={"Retry-After": "30"})
    async with client(call_budget=2) as c:
        start = time.monotonic()
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert time.monotonic() - start < 1 and err.value.code == "rate_limited"


@respx.mock
async def test_timeouts_become_store_unreachable():
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    async with client(max_retries=2) as c:
        with pytest.raises(WooError) as err:
            await c.get("orders")
    assert err.value.code == "store_unreachable" and err.value.retryable


async def test_token_bucket_holds_the_rate():
    limiter = RateLimiter(rate=20, burst=5)
    start = time.monotonic()
    for _ in range(25):
        await limiter.acquire()
    # 5 from the burst, 20 more at 20/s: about one second.
    assert 0.9 <= time.monotonic() - start < 1.5


@respx.mock
async def test_truncated_json_is_retried_but_html_is_not():
    route = respx.get(URL).mock(side_effect=[
        httpx.Response(200, text='[{"id": 1, "sta', headers={"content-type": "application/json"}),
        httpx.Response(200, json=[{"id": 1}]),
    ])
    async with client() as c:
        data, _ = await c.get("orders")
    assert data == [{"id": 1}] and route.call_count == 2


@respx.mock
async def test_budget_includes_time_queued_for_a_token():
    respx.get(URL).respond(200, json=[])
    # 2 req/s, burst 1, 1.5s budget: of 10 simultaneous calls only ~4 can be served in time.
    async with client(rate=2, burst=1, call_budget=1.5) as c:
        start = time.monotonic()
        results = await asyncio.gather(*(c.get("orders") for _ in range(10)), return_exceptions=True)
        elapsed = time.monotonic() - start
    busy = [r for r in results if isinstance(r, WooError)]
    assert elapsed < 2.0, "a queued call outlived the budget"
    assert busy and all(e.code == "busy" and e.retryable for e in busy)
    assert 2 <= len(results) - len(busy) <= 5
