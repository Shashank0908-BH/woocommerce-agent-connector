"""MCP server: the connector as an Agent Studio agent sees it.

Eight tools, all read-only. Errors come back as tool errors whose text is a
JSON object with a stable `error` code, so the agent can branch on
`not_found` versus `rate_limited` versus `auth_failed` instead of parsing prose.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import config
from .client import WooClient, WooError
from .tools import WooTools

INSTRUCTIONS = """\
Read-only access to one WooCommerce store: orders, products and stock levels.
Nothing here can change an order, refund, edit stock or message a customer.
Customer emails and phones are masked unless the merchant enabled full PII.
Lists are paged: pass next_cursor back unchanged to get the next page.
Errors carry a JSON object after the tool name, with an `error` code;
`retryable: true` means the same call may work later."""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)

OrderStatus = Literal["any", "pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed", "checkout-draft"]

mcp = MCPServer("woocommerce", instructions=INSTRUCTIONS, version="0.1.0")
_tools: WooTools | None = None


def get_tools() -> WooTools:
    global _tools
    if _tools is None:
        s = config.from_env()
        client = WooClient(s.store_url, s.creds, auth_mode=s.auth_mode, allow_insecure_basic=s.allow_insecure_basic,
                           rate=s.rate, burst=s.burst, max_concurrency=s.max_concurrency)
        _tools = WooTools(client, pii=s.pii_mode)  # type: ignore[arg-type]
    return _tools


def set_tools(tools: WooTools | None) -> None:
    """Tests inject a WooTools bound to a test store."""
    global _tools
    _tools = tools


async def _run(coro) -> dict[str, Any]:
    try:
        return await coro
    except WooError as exc:
        raise ToolError(json.dumps(exc.to_dict())) from None


@mcp.tool(annotations=READ_ONLY)
async def store_overview() -> dict[str, Any]:
    """Store currency, country, and how many orders and products it has. Call first to confirm the connection."""
    return await _run(get_tools().store_overview())


@mcp.tool(annotations=READ_ONLY)
async def list_orders(
    status: Annotated[list[OrderStatus] | None, Field(description="Filter by one or more statuses; omit for all")] = None,
    created_after: Annotated[str | None, Field(description="Only orders created after this date, e.g. 2026-09-01 (UTC)")] = None,
    created_before: Annotated[str | None, Field(description="Only orders created before this date (UTC)")] = None,
    customer_id: Annotated[int | None, Field(description="Only this customer's orders")] = None,
    page_size: Annotated[int, Field(ge=1, le=50, description="Orders per page, max 50")] = 20,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page; omit for the first page")] = None,
) -> dict[str, Any]:
    """Newest orders first, as summaries (status, total, customer, item names). Use get_order for line items and refunds."""
    return await _run(get_tools().list_orders(status, created_after, created_before, customer_id, page_size, cursor))


@mcp.tool(annotations=READ_ONLY)
async def get_order(
    order_id: Annotated[int, Field(ge=1, description="WooCommerce order ID (the number customers see)")],
    include_notes: Annotated[bool, Field(description="Also return order notes (status changes, staff and customer notes)")] = False,
) -> dict[str, Any]:
    """One order in full: line items with SKUs, totals breakdown, payment, shipping, coupons, refunds, and optionally notes."""
    return await _run(get_tools().get_order(order_id, include_notes))


@mcp.tool(annotations=READ_ONLY)
async def search_orders(
    query: Annotated[str, Field(min_length=2, description="Order number (#1234), customer name, email, phone, or a product name")],
    status: Annotated[list[OrderStatus] | None, Field(description="Optional status filter")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Find orders from a customer's name, email, phone, an order number or a product they bought. An exact order number comes first. Note text is not searched."""
    return await _run(get_tools().search_orders(query, status, limit))


@mcp.tool(annotations=READ_ONLY)
async def list_products(
    stock_status: Annotated[Literal["instock", "outofstock", "onbackorder"] | None, Field(description="Filter by stock status")] = None,
    category_id: Annotated[int | None, Field(description="Only products in this category ID")] = None,
    page_size: Annotated[int, Field(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Field(description="next_cursor from the previous page")] = None,
) -> dict[str, Any]:
    """Published products with price and stock. Variable products show the parent; use get_product for each variation."""
    return await _run(get_tools().list_products(stock_status, category_id, page_size, cursor))


@mcp.tool(annotations=READ_ONLY)
async def get_product(
    product_id: Annotated[int | None, Field(description="Product ID")] = None,
    sku: Annotated[str | None, Field(description="Or the exact SKU")] = None,
) -> dict[str, Any]:
    """One product by ID or SKU, with variations and their stock if it is a variable product."""
    return await _run(get_tools().get_product(product_id, sku))


@mcp.tool(annotations=READ_ONLY)
async def search_products(
    query: Annotated[str, Field(min_length=2, description="Words from the product name or description, or an exact SKU")],
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Find products by name or SKU."""
    return await _run(get_tools().search_products(query, limit))


@mcp.tool(annotations=READ_ONLY)
async def inventory_report(
    only_low_stock: Annotated[bool, Field(description="True: only items at or below threshold or out of stock")] = True,
    low_stock_threshold: Annotated[int | None, Field(ge=0, description="Override the store's low-stock threshold")] = None,
) -> dict[str, Any]:
    """Stock levels for every stock-managed product and variation, flagged out / low / ok. Scans up to 2,000 products."""
    return await _run(get_tools().inventory_report(only_low_stock, low_stock_threshold))
