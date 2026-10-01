"""The read primitives an agent gets: list, get and search over orders and
products, plus an inventory view. Transport-agnostic; server.py exposes them
over MCP and tests call them directly.

What comes back is shaped, not raw. A raw WooCommerce order is 47 fields and
2.4 to 3.5 KB of JSON (240 dev-store orders, median 2.9 KB), most of it links,
hashes and meta the agent cannot use. A list summary is a median 470 bytes,
6.3 times smaller, so a page of 50 orders fits an agent's context instead of
crowding it out. Contact details are masked unless the merchant opted in.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from .client import WooClient, WooError

ORDER_STATUSES = {
    "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed", "checkout-draft",
}
STOCK_STATUSES = {"instock", "outofstock", "onbackorder"}
PiiMode = Literal["masked", "full", "none"]

MAX_ORDER_PAGE = 50
MAX_PRODUCT_PAGE = 100
INVENTORY_PAGE = 100
INVENTORY_MAX_PAGES = 20  # 2,000 products; beyond that the agent is told the view is partial
INVENTORY_FIELDS = "id,name,sku,type,status,stock_quantity,stock_status,manage_stock,low_stock_amount,variations"


# --- cursors ------------------------------------------------------------------
# WooCommerce pages by offset. If an order arrives between page 1 and page 2,
# everything shifts by one and the agent sees a duplicate. The cursor pins
# `before` to the moment page 1 was read, so later orders cannot shift pages.

def encode_cursor(page: int, pinned_before: str | None, query: dict[str, Any]) -> str:
    raw = json.dumps({"p": page, "b": pinned_before, "q": query}, separators=(",", ":"), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(data, dict) or not isinstance(data.get("p"), int) or data["p"] < 1:
            raise ValueError
        return data
    except (ValueError, binascii.Error, json.JSONDecodeError):
        raise WooError("bad_request", "cursor is invalid or from a different query; start again without it") from None


def _iso(value: str | None, field: str) -> str | None:
    """Accept 2026-09-01 or a full ISO timestamp; WooCommerce wants the latter."""
    if value is None or value == "":
        return None
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        value += "T00:00:00"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise WooError("bad_request", f"{field} must be a date like 2026-09-01 or 2026-09-01T10:00:00, got {value!r}") from None
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(timespec="seconds")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat()


# --- shaping --------------------------------------------------------------------

def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email or None
    local, domain = email.split("@", 1)
    return f"{local[0]}***{local[-1] if len(local) > 1 else ''}@{domain}"


def mask_phone(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return f"******{digits[-4:]}" if len(digits) >= 4 else None


def _contact(addr: dict[str, Any], pii: PiiMode) -> dict[str, Any] | None:
    if pii == "none" or not addr:
        return None
    name = " ".join(p for p in (addr.get("first_name"), addr.get("last_name")) if p) or None
    out = {
        "name": name,
        "city": addr.get("city") or None,
        "state": addr.get("state") or None,
        "postcode": addr.get("postcode") or None,
        "country": addr.get("country") or None,
    }
    if "email" in addr:
        out["email"] = addr.get("email") or None if pii == "full" else mask_email(addr.get("email"))
    if "phone" in addr:
        out["phone"] = addr.get("phone") or None if pii == "full" else mask_phone(addr.get("phone"))
    if pii == "full":
        out["address"] = ", ".join(p for p in (addr.get("address_1"), addr.get("address_2")) if p) or None
    return out


def _utc(value: str | None) -> str | None:
    return f"{value}Z" if value else None


def shape_order(o: dict[str, Any], pii: PiiMode, detail: bool) -> dict[str, Any]:
    items = o.get("line_items") or []
    out: dict[str, Any] = {
        "id": o.get("id"),
        "number": o.get("number"),
        "status": o.get("status"),
        "created_at": _utc(o.get("date_created_gmt")),
        "paid_at": _utc(o.get("date_paid_gmt")),
        "completed_at": _utc(o.get("date_completed_gmt")),
        "currency": o.get("currency"),
        "total": o.get("total"),
        "payment_method": o.get("payment_method_title") or o.get("payment_method"),
        "customer_id": o.get("customer_id") or None,
        "customer": _contact(o.get("billing") or {}, pii),
        "item_count": sum(int(i.get("quantity") or 0) for i in items),
        "items": [
            {"product_id": i.get("product_id"), "variation_id": i.get("variation_id") or None, "sku": i.get("sku") or None,
             "name": i.get("name"), "quantity": i.get("quantity"), "total": i.get("total")}
            for i in items
        ] if detail else [i.get("name") for i in items],
    }
    if detail:
        out.update(
            modified_at=_utc(o.get("date_modified_gmt")),
            subtotal_breakdown={
                "discount": o.get("discount_total"), "shipping": o.get("shipping_total"), "tax": o.get("total_tax"),
            },
            transaction_id=o.get("transaction_id") or None,
            shipping=_contact(o.get("shipping") or {}, pii),
            shipping_methods=[s.get("method_title") for s in o.get("shipping_lines") or []],
            coupons=[c.get("code") for c in o.get("coupon_lines") or []],
            refunds=[{"id": r.get("id"), "total": r.get("total"), "reason": r.get("reason") or None}
                     for r in o.get("refunds") or []],
            customer_note=o.get("customer_note") or None,
            needs_payment=o.get("needs_payment"),
            created_via=o.get("created_via") or None,
        )
    return out


def shape_product(p: dict[str, Any], detail: bool) -> dict[str, Any]:
    out = {
        "id": p.get("id"),
        "name": p.get("name"),
        "sku": p.get("sku") or None,
        "type": p.get("type"),
        "status": p.get("status"),
        "price": p.get("price") or None,
        "regular_price": p.get("regular_price") or None,
        "sale_price": p.get("sale_price") or None,
        "stock_status": p.get("stock_status"),
        "stock_quantity": p.get("stock_quantity") if p.get("manage_stock") else None,
        "categories": [c.get("name") for c in p.get("categories") or []],
    }
    if p.get("parent_id"):
        out["parent_id"] = p["parent_id"]  # a variation; get_product(parent_id) shows its siblings
    if detail:
        out.update(
            manage_stock=p.get("manage_stock"),
            low_stock_amount=p.get("low_stock_amount"),
            backorders=p.get("backorders"),
            total_sales=p.get("total_sales"),
            permalink=p.get("permalink"),
            short_description=_strip_html(p.get("short_description")),
            attributes=[{"name": a.get("name"), "options": a.get("options")} for a in p.get("attributes") or []],
            modified_at=_utc(p.get("date_modified_gmt")),
        )
    return out


def shape_variation(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": v.get("id"),
        "sku": v.get("sku") or None,
        "attributes": {a.get("name"): a.get("option") for a in v.get("attributes") or []},
        "price": v.get("price") or None,
        "stock_status": v.get("stock_status"),
        "stock_quantity": v.get("stock_quantity") if v.get("manage_stock") else None,
    }


def _strip_html(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip() or None


# --- primitives -------------------------------------------------------------------

def _page_size(value: int, cap: int) -> int:
    if value < 1:
        raise WooError("bad_request", "page_size must be at least 1")
    return min(value, cap)


def _statuses(status: list[str] | None) -> list[str] | None:
    if not status:
        return None
    bad = sorted(set(status) - ORDER_STATUSES - {"any"})
    if bad:
        raise WooError("bad_request", f"unknown order status {bad}; use any of {sorted(ORDER_STATUSES)}")
    return None if "any" in status else status


class WooTools:
    def __init__(self, client: WooClient, pii: PiiMode = "masked"):
        self.client = client
        self.pii = pii

    async def store_overview(self) -> dict[str, Any]:
        orders = await self.client.get_page("orders", {"per_page": 1, "_fields": "id"})
        products = await self.client.get_page("products", {"per_page": 1, "_fields": "id"})
        settings, _ = await self.client.get("settings/general")
        by_id = {s.get("id"): s.get("value") for s in settings if isinstance(s, dict)}
        return {
            "store_url": self.client.base_url,
            "currency": by_id.get("woocommerce_currency"),
            "country": by_id.get("woocommerce_default_country"),
            "order_count": orders.total,
            "product_count": products.total,
            "auth_mode": self.client.auth_mode,
            "pii_mode": self.pii,
        }

    async def list_orders(
        self,
        status: list[str] | None = None,
        created_after: str | None = None,
        created_before: str | None = None,
        customer_id: int | None = None,
        page_size: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        query = {
            "status": _statuses(status),
            "after": _iso(created_after, "created_after"),
            "before": _iso(created_before, "created_before"),
            "customer": customer_id,
            "per_page": _page_size(page_size, MAX_ORDER_PAGE),
        }
        return await self._paged("orders", query, cursor, lambda o: shape_order(o, self.pii, detail=False))

    async def get_order(self, order_id: int, include_notes: bool = False) -> dict[str, Any]:
        if order_id < 1:
            raise WooError("bad_request", "order_id must be a positive integer")
        order, _ = await self.client.get(f"orders/{order_id}")
        out = shape_order(order, self.pii, detail=True)
        if include_notes:
            notes, _ = await self.client.get(f"orders/{order_id}/notes", {"type": "any"})
            out["notes"] = [
                {"at": _utc(n.get("date_created_gmt")), "to_customer": n.get("customer_note"),
                 "by": n.get("author"), "note": _strip_html(n.get("note"))}
                for n in notes
            ]
        return out

    async def search_orders(self, query: str, status: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        """Order number, customer name, email, phone or product name. Exact number first."""
        query = (query or "").strip()
        if len(query) < 2:
            raise WooError("bad_request", "query must be at least 2 characters")
        results: list[dict[str, Any]] = []
        number = query.lstrip("#")
        if number.isdigit():
            try:
                exact, _ = await self.client.get(f"orders/{int(number)}")
                if not status or exact.get("status") in status:
                    results.append(shape_order(exact, self.pii, detail=False))
            except WooError as exc:
                if exc.code != "not_found":
                    raise
        page = await self.client.get_page("orders", {
            "search": query, "status": _statuses(status), "per_page": _page_size(limit, MAX_ORDER_PAGE),
        })
        total = page.total or 0
        matches = page.items
        words = query.lower().split()
        if not matches and len(words) > 1:
            # WooCommerce matches one field at a time, so "Meera Pillai" finds nothing:
            # first and last name live in separate fields. Search the most specific
            # word, then keep orders whose billing name/email contains every word.
            wide = await self.client.get_page("orders", {
                "search": max(words, key=len), "status": _statuses(status), "per_page": 100,
            })
            matches = [o for o in wide.items if all(w in _haystack(o) for w in words)][:limit]
            total = len(matches)
        seen = {r["id"] for r in results}
        results += [shape_order(o, self.pii, detail=False) for o in matches if o.get("id") not in seen]
        return {"query": query, "total_matches": max(total, len(results)), "orders": results[:limit]}

    async def list_products(
        self,
        stock_status: str | None = None,
        category_id: int | None = None,
        page_size: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if stock_status and stock_status not in STOCK_STATUSES:
            raise WooError("bad_request", f"stock_status must be one of {sorted(STOCK_STATUSES)}")
        query = {
            "stock_status": stock_status,
            "category": category_id,
            "status": "publish",
            "per_page": _page_size(page_size, MAX_PRODUCT_PAGE),
            "orderby": "id",
            "order": "asc",
        }
        return await self._paged("products", query, cursor, lambda p: shape_product(p, detail=False), pin=False)

    async def get_product(self, product_id: int | None = None, sku: str | None = None) -> dict[str, Any]:
        if (product_id is None) == (sku is None):
            raise WooError("bad_request", "pass exactly one of product_id or sku")
        if sku is not None:
            found = await self.client.get_page("products", {"sku": sku.strip(), "per_page": 2})
            if not found.items:
                raise WooError("not_found", f"no product with SKU {sku!r} (variation SKUs are found via their parent)")
            product = found.items[0]
        else:
            product, _ = await self.client.get(f"products/{product_id}")
        out = shape_product(product, detail=True)
        if product.get("type") == "variable":
            out["variations"] = [shape_variation(v) for v in await self._all("products/{}/variations".format(product["id"]))]
        return out

    async def search_products(self, query: str, limit: int = 20) -> dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 2:
            raise WooError("bad_request", "query must be at least 2 characters")
        per_page = _page_size(limit, MAX_PRODUCT_PAGE)
        # Recent WooCommerce (11.1.2 here) matches every word against name or SKU, variations
        # too. Older stores ignore the parameter and return the whole catalogue, so the
        # answer is trusted only if every row really contains a word of the query.
        tokens = query.lower().split()
        modern = await self.client.get_page("products", {"search_name_or_sku": query, "per_page": per_page})
        if all(any(t in f"{p.get('name', '')} {p.get('sku', '')}".lower() for t in tokens) for p in modern.items):
            items = [shape_product(p, detail=False) for p in modern.items]
            return {"query": query, "total_matches": modern.total or len(items), "products": items}
        page = await self.client.get_page("products", {"search": query, "per_page": per_page})
        items = [shape_product(p, detail=False) for p in page.items]
        # WooCommerce's search does not look at SKUs; an exact SKU hit goes first.
        by_sku = await self.client.get_page("products", {"sku": query, "per_page": 1})
        if by_sku.items and by_sku.items[0]["id"] not in {i["id"] for i in items}:
            items.insert(0, shape_product(by_sku.items[0], detail=False))
        return {"query": query, "total_matches": max(page.total or 0, len(items)), "products": items[:limit]}

    async def inventory_report(self, only_low_stock: bool = True, low_stock_threshold: int | None = None) -> dict[str, Any]:
        """Every stock-managed product and variation, flagged against its threshold."""
        store_default = low_stock_threshold
        if store_default is None:
            try:
                setting, _ = await self.client.get("settings/products/woocommerce_notify_low_stock_amount")
                store_default = int(setting.get("value") or 2)
            except (WooError, ValueError, TypeError):
                store_default = 2  # WooCommerce's own default
        rows: list[dict[str, Any]] = []
        truncated = False
        page_no = 1
        while True:
            page = await self.client.get_page("products", {
                "per_page": INVENTORY_PAGE, "page": page_no, "_fields": INVENTORY_FIELDS, "orderby": "id", "order": "asc",
            })
            for p in page.items:
                threshold = low_stock_threshold if low_stock_threshold is not None else (p.get("low_stock_amount") or store_default)
                if p.get("type") == "variable" and p.get("variations"):
                    for v in await self._all(f"products/{p['id']}/variations",
                                             {"_fields": "id,sku,stock_quantity,stock_status,manage_stock,low_stock_amount,attributes"}):
                        vt = low_stock_threshold if low_stock_threshold is not None else (v.get("low_stock_amount") or threshold)
                        name = f"{p.get('name')} ({', '.join(a.get('option', '') for a in v.get('attributes') or [])})"
                        rows.append(_stock_row(v, name, vt, parent_id=p["id"]))
                else:
                    rows.append(_stock_row(p, p.get("name"), threshold))
            if not page.total_pages or page_no >= page.total_pages:
                break
            if page_no >= INVENTORY_MAX_PAGES:
                truncated = True
                break
            page_no += 1
        flagged = [r for r in rows if r["level"] != "ok"]
        return {
            "generated_at": _now_iso() + "Z",
            "products_scanned": len(rows),
            "out_of_stock": sum(r["level"] == "out" for r in rows),
            "low_stock": sum(r["level"] == "low" for r in rows),
            "items": sorted(flagged if only_low_stock else rows, key=lambda r: (r["stock_quantity"] is None, r["stock_quantity"] or 0)),
            "truncated": truncated,
            "note": f"stopped after {INVENTORY_MAX_PAGES * INVENTORY_PAGE} products" if truncated else None,
        }

    # --- helpers ---

    async def _paged(self, path: str, query: dict[str, Any], cursor: str | None, shape, pin: bool = True) -> dict[str, Any]:
        signature = {k: v for k, v in query.items() if v is not None}
        if cursor:
            state = decode_cursor(cursor)
            if state.get("q") != signature:
                raise WooError("bad_request", "cursor belongs to a different query; start again without it")
            page_no, pinned = state["p"], state.get("b")
        else:
            page_no, pinned = 1, (_now_iso() if pin and not query.get("before") else None)
        params = dict(signature, page=page_no)
        if pinned:
            params["before"] = pinned
        page = await self.client.get_page(path, params)
        has_more = bool(page.total_pages and page_no < page.total_pages)
        key = "orders" if path == "orders" else "products"
        return {
            key: [shape(x) for x in page.items],
            "total": page.total,
            "page": page_no,
            "next_cursor": encode_cursor(page_no + 1, pinned, signature) if has_more else None,
        }

    async def _all(self, path: str, params: dict[str, Any] | None = None, cap_pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page_no in range(1, cap_pages + 1):
            page = await self.client.get_page(path, {**(params or {}), "per_page": 100, "page": page_no})
            out += page.items
            if not page.total_pages or page_no >= page.total_pages:
                break
        return out


def _haystack(order: dict[str, Any]) -> str:
    b = order.get("billing") or {}
    return " ".join(str(b.get(k) or "") for k in ("first_name", "last_name", "email", "phone", "company")).lower()


def _stock_row(item: dict[str, Any], name: str | None, threshold: int, parent_id: int | None = None) -> dict[str, Any]:
    qty = item.get("stock_quantity") if item.get("manage_stock") else None
    status = item.get("stock_status")
    if status == "outofstock" or (qty is not None and qty <= 0):
        level = "out"
    elif qty is not None and qty <= threshold:
        level = "low"
    else:
        level = "ok"
    return {
        "id": item.get("id"),
        "parent_id": parent_id,
        "name": name,
        "sku": item.get("sku") or None,
        "stock_quantity": qty,
        "stock_status": status,
        "threshold": threshold,
        "level": level,
    }
