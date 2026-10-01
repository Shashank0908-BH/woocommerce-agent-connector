"""Stress the connector two ways and print what happened.

chaos: a fake store that misbehaves on purpose (429 with and without Retry-After,
       500/502/503, stalls past the timeout, handler crashes, HTML challenge
       pages, truncated JSON) and also enforces its own rate limit. Hundreds of
       tool calls go through it, first as a steady stream, then all at once.
       Checked: every call ends in data or a known error code, no call outlives
       its time budget, the client never sends faster than its configured rate,
       and under steady load at least 85% of valid calls get through the faults.

live:  many simulated agents run a mix of tool calls against the dev store for a
       fixed time, with the host rate limit switched on. Reports latency and how
       many 429s the host sent versus how many reached an agent.

  uv run python scripts/stress.py chaos --calls 400
  uv run python scripts/stress.py live --agents 12 --seconds 60
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import itertools
import json
import random
import socket
import statistics
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from woo_connector.auth import Credentials
from woo_connector.client import WooClient, WooError
from woo_connector.tools import WooTools

KNOWN_CODES = {"busy", "rate_limited", "store_error", "store_unreachable", "unexpected_response", "not_found", "bad_request"}
ROOT = Path(__file__).resolve().parents[1]


# --- chaos store ---------------------------------------------------------------------

def fake_order(i: int) -> dict:
    return {"id": i, "number": str(i), "status": "processing", "currency": "INR", "total": "500.00",
            "date_created_gmt": "2026-09-30T10:00:00", "customer_id": 1,
            "billing": {"first_name": "Test", "last_name": "Buyer", "email": "t.b@example.test", "phone": "+91-9000000000"},
            "line_items": [{"product_id": 1, "name": "Masala Chai 250g", "quantity": 1, "total": "500.00"}]}


class ChaosStore:
    def __init__(self, fault_rate: float, host_limit: int):
        self.fault_rate = fault_rate
        self.host_limit = host_limit  # requests per second before the store itself 429s
        self.arrivals: list[float] = []
        self.faults = collections.Counter()
        self.lock = threading.Lock()

    def app(self) -> Starlette:
        async def handle(request: Request) -> Response:
            now = time.monotonic()
            with self.lock:
                self.arrivals.append(now)
                recent = sum(1 for t in self.arrivals[-200:] if now - t < 1.0)
            if recent > self.host_limit:
                self.faults["host_429"] += 1
                return JSONResponse({"code": "rate_limited"}, status_code=429, headers={"Retry-After": "1"})
            if random.random() < self.fault_rate:
                kind = random.choice(["429_ra", "429_bare", "500", "502", "503", "stall", "crash", "html", "truncated"])
                self.faults[kind] += 1
                if kind == "429_ra":
                    return JSONResponse({"code": "x"}, status_code=429, headers={"Retry-After": "0.3"})
                if kind == "429_bare":
                    return JSONResponse({"code": "x"}, status_code=429)
                if kind in ("500", "502", "503"):
                    return PlainTextResponse("upstream sad", status_code=int(kind))
                if kind == "stall":
                    await asyncio.sleep(3)  # past the client's 1s timeout
                if kind == "crash":
                    raise RuntimeError("handler crashed")  # an unhandled PHP fatal, as the client sees it
                if kind == "html":
                    return Response("<html>Checking your browser</html>", media_type="text/html")
                if kind == "truncated":
                    return Response('[{"id": 1, "sta', media_type="application/json")
            path = request.url.path
            if path.endswith("/orders"):
                page = int(request.query_params.get("page", 1))
                return JSONResponse([fake_order(page * 100 + i) for i in range(5)], headers={"X-WP-Total": "500", "X-WP-TotalPages": "100"})
            if "/orders/" in path:
                oid = int(path.rsplit("/", 1)[1])
                if oid > 10_000:
                    return JSONResponse({"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}, status_code=404)
                return JSONResponse(fake_order(oid))
            return JSONResponse({"code": "rest_no_route"}, status_code=404)

        return Starlette(routes=[Route("/{path:path}", handle)])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def run_chaos(calls: int, fault_rate: float, rate: float, burst: int, arrival_rate: float) -> int:
    store = ChaosStore(fault_rate, host_limit=int(rate * 1.5) + burst)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(store.app(), host="127.0.0.1", port=port, log_level="critical"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        await asyncio.sleep(0.05)

    budget = 8.0
    client = WooClient(f"http://127.0.0.1:{port}", Credentials("ck_chaos", "cs_chaos"), rate=rate, burst=burst,
                       max_concurrency=8, timeout=1.0, backoff_base=0.1, backoff_cap=2.0, call_budget=budget)
    tools = WooTools(client)
    crashes: list[str] = []

    async def one(i: int, delay: float, outcomes: collections.Counter, durations: list[float]) -> None:
        await asyncio.sleep(delay)
        start = time.monotonic()
        try:
            r = random.random()
            if r < 0.5:
                await tools.list_orders(page_size=5)
            elif r < 0.9:
                await tools.get_order(random.randint(1, 500))
            else:
                await tools.get_order(50_000 + i)  # a real 404
            outcomes["ok"] += 1
        except WooError as exc:
            outcomes[exc.code] += 1
            if exc.code not in KNOWN_CODES:
                crashes.append(f"unknown code {exc.code}: {exc}")
        except Exception as exc:  # anything here is a bug
            crashes.append(f"{type(exc).__name__}: {exc}")
        durations.append(time.monotonic() - start)

    phases = {}
    # Steady: calls arrive as a Poisson stream, the way several agents' tool calls would.
    # Overload: every call at the same instant; the point is back-pressure, not success.
    for name, delays in (
        ("steady", list(itertools.accumulate(random.expovariate(arrival_rate) for _ in range(calls)))),
        ("overload", [0.0] * calls),
    ):
        outcomes: collections.Counter = collections.Counter()
        durations: list[float] = []
        t0 = time.monotonic()
        await asyncio.gather(*(one(i, d, outcomes, durations) for i, d in enumerate(delays)))
        phases[name] = {"calls": calls, "wall_s": round(time.monotonic() - t0, 1), "outcomes": dict(outcomes),
                        "slowest_call_s": round(max(durations), 2)}
        await asyncio.sleep(1.5)  # let cooldowns lapse between phases
    await client.aclose()
    server.should_exit = True

    # Peak request rate the store actually saw, over any 1-second window.
    arr = sorted(store.arrivals)
    peak = max((sum(1 for u in arr[i:i + 400] if u - t < 1.0) for i, t in enumerate(arr)), default=0)
    steady = phases["steady"]["outcomes"]
    expected_404 = steady.get("not_found", 0)
    report = {
        "phases": phases, "faults_injected": dict(store.faults), "http_requests": len(arr), "client_stats": client.stats,
        "peak_requests_in_any_1s": peak, "allowed_peak": burst + rate, "budget_s": budget, "crashes": crashes[:10],
    }
    print(json.dumps(report, indent=2))
    failed = []
    if crashes:
        failed.append("calls ended in an unexpected exception")
    if any(p["slowest_call_s"] > budget + 0.5 for p in phases.values()):
        failed.append("a call outlived its time budget")
    if peak > burst + rate + 1:
        failed.append("client exceeded its configured rate")
    if steady.get("ok", 0) < (calls - expected_404) * 0.85:
        failed.append("under steady load fewer than 85% of valid calls got through the faults")
    print("RESULT:", "PASS" if not failed else "FAIL " + "; ".join(failed))
    return 1 if failed else 0


# --- live soak ---------------------------------------------------------------------------

def set_host_limit(rule: str | None) -> None:
    sqls = ["DELETE FROM wp_options WHERE option_name='connector_dev_rate_limit'"]
    if rule:
        sqls.append(f"INSERT INTO wp_options (option_name, option_value, autoload) VALUES ('connector_dev_rate_limit','{rule}','off')")
    for q in sqls:
        subprocess.run(["docker", "compose", "-f", str(ROOT / "dev/docker-compose.yml"), "exec", "-T", "db", "mariadb",
                        "-uwordpress", "-pwordpress", "wordpress", "-e", q], check=True, capture_output=True)


async def run_live(agents: int, seconds: float, host_rule: str, rate: float, burst: int) -> int:
    env = dict(line.split("=", 1) for line in (ROOT / "dev/.env").read_text().split() if "=" in line)
    set_host_limit(host_rule)
    client = WooClient(env["WOO_BASE_URL"], Credentials(env["WOO_CONSUMER_KEY"], env["WOO_CONSUMER_SECRET"]),
                       rate=rate, burst=burst, max_concurrency=6)
    tools = WooTools(client)
    latencies: dict[str, list[float]] = collections.defaultdict(list)
    errors = collections.Counter()
    crashes: list[str] = []
    names = ["Diya", "Kabir Nair", "zoya.khan@example.test", "Rohan", "#120", "Pillai"]

    async def agent(n: int) -> None:
        rnd = random.Random(n)
        end = time.monotonic() + seconds
        cursor = None
        while time.monotonic() < end:
            pick = rnd.random()
            start = time.monotonic()
            try:
                if pick < 0.3:
                    page = await tools.list_orders(status=rnd.choice([None, ["processing"], ["completed", "refunded"]]),
                                                   page_size=20, cursor=None)
                    cursor = page["next_cursor"]
                    name = "list_orders"
                elif pick < 0.5:
                    await tools.get_order(rnd.randint(1, 260), include_notes=rnd.random() < 0.3)
                    name = "get_order"
                elif pick < 0.7:
                    await tools.search_orders(rnd.choice(names), limit=10)
                    name = "search_orders"
                elif pick < 0.85:
                    await tools.search_products(rnd.choice(["chai", "green", "TEA-007", "mug"]))
                    name = "search_products"
                elif pick < 0.95:
                    await tools.get_product(sku=rnd.choice(["TEA-001", "MUG-KULHAD", "TEA-014"]))
                    name = "get_product"
                else:
                    await tools.inventory_report()
                    name = "inventory_report"
                latencies[name].append(time.monotonic() - start)
            except WooError as exc:
                errors[exc.code] += 1
            except Exception as exc:
                crashes.append(f"{type(exc).__name__}: {exc}")
        del cursor

    t0 = time.monotonic()
    try:
        await asyncio.gather(*(agent(i) for i in range(agents)))
    finally:
        set_host_limit(None)
        await client.aclose()
    wall = time.monotonic() - t0

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else 0.0

    total = sum(len(v) for v in latencies.values())
    report = {
        "agents": agents, "seconds": round(wall, 1), "host_limit": host_rule, "client_rate": rate,
        "tool_calls_ok": total, "tool_calls_failed": dict(errors), "crashes": crashes[:10],
        "http": client.stats,
        "latency_s": {k: {"n": len(v), "p50": pct(v, 0.5), "p95": pct(v, 0.95), "max": round(max(v), 2)}
                      for k, v in sorted(latencies.items())},
        "achieved_requests_per_s": round(client.stats["requests"] / wall, 2),
        "overall_p50_s": round(statistics.median([x for v in latencies.values() for x in v]), 2) if total else None,
    }
    print(json.dumps(report, indent=2))
    # Random order IDs land on gaps (WordPress shares one ID space between orders and
    # products), so not_found is a correct answer here, not a failure.
    unexpected = {k: v for k, v in errors.items() if k != "not_found"}
    ok = not crashes and not unexpected and total > 0
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    c = sub.add_parser("chaos")
    c.add_argument("--calls", type=int, default=400)
    c.add_argument("--fault-rate", type=float, default=0.25)
    c.add_argument("--rate", type=float, default=40)
    c.add_argument("--burst", type=int, default=10)
    c.add_argument("--arrival-rate", type=float, default=20, help="steady phase: tool calls per second")
    lv = sub.add_parser("live")
    lv.add_argument("--agents", type=int, default=12)
    lv.add_argument("--seconds", type=float, default=60)
    lv.add_argument("--host-limit", default="10/2")
    lv.add_argument("--rate", type=float, default=8)
    lv.add_argument("--burst", type=int, default=8)
    args = ap.parse_args()
    if args.mode == "chaos":
        return asyncio.run(run_chaos(args.calls, args.fault_rate, args.rate, args.burst, args.arrival_rate))
    return asyncio.run(run_live(args.agents, args.seconds, args.host_limit, args.rate, args.burst))


if __name__ == "__main__":
    raise SystemExit(main())
