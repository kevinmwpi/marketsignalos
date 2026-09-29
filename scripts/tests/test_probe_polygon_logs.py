import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import probe_polygon_logs as probe

V1 = "0x" + "1" * 64
V2 = "0x" + "2" * 64
MAKER = "0x" + "a" * 40
TAKER = "0x" + "b" * 40
TOKEN = 123456789


def word(value: int) -> str:
    return f"{value:064x}"


def log(topic0: str, *values: int) -> dict[str, Any]:
    return {
        "topics": [topic0, "0x" + "0" * 64, "0x" + "0" * 24 + MAKER[2:], "0x" + "0" * 24 + TAKER[2:]],
        "data": "0x" + "".join(word(v) for v in values),
        "transactionHash": "0xABC", "blockNumber": "0x10",
    }


def test_words_and_addresses() -> None:
    assert probe.words("0x" + word(1) + word(2)) == [1, 2]
    assert probe.topic_address("0x" + "0" * 24 + "C" * 40) == "0x" + "c" * 40


def test_v2_buy_fill_pays_collateral_for_shares() -> None:
    # maker BUY: gives 40 collateral, receives 100 shares -> 0.40 per share
    fill = probe.decode_fill(log(V2, 0, TOKEN, 40_000_000, 100_000_000, 0, 0, 0), V1, V2)
    assert fill is not None
    assert (fill["version"], fill["side"], fill["token_id"]) == ("v2", "BUY", str(TOKEN))
    assert (fill["shares"], fill["price"]) == (100.0, 0.4)
    assert (fill["maker"], fill["taker"], fill["tx"], fill["block"]) == (MAKER, TAKER, "0xabc", 16)


def test_v2_sell_fill_gives_shares_for_collateral() -> None:
    fill = probe.decode_fill(log(V2, 1, TOKEN, 50_000_000, 30_000_000, 0, 0, 0), V1, V2)
    assert fill is not None
    assert (fill["side"], fill["shares"], fill["price"]) == ("SELL", 50.0, 0.6)


def test_v1_side_follows_which_asset_is_collateral() -> None:
    buy = probe.decode_fill(log(V1, 0, TOKEN, 25_000_000, 50_000_000, 0), V1, V2)
    sell = probe.decode_fill(log(V1, TOKEN, 0, 50_000_000, 35_000_000, 0), V1, V2)
    assert buy is not None and sell is not None
    assert (buy["side"], buy["token_id"], buy["price"], buy["shares"]) == ("BUY", str(TOKEN), 0.5, 50.0)
    assert (sell["side"], sell["token_id"], sell["price"], sell["shares"]) == (
        "SELL", str(TOKEN), 0.7, 50.0)


def test_unknown_topic_or_wrong_layout_is_not_a_fill() -> None:
    assert probe.decode_fill(log("0x" + "9" * 64, 0, TOKEN, 1, 1, 0), V1, V2) is None
    assert probe.decode_fill(log(V2, 0, TOKEN, 1, 1, 0), V1, V2) is None  # V1 width, V2 topic
    assert probe.decode_fill({"topics": [V2], "data": "0x"}, V1, V2) is None


def test_match_trade_accepts_single_fill_or_summed_maker_fills() -> None:
    fills = [
        {"token_id": str(TOKEN), "maker": MAKER, "taker": TAKER, "shares": 60.0, "price": 0.4},
        {"token_id": str(TOKEN), "maker": MAKER, "taker": TAKER, "shares": 40.0, "price": 0.4},
    ]
    single = probe.match_trade(
        {"proxyWallet": MAKER.upper(), "asset": str(TOKEN), "size": 60, "price": 0.4}, fills)
    assert single["size_price_match"] and single["wallet_as_maker"]
    summed = probe.match_trade(
        {"proxyWallet": MAKER, "asset": str(TOKEN), "size": 100, "price": 0.4}, fills)
    assert summed["size_price_match"]
    other = probe.match_trade(
        {"proxyWallet": "0x" + "d" * 40, "asset": str(TOKEN), "size": 60, "price": 0.4}, fills)
    assert not other["wallet_in_fill"] and not other["size_price_match"]
