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
    return {
        "proxyWallet": WALLET,
        "asset": "123",
        "conditionId": "0x" + "c" * 64,
        "size": 20,
        "title": "Example market",
        "outcome": "Yes",
        "eventSlug": "example",
        "price": 0.4,
        "usdcSize": 8,
        "type": "TRADE",
        "side": "BUY",
        "timestamp": int(collector.time.time()) - 60,
        "redeemable": False,
        "avgPrice": 0.4,
        "curPrice": 0.5,
        "currentValue": 10,
        **overrides,
    }


def client_for(activity=None, positions=None, seeds=None):
    def handler(request):
        assert request.url.host == "data-api.polymarket.com"
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert request.url.params["offset"] == "0"
        if request.url.path == "/v1/leaderboard":
            assert request.url.params["orderBy"] == "VOL"
            return httpx.Response(
                200,
                json=seeds
                if seeds is not None
                else [{"proxyWallet": WALLET, "userName": "Test"}],
            )
        if request.url.path == "/activity":
            assert request.url.params["type"] == "TRADE"
            assert (
                int(request.url.params["end"]) - int(request.url.params["start"])
                == 7 * 86400
            )
            return httpx.Response(
                200, json=activity if activity is not None else [row()]
            )
        return httpx.Response(200, json=positions if positions is not None else [row()])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_bounded_observations_with_receipts_and_no_skill_claim():
    with client_for(activity=[row(asset=str(i + 1)) for i in range(100)]) as client:
        result = collector.Collector(client, wallet_limit=1).collect()
    wallet = result["wallets"][0]
    assert len(result["requests"]) == result["limits"]["max_requests"] == 3
    assert all(len(r["sha256"]) == 64 for r in result["requests"])
    assert wallet["evaluation_status"] == "not_evaluated"
    assert wallet["trades"]["possibly_truncated"] is True
    assert wallet["positions"]["possibly_truncated"] is False
    assert "skill_likelihood" not in json.dumps(result)


def test_invalid_rows_do_not_become_zeros_or_cross_wallet_evidence():
    with client_for(
        activity=[
            row(),
            row(proxyWallet=OTHER),
            row(price=-1),
            row(timestamp=1),
            row(timestamp=int(collector.time.time()) + 10000),
        ],
        positions=[row(avgPrice=None, currentValue=-2), row(), row(redeemable=True)],
    ) as client:
        result = collector.Collector(client).collect()
    wallet = result["wallets"][0]
    assert result["status"] == "partial"
    assert len(wallet["trades"]["rows"]) == 1
    assert wallet["trades"]["rows_rejected"] == 4
    assert wallet["positions"]["rows"][0]["average_price"] is None
    assert wallet["positions"]["rows"][0]["reported_value"] is None
    assert wallet["positions"]["rows_rejected"] == 2


def test_one_failed_source_is_unknown_not_empty_portfolio():
    def handler(request):
        if request.url.path == "/positions":
            return httpx.Response(429)
        return httpx.Response(
            200,
            json=[{"proxyWallet": WALLET}]
            if request.url.path == "/v1/leaderboard"
            else [row()],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = collector.Collector(client).collect()
    assert len(result["requests"]) == 3  # No retry on rate limit.
    assert result["status"] == "partial"
    assert result["wallets"][0]["positions"]["status"] == "unavailable"
    assert result["requests"][-1]["http_status"] == 429


@pytest.mark.parametrize(
    "body",
    [b"{}", b"[NaN]", b"x" * (collector.MAX_RESPONSE_BYTES + 1)],
    ids=["object", "nan", "oversized"],
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
            {"proxyWallet": WALLET},
            {"proxyWallet": WALLET},
            {"proxyWallet": "bad"},
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
