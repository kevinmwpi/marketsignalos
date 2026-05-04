from __future__ import annotations

import json
from pathlib import Path

import pytest

from marketsignalos_ingestor import runner


class StubKalshiClient:
    def __init__(self, config: object) -> None:
        self.config = config
        self.closed = False

    def list_trades(
        self,
        ticker: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, object]:
        assert ticker == "KXTEST"
        assert limit == 500
        assert cursor is None
        return {
            "cursor": "trade-next",
            "trades": [
                {
                    "trade_id": "trade-1",
                    "side": "yes",
                    "yes_price": 61,
                    "no_price": 39,
                    "count": 4,
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
        }

    def list_fills(
        self,
        ticker: str,
        *,
        limit: int = 500,
        cursor: str | None = None,
    ) -> dict[str, object]:
        assert ticker == "KXTEST"
        assert limit == 500
        assert cursor is None
        return {
            "cursor": "fill-next",
            "fills": [
                {
                    "member_id": "acct-1",
                    "fill_id": "fill-1",
                    "trade_id": "trade-1",
                    "side": "yes",
                    "yes_price": 61,
                    "no_price": 39,
                    "count": 4,
                    "created_time": "2026-01-01T00:00:00Z",
                }
            ],
        }

    def list_markets(
        self,
        *,
        status: str = "settled",
        limit: int = 200,
        cursor: str | None = None,
    ) -> dict[str, object]:
        assert status == "settled"
        assert limit == 200
        assert cursor is None
        return {
            "cursor": "resolution-next",
            "markets": [
                {"ticker": "KXTEST", "result": "yes", "close_time": "2026-01-02T00:00:00Z"},
            ],
        }

    def close(self) -> None:
        self.closed = True


class StubConfig:
    @staticmethod
    def from_env() -> object:
        return object()


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_runner_writes_trade_fill_and_resolution_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INGEST_MARKET_TICKERS", "KXTEST")
    monkeypatch.delenv("INGEST_CONTINUOUS", raising=False)
    monkeypatch.setattr(runner, "KalshiClient", StubKalshiClient)
    monkeypatch.setattr(runner, "KalshiClientConfig", StubConfig)

    assert runner.main() == 0

    trades = _read_jsonl(tmp_path / "kalshi_trades.jsonl")
    fills = _read_jsonl(tmp_path / "kalshi_fills.jsonl")
    resolutions = _read_jsonl(tmp_path / "kalshi_market_resolutions.jsonl")
    checkpoints = json.loads((tmp_path / "kalshi_checkpoints.json").read_text(encoding="utf-8"))

    assert len(trades) == 1
    assert trades[0]["trade_id"] == "trade-1"
    assert len(fills) == 1
    assert fills[0]["fill_id"] == "fill-1"
    assert len(resolutions) == 1
    assert resolutions[0]["market_ticker"] == "KXTEST"
    assert checkpoints == {
        "KXTEST": "trade-next",
        "fills:KXTEST": "fill-next",
        "markets:settled": "resolution-next",
    }
