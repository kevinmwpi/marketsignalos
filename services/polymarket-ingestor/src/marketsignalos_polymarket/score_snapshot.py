"""Publish validated, immutable local score generations with a pointer swap.

The caller must hold the exclusive collector/scorer lock for the entire call.
These two derived files are not a complete serving snapshot: positions, market
metadata and their freshness still need their own publication contract. Failed
or interrupted generations are retained for diagnosis; readers follow only
current.json, never the newest directory. No network or database writes occur.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import PolymarketWalletBet, PolymarketWalletEnrichment
from .runner import _build_stores, run_enrichment
from .storage import JsonlEnrichmentStore, JsonlWalletBetStore

INPUTS = (
    "polymarket_activity.jsonl",
    "polymarket_markets.jsonl",
    "polymarket_leaderboard.jsonl",
    "polymarket_wallet_hydration.jsonl",
    "polymarket_price_snapshots.jsonl",
)
ENRICHMENT = "polymarket_wallet_enrichment.jsonl"
BETS = "polymarket_wallet_bets.jsonl"
OUTPUTS = {ENRICHMENT: PolymarketWalletEnrichment, BETS: PolymarketWalletBet}


class _CountedBetStore(JsonlWalletBetStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.rows_written = 0

    @contextmanager
    def rewrite(self) -> Iterator[Callable[[list[PolymarketWalletBet]], int]]:
        self.rows_written = 0
        with super().rewrite() as write_batch:
            def counted(batch: list[PolymarketWalletBet]) -> int:
                written = write_batch(batch)
                self.rows_written += written
                return written

            yield counted


def _validate_run_id(run_id: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", run_id) is None:
        raise ValueError("run_id must be 1-128 letters, digits, underscores or hyphens")


def _input_inventory(data_dir: Path) -> dict[str, Any]:
    inventory: dict[str, Any] = {}
    for name in INPUTS:
        path = data_dir / name
        try:
            stat = path.stat()
        except FileNotFoundError:
            inventory[name] = {"exists": False}
            continue
        if not path.is_file():
            raise ValueError(f"Scoring input is not a regular file: {name}")
        inventory[name] = {"exists": True, "bytes": stat.st_size,
                           "mtime_ns": stat.st_mtime_ns, "inode": stat.st_ino}
    return inventory


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite number in scoring output")
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _validate_output(path: Path) -> dict[str, Any]:
    required = {field.name for field in fields(OUTPUTS[path.name])}
    digest = hashlib.sha256()
    count = 0
    size = 0
    versions: set[str] = set()
    wallets: set[str] = set()
    with path.open("rb") as handle:
        for count, raw in enumerate(handle, 1):
            digest.update(raw)
            size += len(raw)
            row = json.loads(raw, parse_float=_finite_float, parse_constant=_invalid_constant)
            if not isinstance(row, dict) or not required.issubset(row):
                raise ValueError(f"Invalid scoring record: {path.name}:{count}")
            wallet = row["proxy_wallet"]
            if not isinstance(wallet, str) or not wallet:
                raise ValueError(f"Missing wallet: {path.name}:{count}")
            computed_at = row["computed_at"]
            if (not isinstance(computed_at, str)
                    or datetime.fromisoformat(computed_at).utcoffset() is None):
                raise ValueError(f"Scoring record has no timezone: {path.name}:{count}")
            if path.name == ENRICHMENT:
                if wallet.lower() in wallets:
                    raise ValueError(f"Duplicate wallet enrichment: {wallet}")
                wallets.add(wallet.lower())
                version = row["score_version"]
                if not isinstance(version, str) or not version:
                    raise ValueError("Scoring record has no score_version")
                versions.add(version)
    result: dict[str, Any] = {"rows": count, "bytes": size, "sha256": digest.hexdigest()}
    if path.name == ENRICHMENT:
        result["score_versions"] = sorted(versions)
    return result


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_json_atomic(path: Path, payload: dict[str, Any], run_id: str) -> str:
    raw = (json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{run_id}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def score_snapshot(data_dir: Path, snapshots_dir: Path, run_id: str) -> dict[str, Any]:
    """Score local inputs, validate the generation, and atomically publish it.

    run_id must be new. Input size/mtime/inode checks detect ordinary concurrent
    changes, not adversarial same-stat rewrites; the caller's lock is required.
    Missing context files are recorded explicitly and retain the scorer's
    untrusted-data behavior. An activity file must exist, even if empty.
    """
    _validate_run_id(run_id)
    data_dir = data_dir.resolve(strict=True)
    snapshots_dir = snapshots_dir.resolve()
    inventory = _input_inventory(data_dir)
    if not inventory[INPUTS[0]]["exists"]:
        raise ValueError("Cannot publish scores without an activity input file")
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    generation = snapshots_dir / run_id
    generation.mkdir(exist_ok=False)
    started_at = datetime.now(UTC).isoformat()
    code_digest = hashlib.sha256()
    for source in sorted(Path(__file__).parent.glob("*.py")):
        code_digest.update(source.name.encode())
        code_digest.update(source.read_bytes())

    stores = _build_stores(data_dir)
    stores.enrichment = JsonlEnrichmentStore(generation / ENRICHMENT)
    bet_store = _CountedBetStore(generation / BETS)
    stores.wallet_bets = bet_store
    wallet_count = run_enrichment(stores)
    if type(wallet_count) is not int or wallet_count < 0:
        raise ValueError("Scorer returned an invalid wallet count")
    outputs = {name: _validate_output(generation / name) for name in OUTPUTS}
    if outputs[ENRICHMENT]["rows"] != wallet_count:
        raise ValueError("Scorer wallet count does not match enrichment output")
    if outputs[BETS]["rows"] != bet_store.rows_written:
        raise ValueError("Scorer bet count does not match bet output")
    for name in OUTPUTS:
        with (generation / name).open("r+b") as handle:
            os.fsync(handle.fileno())
    if _input_inventory(data_dir) != inventory:
        raise RuntimeError("Scoring inputs changed; refusing to publish")

    manifest: dict[str, Any] = {
        "schema_version": 1, "snapshot_kind": "derived_scores", "run_id": run_id,
        "started_at": started_at, "completed_at": datetime.now(UTC).isoformat(),
        "code": {"package_source_sha256": code_digest.hexdigest()},
        "inputs": inventory, "files": outputs,
    }
    manifest_hash = _write_json_atomic(generation / "manifest.json", manifest, run_id)
    _write_json_atomic(snapshots_dir / "current.json", {
        "schema_version": 1, "run_id": run_id, "manifest_sha256": manifest_hash,
    }, run_id)
    return manifest


def load_current(snapshots_dir: Path, *, verify_files: bool = True) -> dict[str, Any]:
    """Resolve one pointer read and verify its immutable manifest and outputs.

    Verifying large bet files costs an entire sequential read. Consumers can
    verify once when a generation changes and cache the result. This does not
    fall back silently if the current generation has been corrupted.
    """
    pointer = json.loads((snapshots_dir / "current.json").read_bytes())
    run_id = pointer["run_id"]
    if not isinstance(run_id, str):
        raise ValueError("Invalid score snapshot pointer")
    _validate_run_id(run_id)
    raw = (snapshots_dir / run_id / "manifest.json").read_bytes()
    if pointer.get("schema_version") != 1 or (
            hashlib.sha256(raw).hexdigest() != pointer.get("manifest_sha256")):
        raise ValueError("Score snapshot manifest checksum mismatch")
    manifest: dict[str, Any] = json.loads(raw)
    if (manifest.get("schema_version") != 1 or manifest.get("run_id") != run_id
            or manifest.get("snapshot_kind") != "derived_scores"
            or set(manifest.get("files", {})) != set(OUTPUTS)):
        raise ValueError("Invalid score snapshot manifest")
    if verify_files:
        for name in OUTPUTS:
            if _validate_output(snapshots_dir / run_id / name) != manifest["files"][name]:
                raise ValueError(f"Score snapshot output checksum/count mismatch: {name}")
    return manifest
