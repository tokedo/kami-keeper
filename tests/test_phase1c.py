"""Lens-answer validity, feed idempotence, pause bookkeeping and feeding
under a stale lens:

  A. a lens answer whose kami row lacks state / hp.current / node.index
     (while HARVESTING) is INCOMPLETE: a gated tick (`lens-incomplete`),
     never an external change; a lens `status.degraded` carrying
     `stream-stalled…` / `unhealed-ranges…` gates the tick
     (`lens-degraded:<flag>`).
  C. feed idempotence: after a feed lands the kami is not fed again until
     the lens shows it applied or `feedCooldownSec` has elapsed.
  D. `pausedReason` / `pausedAt` in status and `books`; `resume` on an
     external-change pause re-evaluates the world first.
  E. under `lens-stale:<n>` ALONE a harvestAndFeed book may still feed
     (cooldown-guarded, `underStale: true` on its rows) — never start,
     stop or collect; P7 stays off.

(B, the P7 persistence and the fleet-profile re-check, is
tests/test_interference.py.)
"""

from __future__ import annotations

import copy
import csv
import re
from pathlib import Path

import pytest

from keeper.status import is_valid_flag
from keeper.strategies.base import FOOD_HEAL_HP, KamiView, drain_allowance, feed_withheld, kami_view, record_feed

FLOOR = 100  # conftest HAF_CONFIG feedBelowHp
FOOD = 11301  # conftest HAF_CONFIG foodItem: Maple-Flavor Ghost Gum, HP+25


def _start(e, **over):
    e.grant()
    s = e.start_haf(**over)
    assert s["ok"], s
    return e.keeper.store.get(s["data"]["book"]["id"])


def _rows(e, book, kind=None):
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def _events(e, book):
    return [r["event"] for r in _rows(e, book, "lifecycle")]


def _feeds(e):
    return [r for r in e.keeper.signer.recorded if r.system == "system.kami.use.item"]


# ---------------------------------------------------------------- A: validity


def test_a_node_less_harvesting_row_is_an_incomplete_read_not_an_external_change(env):
    """Planned HARVESTING on the node, a stalled lens answers HARVESTING
    with no `node` at all. Read naively that is 'observed HARVESTING on
    node None', an external change that would pause the book; it is an
    INCOMPLETE row and gates the tick instead."""
    book = _start(env)
    env.lens.set_kami(1001, node=None)  # state stays HARVESTING; the node field vanishes
    s = env.keeper.tick_book(book)
    assert s["acted"] is False and s["reason"] == "perception gated"
    assert "lens-incomplete" in s["degraded"] and s["incomplete"] == ["kami 1001: missing node.index"]
    assert book.state == "running" and env.keeper.signer.recorded == []
    assert _events(env, book) == ["started"]  # no external-change, no pause
    assert env.keeper.runtime[book.id].get("contradictionTicks", 0) == 0  # P7 was not evaluated
    st = env.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert "lens-incomplete" in st["degraded"] and is_valid_flag("lens-incomplete")
    tick = _rows(env, book, "tick")[-1]
    assert "lens-incomplete" in tick["degraded"]
    # the field comes back: the next tick is an ordinary one and the plan still stands
    env.lens.set_kami(1001, node=62)
    s2 = env.keeper.tick_book(book)
    assert s2["acted"] is False and "reason" not in s2 and book.plan["1001"] == {"state": "HARVESTING", "node": 62}


def test_a_row_without_state_or_hp_is_incomplete(env):
    book = _start(env)
    full = copy.deepcopy(env.lens.kamis[1001])
    no_hp = {k: v for k, v in full.items() if k != "hp"}
    env.lens.hooks["kami"] = lambda args: no_hp
    s = env.keeper.tick_book(book)
    assert "lens-incomplete" in s["degraded"] and s["incomplete"] == ["kami 1001: missing hp.current"]
    no_state = {k: v for k, v in full.items() if k != "state"}
    env.lens.hooks["kami"] = lambda args: no_state
    s = env.keeper.tick_book(book)
    assert s["incomplete"] == ["kami 1001: missing state"]
    assert env.keeper.signer.recorded == [] and book.state == "running"
    # a RESTING row legitimately has no node: complete
    resting = {**full, "state": "RESTING"}
    resting.pop("node")
    assert kami_view(resting).complete


def test_kami_view_records_the_rate_and_the_missing_fields():
    v = kami_view({"index": 7, "state": "HARVESTING", "hp": {"current": 50, "total": 200}, "hpRatePerHr": "-14.2"})
    assert v.missing == ("node.index",) and v.hp_rate_per_hr == -14.2 and not v.complete
    v = kami_view({"index": 7})
    assert v.missing == ("state", "hp.current") and v.hp_current == 0


@pytest.mark.parametrize("flag,name", [("stream-stalled:128s", "stream-stalled"), ("unhealed-ranges:2", "unhealed-ranges")])
def test_a_stalled_or_unhealed_lens_status_gates_the_tick_with_lens_degraded(env, flag, name):
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200))  # below the floor: a feed is wanted
    env.lens.hooks["status"] = lambda args: {**env.lens._status(), "degraded": [flag]}
    s = env.keeper.tick_book(book)
    expected = f"lens-degraded:{name}"  # the name, never the value
    assert s["acted"] is False and s["reason"] == "perception gated" and expected in s["degraded"]
    assert s["lensDegraded"] == [flag]  # the lens's own text rides the summary
    assert is_valid_flag(expected) and env.keeper.signer.recorded == []
    row = _rows(env, book, "tick")[-1]
    assert row["degraded"] == s["degraded"] and row["summary"]["lensDegraded"] == [flag]
    assert _events(env, book) == ["started"]
    r = env.start_haf()
    assert r["ok"] is False and r["error"]["code"] == "LENS_STALE" and flag in r["error"]["message"]


def test_a_continuing_stall_with_a_moving_value_writes_one_tick_row(env):
    book = _start(env)
    n0 = len(_rows(env, book, "tick"))
    for secs in (128, 158, 188, 218, 248):
        env.lens.hooks["status"] = lambda args, v=secs: {**env.lens._status(), "degraded": [f"stream-stalled:{v}s"]}
        s = env.keeper.tick_book(book)
        assert s["degraded"] == ["lens-degraded:stream-stalled"] and s["lensDegraded"] == [f"stream-stalled:{secs}s"]
    assert len(_rows(env, book, "tick")) == n0 + 1  # the flag did not move, so neither did the summary key
    env.lens.hooks.pop("status")
    env.keeper.tick_book(book)
    assert len(_rows(env, book, "tick")) == n0 + 2


def test_the_stalled_lens_shape_end_to_end(env):
    """Stalled status + node-less rows + HP under the floor: nothing is
    signed, nothing is paused, and the pause never happens once the lens
    recovers with the kami exactly where the plan says."""
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200), node=None)
    env.lens.hooks["status"] = lambda args: {**env.lens._status(), "degraded": ["stream-stalled:128s"]}
    for _ in range(4):
        s = env.keeper.tick_book(book)
        assert s["reason"] == "perception gated" and book.state == "running"
    env.lens.hooks.pop("status")
    for _ in range(4):
        s = env.keeper.tick_book(book)  # status healthy, the row still node-less
        assert "lens-incomplete" in s["degraded"] and book.state == "running"
    assert env.keeper.signer.recorded == [] and _events(env, book) == ["started"]
    env.lens.set_kami(1001, node=62)
    s = env.keeper.tick_book(book)
    assert s["decisions"] == ["feed"] and book.state == "running"


def test_start_refuses_an_incomplete_adoption_read(env):
    env.grant()
    env.lens.set_kami(1001, node=None)
    r = env.start_haf()
    assert r["ok"] is False and r["error"]["code"] == "LENS_STALE" and "incomplete" in r["error"]["message"]
    assert env.keeper.store.all() == []


# ---------------------------------------------------------- C: feed idempotence


def test_a_stalled_lens_that_keeps_serving_the_pre_feed_hp_yields_one_feed_per_cooldown(env):
    """A tick that reads the lens before it has applied a landed feed must
    not feed again: one feed, then `feedWithheld` until feedCooldownSec."""
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200))
    s = env.keeper.tick_book(book, now=1000.0)
    assert s["decisions"] == ["feed"] and len(_feeds(env)) == 1
    guard = env.keeper.runtime[book.id]["feedGuard"]["1001"]
    assert guard == {"lastFeedAt": 1000.0, "lastFeedHp": 40, "expectedHpAfter": 65, "item": FOOD, "heal": 25}
    for t in (1030.0, 1060.0, 1120.0, 1299.0):
        s = env.keeper.tick_book(book, now=t)  # the lens still says 40
        assert s["acted"] is False and s["feedWithheld"]["expectedHpAfter"] == 65 and s["feedWithheld"]["sinceFeedSec"] == int(t - 1000)
        assert len(_feeds(env)) == 1
    s = env.keeper.tick_book(book, now=1300.0)  # cooldown elapsed: exactly one more
    assert s["decisions"] == ["feed"] and len(_feeds(env)) == 2
    assert env.keeper.runtime[book.id]["feedGuard"]["1001"]["lastFeedAt"] == 1300.0
    s = env.keeper.tick_book(book, now=1330.0)
    assert s["acted"] is False and len(_feeds(env)) == 2
    # the ledger explains every withheld tick
    withheld_rows = [r for r in _rows(env, book, "tick") if r["summary"].get("feedWithheld")]
    assert withheld_rows and withheld_rows[0]["summary"]["feedWithheld"]["reason"] == "feed not yet visible in the lens HP read"


def test_a_feed_the_lens_has_applied_clears_the_guard_before_the_cooldown(env):
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200))
    env.keeper.tick_book(book, now=1000.0)
    assert len(_feeds(env)) == 1
    env.lens.set_kami(1001, hp=(64, 200))  # 40 + 25 - 1 of drain: applied, still under the floor
    s = env.keeper.tick_book(book, now=1030.0)
    assert s["decisions"] == ["feed"] and len(_feeds(env)) == 2
    env.lens.set_kami(1001, hp=(150, 200))  # someone else fed it too: above the floor, nothing to do
    s = env.keeper.tick_book(book, now=1060.0)
    assert s["acted"] is False and "feedWithheld" not in s


def test_feed_guard_arithmetic_catalog_heal_cap_and_the_plus_one_rule():
    rt: dict = {}
    g = record_feed(rt, 5, 100.0, hp_before=120, hp_total=200, item=11304)
    assert g["expectedHpAfter"] == 200 and g["heal"] == 100  # capped at max HP
    g = record_feed(rt, 6, 100.0, hp_before=40, hp_total=200, item=99999)
    assert g["expectedHpAfter"] == 41 and g["heal"] is None  # unknown item: +1 rule
    view = lambda hp, rate=None: KamiView(6, "HARVESTING", hp, 200, 62, 0, 0, None, None, rate)  # noqa: E731
    assert feed_withheld(rt, 6, view(40), 130.0, 300) is not None  # same read: not applied
    assert feed_withheld(rt, 6, view(39), 130.0, 300) is not None  # drained: not applied
    assert feed_withheld(rt, 6, view(41), 130.0, 300) is None  # rose: applied
    assert feed_withheld(rt, 6, view(40), 400.0, 300) is None  # cooldown elapsed
    assert feed_withheld(rt, 6, view(41), 130.0, 300, stale=True) is not None  # stale: only the cooldown counts
    # the drain allowance follows the lens's projected rate over the elapsed time
    assert drain_allowance(None, 300) == 1 and drain_allowance(-14.2, 300) == 3 and drain_allowance(-120.0, 3600) == 121
    rt2: dict = {}
    record_feed(rt2, 7, 0.0, hp_before=100, hp_total=200, item=11303)  # +50 -> 150
    assert feed_withheld(rt2, 7, view(147, -14.2), 290.0, 300) is None  # 150 - (1 + ceil(14.2*290/3600)=2) = 147
    assert feed_withheld(rt2, 7, view(146, -14.2), 290.0, 300) is not None


def test_food_heal_table_carries_the_item_heal_values():
    """FOOD_HEAL_HP carries the game item table's heal values (Pom-Pom 50,
    Golden Apple 150, ...)."""
    assert FOOD_HEAL_HP[11302] == 50 and FOOD_HEAL_HP[11303] == 50 and FOOD_HEAL_HP[11304] == 100 and FOOD_HEAL_HP[11312] == 75 and FOOD_HEAL_HP[11313] == 150


def test_a_feed_that_did_not_land_does_not_arm_the_guard(env):
    from types import SimpleNamespace

    book = _start(env)
    strategy = env.keeper.strategies[book.id]
    rt: dict = {}
    from keeper.strategies.base import Decision

    d = Decision("feed", {"kami": 1001, "hp": 40, "hpMax": 200, "item": FOOD}, [object()])  # type: ignore[list-item]
    strategy.on_records([d], [SimpleNamespace(status="not_sent", precheck="reverted")], 1000.0, rt)
    assert "feedGuard" not in rt
    strategy.on_records([d], [SimpleNamespace(status="dry_run", precheck="ok")], 1000.0, rt)
    assert rt["feedGuard"]["1001"]["lastFeedAt"] == 1000.0 and rt["lastFeedAt"] == 1000.0


# ------------------------------------------------------------- D: status/resume


def test_status_and_books_carry_paused_reason_and_paused_at(env):
    book = _start(env)
    st = env.keeper.handle("books", {"account": "lab"})["data"]["books"][0]
    assert st["pausedReason"] is None and st["pausedAt"] is None
    p = env.keeper.handle("pause", {"book": book.id})["data"]["book"]
    assert p["pausedReason"] == "agent" and p["pausedAt"] and p["pausedAt"].endswith("Z")
    st = env.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["pausedReason"] == "agent" and st["pausedAt"] == p["pausedAt"]
    r = env.keeper.handle("resume", {"book": book.id})["data"]["book"]
    assert r["state"] == "running" and r["pausedReason"] is None and r["pausedAt"] is None
    env.lens.set_kami(1001, state="DEAD", node=None)
    env.keeper.tick_book(book)
    st = env.keeper.handle("books", {"account": "lab"})["data"]["books"][0]
    assert st["state"] == "paused" and st["pausedReason"] == "kami-dead"
    # persisted and reloaded
    from keeper.books import BookStore

    reloaded = {b.id: b for b in BookStore(env.data_dir).load_all()}[book.id]
    assert reloaded.pausedReason == "kami-dead" and reloaded.pausedAt == st["pausedAt"]


def test_resume_on_an_external_change_pause_re_evaluates_the_world_first(env):
    book = _start(env)
    env.lens.set_kami(1001, state="HARVESTING", node=63)
    for _ in range(3):
        env.keeper.tick_book(book)
    assert book.state == "paused" and book.pausedReason == "external-change"
    r = env.keeper.handle("resume", {"book": book.id})
    assert r["ok"] is False and r["error"]["code"] == "PLAN_CONTRADICTED"
    assert "node 62" in r["error"]["message"] and "node 63" in r["error"]["message"]
    assert book.state == "paused" and env.keeper.signer.recorded == []
    # an invalid read is not a verdict either way
    env.lens.set_kami(1001, node=None)
    r = env.keeper.handle("resume", {"book": book.id})
    assert r["error"]["code"] == "LENS_STALE" and "incomplete" in r["error"]["message"] and book.state == "paused"
    env.lens.mode = "stale"
    env.lens.lag = 500
    assert env.keeper.handle("resume", {"book": book.id})["error"]["code"] == "LENS_STALE"
    env.lens.mode = "live"
    # the world is back in a state the book can own: the resume re-adopts it and runs
    env.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    r = env.keeper.handle("resume", {"book": book.id})
    assert r["ok"], r
    assert book.state == "running" and book.plan["1001"] == {"state": "RESTING", "node": None} and book.contradictedAt is None
    resumed = [x for x in _rows(env, book, "lifecycle") if x["event"] == "resumed"][-1]
    assert "re-adopted" in resumed["reason"] and resumed["plan"]["1001"]["state"] == "RESTING"
    s = env.keeper.tick_book(book)
    assert s["decisions"] == ["start"]  # the resting kami at full HP is deployed on the book's node


# ------------------------------------------------------ E: feeding under stale


def test_under_a_stale_lens_a_feed_is_signed_once_per_stale_cooldown_and_nothing_else(env):
    """The stale branch runs on staleFeedCooldownSec (1800),
    not feedCooldownSec (300): feeds at t=0 and t=1800, not at t=300."""
    book = _start(env)
    assert book.config["staleFeedCooldownSec"] == 1800 and book.config["feedCooldownSec"] == 300
    env.lens.mode = "stale"
    env.lens.lag = 500  # beyond maxStalenessBlocks 90
    env.lens.set_kami(1001, hp=(40, 200))
    s = env.keeper.tick_book(book, now=0.0)
    assert s["acted"] is True and s["decisions"] == ["feed"] and s["underStale"] is True
    assert any(f.startswith("lens-stale:") for f in s["degraded"]) and len(_feeds(env)) == 1
    dec = _rows(env, book, "decision")[-1]
    tx = _rows(env, book, "tx")[-1]
    assert dec["decision"]["parameters"]["underStale"] is True and any(f.startswith("lens-stale:") for f in dec["degraded"])
    assert tx["tx"]["underStale"] is True and any(f.startswith("lens-stale:") for f in tx["degraded"])
    for t in (30.0, 300.0, 330.0, 1200.0, 1799.0):  # the normal cooldown (300) does NOT clear it
        s = env.keeper.tick_book(book, now=t)
        assert s["acted"] is False and s["feedWithheld"]["reason"] == "lens stale; cooldown not elapsed" and s["feedWithheld"]["feedCooldownSec"] == 1800
        assert len(_feeds(env)) == 1
    env.lens.set_kami(1001, hp=(90, 200))  # even a read that looks applied does not clear the guard under stale
    s = env.keeper.tick_book(book, now=1500.0)
    assert s["acted"] is False and len(_feeds(env)) == 1
    s = env.keeper.tick_book(book, now=1800.0)  # stale cooldown elapsed: exactly one more
    assert s["decisions"] == ["feed"] and len(_feeds(env)) == 2
    assert book.state == "running" and _events(env, book) == ["started"]
    assert all(r.system == "system.kami.use.item" for r in env.keeper.signer.recorded)
    env.lens.mode = "live"  # back on a valid lens the normal 300 s guard applies (t=1800 feed at HP 90 -> expected 115)
    env.lens.set_kami(1001, hp=(90, 200))
    assert env.keeper.tick_book(book, now=1830.0)["acted"] is False
    assert env.keeper.tick_book(book, now=2100.0)["decisions"] == ["feed"]


def test_under_a_stale_lens_no_start_stop_collect_and_no_p7(make_keeper):
    e = make_keeper()
    book = _start(e, collectEvery=60)
    e.keeper.tick_book(book, now=0.0)  # a valid first tick: collect clock starts
    e.lens.mode = "stale"
    e.lens.lag = 500
    s = e.keeper.tick_book(book, now=5000.0)  # collectEvery long elapsed, HP above floor
    assert s["acted"] is False and e.keeper.signer.recorded == []
    e.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))  # would start on a valid read
    s = e.keeper.tick_book(book, now=5030.0)
    assert s["acted"] is False and e.keeper.signer.recorded == [] and book.state == "running"
    e.lens.set_kami(1001, state="HARVESTING", node=63, hp=(40, 200))  # a contradiction with HP under the floor
    for t in (5060.0, 5090.0, 5120.0):
        s = e.keeper.tick_book(book, now=t)
        assert s["acted"] is False and "contradiction" not in s
    assert book.state == "running" and e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0  # P7 stayed off
    assert e.keeper.signer.recorded == []  # off the book's node: no feed either
    e.lens.set_inventory(4242, FOOD, 0)
    e.lens.set_kami(1001, state="HARVESTING", node=62, hp=(40, 200))
    s = e.keeper.tick_book(book, now=5150.0)
    assert s["acted"] is False and e.keeper.signer.recorded == []  # no food: no failsafe stop under stale either


def test_stale_plus_incomplete_or_stalled_stays_gated(env):
    book = _start(env)
    env.lens.mode = "stale"
    env.lens.lag = 500
    env.lens.set_kami(1001, hp=(40, 200), node=None)
    s = env.keeper.tick_book(book, now=1000.0)
    assert s["reason"] == "perception gated" and "lens-incomplete" in s["degraded"] and env.keeper.signer.recorded == []
    env.lens.set_kami(1001, node=62)
    env.lens.hooks["status"] = lambda args: {**env.lens._status(), "degraded": ["stream-stalled:128s"]}
    s = env.keeper.tick_book(book, now=1030.0)
    assert s["reason"] == "perception gated" and "lens-degraded:stream-stalled" in s["degraded"] and env.keeper.signer.recorded == []
