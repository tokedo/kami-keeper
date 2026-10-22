"""SPEC P4 / P1 — fault injection: lens socket absent, lens
answering NOT_READY, stale reconciledThrough. No send, the flag recorded
in status and in the ledger; `start`/`resume` refused with the code."""

from __future__ import annotations

import pytest

from keeper.status import is_valid_flag


def _running_book(env):
    env.grant()
    s = env.start_haf()
    assert s["ok"], s
    return env.keeper.store.get(s["data"]["book"]["id"])


@pytest.mark.parametrize("mode,flag,code", [("absent", "lens-unavailable", "LENS_UNAVAILABLE"), ("not-ready", "lens-not-live", "LENS_UNAVAILABLE"), ("stale", "lens-stale:", "LENS_STALE")])
def test_start_is_refused_with_the_lens_code(make_keeper, mode, flag, code):
    e = make_keeper(lens_mode=mode, lag=100)
    e.grant()
    s = e.start_haf()
    assert s["ok"] is False and s["error"]["code"] == code, s
    assert e.keeper.store.all() == []
    assert e.keeper.signer.recorded == []


def test_a_running_book_does_not_act_on_a_stale_lens_and_says_so(env):
    book = _running_book(env)
    env.lens.mode = "stale"
    env.lens.lag = 100
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is False and any(f.startswith("lens-stale:") for f in summary["degraded"])
    assert env.keeper.signer.recorded == []
    st = env.keeper.handle("status", {"book": book.id})["data"]
    assert any(f.startswith("lens-stale:") for f in st["books"][0]["degraded"])
    rows = env.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    tick = [r for r in rows if r["kind"] == "tick"][-1]
    assert any(f.startswith("lens-stale:") for f in tick["degraded"]) and all(is_valid_flag(f) for f in tick["degraded"])
    assert book.state == "running"  # never errors on staleness


def test_a_running_book_does_not_act_when_the_lens_is_not_ready(env):
    book = _running_book(env)
    env.lens.mode = "not-ready"
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is False and "lens-not-live" in summary["degraded"]
    assert env.keeper.signer.recorded == []


def test_a_running_book_does_not_act_when_the_lens_socket_is_gone(env):
    book = _running_book(env)
    env.lens.stop()
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is False and "lens-unavailable" in summary["degraded"]
    assert env.keeper.signer.recorded == []
    rows = env.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    assert "lens-unavailable" in rows[-1]["degraded"]


def test_within_max_staleness_the_book_acts(env):
    book = _running_book(env)
    env.lens.mode = "stale"
    env.lens.lag = 10  # <= default maxStalenessBlocks 90
    env.lens.set_kami(1001, hp=(40, 200))  # below feedBelowHp 100
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is True and summary["decisions"] == ["feed"]
    assert env.keeper.signer.recorded[0].system == "system.kami.use.item"


def test_unknown_reconciled_through_fails_closed(env):
    book = _running_book(env)
    env.lens.hooks["status"] = lambda args: {**env.lens._status(), "sync": {**env.lens._status()["sync"], "reconciledThrough": None}}
    env.lens.reconciled_through = lambda: None  # meta too
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is False and any(f.startswith("lens-stale:") for f in summary["degraded"])


def test_resume_is_refused_against_a_stale_lens(env):
    book = _running_book(env)
    assert env.keeper.handle("pause", {"book": book.id})["ok"]
    env.lens.mode = "stale"
    env.lens.lag = 100
    r = env.keeper.handle("resume", {"book": book.id})
    assert r["error"]["code"] == "LENS_STALE" and book.state == "paused"
