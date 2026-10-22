"""SPEC P7 — simulation on a scripted node history through the fake lens:
the keeper never resends a plan the world has contradicted (both
profiles); an external harvest-state / node change observed on
`externalChangeTicks` consecutive valid ticks pauses the book;
under fleet the paused book re-checks every tick and re-adopts after
`readoptAfterBlocks` of a world matching the plan; under lab it waits for
`resume`; a feed or level-up by another signer is coexistence."""

from __future__ import annotations

from conftest import HAF_CONFIG


def _start(e, **over):
    e.grant()
    s = e.start_haf(**over)
    assert s["ok"], s
    return e.keeper.store.get(s["data"]["book"]["id"])


def _events(e, book):
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r["event"] for r in rows if r["kind"] == "lifecycle"]


def test_start_adopts_the_observed_harvest(env):
    book = _start(env)
    assert book.plan == {"1001": {"state": "HARVESTING", "node": 62}}


def test_external_stop_pauses_under_lab_after_three_valid_ticks_and_is_never_re_asserted(env):
    book = _start(env)
    env.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))  # another signer stopped it; HP full
    s1 = env.keeper.tick_book(book)
    assert s1["acted"] is False and s1["observedTicks"] == 1 and s1["confirmAfterTicks"] == 3
    assert book.state == "running" and env.keeper.signer.recorded == []  # not paused yet, and NOT re-asserted
    assert _events(env, book) == ["started"]  # a single contradicting read is a tick note, not a lifecycle event
    env.keeper.tick_book(book)
    assert book.state == "running"
    s3 = env.keeper.tick_book(book)
    assert book.state == "paused" and book.pausedReason == "external-change" and book.pausedAt
    assert "paused" in s3["reason"]
    assert env.keeper.signer.recorded == []  # no restart was signed although HP >= resumeHpPct
    assert _events(env, book) == ["started", "external-change", "paused"]
    rows = env.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    ec = [r for r in rows if r.get("event") == "external-change"][0]
    assert ec["observedTicks"] == 3
    for _ in range(3):
        env.keeper.tick_book(book)  # lab: paused is paused; nothing happens
    assert env.keeper.signer.recorded == [] and book.state == "paused"
    assert env.keeper.tick_due() == []  # the engine never ticks a lab-paused book


def test_a_contradiction_that_clears_before_confirmation_never_pauses(env):
    book = _start(env)
    env.lens.set_kami(1001, state="HARVESTING", node=63)
    env.keeper.tick_book(book)
    env.keeper.tick_book(book)
    env.lens.set_kami(1001, state="HARVESTING", node=62)  # back on the book's node (a wrong read, or a round trip)
    s = env.keeper.tick_book(book)
    assert book.state == "running" and "contradiction cleared" in s.get("note", "")
    env.lens.set_kami(1001, state="HARVESTING", node=63)
    env.keeper.tick_book(book)
    env.keeper.tick_book(book)
    assert book.state == "running"  # the streak restarted from zero
    assert _events(env, book) == ["started"]


def test_a_gated_tick_neither_counts_nor_resets_the_streak(env):
    book = _start(env)
    env.lens.set_kami(1001, state="HARVESTING", node=63)
    env.keeper.tick_book(book)
    env.keeper.tick_book(book)
    env.lens.hooks["status"] = lambda args: {**env.lens._status(), "degraded": ["stream-stalled:40s"]}
    s = env.keeper.tick_book(book)
    assert s["reason"] == "perception gated" and book.state == "running"
    env.lens.hooks.pop("status")
    env.keeper.tick_book(book)  # the third VALID contradicting read
    assert book.state == "paused" and book.pausedReason == "external-change"


def test_external_node_change_is_a_contradiction(env):
    book = _start(env)
    env.lens.set_kami(1001, state="HARVESTING", node=63)
    for _ in range(3):
        env.keeper.tick_book(book)
    assert book.state == "paused"
    rows = env.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    ec = [r for r in rows if r.get("event") == "external-change"][0]
    assert "node 62" in ec["reason"] and "node 63" in ec["reason"]


def test_a_feed_by_another_signer_is_coexistence(env):
    book = _start(env)
    env.lens.set_kami(1001, hp=(200, 200))  # someone fed it; still HARVESTING on 62
    summary = env.keeper.tick_book(book)
    assert book.state == "running" and summary["acted"] is False
    rows = env.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    assert not any(r.get("event") == "external-change" for r in rows)


def test_fleet_pauses_then_rechecks_and_readopts_after_the_matching_span(make_keeper):
    """Fleet: the confirmed change pauses the book (pausedReason
    external-change); the engine keeps ticking it; while the kami harvests
    elsewhere it never re-adopts; once the world is in a state the book
    can own for readoptAfterBlocks it re-adopts (logged) and runs."""
    e = make_keeper(profile="fleet", cap=0)
    book = _start(e, readoptAfterBlocks=10)
    e.lens.set_kami(1001, state="HARVESTING", node=63)  # moved to another node by someone else
    for _ in range(3):
        e.keeper.tick_book(book)
    assert book.state == "paused" and book.pausedReason == "external-change" and book.contradictedAt is not None
    st = e.keeper.handle("books", {"account": "lab"})["data"]["books"][0]
    assert st["pausedReason"] == "external-change" and st["pausedAt"] == book.pausedAt
    base = e.keeper.clock() + 10_000
    for i in range(5):
        e.lens.advance(5)
        assert e.keeper.tick_due(now=base + 60 * i) == [book.id]  # fleet ticks the paused book on its period
        s = e.keeper.runtime[book.id]["lastSummary"]
        assert "does not match" in s["reason"]
    assert e.keeper.signer.recorded == [] and book.state == "paused"  # quiet, but still contradicted: never re-adopted
    e.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))  # back: a state the book can own
    s = e.keeper.tick_book(book)
    assert book.state == "paused" and s["matchingBlocks"] == 0 and s["readoptAfterBlocks"] == 10
    e.lens.advance(5)
    e.keeper.tick_book(book)
    assert book.state == "paused" and e.keeper.signer.recorded == []
    e.lens.advance(5)
    s = e.keeper.tick_book(book)
    assert book.state == "running" and book.pausedReason is None and book.contradictedAt is None
    assert _events(e, book) == ["started", "external-change", "paused", "readopted"]
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    readopted = [r for r in rows if r.get("event") == "readopted"][0]
    assert readopted["plan"]["1001"]["state"] == "RESTING"  # what was adopted
    # after the logged re-adopt the book assesses afresh in the same tick: the resting kami at full HP is started
    assert s["decisions"] == ["start"] and [r.system for r in e.keeper.signer.recorded] == ["system.harvest.start"]
    assert book.plan["1001"] == {"state": "HARVESTING", "node": 62}


def test_fleet_matching_span_resets_on_a_contradicting_read(make_keeper):
    e = make_keeper(profile="fleet", cap=0)
    book = _start(e, readoptAfterBlocks=20)
    e.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    for _ in range(3):
        e.keeper.tick_book(book)
    assert book.state == "paused"
    e.lens.advance(15)
    assert e.keeper.tick_book(book)["matchingBlocks"] == 15
    e.lens.set_kami(1001, state="HARVESTING", node=63)  # deployed elsewhere: the span restarts
    e.lens.advance(15)
    assert "does not match" in e.keeper.tick_book(book)["reason"]
    e.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    e.lens.advance(15)
    assert e.keeper.tick_book(book)["matchingBlocks"] == 0 and book.state == "paused"
    e.lens.advance(20)
    e.keeper.tick_book(book)
    assert book.state == "running" and len(e.keeper.signer.recorded) == 1


def test_fleet_readopt_uses_the_schema_default_when_a_persisted_book_lacks_the_key(make_keeper):
    e = make_keeper(profile="fleet", cap=0)
    book = _start(e)
    book.config.pop("readoptAfterBlocks", None)  # a book persisted before the key existed
    e.keeper.store.save(book)
    e.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    for _ in range(3):
        e.keeper.tick_book(book)
    assert book.state == "paused"
    e.lens.advance(239)
    assert e.keeper.tick_book(book)["readoptAfterBlocks"] == 240 and book.state == "paused"
    e.lens.advance(1)
    e.keeper.tick_book(book)
    assert book.state == "running"


def test_lab_never_carries_readopt_after_blocks_and_never_rechecks(env):
    book = _start(env)
    assert "readoptAfterBlocks" not in book.config and book.config["externalChangeTicks"] == 3
    env.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    for _ in range(3):
        env.keeper.tick_book(book)
    assert book.state == "paused"
    env.lens.advance(1000)
    assert env.keeper.tick_due(now=env.keeper.clock() + 10_000) == []
    assert book.state == "paused" and env.keeper.signer.recorded == []


def test_the_keeper_own_actions_are_not_external_changes(env):
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200))
    env.lens.set_inventory(4242, 11301, 0)  # no food: failsafe stop
    s = env.keeper.tick_book(book)
    assert s["decisions"] == ["failsafe-stop"] and book.plan["1001"] == {"state": "RESTING", "node": None}
    env.lens.set_kami(1001, state="RESTING", node=None)  # the world followed the keeper's own stop
    s2 = env.keeper.tick_book(book)
    assert book.state == "running" and "external" not in s2.get("reason", "")
