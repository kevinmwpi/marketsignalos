from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from marketsignalos_polymarket.metadata_probe import probe_audit
from marketsignalos_polymarket.polymarket_client import PolymarketClient


def test_probe_frozen_candidates_without_touching_source(tmp_path: Path) -> None:
    audit = Path(__file__).resolve().parents[3] / "docs/benchmarks/2026-09-10-metadata-coverage.json"
    source_hash = hashlib.sha256(audit.read_bytes()).hexdigest()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=[])

    output = tmp_path / "isolated"
    with closing(PolymarketClient(client=httpx.Client(transport=httpx.MockTransport(handler)))) as api:
        result = probe_audit(audit, output, api)
        with pytest.raises(FileExistsError):
            probe_audit(audit, output, api)
    assert len(calls) == 4
    assert result["summary"] == {"conditions": 41, "returned_now": 0,
                                 "not_returned_either_filter": 41, "inconclusive": 0}
    assert len(result["candidate_wallets"]) == 2
    assert len(result["condition_observations"]) == 82
    assert result["source_audit"]["sha256"] == source_hash
    assert hashlib.sha256(audit.read_bytes()).hexdigest() == source_hash
    assert not (output / "observations.jsonl").exists()
    assert json.loads((output / "report.json").read_text()) == result


def test_probe_counts_one_return_and_preserves_raw_observation(tmp_path: Path) -> None:
    audit = tmp_path / "audit.json"
    cid = "0x" + "1" * 64
    audit.write_text(json.dumps({"schema_version": 1, "status": "passed", "wallets": [
        {"wallet": "wallet", "metadata_only_candidate": True, "missing_condition_ids": [cid]},
    ]}))

    def handler(request: httpx.Request) -> httpx.Response:
        payload = [{"id": "123", "conditionId": cid}] if request.url.params["closed"] == "true" else []
        return httpx.Response(200, json=payload)

    output = tmp_path / "probe"
    with closing(PolymarketClient(client=httpx.Client(transport=httpx.MockTransport(handler)))) as api:
        result = probe_audit(audit, output, api)
    assert result["summary"]["returned_now"] == 1
    observed = (output / "observations.jsonl").read_bytes()
    assert hashlib.sha256(observed).hexdigest() == result["observations_sha256"]
    assert json.loads(observed)["market"]["conditionId"] == cid
