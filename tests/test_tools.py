import pytest
import respx

from woo_connector.auth import Credentials
from woo_connector.client import WooClient, WooError
from woo_connector.tools import (
    WooTools, _iso, decode_cursor, encode_cursor, mask_email, mask_phone, shape_order,
)

BASE = "https://shop.test/wp-json/wc/v3"

ORDER = {
    "id": 42, "number": "42", "status": "processing", "currency": "INR", "total": "640.00",
    "date_created_gmt": "2026-09-30T10:00:00", "date_paid_gmt": None, "customer_id": 3,
    "billing": {"first_name": "Diya", "last_name": "Iyer", "email": "diya.iyer@example.test",
                "phone": "+91-90000-00001", "address_1": "11 Demo Road", "city": "Mumbai", "country": "IN"},
    "shipping": {"first_name": "Diya", "last_name": "Iyer", "address_1": "11 Demo Road", "city": "Mumbai"},
    "line_items": [{"product_id": 9, "name": "Masala Chai 250g", "quantity": 2, "total": "360.00", "sku": "TEA-001"},
                   {"product_id": 10, "name": "Earl Grey 250g", "quantity": 1, "total": "280.00", "sku": "TEA-008"}],
    "refunds": [], "meta_data": [{"key": "_secret_plugin_field", "value": "x"}], "_links": {},
}


def test_masking():
    assert mask_email("diya.iyer@example.test") == "d***r@example.test"
    assert mask_email("a@b.test") == "a***@b.test"
    assert mask_phone("+91-90000-00001") == "******0001"
    assert mask_phone("12") is None


@pytest.mark.parametrize("pii", ["masked", "full", "none"])
def test_order_shape_respects_pii_mode(pii):
    out = shape_order(ORDER, pii, detail=True)
    text = repr(out)
    assert "meta_data" not in out and "_secret_plugin_field" not in text
    assert out["item_count"] == 3
    if pii == "masked":
        assert "diya.iyer@example.test" not in text and "11 Demo Road" not in text and "00001" not in text
        assert out["customer"]["email"] == "d***r@example.test"
    elif pii == "full":
        assert out["customer"]["email"] == "diya.iyer@example.test" and out["customer"]["address"] == "11 Demo Road"
    else:
        assert out["customer"] is None and out["shipping"] is None and "Diya" not in text


def test_dates():
    assert _iso("2026-09-01", "x") == "2026-09-01T00:00:00"
    assert _iso("2026-09-01T10:00:00+05:30", "x") == "2026-09-01T04:30:00"
    assert _iso("2026-09-01T10:00:00Z", "x") == "2026-09-01T10:00:00"
    with pytest.raises(WooError, match="date like"):
        _iso("last tuesday", "created_after")


@pytest.mark.parametrize("bad", ["", "!!!", "eyJwIjowfQ", "eyJwIjoiMSJ9", "bm90IGpzb24"])
def test_bad_cursors_are_rejected(bad):
    with pytest.raises(WooError) as err:
        decode_cursor(bad)
    assert err.value.code == "bad_request"


def tools(pii="masked") -> WooTools:
    return WooTools(WooClient("https://shop.test", Credentials("ck_t", "cs_t"), rate=1000, burst=1000), pii=pii)


@respx.mock
async def test_cursor_pins_before_and_refuses_a_different_query():
    route = respx.get(f"{BASE}/orders").respond(200, json=[ORDER], headers={"X-WP-Total": "3", "X-WP-TotalPages": "3"})
    t = tools()
    first = await t.list_orders(status=["processing"], page_size=1)
    pinned = route.calls.last.request.url.params["before"]
    second = await t.list_orders(status=["processing"], page_size=1, cursor=first["next_cursor"])
    req = route.calls.last.request.url.params
    assert req["page"] == "2" and req["before"] == pinned and second["page"] == 2
    with pytest.raises(WooError, match="different query"):
        await t.list_orders(status=["completed"], page_size=1, cursor=first["next_cursor"])
    last = await t.list_orders(status=["processing"], page_size=1,
                               cursor=encode_cursor(3, pinned, {"status": ["processing"], "per_page": 1}))
    assert last["next_cursor"] is None


@respx.mock
async def test_page_size_is_capped_and_status_validated():
    route = respx.get(f"{BASE}/orders").respond(200, json=[], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"})
    t = tools()
    await t.list_orders(page_size=500)
    assert route.calls.last.request.url.params["per_page"] == "50"
    with pytest.raises(WooError, match="unknown order status"):
        await t.list_orders(status=["shipped"])
    await t.list_orders(status=["any"])
    assert "status" not in route.calls.last.request.url.params


@respx.mock
async def test_search_orders_puts_exact_number_first_without_duplicates():
    respx.get(f"{BASE}/orders/42").respond(200, json=ORDER)
    respx.get(f"{BASE}/orders").respond(200, json=[ORDER, {**ORDER, "id": 43, "number": "43"}],
                                        headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"})
    out = await tools().search_orders("#42")
    assert [o["id"] for o in out["orders"]] == [42, 43]


@respx.mock
async def test_search_orders_number_that_does_not_exist_falls_back_to_search():
    respx.get(f"{BASE}/orders/999").respond(404, json={"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."})
    respx.get(f"{BASE}/orders").respond(200, json=[], headers={"X-WP-Total": "0", "X-WP-TotalPages": "0"})
    out = await tools().search_orders("999")
    assert out["orders"] == []


async def test_get_product_needs_exactly_one_identifier():
    with pytest.raises(WooError):
        await tools().get_product()
    with pytest.raises(WooError):
        await tools().get_product(product_id=1, sku="X")


@respx.mock
async def test_inventory_report_flags_levels_and_walks_variations():
    respx.get(f"{BASE}/settings/products/woocommerce_notify_low_stock_amount").respond(200, json={"value": "3"})
    respx.get(f"{BASE}/products").respond(200, headers={"X-WP-Total": "3", "X-WP-TotalPages": "1"}, json=[
        {"id": 1, "name": "A", "type": "simple", "manage_stock": True, "stock_quantity": 0, "stock_status": "outofstock"},
        {"id": 2, "name": "B", "type": "simple", "manage_stock": True, "stock_quantity": 50, "stock_status": "instock",
         "low_stock_amount": None},
        {"id": 3, "name": "Mug", "type": "variable", "variations": [4, 5]},
    ])
    respx.get(f"{BASE}/products/3/variations").respond(200, headers={"X-WP-Total": "2", "X-WP-TotalPages": "1"}, json=[
        {"id": 4, "manage_stock": True, "stock_quantity": 2, "stock_status": "instock", "attributes": [{"option": "Small"}]},
        {"id": 5, "manage_stock": True, "stock_quantity": 40, "stock_status": "instock", "attributes": [{"option": "Large"}]},
    ])
    out = await tools().inventory_report()
    assert out["products_scanned"] == 4 and out["out_of_stock"] == 1 and out["low_stock"] == 1
    assert [(i["id"], i["level"]) for i in out["items"]] == [(1, "out"), (4, "low")]
    assert out["items"][1]["name"] == "Mug (Small)" and out["items"][1]["parent_id"] == 3


CATALOGUE = [{"id": 1, "name": "Masala Chai 250g", "sku": "TEA-001"}, {"id": 2, "name": "Earl Grey 250g", "sku": "TEA-008"},
             {"id": 3, "name": "Kulhad Mug", "sku": "MUG-KULHAD"}]


def _products(handler):
    def respond(request):
        status, body = handler(dict(request.url.params))
        return __import__("httpx").Response(status, json=body, headers={"X-WP-Total": str(len(body)), "X-WP-TotalPages": "1"})
    respx.get(f"{BASE}/products").mock(side_effect=respond)


@respx.mock
async def test_search_products_uses_name_or_sku_search_on_a_modern_store():
    def modern(params):
        if "search_name_or_sku" in params:
            q = params["search_name_or_sku"].lower()
            return 200, [p for p in CATALOGUE if q in (p["name"] + p["sku"]).lower()]
        raise AssertionError("fell back on a store that supports search_name_or_sku")
    _products(modern)
    out = await tools().search_products("tea-00")
    assert [p["sku"] for p in out["products"]] == ["TEA-001", "TEA-008"]


@respx.mock
async def test_search_products_falls_back_when_an_old_store_ignores_the_parameter():
    def old(params):
        if "search" in params:
            return 200, [p for p in CATALOGUE if params["search"].lower() in p["name"].lower()]
        if "sku" in params:
            return 200, [p for p in CATALOGUE if p["sku"] == params["sku"]]
        return 200, CATALOGUE  # unknown parameter ignored: the whole catalogue
    _products(old)
    assert [p["id"] for p in (await tools().search_products("chai"))["products"]] == [1]
    assert [p["id"] for p in (await tools().search_products("MUG-KULHAD"))["products"]] == [3]
