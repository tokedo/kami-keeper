"""An `auto` REST book rests from the chain under a lens outage:

  (i)    the floor rest itself: one batched `harvest stop` for the kamis
         the chain shows HARVESTING on the node at or below the floor,
         `underChain` on the decision, the detail rows and the tx rows,
         the pending plan, one `deviation` row per kami offered;
  (ii)   15 at the floor -> two intents (12 + 3, the stop batch cap);
  (iii)  the same outage never attempts a kami twice, landed or not;
  (iv)   the lens comes back and falls over again: a NEW outage may
         offer the same kami again;
  (v)    `chainRests` is persisted and restores (see the note on the
         test — the keeper-level outage clock is NOT persisted);
  (vi)   `chainFallback` false on a REST book: the plain gated tick;
  (vii)  an outage shorter than `chainFallbackAfterSec`: `waiting…`;
  (viii) a FEED book under the same reading feeds and never rests.
"""

from __future__ import annotations

from typing import Any

from keeper.runtime import PERSISTED_KEYS, load_runtime, restore_into
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.base import HARVESTING, RESTING
from test_chainhp import FakeHpReader, _chain_env, _tick
from test_phase2a import BOOK_43, NODE, _rows, _seed, _start

BLOCK = 12278391


def _env(make_keeper, tmp_path, reader: FakeHpReader, kamis: list[int], **over: Any):
    """A lab keeper with the recorded fixtures, one `auto` REST book on the
    node and the scripted chain reader; the lens still LIVE."""
    e = _chain_env(make_keeper, tmp_path, reader)
    _seed(e.lens)
    book = _start(e, kamis, **over)
    return e, book


def _devs(e, book) -> list[dict[str, Any]]:
    return [r["deviation"] for r in _rows(e, book, "deviation")]


def test_a_rest_book_stops_the_kamis_the_chain_shows_at_the_floor_and_only_those(make_keeper, tmp_path):
    # the first kami exactly at the floor (10 % of 240 = 24), the second below it (22), the third above
    reader = FakeHpReader({263: ("HARVESTING", 24, 240, NODE), 508: ("HARVESTING", 12, 220, NODE), 699: ("HARVESTING", 100, 220, NODE)})
    e, book = _env(make_keeper, tmp_path, reader, [263, 508, 699], chainFallbackAfterSec=0)
    e.lens.mode = "not-ready"
    s = _tick(e, book)
    assert "lens-not-live" in s["degraded"] and s["underChain"] is True and s["chainBlock"] == BLOCK and reader.reads == 1
    assert s["decisions"] == ["rest"] and s["chainRested"] == [263, 508] and s["chainHp"] == {"263": 24, "508": 12, "699": 100}
    d = _rows(e, book, "decision")[-1]["decision"]["parameters"]
    assert d["kamis"] == [263, 508] and d["batchSize"] == 2 and d["regenMethod"] == "REST" and d["underChain"] is True
    assert [(m["kami"], m["hp"], m["hpTotal"], m["hpProjected"], m["restFloor"]) for m in d["detail"]] == [(263, 24, 240, 24, 24), (508, 12, 220, 12, 22)]
    assert all(m["bound"] == 0 and m["boundBy"] is None and m["underChain"] is True and m["chainBlock"] == BLOCK for m in d["detail"])
    tx = [r for r in _rows(e, book, "tx")]
    assert [t["tx"]["system"] for t in tx] == ["system.harvest.stop"] * len(tx) and len(tx) >= 1
    assert all(t["tx"]["underChain"] is True and t["tx"]["chainBlock"] == BLOCK and t["degraded"] == ["lens-not-live"] for t in tx)
    assert [r.system for r in e.keeper.signer.recorded] == ["system.harvest.stop"]
    # the plan: the two are RESTING `pending` (an AllowFailure stop), the third untouched
    assert book.plan["263"] == {"state": RESTING, "node": None, "pending": True} and book.plan["508"] == {"state": RESTING, "node": None, "pending": True}
    assert book.plan["699"] == {"state": HARVESTING, "node": NODE}
    devs = _devs(e, book)
    assert [x["kami"] for x in devs] == [263, 508] and all(x["action"] == "rest" and x["chainBlock"] == BLOCK for x in devs)
    assert [(x["hpProjected"], x["restFloor"]) for x in devs] == [(24, 24), (12, 22)]
    assert all("chain" in x["reason"] for x in devs)
    # (iii) one attempt per kami per chainFeedCooldownSec —
    # the chain still shows the two HARVESTING at the floor (the stop did
    # not apply): nothing is offered again inside the cooldown
    s2 = _tick(e, book)
    assert s2["decisions"] == [] and s2["chainRested"] == [] and s2["chainHp"] == {"263": 24, "508": 12, "699": 100}
    assert len(e.keeper.signer.recorded) == 1 and _devs(e, book) == devs and reader.reads == 2
    # (iv) a NEW outage inside the cooldown offers nothing either: the guard
    # is a clock per kami, not the outage's identity (that is process state
    # and would not survive a keeper restart)
    e.lens.mode, e.lens.lag = "stale", 500
    s3 = _tick(e, book)
    assert s3["underStale"] is True and s3["decisions"] == [] and e.keeper.lens_outage_since is None
    e.lens.mode, e.lens.lag = "not-ready", 0
    s4 = _tick(e, book)
    assert s4["underChain"] is True and s4["decisions"] == [] and s4["chainRested"] == [] and len(e.keeper.signer.recorded) == 1
    # once the cooldown has passed a stop that did not land is tried again
    e.clock["now"] += 1800
    s5 = _tick(e, book)
    assert s5["underChain"] is True and s5["decisions"] == ["rest"] and s5["chainRested"] == [263, 508]
    assert len(e.keeper.signer.recorded) == 2 and [x["kami"] for x in _devs(e, book)] == [263, 508, 263, 508]
    # the new surfaces carry no policy language
    frames = [e.keeper.handle("describe", {"type": "auto"}), e.keeper.handle("status"), e.keeper.handle("logs"), e.keeper.handle("ledger", {"book": book.id})]
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)


def test_fifteen_kamis_at_the_floor_go_out_as_two_batched_stops(make_keeper, tmp_path):
    kamis = BOOK_43[:15]
    reader = FakeHpReader({k: ("HARVESTING", 10, 200, NODE) for k in kamis})
    e, book = _env(make_keeper, tmp_path, reader, kamis, chainFallbackAfterSec=0)
    e.lens.mode = "not-ready"
    s = _tick(e, book)
    assert s["decisions"] == ["rest", "rest"] and s["chainRested"] == kamis
    params = [r["decision"]["parameters"] for r in _rows(e, book, "decision")]
    assert [p["batchSize"] for p in params] == [12, 3] and params[0]["kamis"] + params[1]["kamis"] == kamis
    assert len(_devs(e, book)) == 15 and all(r.system == "system.harvest.stop" for r in e.keeper.signer.recorded)
    assert sorted(int(k) for k in e.keeper.runtime[book.id]["chainRests"]) == sorted(kamis)


def test_chain_rests_is_persisted_and_the_restored_guard_holds_across_a_restart(make_keeper, tmp_path):
    """(v) `chainRests` (kami -> the clock of its last attempt) survives the
    runtime round trip: a runtime restored from the file refuses a second
    attempt inside `chainFeedCooldownSec` — whatever the restarted keeper's
    outage clock says — and allows one after it."""
    reader = FakeHpReader({263: ("HARVESTING", 10, 240, NODE)})
    e, book = _env(make_keeper, tmp_path, reader, [263, 508], chainFallbackAfterSec=0)
    e.lens.mode = "not-ready"
    assert _tick(e, book)["chainRested"] == [263]
    assert "chainRests" in PERSISTED_KEYS
    snap, status = load_runtime(e.keeper.store.book_dir(book.id), book.id)
    assert status == "restored" and list(snap["chainRests"]) == ["263"]
    rt: dict[str, Any] = {}
    restore_into(rt, snap)
    strategy = e.keeper.strategies[book.id]
    reading = reader.read([263, 508], NODE, e.clock["now"])
    assert strategy.assess_under_chain(reading, e.clock["now"], rt) == []
    assert rt["summaryExtra"]["chainRested"] == [] and rt["deviations"] == []
    later = e.clock["now"] + 1800
    again = strategy.assess_under_chain(reader.read([263, 508], NODE, later), later, rt)
    assert [d.kind for d in again] == ["rest"] and rt["summaryExtra"]["chainRested"] == [263]


def test_a_rest_book_with_chain_fallback_off_or_a_short_outage_signs_nothing(make_keeper, tmp_path):
    reader = FakeHpReader({263: ("HARVESTING", 10, 240, NODE)})
    e, off = _env(make_keeper, tmp_path, reader, [263], chainFallback=False, chainFallbackAfterSec=0)
    waiting = _start(e, [508], chainFallbackAfterSec=300)
    e.lens.mode = "not-ready"
    # (vi) chainFallback false: the plain gated tick, no chain read
    s = _tick(e, off)
    assert s["reason"] == "perception gated" and "underChain" not in s and "chainFallback" not in s and reader.reads == 0
    # (vii) the outage is younger than chainFallbackAfterSec: the wait, stated
    s2 = _tick(e, waiting)
    assert s2["reason"] == "perception gated" and s2["chainFallback"].startswith("waiting") and s2["chainFallbackAfterSec"] == 300
    assert s2["outageSec"] < 300 and reader.reads == 0 and e.keeper.signer.recorded == []
    s3 = _tick(e, waiting, dt=400.0)  # past the wait: the branch, and the second kami is not in the reading
    assert s3["underChain"] is True and s3["decisions"] == [] and reader.reads == 1 and e.keeper.signer.recorded == []


def test_a_feed_book_under_the_same_reading_feeds_and_never_rests(make_keeper, tmp_path):
    # (viii) the FEED branch, unmoved: it feeds at the floor
    reader = FakeHpReader({263: ("HARVESTING", 20, 240, NODE), 508: ("HARVESTING", 100, 220, NODE)})
    e, book = _env(make_keeper, tmp_path, reader, [263, 508], regenMethod="FEED", foodItem=11304, chainFallbackAfterSec=0)
    e.lens.mode = "not-ready"
    s = _tick(e, book)
    assert s["decisions"] == ["feed"] and "rest" not in s["decisions"] and s["chainHp"] == {"263": 20, "508": 100}
    assert "chainRested" not in s and "chainRests" not in e.keeper.runtime[book.id] and _devs(e, book) == []
    assert [r.system for r in e.keeper.signer.recorded] == ["system.kami.use.item"]
