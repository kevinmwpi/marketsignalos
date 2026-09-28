from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest

from marketsignalos_polymarket import metadata_backfill as backfill
from marketsignalos_polymarket.lean_pilot import WorkerBusy, worker_lock
from marketsignalos_polymarket.polymarket_client import PolymarketClient, PolymarketClientConfig
from marketsignalos_polymarket.runner import (
    _build_stores,
    parse_activity_row,
    run_markets_backfill_from_activity,
)

NOW = 1_800_000_000.0


def client(handler: Callable[[httpx.Request], httpx.Response]) -> PolymarketClient:
    return PolymarketClient(
        config=PolymarketClientConfig(max_retries=5),
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
    )


def rows(directory: Path, table: str) -> list[dict[str, Any]]:
    assert table in {"attempts", "conditions"}
    with closing(sqlite3.connect(directory / "attempts.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]


def test_caps_actual_attempts_and_restarts_fairly(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[])

    ids = [f"c{i:03}" for i in range(150)]
    with closing(client(handler)) as upstream:
        first = backfill.run_backfill(upstream, ids, directory=tmp_path, persist=len,
                                      clock=lambda: NOW)
        assert first["requests"] == len(calls) == 8
        assert first["attempted_lookups"] == 200
        assert first["deferred_lookups"] == 100
        assert first["status"] == "partial"
        first_ids = {cid for req in calls for cid in req.url.params.get_list("condition_ids")}
        assert len(first_ids) == 100
        calls.clear()
        second = backfill.run_backfill(upstream, ids, directory=tmp_path, persist=len,
                                       clock=lambda: NOW + 10)
        assert second["requests"] == len(calls) == 4
        assert second["cooldown_lookups"] == 200
        assert first_ids.isdisjoint(cid for req in calls
                                    for cid in req.url.params.get_list("condition_ids"))


def test_tiny_budget_does_not_starve_open_filter(tmp_path: Path) -> None:
    sides: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sides.append(request.url.params["closed"])
        return httpx.Response(200, json=[{"id": "1", "conditionId": "c"}])

    with closing(client(handler)) as upstream:
        for now in (NOW, NOW + 3601):
            backfill.run_backfill(upstream, ["c"], directory=tmp_path, persist=len,
                                  config=backfill.BackfillConfig(max_requests=1),
                                  clock=partial(float, now))
    assert sides == ["true", "false"]


@pytest.mark.parametrize("retry_header", ["7200", "Fri, 15 Jan 2027 10:00:00 GMT"])
def test_429_stops_without_sleep_and_survives_restart(tmp_path: Path, retry_header: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_header})

    with closing(client(handler)) as upstream:
        result = backfill.run_backfill(upstream, ["a", "b"], directory=tmp_path,
                                      persist=len, clock=lambda: NOW)
        assert result["requests"] == len(calls) == 1  # configured retries=5 are bypassed
        assert result["outcomes"] == {"http_error": 1}
        assert result["upstream_retry_at"] >= NOW + 7200
        backfill.run_backfill(upstream, ["new"], directory=tmp_path,
                              persist=len, clock=lambda: NOW + 1)
        assert len(calls) == 1  # cooldown is endpoint-wide, even for an untouched ID
    assert rows(tmp_path, "attempts")[0]["http_status"] == 429


@pytest.mark.parametrize("status", [302, 404, 503])
def test_http_failure_is_a_batch_result_and_redirects_are_not_followed(
    tmp_path: Path, status: int,
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://example.org/other"})

    with closing(client(handler)) as upstream:
        result = backfill.run_backfill(upstream, ["a", "b"], directory=tmp_path,
                                      persist=len, clock=lambda: NOW)
    assert len(calls) == result["requests"] == 1
    assert all(row["outcome"] == "http_error" for row in rows(tmp_path, "conditions"))
    assert rows(tmp_path, "attempts")[0]["http_status"] == status


def test_transport_failure_has_no_hidden_retries(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with closing(client(handler)) as upstream:
        result = backfill.run_backfill(upstream, ["a"], directory=tmp_path,
                                      persist=len, clock=lambda: NOW)
    assert calls == 1
    assert result["outcomes"] == {"transport_error": 1}


@pytest.mark.parametrize("payload,expected", [
    ({"unexpected": True}, "invalid_response"),
    (["bad"], "invalid_response"),
    ([{"id": "1", "conditionId": "unrequested"}], "invalid_response"),
    ([{"conditionId": "a"}], "invalid_response"),
    ([{"id": "1", "conditionId": "a"}] * 100, "response_limit"),
])
def test_bad_or_saturated_response_never_becomes_absence_or_storage(
    tmp_path: Path, payload: Any, expected: str,
) -> None:
    def persist(_: list[dict[str, Any]]) -> int:
        pytest.fail("Invalid or saturated response must not be persisted")

    with closing(client(lambda _: httpx.Response(200, json=payload))) as upstream:
        result = backfill.run_backfill(upstream, ["a"], directory=tmp_path,
                                      persist=persist, clock=lambda: NOW)
    assert result["outcomes"] == {expected: 2}
    assert result["not_returned_lookups"] == result["stored_lookups"] == 0
    assert all(row["outcome"] == expected for row in rows(tmp_path, "conditions"))


def test_persist_once_then_distinguish_stored_from_not_returned(tmp_path: Path) -> None:
    payload = [{"id": "1", "conditionId": "a"}]
    writes: list[list[dict[str, Any]]] = []

    def persist(data: list[dict[str, Any]]) -> int:
        assert all(row["outcome"] == "received" for row in rows(tmp_path, "attempts"))
        writes.append(data)
        return len(data)

    with closing(client(lambda _: httpx.Response(200, json=payload))) as upstream:
        result = backfill.run_backfill(upstream, ["a", "b"], directory=tmp_path,
                                      persist=persist, clock=lambda: NOW)
    assert writes == [payload * 2]
    assert result["rows_written"] == result["stored_lookups"] == 2
    assert result["not_returned_lookups"] == 2
    assert result["outcomes"] == {"recorded": 2}
    assert all(len(row["response_sha256"]) == 64 for row in rows(tmp_path, "attempts"))
    states = rows(tmp_path, "conditions")
    assert {r["next_retry_at"] for r in states if r["condition_id"] == "b"} == {NOW + 86400}
    assert {r["outcome"] for r in states if r["condition_id"] == "b"} == {"not_returned"}


@pytest.mark.parametrize("mode", ["exception", "short_write", "interrupt"])
def test_storage_failure_and_crash_do_not_claim_success(tmp_path: Path, mode: str) -> None:
    def persist(_: list[dict[str, Any]]) -> int:
        if mode == "exception":
            raise OSError("disk full")
        if mode == "interrupt":
            raise KeyboardInterrupt
        return 0

    with closing(client(lambda _: httpx.Response(200, json=[
        {"id": "1", "conditionId": "a"},
    ]))) as upstream:
        with pytest.raises((OSError, ValueError, KeyboardInterrupt)):
            backfill.run_backfill(upstream, ["a"], directory=tmp_path,
                                  persist=persist, clock=lambda: NOW)
        if mode != "interrupt":
            assert json.loads((tmp_path / "latest.json").read_text())["status"] == "storage_error"
        # A restart quarantines uncertain writes without claiming they were stored.
        result = backfill.run_backfill(upstream, ["a"], directory=tmp_path,
                                      persist=len, clock=lambda: NOW + 1)
    assert result["requests"] == 0
    expected = "interrupted" if mode == "interrupt" else "storage_error"
    assert {row["outcome"] for row in rows(tmp_path, "conditions")} == {expected}


def test_request_reservation_survives_transport_interrupt(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        assert rows(tmp_path, "attempts")[0]["outcome"] == "started"
        raise KeyboardInterrupt

    with closing(client(handler)) as upstream, pytest.raises(KeyboardInterrupt):
        backfill.run_backfill(upstream, ["a"], directory=tmp_path,
                              persist=len, clock=lambda: NOW)
    with closing(client(lambda _: httpx.Response(200, json=[]))) as upstream:
        backfill.run_backfill(upstream, [], directory=tmp_path, persist=len, clock=lambda: NOW + 1)
    assert rows(tmp_path, "attempts")[0]["outcome"] == "interrupted"


def test_lock_and_unknown_schema_fail_before_network(tmp_path: Path) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("Must fail before network")

    with closing(client(handler)) as upstream:
        with worker_lock(tmp_path / "backfill.lock"), pytest.raises(WorkerBusy):
            backfill.run_backfill(upstream, ["a"], directory=tmp_path, persist=len)
        with closing(sqlite3.connect(tmp_path / "attempts.sqlite3")) as db:
            db.execute("PRAGMA user_version=99")
        with pytest.raises(ValueError, match="version"):
            backfill.run_backfill(upstream, ["a"], directory=tmp_path, persist=len)


def test_receipt_retention_and_cooldown_survive_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backfill, "RECEIPT_LIMIT", 2)
    with closing(client(lambda _: httpx.Response(200, json=[]))) as upstream:
        for index in range(3):
            backfill.run_backfill(upstream, [str(index)], directory=tmp_path,
                                  persist=len, clock=lambda: NOW)
        result = backfill.run_backfill(upstream, ["0"], directory=tmp_path,
                                      persist=len, clock=lambda: NOW + 1)
    assert len(rows(tmp_path, "attempts")) == 2
    assert len(rows(tmp_path, "conditions")) == 6
    assert result["requests"] == 0


def test_runner_uses_budget_and_persists_partial_receipt(tmp_path: Path) -> None:
    stores = _build_stores(tmp_path)
    stores.activity.write_activity([parse_activity_row({
        "proxyWallet": "wallet", "conditionId": "a", "timestamp": 1000, "type": "TRADE",
    })])
    with closing(client(lambda _: httpx.Response(200, json=[]))) as upstream:
        assert run_markets_backfill_from_activity(
            upstream, stores, config=backfill.BackfillConfig(max_requests=1),
        ) == 0
    report = json.loads((tmp_path / "metadata-backfill" / "latest.json").read_text())
    assert report["requests"] == report["deferred_lookups"] == 1
    assert report["status"] == "partial"


@pytest.mark.parametrize("settings", [
    {"max_requests": 0}, {"max_requests": 9}, {"max_conditions": 101},
    {"batch_size": 26}, {"max_requests": True},
])
def test_config_rejects_excess_budget(settings: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        backfill.BackfillConfig(**settings)
