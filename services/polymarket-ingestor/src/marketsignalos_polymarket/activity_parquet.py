"""Offline, loss-preserving activity prototype. Production stores are unchanged.

Install the ``benchmark`` extra to use this module. A completed manifest is the
commit marker: incomplete exports cannot be queried through this interface.
"""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

SCHEMA_VERSION = 1
BUCKETS = 64


def wallet_bucket(wallet: str) -> int:
    return zlib.crc32(wallet.lower().encode("utf-8")) % BUCKETS


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def connect(*, database: str = ":memory:", memory_mb: int = 512,
            threads: int = 2) -> duckdb.DuckDBPyConnection:
    if memory_mb < 64 or threads < 1:
        raise ValueError("Use at least 64 MiB and one thread")
    return duckdb.connect(database, config={
        "memory_limit": f"{memory_mb}MiB", "threads": str(threads),
        # Explicit-offset timestamps use DuckDB's core UTC timestamp support.
        # Setting TimeZone loads ICU and can trigger a download on a fresh host.
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "max_temp_directory_size": "32GiB",
    })


def fingerprint(path: Path) -> dict[str, Any]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("Source changed while hashing; take a stable snapshot")
    return {"bytes": after.st_size, "mtime_ns": after.st_mtime_ns,
            "sha256": digest.hexdigest()}


def assert_source_unchanged(source: Path, signature: dict[str, Any]) -> None:
    current = source.stat()
    if (current.st_size, current.st_mtime_ns) != (signature["bytes"], signature["mtime_ns"]):
        raise RuntimeError("Source changed; dataset/result is not a valid snapshot comparison")


def _signature(db: duckdb.DuckDBPyConnection, relation: str) -> list[Any]:
    # Order-independent invariants include source ordinal AND original payload.
    # DuckDB's hash is an integrity check, not a cryptographic proof of equality.
    row = db.execute(f"""
        SELECT count(*), min(event_ts), max(event_ts),
               count(*) FILTER (WHERE observed_at IS NULL),
               bit_xor(hash(source_record, raw_json)),
               sum(hash(source_record, raw_json)::HUGEINT)
        FROM {relation}
    """).fetchone()
    assert row is not None
    return list(row)


def build_dataset(source: Path, output: Path, *, memory_mb: int = 512,
                  threads: int = 2) -> dict[str, Any]:
    source = source.resolve(strict=True)
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Output must be a new directory; existing datasets are immutable")
    signature = fingerprint(source)
    output.mkdir(parents=True)
    staging = output / "staging.duckdb"
    with connect(database=str(staging), memory_mb=memory_mb, threads=threads) as db:
        # The single-file JSON reader preserves insertion order. Keep its ordinal
        # before the export sort; do not mistake it for a blockchain log index.
        db.execute("""
            CREATE TABLE activity AS
            WITH source AS (
                SELECT row_number() OVER () AS source_record, json AS raw_json,
                       json_extract_string(json, ['proxy_wallet', 'timestamp', 'type',
                           'side', 'size', 'usdc_size', 'fetched_at']) AS fields
                FROM read_json_objects(?, format='newline_delimited',
                                       maximum_object_size=16777216)
            )
            SELECT source_record, raw_json, lower(fields[1]) AS wallet,
                   CASE WHEN regexp_full_match(fields[2], '[0-9]+')
                        THEN fields[2]::BIGINT END AS event_ts,
                   upper(fields[3]) AS activity_type,
                   upper(coalesce(fields[4], '')) AS side,
                   coalesce(fields[5]::DOUBLE, 0) AS size,
                   coalesce(fields[6]::DOUBLE, 0) AS usdc_size,
                   CASE WHEN regexp_matches(fields[7], '(Z|[+-][0-9]{2}:[0-9]{2})$')
                        THEN try_cast(fields[7] AS TIMESTAMPTZ) END AS observed_at
            FROM source
        """, [str(source)])
        invalid = db.execute("""
            SELECT count(*) FROM activity WHERE wallet IS NULL
                OR NOT regexp_full_match(wallet, '0x[0-9a-f]{40}')
                OR event_ts IS NULL OR event_ts < 0 OR activity_type IS NULL
                OR NOT isfinite(size) OR NOT isfinite(usdc_size)
        """).fetchone()
        if invalid and invalid[0]:
            raise ValueError(f"Invalid required activity fields in {invalid[0]} rows")
        source_signature = _signature(db, "activity")
        if source_signature[0] == 0:
            raise ValueError("Source contains no activity records")
        db.execute("CREATE TABLE buckets (wallet VARCHAR, wallet_bucket INTEGER)")
        # Compute CRC32 once per distinct wallet, not once per activity row.
        # Separate cursor permits bounded batches while inserting the lookup.
        with db.cursor() as cursor:
            cursor.execute("SELECT DISTINCT wallet FROM activity")
            while wallets := cursor.fetchmany(1000):
                db.executemany("INSERT INTO buckets VALUES (?, ?)",
                               [(row[0], wallet_bucket(row[0])) for row in wallets])
        parquet_path = sql_literal((output / "activity").as_posix())
        db.execute(f"""
            COPY (SELECT a.*, b.wallet_bucket FROM activity a JOIN buckets b USING (wallet)
                  ORDER BY wallet, event_ts, source_record)
            TO {parquet_path} (FORMAT PARQUET, COMPRESSION ZSTD,
                PARTITION_BY (wallet_bucket), ROW_GROUP_SIZE 65536)
        """)
        glob = sql_literal((output / "activity" / "**" / "*.parquet").as_posix())
        target_signature = _signature(db, f"read_parquet({glob}, hive_partitioning=true)")
        if source_signature != target_signature:
            raise RuntimeError("Parquet round-trip integrity check failed")
        stats = db.execute("""
            SELECT wallet, count(*) AS n FROM activity
            GROUP BY wallet ORDER BY n DESC, wallet
        """).fetchall()
        # Predeclared workload selection by activity count, never by profit.
        indices = sorted({0, len(stats) // 2, len(stats) - 1})
        selected = [{"wallet": stats[i][0], "records": stats[i][1]} for i in indices]
        last_observed = db.execute("SELECT max(observed_at)::VARCHAR FROM activity").fetchone()
        assert last_observed is not None
        assert_source_unchanged(source, signature)
        files = sorted((output / "activity").rglob("*.parquet"))
        manifest = {
            "schema_version": SCHEMA_VERSION, "created_at": datetime.now(UTC).isoformat(),
            "source": str(source), "source_fingerprint": signature,
            "duckdb_version": duckdb.__version__, "wallet_buckets": BUCKETS,
            "rows": source_signature[0], "wallets": len(stats),
            "min_event_ts": source_signature[1], "max_event_ts": source_signature[2],
            "unknown_observed_at_rows": source_signature[3],
            "integrity_signature": source_signature,
            "max_observed_at": last_observed[0],
            "selected_wallets": selected,
            "parquet_bytes": sum(p.stat().st_size for p in files),
            "files": [{"path": p.relative_to(output).as_posix(), "bytes": p.stat().st_size}
                      for p in files],
            "deduplicated": False, "point_in_time_policy": "event_ts and fetched_at <= cutoff",
        }
    # Remove only this builder's closed intermediate DB. Never touch the source
    # JSONL or its dedupe sidecar. Failed builds retain intermediates for diagnosis.
    staging.unlink()
    temporary = output / "manifest.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output / "manifest.json")
    return manifest


def load_manifest(dataset: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != SCHEMA_VERSION or value.get("wallet_buckets") != BUCKETS:
        raise ValueError("Unsupported activity dataset version")
    root = dataset.resolve()
    expected = {entry["path"] for entry in value["files"]}
    actual = {p.relative_to(root).as_posix() for p in (root / "activity").rglob("*.parquet")}
    if expected != actual:
        raise ValueError("Dataset inventory is incomplete or changed")
    for entry in value["files"]:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root) or path.stat().st_size != entry["bytes"]:
            raise ValueError("Dataset inventory is incomplete or changed")
    return value


def parse_cutoff(value: str) -> datetime:
    cutoff = datetime.fromisoformat(value)
    if cutoff.tzinfo is None:
        raise ValueError("Cutoff must include a timezone")
    return cutoff.astimezone(UTC)


def parse_observation(value: str) -> datetime | None:
    """Match the export's accepted source timestamp representation."""
    if not isinstance(value, str) or not re.search(r"(Z|[+-][0-9]{2}:[0-9]{2})$", value):
        return None
    try:
        return parse_cutoff(value)
    except ValueError:
        return None


def activity_query(dataset: Path, wallet: str, *, since: int = 0,
                   as_of: str | None = None) -> tuple[str, list[Any]]:
    wallet = wallet.lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", wallet):
        raise ValueError("Wallet must be a 20-byte hexadecimal address")
    glob = sql_literal((dataset / "activity" / "**" / "*.parquet").as_posix())
    sql = f"""
        SELECT count(*), count(*) FILTER (WHERE activity_type='TRADE' AND side='BUY'),
               coalesce(fsum(usdc_size), 0), min(event_ts), max(event_ts)
        FROM read_parquet({glob}, hive_partitioning=true)
        WHERE wallet_bucket=? AND wallet=? AND event_ts>=?
    """
    params: list[Any] = [wallet_bucket(wallet), wallet, since]
    if as_of is not None:
        cutoff = parse_cutoff(as_of)
        sql += " AND event_ts<=? AND observed_at<=?"
        params.extend([int(cutoff.timestamp()), cutoff])
    return sql, params


def query_activity(dataset: Path, wallet: str, *, since: int = 0,
                   as_of: str | None = None, memory_mb: int = 512,
                   threads: int = 2) -> list[Any]:
    load_manifest(dataset)
    sql, params = activity_query(dataset, wallet, since=since, as_of=as_of)
    with connect(memory_mb=memory_mb, threads=threads) as db:
        row = db.execute(sql, params).fetchone()
        assert row is not None
        return list(row)
