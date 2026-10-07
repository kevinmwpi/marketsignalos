from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from marketsignalos_polymarket import lean_pilot as pilot
from marketsignalos_polymarket.pilot_recovery import (
    ACTIVITY_FILE,
    CHECKPOINTS_FILE,
    UNRECORDED,
    recover,
)
from marketsignalos_polymarket.runner import parse_activity_row
from marketsignalos_polymarket.storage import JsonlActivityStore, JsonWalletCheckpointStore

WALLET = "0x" + "a" * 40
T0 = 1_759_000_000


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 7, 12, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


def _trade(n: int) -> Any:
    return parse_activity_row({
        "proxyWallet": WALLET, "timestamp": T0 + n, "conditionId": f"0xc{n}",
        "type": "TRADE", "side": "BUY", "size": 10, "usdcSize": 4, "price": 0.4,
        "outcomeIndex": 0, "eventSlug": f"e{n}", "transactionHash": f"0xt{n}"})


def _state(data: Path) -> dict[str, Any]:
    state: dict[str, Any] = json.loads((data / ".lean-pilot/state.json").read_text())
    return state


def _index(data: Path) -> set[str]:
    return set(json.loads((data / f"{ACTIVITY_FILE}.index.json").read_text()))


def _interrupted_collection(data: Path) -> None:
    """A collect stage that wrote rows, never flushed the dedupe index, left a torn
    row and a half-written checkpoint file, then failed."""
    clock = Clock()

    def execute(stage: str, d: Path, cfg: pilot.PilotConfig, run: str) -> dict[str, Any]:
        store = JsonlActivityStore(d / ACTIVITY_FILE)
        store.write_activity([_trade(1)])
        store.flush()
        store.write_activity([_trade(2), _trade(3)])  # index never flushed
        with (d / ACTIVITY_FILE).open("a", encoding="utf-8") as handle:
            handle.write('{"proxy_wallet": "0xa')  # killed mid-row
        (d / CHECKPOINTS_FILE).write_text('{"' + WALLET + '": 17590', encoding="utf-8")
        raise OSError("killed")

    receipt = pilot.run_cycle(data, pilot.PilotConfig(), "crashed", execute=execute,
                              now_fn=clock.now, monotonic=clock.monotonic)
    assert receipt["status"] == "failed"


def test_an_interrupted_collection_records_the_run_to_recover(tmp_path: Path) -> None:
    _interrupted_collection(tmp_path)
    state = _state(tmp_path)
    assert state["recovery_required"] is True and state["recovery_run_id"] == "crashed"
    plan = pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=Clock().now())
    assert plan["recovery_run_id"] == "crashed" and plan["can_run"] is False


def test_recovery_repairs_every_store_and_clears_the_flag(tmp_path: Path) -> None:
    _interrupted_collection(tmp_path)
    assert _index(tmp_path) == {"0xt1:0xc1:0:TRADE"}  # rows 2 and 3 unknown to dedupe

    receipt = recover(tmp_path, "crashed")
    assert receipt["status"] == "recovered"
    assert receipt["torn_tails"] == {ACTIVITY_FILE: len('{"proxy_wallet": "0xa')}
    assert receipt["checkpoints"] == "rebuilt from activity for 1 wallets"
    assert receipt["activity_index"] == "rebuilt"
    # The torn row is gone, the stored rows are all in the index, and the cut bytes
    # and the broken checkpoint file are kept for inspection.
    assert (tmp_path / ACTIVITY_FILE).read_bytes().endswith(b"\n")
    assert _index(tmp_path) == {f"0xt{n}:0xc{n}:0:TRADE" for n in (1, 2, 3)}
    saved = tmp_path / ".lean-pilot/recoveries/crashed"
    assert (saved / f"{ACTIVITY_FILE}.torn").read_bytes() == b'{"proxy_wallet": "0xa'
    assert (saved / f"{CHECKPOINTS_FILE}.broken").exists()
    # Checkpoints come back as the newest stored timestamp, never newer.
    assert JsonWalletCheckpointStore(tmp_path / CHECKPOINTS_FILE).get_last_timestamp(
        WALLET) == T0 + 3
    # The next append starts a clean line, and a refetch of stored trades is dropped.
    store = JsonlActivityStore(tmp_path / ACTIVITY_FILE)
    assert store.write_activity([_trade(2), _trade(4)]) == 1
    rows = [json.loads(line) for line in (tmp_path / ACTIVITY_FILE).read_text().splitlines()]
    assert [row["transaction_hash"] for row in rows] == ["0xt1", "0xt2", "0xt3", "0xt4"]

    state = _state(tmp_path)
    assert state["recovery_required"] is False and "recovery_run_id" not in state
    assert state["last_recovery"]["run_id"] == "crashed"
    assert json.loads((tmp_path / ".lean-pilot/recoveries/crashed.json").read_text())[
        "status"] == "recovered"
    assert pilot.plan_cycle(tmp_path, pilot.PilotConfig(), now=Clock().now())["can_run"]
    assert recover(tmp_path, "crashed")["status"] == "nothing_to_recover"


def test_recovery_refuses_any_run_but_the_one_recorded(tmp_path: Path) -> None:
    # A PILOT_RECOVER left set from an old incident must not repair a new one.
    _interrupted_collection(tmp_path)
    before = (tmp_path / ACTIVITY_FILE).read_bytes()
    outcome = recover(tmp_path, "olderincident")
    assert outcome["status"] == "refused" and outcome["recovery_run_id"] == "crashed"
    assert _state(tmp_path)["recovery_required"] is True
    assert (tmp_path / ACTIVITY_FILE).read_bytes() == before


def test_a_flag_set_before_run_ids_were_recorded_needs_the_unrecorded_id(
    tmp_path: Path,
) -> None:
    _interrupted_collection(tmp_path)
    state = _state(tmp_path)
    del state["recovery_run_id"]
    (tmp_path / ".lean-pilot/state.json").write_text(json.dumps(state))
    assert recover(tmp_path, "crashed")["status"] == "refused"
    assert recover(tmp_path, UNRECORDED)["status"] == "recovered"


def test_a_failed_recovery_keeps_the_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from marketsignalos_polymarket import pilot_recovery

    _interrupted_collection(tmp_path)

    def broken(path: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pilot_recovery, "_rebuild_activity_index", broken)
    outcome = recover(tmp_path, "crashed")
    assert outcome["status"] == "failed" and outcome["error_type"] == "OSError"
    assert _state(tmp_path)["recovery_required"] is True


def test_the_cli_exits_nonzero_unless_recovered(tmp_path: Path) -> None:
    _interrupted_collection(tmp_path)
    assert pilot.main(["--data-dir", str(tmp_path), "--recover", "wrongrun"]) == 1
    assert pilot.main(["--data-dir", str(tmp_path), "--recover", "crashed"]) == 0
    assert pilot.main(["--data-dir", str(tmp_path), "--recover", "crashed"]) == 0


def test_checkpoint_writes_leave_no_partial_file(tmp_path: Path) -> None:
    store = JsonWalletCheckpointStore(tmp_path / CHECKPOINTS_FILE)
    store.set_last_timestamp(WALLET, T0)
    assert json.loads((tmp_path / CHECKPOINTS_FILE).read_text()) == {WALLET: T0}
    assert not list(tmp_path.glob("*.tmp"))
