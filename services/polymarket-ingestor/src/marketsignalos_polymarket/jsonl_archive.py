"""
Compressed archive segments for an append-only JSONL store whose rows must never
repeat.

Stage 3 step 0 (docs/stage3-cohort-v1-plan.md): ``polymarket_activity.jsonl`` grows by
tens of megabytes a day against a 5 GB volume. Writers keep appending plain rows to
``<name>``. ``compact`` moves complete rows into gzip segments under
``<name>.archive/``. Every reader goes through ``iter_lines``, so rows and their order
are unchanged, and every score with them.

A duplicated activity row is a fill counted twice. So unlike the entry-price archive,
whose readers collapse repeats, compaction here is exactly-once under a crash at any
point:

1. ``<name>`` is renamed to ``<name>.pending-<n>``. Appends after this go to a new
   ``<name>``.
2. Its complete rows are compressed to ``<name>.archive/<n>.jsonl.gz.tmp``, synced, and
   renamed to ``<n>.jsonl.gz``. **That rename is the commit.**
3. The pending file is deleted.

Readers include ``pending-<n>`` only while segment ``<n>`` does not exist, so a row is
read once whether a crash came before, during or after the commit. The next
``compact`` finishes any pending file first. Bytes after a pending file's last newline
are a torn row from a crash mid-append. They are dropped, as the readers already
ignore them.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path

log = logging.getLogger("marketsignalos.polymarket.jsonl_archive")

_SEGMENT = re.compile(r"^(\d{6})\.jsonl\.gz$")
_PENDING = re.compile(r"\.pending-(\d{6})$")


def archive_dir(path: Path) -> Path:
    return path.with_name(path.name + ".archive")


def iter_lines(path: Path) -> Iterator[str]:
    """Every row's line, oldest first: committed segments, an uncommitted pending
    file, then the plain file."""
    committed = set(_segments(path))
    for number in sorted(committed):
        with gzip.open(_segment_path(path, number), "rt", encoding="utf-8") as handle:
            yield from handle
    for number, pending in sorted(_pending(path).items()):
        if number not in committed:
            with pending.open(encoding="utf-8") as handle:
                yield from handle
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            yield from handle


def compact(path: Path, *, min_bytes: int = 0) -> int:
    """Finish any interrupted compaction, then move ``path``'s rows into a new segment
    when it holds at least ``min_bytes``. Returns the bytes moved."""
    moved = finish_pending(path)
    if not path.exists() or path.stat().st_size < max(1, min_bytes):
        return moved
    number = max([*_segments(path), *_pending(path), 0]) + 1
    pending = path.with_name(f"{path.name}.pending-{number:06d}")
    os.replace(path, pending)
    moved += _commit(path, number, pending)
    path.touch(exist_ok=True)  # the store still "exists" for its readers and writers
    return moved


def finish_pending(path: Path) -> int:
    """Commit, or discard if already committed, every pending file."""
    moved = 0
    committed = set(_segments(path))
    for number, pending in sorted(_pending(path).items()):
        if number in committed:
            pending.unlink()
        else:
            moved += _commit(path, number, pending)
    return moved


def segment_paths(path: Path) -> list[Path]:
    """Committed segments, oldest first."""
    return [_segment_path(path, number) for number in sorted(_segments(path))]


def footprint(path: Path) -> list[Path]:
    """Every file holding the store's rows: segments, pending files, the plain file."""
    files = segment_paths(path) + [p for _, p in sorted(_pending(path).items())]
    return files + ([path] if path.exists() else [])


def _commit(path: Path, number: int, pending: Path) -> int:
    end = _complete_bytes(pending)
    if end < pending.stat().st_size:
        log.warning("dropping a torn final row from %s", pending.name)
    if end:
        directory = archive_dir(path)
        directory.mkdir(exist_ok=True)
        segment = _segment_path(path, number)
        tmp = segment.with_name(segment.name + ".tmp")
        try:
            with pending.open("rb") as source, tmp.open("wb") as raw:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as out:
                    remaining = end
                    while remaining > 0:
                        block = source.read(min(1 << 20, remaining))
                        if not block:
                            raise OSError(f"{pending.name} shrank while it was compacted")
                        out.write(block)
                        remaining -= len(block)
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(tmp, segment)  # the commit
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    pending.unlink()
    return end


def _segment_path(path: Path, number: int) -> Path:
    return archive_dir(path) / f"{number:06d}.jsonl.gz"


def _segments(path: Path) -> list[int]:
    directory = archive_dir(path)
    if not directory.is_dir():
        return []
    return [int(match.group(1)) for item in directory.iterdir()
            if (match := _SEGMENT.match(item.name))]


def _pending(path: Path) -> dict[int, Path]:
    found: dict[int, Path] = {}
    if not path.parent.is_dir():
        return found
    for item in path.parent.iterdir():
        if item.name.startswith(path.name + ".pending-"):
            match = _PENDING.search(item.name)
            if match:
                found[int(match.group(1))] = item
    return found


def _complete_bytes(path: Path) -> int:
    """The length of ``path`` up to and including its last newline."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        position = size
        while position > 0:
            step = min(1 << 16, position)
            position -= step
            handle.seek(position)
            newline = handle.read(step).rfind(b"\n")
            if newline >= 0:
                return position + newline + 1
    return 0
