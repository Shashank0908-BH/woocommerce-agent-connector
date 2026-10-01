# What an agent can and cannot do with this connector

The connector gives an Agent Studio agent eyes on one WooCommerce store and no hands. Every tool is a GET; the key is requested with `read` scope; and even a read-write key, if a merchant pastes one, is only ever used to read (`tests/test_live.py::test_connector_only_ever_sends_get_even_with_a_read_write_key`).

## It can

- **Find an order from what a customer says:** "#1042", "Diya Iyer", `diya.iyer@example.test`, a phone number or a product they bought, through `search_orders`. An exact order number comes first.
- **Explain an order:** line items with SKUs and quantities, discount, shipping and tax, payment method and transaction ID, coupons, refunds with reasons, and the order notes timeline, through `get_order`.
- **Answer "where is my order":** status, when it was paid and completed, shipping method and destination city, and any note the store left for the customer.
- **List orders for an operations question:** by status, date range or customer, newest first, 50 a page, with a cursor that does not skip or repeat when new orders arrive mid-walk.
- **Look up a product or variation:** by ID or exact SKU, with price, stock status, stock quantity and every variation's stock, through `get_product`.
- **Search the catalogue:** by name or SKU, through `search_products`.
- **Report stock:** every stock-managed product and variation flagged `out`, `low` or `ok` against the store's own low-stock threshold or one the agent passes, through `inventory_report`.
- **Tell the user what went wrong:** every failure is a stable code (`not_found`, `auth_failed`, `forbidden`, `rate_limited`, `busy`, `store_unreachable`, `redirected`, `api_unavailable`, `unexpected_response`, `bad_request`) with `retryable` set, so the agent can retry, ask the user, or escalate rather than guess.

## It cannot

- **Change anything:** no status changes, refunds, cancellations, notes, stock edits, price edits, coupons or customer edits. A support agent that wants to act needs a human in the loop, or a write tool designed with confirmations, which this is not.
- **Contact a customer:** no email, SMS or WhatsApp. Order notes "to customer" are read, never written.
- **See full contact details by default:** emails, phones and street addresses are masked unless the merchant sets `WOO_PII_MODE=full`. With `none`, the customer block is removed entirely.
- **Read payment card or Razorpay payment data:** WooCommerce does not store card data; the connector returns the payment method name and the gateway's transaction ID only.
- **Browse customers, coupons, reports or settings:** out of scope for orders and inventory. `store_overview` reads two general settings (currency and country) and nothing else.
- **See other stores:** one process, one store, one key.
- **Search by address or note text:** WooCommerce 11's order search matches customer names, emails, phones and product names; street addresses and notes return nothing, and so does a two-word name. The connector rescues full names only (search the longest word, then filter).
- **Get real-time stock under heavy sales:** `inventory_report` is a scan at the moment it is called. Two calls a minute apart can disagree, and a catalogue over 2,000 products is reported as partial (`truncated: true`).
- **Bypass the host:** if the store's host or firewall answers with an HTML challenge page, the agent gets `unexpected_response`. The connector does not solve challenges or rotate IPs.

## Guardrails worth knowing

| Guardrail | Where |
|---|---|
| Read-only scope requested; only GET is ever sent | `connect.py`, `client.py` |
| Contact details masked by default | `tools.py` `_contact` |
| Page sizes capped at 50 orders and 100 products, so one call cannot flood the context | `tools.py` |
| Every call ends inside a time budget, including time queued behind other calls | `client.py` `get` |
| Store-wide pause on a 429, so parallel tool calls do not pile onto a rate-limited host | `client.py` `RateLimiter.cool_down` |
| Forged or over-privileged key callbacks rejected | `connect.py` `_accept` |
| HTTP transport refuses to start without a 24+ character bearer token | `cli.py` `cmd_serve` |
