"""Two rules:

  A. the batched collect is capped at 12 — a batch of 17 fails the
     eth_call pre-check (`Reverted [code -32000]` at gasLimit 30,500,000),
     and no landed collect batch above 12 is known;
  B. the tick-row change key ignores `kamiden-unavailable` — it gates no
     tick (only the optional battles seed reads it) and can flap on and
     off from tick to tick, so a node-watching book would write a row on
     almost every tick.
"""

from __future__ import annotations

from typing import Any

import pytest

from keeper.signer import BATCH_CAPS, Intent, WhitelistRefused, check_whitelist
from test_phase1d import T, _rows, _start
from test_phase2a import _rows as _auto_rows, _settle_previews, carol  # noqa: F401 — `carol` is the fixture
from test_phase2l import _note

CFG = {"foodItem": 11301, "reviveItems": [21301]}


# ------------------------------------------------------- A. the collect cap


def test_thirty_six_pooled_collects_go_out_as_three_batches_of_twelve(make_keeper):
    """A pooled collect of 36 books of one account: three txs, never a batch
    of 17 (which the chain's pre-check refuses) and never 36 singles."""
    e = make_keeper(cap=40)
    e.grant()
    kamis = list(range(2001, 2037))  # 36 books, one account
    for k in kamis:
        e.lens.set_kami(k, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        _start(e, kamiIndex=k, collectEvery=1000)
    e.keeper.tick_due(now=T)
    e.keeper.tick_due(now=T + 1000)
    collects = [r for r in e.keeper.signer.recorded if r.system == "system.harvest.collect"]
    assert [c.count for c in collects] == [12, 12, 12]
    assert [c.params["kamiIndices"] for c in collects] == [kamis[:12], kamis[12:24], kamis[24:]]
    assert [c.batchSize for c in collects] == [12, 12, 12]
    assert all(c.gasLimit == 1_600_000 + 12 * 1_700_000 for c in collects)


def test_a_collect_intent_of_thirteen_is_refused_before_signing():
    assert BATCH_CAPS["system.harvest.collect"] == 12
    check_whitelist(Intent("system.harvest.collect", "lab", None, {"kamiIndices": list(range(12))}), CFG)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.harvest.collect", "lab", None, {"kamiIndices": list(range(13))}), CFG)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.harvest.collect", "lab", None, {"count": 13}), CFG)


# ------------------------------------------------------------ B. the tick row


def _kamiden(e, on: bool) -> None:
    """The lens flag as the daemon serves it: `feedsDegraded` non-empty is
    what `perceive` turns into `kamiden-unavailable`."""
    e.lens.status_data["feedsDegraded"] = ["kamiden-stream"] if on else []


def test_the_kamiden_flag_flapping_writes_one_tick_row(env):
    env.grant()
    book = _start(env, tickHeartbeat=50)
    rows = lambda: _rows(env.keeper, book, "tick")  # noqa: E731
    env.keeper.tick_book(book, now=T)
    n = len(rows())
    for on in (True, False, True, False):
        _kamiden(env, on)
        env.keeper.tick_book(book, now=T + 30)
        assert ("kamiden-unavailable" in env.keeper.runtime[book.id]["degraded"]) is on
    assert len(rows()) == n  # nothing else moved: the flag alone writes no row


def test_the_row_that_is_written_carries_the_flag(env):
    """It is dropped from the change KEY only — a row written while the
    lens reports the flag still carries it, as the ledger's evidence."""
    env.grant()
    book = _start(env, tickHeartbeat=50)
    env.keeper.tick_book(book, now=T)
    _kamiden(env, True)
    env.lens.set_kami(1001, hp=(40, 200))  # a real change: the book feeds
    s = env.keeper.tick_book(book, now=T + 30)
    row = _rows(env.keeper, book, "tick")[-1]
    assert s["decisions"] == ["feed"] and row["degraded"] == ["kamiden-unavailable"]


def _tick(e, book, flags: list[str]) -> dict[str, Any]:
    """A tick of a node-watching book with the kill scan's flags as the pass
    left them (keeper/core.py: they ride every such tick and gate nothing);
    the note is quiet and unchanging, so only the flags can move the key."""
    e.clock["now"] += 30
    e.keeper.context.scan = {"flags": list(flags), "note": _note()}
    return e.keeper.tick_book(book, e.clock["now"])


def test_a_change_in_another_degraded_flag_still_writes_a_row(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    _tick(e, book, [])
    n = len(_auto_rows(e, book, "tick"))
    _tick(e, book, ["ws-logs-gap:40"])  # non-gating too, but compared
    assert len(_auto_rows(e, book, "tick")) == n + 1
    _tick(e, book, ["ws-logs-gap:40"])
    assert len(_auto_rows(e, book, "tick")) == n + 1  # unchanged: no row
    _tick(e, book, [])
    assert len(_auto_rows(e, book, "tick")) == n + 2
