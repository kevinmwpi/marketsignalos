"""Read-only probe: can Polymarket trades be read straight from Polygon, and at what cost?

Polymarket settles every order-book fill on Polygon as an ``OrderFilled`` event.
Reading those events directly would make wallet discovery complete (every trader,
not only leaderboard names) and remove the Data API's pagination caps. This probe
measures, on live public infrastructure, what that would take:

  1. which public RPC endpoints answer, and how large a block range one
     ``eth_getLogs`` call accepts before the node refuses;
  2. how many fills per block the V1 exchanges (2022 to April 2026) and the V2
     exchanges (April 2026 on) emit, to size a full-history backfill;
  3. whether fills decoded from transaction receipts match the same trades as
     reported by the Data API (wallet, token, side, size, price);
  4. whether the Goldsky orderbook subgraph this repository already queries still
     receives trades after the April 2026 move to the V2 exchanges;
  5. how wide a block range one ``eth_getLogs`` call accepts when it is filtered to a
     single wallet as maker, which decides whether one wallet's full V2 history is
     a handful of calls or thousands.

Event layouts come from Polymarket's source, not memory:
  V1  ctf-exchange  src/exchange/interfaces/ITrading.sol
      OrderFilled(bytes32 indexed orderHash, address indexed maker, address indexed taker,
                  uint256 makerAssetId, uint256 takerAssetId, uint256 makerAmountFilled,
                  uint256 takerAmountFilled, uint256 fee)
  V2  ctf-exchange-v2 @ ccc0596  src/exchange/mixins/Events.sol
      OrderFilled(bytes32 orderHash [t1], address maker [t2], address taker [t3], uint8 side,
                  uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled,
                  uint256 fee, bytes32 builder, bytes32 metadata)      side: 0 BUY, 1 SELL
Topic hashes are computed by the node (``web3_sha3``) after it reproduces the
well-known ERC-20 Transfer topic, so no Keccak code is hand-written here.

Never writes any store. Prints a summary and the full JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx

RPC_CANDIDATES = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
)
EXCHANGES = {
    "v1_ctf": "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "v1_negrisk": "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "v2_ctf": "0xe111180000d2663c0091e4f400237545b87b996b",
    "v2_negrisk": "0xe2222d279d744050d28e00520010520000310f59",
}
V1_SIGNATURE = "OrderFilled(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)"
V2_SIGNATURE = (
    "OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,bytes32,bytes32)"
)
TRANSFER_SIGNATURE = "Transfer(address,address,uint256)"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
RANGES = (100, 500, 1000, 2000, 5000, 10000)
WALLET_RANGES = (5_000, 20_000, 100_000, 500_000, 2_000_000, 10_000_000)
WALLETS_TO_TRACE = 3
V1_SAMPLE_DATE = datetime(2025, 6, 2, tzinfo=UTC)
DATA_API = "https://data-api.polymarket.com"
GOLDSKY_ORDERBOOK = (
    "https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/"
    "subgraphs/polymarket-orderbook-resync/prod/gn"
)
V2_LAUNCH = datetime(2026, 4, 1, tzinfo=UTC)
SPACING_SECONDS = 0.15
AMOUNT_SCALE = 1_000_000  # collateral and outcome tokens both use 6 decimals


# ── Pure decoding (unit-tested) ──────────────────────────────────────────────

def words(data: str) -> list[int]:
    raw = data.removeprefix("0x")
    return [int(raw[i:i + 64], 16) for i in range(0, len(raw), 64) if raw[i:i + 64]]


def topic_address(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def address_topic(address: str) -> str:
    """An address left-padded to 32 bytes, as it appears in an indexed topic."""
    return "0x" + address.lower().removeprefix("0x").rjust(64, "0")


def decode_fill(log: dict[str, Any], v1_topic: str, v2_topic: str) -> dict[str, Any] | None:
    """One OrderFilled log -> {version, maker, taker, side, token_id, shares, price}.

    Prices are collateral per share. In V1 the side is implied by which asset id is
    zero (collateral): maker gives collateral means a maker BUY of the other token.
    """
    topics = [t.lower() for t in log.get("topics", [])]
    if len(topics) != 4:
        return None
    maker, taker = topic_address(topics[2]), topic_address(topics[3])
    w = words(log.get("data", "0x"))
    if topics[0] == v2_topic and len(w) == 7:
        side_raw, token_id, maker_amount, taker_amount, fee = w[0], w[1], w[2], w[3], w[4]
        if side_raw == 0:  # maker BUY: pays collateral, receives shares
            collateral, shares = maker_amount, taker_amount
        else:  # maker SELL: gives shares, receives collateral
            collateral, shares = taker_amount, maker_amount
        side = "BUY" if side_raw == 0 else "SELL"
        version = "v2"
    elif topics[0] == v1_topic and len(w) == 5:
        maker_asset, taker_asset, maker_amount, taker_amount, fee = w
        if maker_asset == 0:
            side, token_id, collateral, shares = "BUY", taker_asset, maker_amount, taker_amount
        else:
            side, token_id, collateral, shares = "SELL", maker_asset, taker_amount, maker_amount
        version = "v1"
    else:
        return None
    return {
        "version": version, "maker": maker, "taker": taker, "side": side,
        "token_id": str(token_id), "shares": shares / AMOUNT_SCALE,
        "price": round(collateral / shares, 6) if shares else None,
        "fee": fee / AMOUNT_SCALE,
        "tx": str(log.get("transactionHash", "")).lower(),
        "block": int(str(log.get("blockNumber", "0x0")), 16),
    }


def match_trade(trade: dict[str, Any], fills: list[dict[str, Any]]) -> dict[str, Any]:
    """How well the decoded fills of a transaction explain one Data API trade."""
    wallet = str(trade.get("proxyWallet", "")).lower()
    token = str(trade.get("asset", ""))
    size, price = float(trade.get("size") or 0), float(trade.get("price") or 0)
    same_token = [f for f in fills if f["token_id"] == token]
    with_wallet = [f for f in same_token if wallet in (f["maker"], f["taker"])]
    as_maker = [f for f in with_wallet if f["maker"] == wallet]
    # The Data API may report one side of a match or an aggregate; accept a fill,
    # or the sum of the wallet's fills, within 1% on size and 1 cent on price.
    shares = sum(f["shares"] for f in as_maker) if as_maker else 0.0
    exact = any(abs(f["shares"] - size) <= max(1e-6, 0.01 * size)
                and f["price"] is not None and abs(f["price"] - price) <= 0.01 for f in with_wallet)
    summed = bool(as_maker) and abs(shares - size) <= max(1e-6, 0.01 * size)
    return {
        "fills_in_tx": len(fills), "token_matches": len(same_token),
        "wallet_in_fill": bool(with_wallet), "wallet_as_maker": bool(as_maker),
        "size_price_match": exact or summed,
    }


# ── Network ──────────────────────────────────────────────────────────────────

class Rpc:
    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client, self.url, self.calls, self.errors = client, url, 0, 0

    def call(self, method: str, params: list[Any]) -> tuple[Any, str | None, float, int]:
        """(result, error, seconds, response bytes)."""
        time.sleep(SPACING_SECONDS)
        self.calls += 1
        started = time.monotonic()
        try:
            response = self.client.post(self.url, json={
                "jsonrpc": "2.0", "id": self.calls, "method": method, "params": params,
            })
            elapsed = time.monotonic() - started
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.errors += 1
            return None, f"{type(exc).__name__}: {str(exc)[:160]}", time.monotonic() - started, 0
        if "error" in body:
            self.errors += 1
            return None, json.dumps(body["error"])[:200], elapsed, len(response.content)
        return body.get("result"), None, elapsed, len(response.content)


def keccak_via_node(rpc: Rpc, text: str) -> str | None:
    result, _, _, _ = rpc.call("web3_sha3", ["0x" + text.encode().hex()])
    return str(result).lower() if isinstance(result, str) else None


def choose_rpc(client: httpx.Client) -> tuple[Rpc | None, list[dict[str, Any]]]:
    tried = []
    chosen = None
    for url in RPC_CANDIDATES:
        rpc = Rpc(client, url)
        head, error, seconds, _ = rpc.call("eth_blockNumber", [])
        sha = keccak_via_node(rpc, TRANSFER_SIGNATURE) if head else None
        tried.append({"url": url, "head": int(head, 16) if head else None, "error": error,
                      "seconds": round(seconds, 3), "web3_sha3_verified": sha == TRANSFER_TOPIC})
        if head and sha == TRANSFER_TOPIC and chosen is None:
            chosen = rpc
    return chosen, tried


def block_at(rpc: Rpc, target_ts: int, head: int) -> int:
    """Binary-search the first block at or after ``target_ts``."""
    low, high = 1, head
    while low < high:
        mid = (low + high) // 2
        block, _, _, _ = rpc.call("eth_getBlockByNumber", [hex(mid), False])
        ts = int(block["timestamp"], 16) if isinstance(block, dict) else 0
        if ts < target_ts:
            low = mid + 1
        else:
            high = mid
    return low


def range_limits(rpc: Rpc, head: int, addresses: list[str], topic: str) -> list[dict[str, Any]]:
    rows = []
    for size in RANGES:
        start = head - 20 - size
        result, error, seconds, nbytes = rpc.call("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(start + size - 1),
            "address": addresses, "topics": [topic],
        }])
        rows.append({"blocks": size, "ok": error is None,
                     "logs": len(result) if isinstance(result, list) else None,
                     "seconds": round(seconds, 3), "bytes": nbytes, "error": error})
        if error:
            break
    return rows


def density(rpc: Rpc, start: int, blocks: int, addresses: list[str],
            topic: str) -> dict[str, Any]:
    result, error, seconds, nbytes = rpc.call("eth_getLogs", [{
        "fromBlock": hex(start), "toBlock": hex(start + blocks - 1),
        "address": addresses, "topics": [topic],
    }])
    logs = result if isinstance(result, list) else []
    return {"start_block": start, "blocks": blocks, "logs": len(logs),
            "logs_per_block": round(len(logs) / blocks, 3), "bytes": nbytes,
            "seconds": round(seconds, 3), "error": error, "sample": logs[:3]}


def wallet_ranges(rpc: Rpc, head: int, wallet: str, addresses: list[str],
                  topic: str) -> list[dict[str, Any]]:
    """Widening ranges ending near the head, filtered to ``wallet`` as maker (topic 2)."""
    rows = []
    end = head - 20
    for size in WALLET_RANGES:
        result, error, seconds, nbytes = rpc.call("eth_getLogs", [{
            "fromBlock": hex(max(1, end - size + 1)), "toBlock": hex(end),
            "address": addresses, "topics": [topic, None, address_topic(wallet)],
        }])
        rows.append({"blocks": size, "ok": error is None,
                     "logs": len(result) if isinstance(result, list) else None,
                     "seconds": round(seconds, 3), "bytes": nbytes, "error": error})
        if error:
            break
    return rows


def cross_check(client: httpx.Client, rpc: Rpc, v1: str, v2: str) -> dict[str, Any]:
    response = client.get(f"{DATA_API}/trades", params={"limit": 40})
    trades = response.json() if response.is_success else []
    trades = trades if isinstance(trades, list) else []
    keys = sorted(trades[0]) if trades else []
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for trade in trades:
        tx = str(trade.get("transactionHash", "")).lower()
        if not tx or tx in seen or len(results) >= 15:
            continue
        seen.add(tx)
        receipt, error, _, _ = rpc.call("eth_getTransactionReceipt", [tx])
        logs = receipt.get("logs", []) if isinstance(receipt, dict) else []
        fills = [f for log in logs
                 if str(log.get("address", "")).lower() in EXCHANGES.values()
                 and (f := decode_fill(log, v1, v2))]
        results.append({"tx": tx, "error": error, "trade": {
            k: trade.get(k) for k in ("proxyWallet", "side", "asset", "size", "price")},
            **match_trade(trade, fills), "versions": sorted({f["version"] for f in fills})})
    return {"data_api_status": response.status_code, "trade_keys": keys, "checked": results}


def subgraph_freshness(client: httpx.Client) -> dict[str, Any]:
    query = ("{ orderFilledEvents(first: 1, orderBy: timestamp, orderDirection: desc) "
             "{ timestamp transactionHash } }")
    try:
        response = client.post(GOLDSKY_ORDERBOOK, json={"query": query})
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    events = (body.get("data") or {}).get("orderFilledEvents") or []
    latest = int(events[0]["timestamp"]) if events else None
    return {
        "status": response.status_code, "errors": body.get("errors"),
        "latest_fill": datetime.fromtimestamp(latest, UTC).isoformat() if latest else None,
        "covers_v2_era": bool(latest and latest >= int(V2_LAUNCH.timestamp())),
    }


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    report: dict[str, Any] = {"schema_version": 1, "started_at": datetime.now(UTC).isoformat()}
    headers = {"User-Agent": "MarketSignalOS-polygon-probe/0.1"}
    with httpx.Client(timeout=30.0, headers=headers) as client:
        rpc, tried = choose_rpc(client)
        report["rpc_candidates"] = tried
        report["subgraph"] = subgraph_freshness(client)
        if rpc is None:
            report["error"] = "no public RPC answered with a verified web3_sha3"
        else:
            report["rpc"] = rpc.url
            head = int(rpc.call("eth_blockNumber", [])[0], 16)
            v1 = keccak_via_node(rpc, V1_SIGNATURE) or ""
            v2 = keccak_via_node(rpc, V2_SIGNATURE) or ""
            report["topics"] = {"v1_order_filled": v1, "v2_order_filled": v2}
            v2_addresses = [EXCHANGES["v2_ctf"], EXCHANGES["v2_negrisk"]]
            v1_addresses = [EXCHANGES["v1_ctf"], EXCHANGES["v1_negrisk"]]
            report["head_block"] = head
            report["range_limits_v2"] = range_limits(rpc, head, v2_addresses, v2)
            report["density_v2_recent"] = density(rpc, head - 520, 500, v2_addresses, v2)
            v1_start = block_at(rpc, int(V1_SAMPLE_DATE.timestamp()), head)
            report["density_v1_2025"] = density(rpc, v1_start, 500, v1_addresses, v1)
            report["v1_after_v2_launch"] = density(rpc, head - 520, 500, v1_addresses, v1)
            report["cross_check"] = cross_check(client, rpc, v1, v2)
            report["v2_launch_block"] = block_at(rpc, int(V2_LAUNCH.timestamp()), head)
            wallets = list(dict.fromkeys(
                str(c["trade"]["proxyWallet"]).lower()
                for c in report["cross_check"]["checked"] if c["trade"].get("proxyWallet")
            ))[:WALLETS_TO_TRACE]
            report["wallet_ranges_v2"] = {
                wallet: wallet_ranges(rpc, head, wallet, v2_addresses, v2) for wallet in wallets
            }
            head_block, _, _, _ = rpc.call("eth_getBlockByNumber", [hex(head), False])
            old_block, _, _, _ = rpc.call("eth_getBlockByNumber", [hex(head - 43_200), False])
            if isinstance(head_block, dict) and isinstance(old_block, dict):
                span = int(head_block["timestamp"], 16) - int(old_block["timestamp"], 16)
                report["seconds_per_block"] = round(span / 43_200, 4)
            report["rpc_calls"], report["rpc_errors"] = rpc.calls, rpc.errors
    report["finished_at"] = datetime.now(UTC).isoformat()
    sys.stdout.write(summarize(report) + "\n")
    sys.stdout.write("PROBE_JSON_BEGIN\n" + json.dumps(report) + "\nPROBE_JSON_END\n")
    return 0 if "rpc" in report else 1


def summarize(report: dict[str, Any]) -> str:
    lines = [(f"RPC: {report.get('rpc')}  head={report.get('head_block')}  "
              f"s/block={report.get('seconds_per_block')}"),
             f"topics: {report.get('topics')}", f"subgraph: {report.get('subgraph')}"]
    for row in report.get("range_limits_v2", []):
        lines.append(f"range {row['blocks']:>6}: ok={row['ok']} logs={row['logs']} "
                     f"bytes={row['bytes']} s={row['seconds']} err={row['error']}")
    for key in ("density_v2_recent", "density_v1_2025", "v1_after_v2_launch"):
        d = report.get(key) or {}
        lines.append(f"{key}: {d.get('logs')} logs / {d.get('blocks')} blocks = "
                     f"{d.get('logs_per_block')}/block err={d.get('error')}")
    lines.append(f"v2_launch_block: {report.get('v2_launch_block')}")
    for wallet, rows in (report.get("wallet_ranges_v2") or {}).items():
        for row in rows:
            lines.append(f"wallet {wallet[:10]} range {row['blocks']:>8}: ok={row['ok']} "
                         f"logs={row['logs']} bytes={row['bytes']} s={row['seconds']} "
                         f"err={row['error']}")
    checked = (report.get("cross_check") or {}).get("checked", [])
    for key in ("wallet_in_fill", "wallet_as_maker", "size_price_match"):
        lines.append(f"cross-check {key}: {sum(bool(c[key]) for c in checked)}/{len(checked)}")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
