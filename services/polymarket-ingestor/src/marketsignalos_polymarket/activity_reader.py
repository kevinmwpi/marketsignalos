"""Replay an immutable Parquet activity snapshot through the existing scorer.

This is an offline adapter, not a production store. The scorer still holds a
whole wallet bucket in Python; DuckDB's memory setting does not cap that memory.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from .activity_parquet import BUCKETS, connect, load_manifest, sql_literal
from .models import PolymarketActivity
from .runner import _activity_from_row

log = logging.getLogger(__name__)


class ParquetActivityReader:
    """Replay the legacy 64-bucket order, including duplicate rows and ties."""

    def __init__(self, dataset: Path, *, memory_mb: int = 512, threads: int = 2) -> None:
        self.dataset = dataset.resolve(strict=True)
        self.manifest = load_manifest(self.dataset)
        self.memory_mb = memory_mb
        self.threads = threads
        self._inventory = self._snapshot()

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        paths = [self.dataset / "manifest.json", *sorted(
            (self.dataset / "activity").rglob("*.parquet"))]
        return {str(p): (p.stat().st_size, p.stat().st_mtime_ns) for p in paths}

    def validate(self) -> None:
        # Cheap mutation detection on every replay. The shadow command also
        # hashes every input before and after the experiment.
        if self._snapshot() != self._inventory:
            raise RuntimeError("Parquet snapshot changed during scoring")

    def __call__(self) -> Iterator[list[PolymarketActivity]]:
        self.validate()
        glob = sql_literal((self.dataset / "activity" / "**" / "*.parquet").as_posix())
        with connect(memory_mb=self.memory_mb, threads=self.threads) as db:
            for bucket in range(BUCKETS):
                db.execute(f"""
                    SELECT source_record, raw_json FROM read_parquet({glob}, hive_partitioning=true)
                    WHERE wallet_bucket=?
                """, [bucket])
                ordered: list[tuple[int, PolymarketActivity]] = []
                while rows := db.fetchmany(4096):
                    for ordinal, raw in rows:
                        # Use the same conversion/defaults as the JSONL reader.
                        record = _activity_from_row(json.loads(raw))
                        if record is not None:
                            ordered.append((ordinal, record))
                # Sorting variable-length raw JSON in DuckDB exhausted its
                # buffer budget on real whale partitions. Parse in batches,
                # then sort model references by the immutable source ordinal.
                # Python memory remains proportional to this whole bucket.
                ordered.sort(key=lambda item: item[0])
                records = [record for _, record in ordered]
                del ordered
                if bucket % 8 == 0 or bucket == BUCKETS - 1:
                    log.info("parquet enrichment shard %d/%d rows=%d",
                             bucket + 1, BUCKETS, len(records))
                yield records
                del records
        self.validate()
