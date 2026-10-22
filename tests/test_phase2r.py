"""No decision on a lens read older than the book's own last landed tx.

A stop lands at its receipt block, and the next tick's lens read is STILL
at a block before it (the mirror has not advanced): `_settle_pending`
would read the pre-stop world as "the stop did not apply", re-offer the
rest, have it refused pre-send, and the plan / world mismatch it leaves
would pause the book `external-change` with nothing external on the
chain.

The mirror block can pass the bar (a read at receipt + 1) while
reconciledThrough is still behind the receipt and the stop is not yet
applied — the same re-offer, refusal and pause. The bar is the reconciled
bound only.

  a. the rest: the bar is the receipt block,
     the stale read decides nothing, the fresh one settles the plan;
  b. the same for a landed START;
  c. the rule itself, on `Basis`.
"""

from __future__ import annotations

from keeper.core import Keeper
from keeper.protocol import Basis
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import BOOK_43, NODE, _apply_plans_to_world, _last_tick, _rows, _set_party, _start, _tick_all
from test_phase2c import _live_env


def _counts(e, book) -> tuple[int, int, int]:
    return len(_rows(e, book, "decision")), len(_rows(e, book, "tx")), len(_rows(e, book, "deviation"))


def _events(e, book) -> list[str]:
    return [r["event"] for r in _rows(e, book, "lifecycle")]


# ------------------------------------------------------------ a. the landed rest


def test_a_read_older_than_the_last_landed_tx_decides_nothing_then_settles(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    k = BOOK_43[0]
    _set_party(e.lens, k, hp=(20, 240))  # below the floor: one stop
    _tick_all(e)
    receipts = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["stage"] == "receipt"]
    assert len(receipts) == 1 and receipts[0]["system"] == "system.harvest.stop" and receipts[0]["status"] == "success"
    B = receipts[0]["block"]
    assert B > 0 and e.keeper.runtime[book.id]["lastActBlock"] == B  # the bar
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}

    # the next read is still at the block BEFORE the receipt,
    # so it shows the kami HARVESTING on the node at 20/240 as before
    e.lens.block = B - 1
    before = _counts(e, book)
    sent = provider.count("eth_sendRawTransaction")
    _tick_all(e)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lastActBlock"] == B and summary["lensBlock"] == B - 1 and summary["lensReconciledThrough"] < B
    assert _counts(e, book) == before  # nothing decided, nothing sent, nothing deviated
    assert provider.count("eth_sendRawTransaction") == sent
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}  # the pending entry stands
    assert e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0
    assert e.keeper.handle("status", {})["data"]["books"][0]["lastActBlock"] == B

    # the mirror block is a chain HEAD, not an applied cursor — a read at
    # receipt + 1 whose reconciled bound is still behind the receipt decides
    # nothing either
    e.lens.block = B + 1
    before = _counts(e, book)
    sent = provider.count("eth_sendRawTransaction")
    _tick_all(e)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lastActBlock"] == B and summary["lensBlock"] == B + 1
    assert summary["lensReconciledThrough"] == B - 1
    assert _counts(e, book) == before
    assert provider.count("eth_sendRawTransaction") == sent
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}
    assert e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0

    # a read past the bar: the world shows what the chain took, and the
    # pending entry settles with no contradiction at all
    e.lens.block = B + 2  # reconciledThrough == B
    _apply_plans_to_world(e)  # RESTING, off the node
    _tick_all(e)
    assert book.plan[str(k)] == {"state": RESTING, "node": None}
    assert "external-change" not in _events(e, book) and "paused" not in _events(e, book)
    assert book.state == "running" and e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0
    assert _last_tick(e, book).get("reason") != "awaiting a read past the last act"


# ------------------------------------------------------------ b. the landed start


def test_a_landed_start_is_not_re_offered_on_a_read_older_than_its_receipt(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, [25814, 27850, 28532], previewsPerTick=50)  # the first kami HARVESTING on the node, the second RESTING
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, 27850, hp=(220, 220))  # RESTING at 100 %: one start
    _tick_all(e)
    receipts = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["stage"] == "receipt"]
    assert len(receipts) == 1 and receipts[0]["system"] == "system.harvest.start" and receipts[0]["status"] == "success"
    B = receipts[0]["block"]
    assert B > 0 and e.keeper.runtime[book.id]["lastActBlock"] == B
    assert book.plan["27850"] == {"state": HARVESTING, "node": NODE}

    # the stale read still shows it RESTING at 100 %: a second start would
    # be refused pre-send and leave the plan against the world
    e.lens.block = B - 1
    before = _counts(e, book)
    sent = provider.count("eth_sendRawTransaction")
    _tick_all(e)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lastActBlock"] == B and summary["lensBlock"] == B - 1
    assert _counts(e, book) == before and provider.count("eth_sendRawTransaction") == sent
    assert book.plan["27850"] == {"state": HARVESTING, "node": NODE}

    # the head past the receipt, the reconciled bound still behind it
    e.lens.block = B + 1
    before = _counts(e, book)
    sent = provider.count("eth_sendRawTransaction")
    _tick_all(e)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lastActBlock"] == B and summary["lensBlock"] == B + 1
    assert summary["lensReconciledThrough"] == B - 1
    assert _counts(e, book) == before and provider.count("eth_sendRawTransaction") == sent
    assert book.plan["27850"] == {"state": HARVESTING, "node": NODE}

    e.lens.block = B + 2  # reconciledThrough == B
    _apply_plans_to_world(e)  # HARVESTING on the book's node
    _tick_all(e)
    assert "external-change" not in _events(e, book) and "paused" not in _events(e, book)
    assert e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0 and book.state == "running"


# ------------------------------------------------------------ c. the rule


def test_read_past_last_act_rule():
    """The rule itself: only the reconciled bound reaching the bar passes it;
    the mirror block never does."""
    assert Keeper._read_past_last_act(Basis(lensBlock=5, lensReconciledThrough=None), 6) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=6, lensReconciledThrough=4), 6) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=7, lensReconciledThrough=5), 6) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=None, lensReconciledThrough=6), 6) is True
    assert Keeper._read_past_last_act(Basis(lensBlock=None, lensReconciledThrough=None), 6) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=5, lensReconciledThrough=6), 6) is True
    assert Keeper._read_past_last_act(Basis(lensBlock=6, lensReconciledThrough=6), 6) is True
