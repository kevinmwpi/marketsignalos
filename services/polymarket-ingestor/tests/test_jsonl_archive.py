from __future__ import annotations

import gzip
import json
import os
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket.cohort import _rebuild_activity_index, apply_exclusions
from marketsignalos_polymarket.jsonl_archive import (
    archive_dir,
    compact,
    finish_pending,
    footprint,
    iter_lines,
    segment_paths,
)
from marketsignalos_polymarket.runner import _iter_jsonl, parse_activity_row
from marketsignalos_polymarket.storage import JsonlActivityStore

A = "0x" + "a" * 40
B = "0x" + "b" * 40


def _trade(n: int, wallet: str = A) -> Any:
    return parse_activity_row({
        "proxyWallet": wallet, "timestamp": 1_759_000_000 + n, "conditionId": f"0xc{n}",
        "type": "TRADE", "side": "BUY", "size": 10, "usdcSize": 4, "price": 0.4,
        "outcomeIndex": 0, "eventSlug": f"e{n}", "transactionHash": f"0xt{n}"})


def _store(tmp_path: Path, numbers: range, wallet: str = A) -> Path:
    path = tmp_path / "polymarket_activity.jsonl"
    store = JsonlActivityStore(path)
    store.write_activity([_trade(n, wallet) for n in numbers])
    store.flush()
    return path


def _hashes(path: Path) -> list[str]:
    return [row["transaction_hash"] for row in _iter_jsonl(path)]


def test_compaction_keeps_every_row_in_order(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 4))
    before = list(iter_lines(path))
    size = path.stat().st_size  # bytes on disk: Windows text mode writes \r\n
    moved = compact(path)
    assert moved == size
    assert path.exists() and path.stat().st_size == 0  # still a store for writers
    assert [p.name for p in segment_paths(path)] == ["000001.jsonl.gz"]
    assert list(iter_lines(path)) == before
    # New rows land in the plain file after the segment, and a second compaction
    # adds a second segment.
    JsonlActivityStore(path).write_activity([_trade(4)])
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3", "0xt4"]
    compact(path)
    assert len(segment_paths(path)) == 2 and _hashes(path) == ["0xt1", "0xt2", "0xt3", "0xt4"]


def test_a_small_plain_file_waits_for_the_threshold(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 3))
    assert compact(path, min_bytes=10 * 1024 * 1024) == 0
    assert segment_paths(path) == []
    assert compact(path, min_bytes=1) > 0


def _pending_without_commit(path: Path) -> Path:
    """Step 1 done, step 2 not: the plain file renamed, no segment yet."""
    pending = path.with_name(f"{path.name}.pending-000001")
    os.replace(path, pending)
    return pending


def test_a_crash_before_the_commit_reads_the_pending_rows_once(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 4))
    pending = _pending_without_commit(path)
    JsonlActivityStore(path).write_activity([_trade(4)])  # the next pass appends
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3", "0xt4"]
    # A half-written segment from a crash during step 2 is ignored.
    archive_dir(path).mkdir()
    (archive_dir(path) / "000001.jsonl.gz.tmp").write_bytes(b"\x1f\x8b partial")
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3", "0xt4"]
    assert finish_pending(path) > 0 and not pending.exists()
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3", "0xt4"]


def test_a_crash_after_the_commit_never_duplicates_a_row(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 4))
    pending = _pending_without_commit(path)
    keep = pending.read_bytes()
    finish_pending(path)  # commits segment 1 and deletes the pending file...
    pending.write_bytes(keep)  # ...but say the deletion never happened
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3"]  # the committed segment wins
    assert finish_pending(path) == 0 and not pending.exists()
    assert _hashes(path) == ["0xt1", "0xt2", "0xt3"]


def test_a_torn_final_row_is_dropped_at_the_commit(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 3))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"proxy_wallet": "0xa')
    compact(path)
    with gzip.open(segment_paths(path)[0], "rt", encoding="utf-8") as handle:
        assert handle.read().endswith("\n")
    assert _hashes(path) == ["0xt1", "0xt2"]


def test_the_purge_and_the_index_reach_rows_in_segments(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 3), wallet=A)
    JsonlActivityStore(path).write_activity([_trade(3, B)])
    compact(path)
    JsonlActivityStore(path).write_activity([_trade(4, B), _trade(5, A)])
    removed = apply_exclusions(tmp_path, frozenset({B}))
    assert removed == {"polymarket_activity.jsonl": 2}
    assert _hashes(path) == ["0xt1", "0xt2", "0xt5"]
    index = json.loads(path.with_suffix(".jsonl.index.json").read_text())
    assert sorted(index) == [f"0xt{n}:0xc{n}:0:TRADE" for n in (1, 2, 5)]
    # Rebuilding the index from scratch reads the segments too.
    path.with_suffix(".jsonl.index.json").unlink()
    _rebuild_activity_index(path)
    assert len(json.loads(path.with_suffix(".jsonl.index.json").read_text())) == 3


def test_the_footprint_lists_every_file_holding_rows(tmp_path: Path) -> None:
    path = _store(tmp_path, range(1, 3))
    compact(path)
    pending = _pending_without_commit(path)
    names = [p.name for p in footprint(path)]
    assert names == ["000001.jsonl.gz", pending.name]


def test_a_failed_commit_leaves_the_rows_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _store(tmp_path, range(1, 3))

    def no_disk(fd: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", no_disk)
    with pytest.raises(OSError, match="disk full"):
        compact(path)
    assert segment_paths(path) == []
    assert _hashes(path) == ["0xt1", "0xt2"]  # from the pending file
    monkeypatch.undo()
    compact(path)
    assert _hashes(path) == ["0xt1", "0xt2"] and len(segment_paths(path)) == 1
