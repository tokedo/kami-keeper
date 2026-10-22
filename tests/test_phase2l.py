"""The tick-row change key must not compare the kill scan's moving
counters: the `killScan` note carries `head`, `lastScanned`, `logCalls`,
`deadWrites` and `ignored`, which move every pass, so comparing them
would write a `tick` row on EVERY tick of every node-watching book. A direct `tick_book` does not run the scan, so the note is the
test's: it is set on `keeper.context.scan`, exactly where `_scan_kills`
leaves it for the books of the pass.
"""

from __future__ import annotations

from typing import Any

from test_phase2a import _rows, _settle_previews, carol  # noqa: F401 — `carol` is the fixture


def _note(**over: Any) -> dict[str, Any]:
    """A `_scan_kills` note in its written shape (keeper/core.py)."""
    note = {"head": 12143991, "lastScanned": 12143991, "logCalls": 1, "deadWrites": 0, "ignored": 0, "kills": 0, "casts": 0, "castsOnOthers": 0, "unattributed": 0, "gap": 0}
    note.update(over)
    return note


def _moved(pass_no: int, **over: Any) -> dict[str, Any]:
    """The same quiet scan one pass later: only the cursor and the per-pass
    counters have moved."""
    return _note(head=12143991 + pass_no, lastScanned=12143991 + pass_no, logCalls=1 + pass_no, deadWrites=pass_no, ignored=2 * pass_no, **over)


def _tick(e, book, note: dict[str, Any] | str) -> dict[str, Any]:
    e.clock["now"] += 30
    e.keeper.context.scan = {"flags": [], "note": note}
    return e.keeper.tick_book(book, e.clock["now"])


def _ticks(e, book) -> list[dict[str, Any]]:
    return _rows(e, book, "tick")


def test_a_note_moving_only_on_the_cursor_and_the_counters_writes_one_row(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    _tick(e, book, _note())
    n = len(_ticks(e, book))
    assert _ticks(e, book)[-1]["summary"]["killScan"] == _note()
    for p in range(1, 4):
        _tick(e, book, _moved(p))
    assert len(_ticks(e, book)) == n
    # the note of the quiet ticks is not lost: the next row that IS written
    # carries the whole note of its own tick, counters and all
    _tick(e, book, _moved(4, kills=1))
    rows = _ticks(e, book)
    assert len(rows) == n + 1 and rows[-1]["summary"]["killScan"] == _moved(4, kills=1)


def test_a_change_in_kills_in_gap_or_an_appearing_rpc_error_writes_a_row(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    _tick(e, book, _note())
    n = len(_ticks(e, book))
    _tick(e, book, _moved(1, kills=2))
    assert len(_ticks(e, book)) == n + 1
    _tick(e, book, _moved(2, kills=2, gap=490))
    assert len(_ticks(e, book)) == n + 2
    _tick(e, book, _moved(3, kills=2, gap=490, rpcError="eth_getLogs: ConnectionError"))
    assert len(_ticks(e, book)) == n + 3
    # and a cast, a cast on another occupant and an unattributed kill likewise
    for extra in ({"casts": 1}, {"casts": 1, "castsOnOthers": 1}, {"casts": 1, "castsOnOthers": 1, "unattributed": 1}):
        before = len(_ticks(e, book))
        _tick(e, book, _moved(9, **extra))
        assert len(_ticks(e, book)) == before + 1


def test_the_heartbeat_still_writes_a_row_after_quiet_ticks(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    book.config["tickHeartbeat"] = 5
    _tick(e, book, _note())
    n = len(_ticks(e, book))
    for p in range(1, 5):
        _tick(e, book, _moved(p))
    assert len(_ticks(e, book)) == n
    _tick(e, book, _moved(5))
    assert len(_ticks(e, book)) == n + 1


def test_a_static_harvest_and_feed_book_is_unaffected(env):
    env.grant()
    bid = env.start_haf(tickHeartbeat=5)["data"]["book"]["id"]
    book = env.keeper.store.get(bid)

    def ticks():
        return [r for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tick"]

    env.keeper.context.scan = {"flags": [], "note": _note()}
    summary = env.keeper.tick_book(book)
    assert "killScan" not in summary and len(ticks()) == 1
    for p in range(1, 5):
        env.keeper.context.scan = {"flags": [], "note": _moved(p)}
        env.keeper.tick_book(book)
    assert len(ticks()) == 1
    env.keeper.tick_book(book)  # 5 ticks after the last row: the heartbeat
    assert len(ticks()) == 2
