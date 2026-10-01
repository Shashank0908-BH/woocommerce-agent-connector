# WooCommerce connector for Agent Studio

A merchant on WooCommerce keeps orders, products and stock inside their own WordPress install, behind a REST API that answers to a consumer key and secret. This connector is the piece between that store and an Agent Studio agent: it obtains a read-only key through the store's own approve-access screen, signs every request the way WooCommerce checks it, paces itself under the host's rate limit, and hands the agent eight MCP tools whose output is shaped for a model to reason over.

It is not a REST wrapper. A raw WooCommerce order is 47 fields and a median 2.9 KB of JSON; the agent gets the 470 bytes that matter, with customer contact details masked by default, and an error code it can branch on when something goes wrong.

Built for the Razorpay Forward-Deployed Engineer (Agent Studio) assignment, option 3. Everything in this repository runs against a local WooCommerce 11.1.2 store with fictional data; no real merchant, customer or key is involved.

## What the agent can call

| Tool | What it answers |
|---|---|
| `store_overview` | Currency, country, order and product counts. The connection check. |
| `list_orders` | Orders newest first, filtered by status, date range or customer, paged by cursor. |
| `get_order` | One order: line items with SKUs, totals, payment, shipping, coupons, refunds, notes. |
| `search_orders` | By order number (`#1234`), customer name, email or phone. |
| `list_products` | Published products with price and stock, filtered by stock status or category. |
| `get_product` | One product by ID or SKU, with every variation and its stock. |
| `search_products` | By name, description or exact SKU. |
| `inventory_report` | Every stock-managed product and variation, flagged out, low or ok against the store's threshold. |

All eight are annotated `readOnlyHint: true`. The full schema is in [`docs/mcp-tools.json`](docs/mcp-tools.json), and [`docs/AGENT_CAPABILITIES.md`](docs/AGENT_CAPABILITIES.md) is the plain list of what an agent can and cannot do with them.

## Run it

Needs Docker, [uv](https://docs.astral.sh/uv/) and Python 3.12 (uv fetches it).

```bash
uv sync
./dev/setup.sh                 # local WooCommerce: 21 products, 12 customers, 240 orders, read-only keys in dev/.env
uv run pytest -m "not live"    # 47 unit tests, no store needed
uv run pytest -m live          # 15 tests against the local store
uv run python scripts/stress.py chaos --calls 400
uv run python scripts/stress.py live --agents 12 --seconds 60
```

### Connect a store

```bash
# Opens the store's approve-access screen; keys arrive by server-to-server POST.
uv run woo-connector connect https://shop.example.com --public-callback-url https://<tunnel-to-this-machine>

# Against the dev store, which reaches this machine as host.docker.internal:
uv run woo-connector connect http://localhost:8088 --callback-host host.docker.internal
```

Or skip the flow and paste keys made under WooCommerce > Settings > Advanced > REST API (permission: Read):

```bash
export WOO_BASE_URL=https://shop.example.com WOO_CONSUMER_KEY=ck_... WOO_CONSUMER_SECRET=cs_...
uv run woo-connector check
```

### Serve the tools

```bash
uv run woo-connector serve                                   # stdio, for an agent host that launches it
WOO_MCP_TOKEN=$(openssl rand -hex 24) uv run woo-connector serve --http --port 8765   # streamable HTTP, bearer token required
```

An MCP host config looks like this:

```json
{
  "mcpServers": {
    "woocommerce": {
      "command": "uv",
      "args": ["--directory", "/path/to/woocommerce-agent-connector", "run", "woo-connector", "serve"],
      "env": { "WOO_BASE_URL": "https://shop.example.com", "WOO_PII_MODE": "masked" }
    }
  }
}
```

| Variable | Default | Meaning |
|---|---|---|
| `WOO_BASE_URL` | required | Store address, exactly as WordPress knows it (`www` matters for signatures). |
| `WOO_CONSUMER_KEY` / `WOO_CONSUMER_SECRET` | from `connect` | Override the stored keys. |
| `WOO_PII_MODE` | `masked` | `masked`: `d***r@example.test`, `******0001`, no street. `full`: as stored. `none`: no customer block at all. |
| `WOO_RATE` / `WOO_BURST` | `5` / `10` | Client-side requests per second and burst. Set under the host's limit. |
| `WOO_MAX_CONCURRENCY` | `4` | Requests in flight at once. |
| `WOO_AUTH_MODE` | `auto` | `basic` on https, `oauth1` on http. |
| `WOO_MCP_TOKEN` | none | Required for `--http`; 24+ characters. |

## How authentication works

**Keys through the approve screen, not by copy-paste:** `connect` opens `/wc-auth/v1/authorize` with `scope=read`. The owner logs in to their own store and clicks Approve; WooCommerce creates the key pair and POSTs it to the callback, so the secret never appears in a browser URL or a chat window. This is WooCommerce's equivalent of an OAuth consent screen rather than OAuth 2.0 itself; there is no refresh token because the keys do not expire, they are revoked.

**State, checked:** the `user_id` field travels with the request and comes back on both the callback and the return redirect, so it carries a random 32-character state per attempt. A callback with any other state is rejected, as is a key with more permission than was asked for, a second key for the same attempt, or a body over 16 KB.

**Signing:** on https the key goes as HTTP Basic. On plain http the secret never leaves the machine: each request carries a one-legged OAuth 1.0a HMAC-SHA256 signature with a fresh nonce. The signature was checked against WooCommerce's own PHP on identical input and matches byte for byte (`tests/test_auth.py`). Basic over plain http is refused.

**At rest:** keys live in `~/.config/woo-connector/credentials.json`, mode 0600, keyed by store URL; the file is refused if anyone else can read it. Neither keys nor signed URLs are logged.

## How rate limits are handled

WooCommerce core does not rate-limit `/wc/v3`; the hosts merchants run it on do, and they answer 429. Three layers, in order:

1. **A shared token bucket** paces every call in the process, so an agent firing twenty tool calls at once becomes a steady stream.
2. **A shared cooldown:** a 429 pauses the whole client for the store's `Retry-After`, not just the request that got it. Without this, the other nineteen calls walk into the same wall.
3. **Retries** with capped exponential backoff and full jitter for 429, 5xx, timeouts, dropped connections and JSON cut off mid-body, inside a time budget that covers queueing too. Past the budget the agent gets `busy` with `retryable: true`, never a hang.

The dev store has a mu-plugin that reproduces a host limit (`dev/mu-plugins/connector-dev-rate-limit.php`), so this is tested against a real 429 from PHP, not only a mock.

## Stress testing, measured on 2 October 2026

| Run | Setup | Result |
|---|---|---|
| Unit | 47 tests: signing, retries, 429 cooldown, budget, cursors, masking, callback forgery, old and new store search | 47 pass |
| Live | 15 tests against WooCommerce 11.1.2: all tools over MCP, both auth paths, revoked key, replayed nonce, read-write key, stdio and HTTP transports, the approve and deny screens driven by a browser | 15 pass |
| Chaos, steady | 400 tool calls at 20 per second into a fake store failing 25% of requests nine different ways (one run; the fault mix is random) | 352 of 361 valid calls succeed; the 9 failures are HTML firewall pages, which are not retried by design |
| Chaos, overload | 400 tool calls at the same instant | every call returns within the 8 s budget (slowest 8.01 s); 0 crashes; peak 38 requests in any second against a configured ceiling of 50 |
| Live soak | 12 agents for 60 s, host limited to 5 per second, client set to 8 per second; three runs | 221 to 254 tool calls succeed per run and 0 rate-limit errors reach an agent; 21 to 22 host 429s absorbed per run; throughput settles at the host's ceiling, 5.2 to 5.3 requests per second |
| Pagination | Walk all 240 orders, 50 a page, while a new order is created between every page | 240 seen, each exactly once; none of the new orders leak in |

Three bugs were found by these runs and fixed: time spent queued for a token was not counted against the call budget (a call took 15.5 s on an 8 s budget); a malformed key in a forged callback crashed the listener thread instead of being rejected; and searching an order by full name ("Meera Pillai") returned nothing, because WooCommerce matches one field at a time.

## Assumptions

- The merchant runs WooCommerce 3.5 or later with the REST API on and pretty permalinks (`/wp-json/`). Tested on 11.1.2.
- Agent Studio speaks MCP, either launching the server over stdio or calling it over streamable HTTP. If it wants plain function-calling JSON instead, `docs/mcp-tools.json` already holds the name, description and JSON Schema per tool.
- One connector process serves one store. Multi-merchant hosting is one process per merchant, or a thin router in front.
- Read access is the job. The assignment asks to read orders and inventory, so the key is requested with `read` scope and the code only issues GET.

## Limitations, and the long-term fix

**Offset paging under deletes:** the cursor pins `before` to the time of the first page, which stops new orders shifting pages, but an order deleted mid-walk still shifts later pages by one. The fix is keyset paging on `id`, which WooCommerce's REST API does not offer; a merchant-side plugin endpoint would.

**Search is WooCommerce's search:** it is a SQL `LIKE` per field with no ranking. Recent WooCommerce matches products by name or SKU; older stores only by name, where the connector adds an exact SKU lookup. Order search cannot match a full name, so the connector falls back to the longest word and filters, but only across the first 100 orders that word matches. For large stores the fix is webhooks into an index the connector owns, which also removes most read traffic.

**Inventory is a scan:** `inventory_report` walks every product, up to 2,000, then says it stopped. Large catalogues need the `wc-analytics` stock report or the same webhook-fed index.

**OAuth 1.0a over http puts the consumer key in the query string,** where server access logs keep it, and each request writes a nonce to the store's database. Https with Basic avoids both; the connector prefers it whenever the store has TLS.

**Callback needs public https:** the approve-access flow needs a callback the store can reach. In Agent Studio that is a platform endpoint; on a laptop it means a tunnel. Pasting keys is the fallback.

**Rate limits are per process:** two connector processes on the same key each pace themselves and together can exceed the host limit. The fix is a shared limiter (Redis) when more than one runs.

**Masking is not access control:** `masked` hides contact details from the model, but whoever holds the key can read everything the key allows. Tenant isolation belongs in Agent Studio, one key per merchant.

## Layout

```
src/woo_connector/
  auth.py        OAuth 1.0a signing, Basic, credential file
  connect.py     approve-access flow: authorise URL, https callback listener, state check
  client.py      token bucket, cooldown, retries, budget, error mapping
  tools.py       list/get/search primitives, cursors, response shaping, PII masking
  server.py      MCP server (stdio and streamable HTTP)
  cli.py         connect | check | serve | tools-spec
dev/             docker compose WooCommerce, seed data, dev-only mu-plugins
tests/           unit tests and live tests (pytest -m live)
scripts/stress.py  chaos and soak runs
docs/            agent capabilities, MCP tool specification
```
