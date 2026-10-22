"""SPEC P3 — created -> running -> paused | stopped | failed, enumerated
reasons, stopped books enumerable, status derived at answer time."""

from __future__ import annotations

import pytest

from keeper.books import STATES, Book, BookError, IllegalTransition, TRANSITIONS
from keeper.status import BOOK_STATUS_KEYS


def _b():
    return Book(id="b1", account="lab", strategyType="craft", config={"recipes": [], "staminaFloor": 0, "tickSeconds": 30, "maxStalenessBlocks": 20})


def test_state_set_and_transitions():
    assert set(STATES) == {"created", "running", "paused", "stopped", "failed"}
    assert TRANSITIONS[("created", "running")] == "started"
    b = _b()
    assert b.transition("running") == "started"
    with pytest.raises(BookError):
        b.transition("paused")  # a pause names its reason
    assert b.transition("paused", paused_reason="agent") == "paused"
    assert b.pausedReason == "agent" and b.pausedAt
    assert b.transition("running") == "resumed"
    assert b.pausedReason is None and b.pausedAt is None
    with pytest.raises(IllegalTransition):
        b.transition("created")
    assert b.transition("stopped", {"stopped_by": "operator", "reason": "x"}) == "stopped_by"
    assert b.terminal and b.reason["stopped_by"] == "operator"
    for dst in STATES:
        with pytest.raises(IllegalTransition):
            b.transition(dst, {"stopped_by": "agent"})


def test_terminal_states_need_an_enumerated_reason():
    b = _b()
    b.transition("running")
    with pytest.raises(BookError):
        b.transition("stopped")
    with pytest.raises(BookError):
        b.transition("stopped", {"stopped_by": "nobody"})
    with pytest.raises(BookError):
        b.transition("failed", {"failed": "weather"})
    assert b.transition("failed", {"failed": "lens"}) == "failed"


def test_status_shape_and_running_ne_executing(env):
    env.grant()
    s = env.start_haf()
    assert s["ok"], s
    b = env.keeper.store.get(s["data"]["book"]["id"])
    st = env.keeper.handle("status", {"book": b.id})["data"]["books"][0]
    assert set(st) == set(BOOK_STATUS_KEYS)
    assert st["state"] == "running" and st["ticksSinceAct"] == 0 and st["expectedTicksBetweenActs"] is not None
    for _ in range(3):
        env.keeper.tick_book(b)  # HP 120 >= floor 100: nothing to do
    st = env.keeper.handle("status", {"book": b.id})["data"]["books"][0]
    assert st["ticksSinceAct"] == 3 and st["lastTickAt"] is not None and st["lastActAt"] is None


def test_stopped_books_stay_enumerable_with_their_reason(env):
    env.grant()
    b = env.keeper.store.get(env.start_haf()["data"]["book"]["id"])
    r = env.keeper.handle("stop", {"book": b.id})
    assert r["ok"] and r["data"]["stopped"] == [b.id]
    listed = env.keeper.handle("books", {"account": "lab"})["data"]["books"]
    assert listed[0]["state"] == "stopped" and listed[0]["reason"]["stopped_by"] == "agent"
    r2 = env.keeper.handle("pause", {"book": b.id})
    assert r2["error"]["code"] == "BAD_ARGS"
