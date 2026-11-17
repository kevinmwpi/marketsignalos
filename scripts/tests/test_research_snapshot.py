import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "research_collector",
    Path(__file__).resolve().parents[1] / "collect_research_snapshot.py",
)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)
WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40


def row(**overrides):
    """One Data API v2 row carrying both a trade's fields and a holding's."""
    return {
        "proxy_wallet": WALLET,
        "token_id": "123",
        "condition_id": "0x" + "c" * 64,
        "size": 20,
        "current_size": 20,
        "title": "Example market",
        "outcome": "Yes",
        "event_slug": "example",
        "price": 0.4,
        "usdc_size": 8,
        "type": "TRADE",
        "side": "BUY",
        "timestamp": int(collector.time.time()) - 60,
        "transaction_hash": "0x" + "d" * 64,
        "redeemable": False,
        "avg_price": 0.4,
        "current_price": 0.5,
        "current_value": 10,
        **overrides,
    }


def listing(rows):
    """Data API v2 wraps every listing."""
    return {"data": rows, "pagination": {"has_more": False, "next_cursor": None}}


def client_for(activity=None, positions=None, seeds=None):
    def handler(request):
        assert request.url.host == "data-api.polymarket.com"
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "offset" not in request.url.params  # v2 answers 400 to it
        if request.url.path == "/v2/leaderboard":
            assert request.url.params["sort_by"] == "VOLUME"  # "VOL" is a 400 in v2
            assert request.url.params["time_period"] == "day"
            return httpx.Response(
                200,
                json=listing(
                    seeds
                    if seeds is not None
                    else [{"user_id": WALLET, "user_name": "Test"}]
                ),
            )
        if request.url.path == "/v2/activity":
            assert request.url.params["type"] == "TRADE"
            assert (
                int(request.url.params["end"]) - int(request.url.params["start"])
                == 7 * 86400
            )
            return httpx.Response(
                200, json=listing(activity if activity is not None else [row()])
            )
        assert request.url.path == "/v2/positions"
        assert request.url.params["sort_by"] == "CURRENT_VALUE"
        return httpx.Response(
            200, json=listing(positions if positions is not None else [row()])
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_bounded_observations_with_receipts_and_no_skill_claim():
    with client_for(activity=[row(token_id=str(i + 1)) for i in range(100)]) as client:
        result = collector.Collector(client, wallet_limit=1).collect()
    wallet = result["wallets"][0]
    assert len(result["requests"]) == result["limits"]["max_requests"] == 3
    assert [r["path"] for r in result["requests"]] == [
        "/v2/leaderboard", "/v2/activity", "/v2/positions"]
    assert result["status"] == "complete" and result["source_api"] == "v2"
    assert all(len(r["sha256"]) == 64 for r in result["requests"])
    assert wallet["evaluation_status"] == "not_evaluated"
    assert wallet["trades"]["possibly_truncated"] is True
    assert wallet["positions"]["possibly_truncated"] is False
    assert "skill_likelihood" not in json.dumps(result)


def test_the_published_rows_keep_their_v1_names():
    """The dashboard validates this file: v2's field names must not leak into it."""
    with client_for() as client:
        result = collector.Collector(client, wallet_limit=1).collect()
    wallet = result["wallets"][0]
    assert wallet["address"] == WALLET and wallet["name"] == "Test"
    assert wallet["trades"]["rows"] == [{
        "condition_id": "0x" + "c" * 64, "asset": "123", "title": "Example market",
        "outcome": "Yes", "event_slug": "example", "size": 20.0,
        "timestamp": wallet["trades"]["rows"][0]["timestamp"], "side": "BUY", "price": 0.4,
        "notional": 8.0, "transaction_hash": "0x" + "d" * 64,
    }]
    assert wallet["positions"]["rows"] == [{
        "condition_id": "0x" + "c" * 64, "asset": "123", "title": "Example market",
        "outcome": "Yes", "event_slug": "example", "size": 20.0,
        "average_price": 0.4, "reported_price": 0.5, "reported_value": 10.0,
    }]
    assert result["collector_version"] == "research-snapshot-v1"
    assert result["selection"] == "OVERALL / DAY / VOL, first page"


def test_combo_trades_and_redeemable_holdings_are_left_out_without_being_faults():
    """v2 serves both with no request filter to drop them. Neither was ever in
    this sample, and counting them as rejected would block every publication."""
    with client_for(
        activity=[row(), row(is_combo=True, condition_id="basket")],
        positions=[row(), row(token_id="456", redeemable=True)],
    ) as client:
        result = collector.Collector(client, wallet_limit=1).collect()
    wallet = result["wallets"][0]
    assert result["status"] == "complete"
    for kind in ("trades", "positions"):
        assert len(wallet[kind]["rows"]) == 1
        assert (wallet[kind]["rows_returned"], wallet[kind]["rows_rejected"],
                wallet[kind]["rows_excluded"]) == (2, 0, 1)


def test_holdings_are_read_past_the_resolved_ones_and_published_up_to_the_cap():
    """Sorted by value, unredeemed winners come first. v1 never listed them, so
    v2 is read deeper for the sample's 50 live holdings."""
    resolved = [row(token_id=str(i), redeemable=True) for i in range(120)]
    live = [row(token_id=str(1000 + i)) for i in range(70)]
    with client_for(positions=resolved + live) as client:
        result = collector.Collector(client, wallet_limit=1).collect()
    sample = result["wallets"][0]["positions"]
    assert result["requests"][-1]["params"]["limit"] == collector.POSITION_FETCH_LIMIT == 200
    assert [r["asset"] for r in sample["rows"]] == [str(1000 + i) for i in range(50)]
    assert (sample["rows_returned"], sample["rows_excluded"], sample["rows_rejected"]) == (
        190, 120, 0)
    assert sample["possibly_truncated"] is True  # 70 live holdings, 50 published
    assert result["limits"]["positions_per_wallet"] == 50 and result["status"] == "complete"


def test_a_v1_shaped_response_is_unavailable_not_empty():
    def handler(request):
        if request.url.path == "/v2/leaderboard":
            return httpx.Response(200, json=listing([{"user_id": WALLET}]))
        return httpx.Response(200, json=[row()])  # v1's bare list

    with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(
        ValueError, match="previous snapshot preserved"
    ):
        collector.Collector(client, wallet_limit=1).collect()


def test_invalid_rows_do_not_become_zeros_or_cross_wallet_evidence():
    with client_for(
        activity=[
            row(),
            row(proxy_wallet=OTHER),
            row(price=-1),
            row(timestamp=1),
            row(timestamp=int(collector.time.time()) + 10000),
        ],
        positions=[
            row(avg_price=None, current_value=-2),
            row(),  # the same asset again
            row(token_id="456", redeemable=None),  # redeemability unknown
            row(token_id="789", current_size=None),  # a null size is not zero
        ],
    ) as client:
        result = collector.Collector(client).collect()
    wallet = result["wallets"][0]
    assert result["status"] == "partial"
    assert len(wallet["trades"]["rows"]) == 1
    assert wallet["trades"]["rows_rejected"] == 4
    assert wallet["positions"]["rows"][0]["average_price"] is None
    assert wallet["positions"]["rows"][0]["reported_value"] is None
    assert len(wallet["positions"]["rows"]) == 1
    assert wallet["positions"]["rows_rejected"] == 3


def test_one_failed_source_is_unknown_not_empty_portfolio():
    def handler(request):
        if request.url.path == "/v2/positions":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json=listing(
                [{"user_id": WALLET}]
                if request.url.path == "/v2/leaderboard"
                else [row()]
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collector.Collector(client).collect()
    assert len(result["requests"]) == 3  # No retry on rate limit.
    assert result["status"] == "partial"
    assert result["wallets"][0]["positions"]["status"] == "unavailable"
    assert result["requests"][-1]["http_status"] == 429


@pytest.mark.parametrize(
    "body",
    [b"{}", b"[NaN]", b'{"data": {}}', b"x" * (collector.MAX_RESPONSE_BYTES + 1)],
    ids=["object", "nan", "data-not-a-list", "oversized"],
)
def test_invalid_or_oversized_source_cannot_replace_snapshot(tmp_path, body):
    output = tmp_path / "snapshot.json"
    output.write_text("previous capture")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body))
    ) as client, pytest.raises(ValueError, match="previous snapshot preserved"):
        collector.publish(collector.Collector(client).collect(), output)
    assert output.read_text() == "previous capture"


def test_deadline_prevents_additional_network_calls():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = collector.Collector(client)
        run.deadline = 0
        with pytest.raises(ValueError):
            run.collect()
    assert not calls


def test_wallet_dedupe_and_empty_sources():
    with client_for(
        seeds=[
            {"user_id": WALLET},
            {"user_id": WALLET},
            {"user_id": "bad"},
        ],
        positions=[],
    ) as client:
        result = collector.Collector(client).collect()
    assert len(result["wallets"]) == 1
    assert len(result["requests"]) == 3
    assert result["requests"][0]["rows_rejected"] == 2
    assert result["wallets"][0]["positions"]["status"] == "ok"


def test_output_safety_and_atomic_replacement(tmp_path, monkeypatch):
    output = tmp_path / "snapshot.json"
    collector.publish({"valid": 1}, output)
    with pytest.raises(ValueError):
        collector.publish({"invalid": float("nan")}, output)
    assert json.loads(output.read_text()) == {"valid": 1}

    def fail_replace(*_):
        raise OSError("simulated interruption")

    monkeypatch.setattr(collector.os, "replace", fail_replace)
    with pytest.raises(OSError):
        collector.publish({"valid": 2}, output)
    assert json.loads(output.read_text()) == {"valid": 1}
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(
    "value", [None, True, "0.5", float("inf"), float("nan"), -1, 1e20]
)
def test_unknown_or_invalid_numbers_are_not_zero(value):
    assert collector.number(value) is None
