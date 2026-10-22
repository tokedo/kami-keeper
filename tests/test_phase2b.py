"""Absence handling and the battles seed, on the recorded fixtures:

  2. a kami ABSENT from the party answer: a single such read is a
     gated tick (`lens-incomplete`) that counts; on `externalChangeTicks`
     consecutive otherwise-valid reads the book pauses with a reason
     naming the kami; a lab `resume` answers PLAN_CONTRADICTED while it is
     absent and re-adopts once the row is back; a fleet book re-checking
     the world cannot match while absent and re-adopts after
     `readoptAfterBlocks` once it returns; a row lacking a field elsewhere
     in the same read is a plain gated tick that neither counts nor resets.
  3. `killedAccounts` seeded from the lens `battles` of the book's
     kamis at `start` (ADD only, `kills` untouched, the kamis marked per
     account so a second book / a restart re-reads none), skipped and
     stated on the lifecycle row under Kamiden-degraded feeds or a lens
     refusal, never a gate; the boot path for a persisted book.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from keeper import core as coremod
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import BOOK_43, CAROL, NODE, T, _apply_plans_to_world, _config, _last_tick, _party_row, _rows, _seed, _set_party, _start

BATTLES = json.loads((Path(__file__).with_name("fixtures") / "lens" / "battles-42823-2026-09-15.json").read_text())["data"]


@pytest.fixture
def cp(make_keeper):
    """A lab keeper with the recorded fixtures seeded and the clock at T."""
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    _seed(e.lens)
    e.grant()
    e.books = []  # type: ignore[attr-defined]
    e.clock = clock  # type: ignore[attr-defined]
    return e


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    return e.keeper.tick_book(book, e.clock["now"])


def _remove_from_party(e: Env, kami: int) -> dict[str, Any]:
    row = _party_row(e.lens, kami)
    e.lens.party[CAROL]["kamis"] = [r for r in e.lens.party[CAROL]["kamis"] if r["index"] != kami]
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]
    return row


def _events(e: Env, book) -> list[str]:
    return [r["event"] for r in _rows(e, book, "lifecycle")]


# ------------------------------------------------------------ 2. absence


def test_f1_a_kami_absent_from_the_party_answer_pauses_the_book_after_three_valid_reads_and_resume_re_adopts_when_it_returns(cp):
    e = cp
    book = _start(e, BOOK_43[:5])
    _tick(e, book)
    _apply_plans_to_world(e) if e.books else None
    row = _remove_from_party(e, 263)  # transferred out: no row at all
    s1 = _tick(e, book)
    assert s1["reason"] == "perception gated" and "lens-incomplete" in s1["degraded"] and s1["incomplete"] == ["kami 263: missing row (absent from the party answer)"]
    assert s1["contradiction"] == "kami 263: not in the account's party" and s1["observedTicks"] == 1 and s1["confirmAfterTicks"] == 3
    assert book.state == "running" and _events(e, book) == ["started"]
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert "lens-incomplete" in st["degraded"] and st["pausedReason"] is None
    # a row lacking a field elsewhere in the same read: a plain gated tick that neither counts nor resets
    _party_row(e.lens, 508).pop("node")
    s_gap = _tick(e, book)
    assert s_gap["reason"] == "perception gated" and "contradiction" not in s_gap and s_gap["incomplete"] == ["kami 263: missing row (absent from the party answer)", "kami 508: missing node.index"]
    assert e.keeper.runtime[book.id]["contradictionTicks"] == 1
    _party_row(e.lens, 508)["node"] = {"index": NODE, "name": f"node-{NODE}"}
    s2 = _tick(e, book)
    assert s2["observedTicks"] == 2 and book.state == "running"
    s3 = _tick(e, book)
    assert book.state == "paused" and book.pausedReason == "external-change" and book.pausedAt and "paused" in s3["reason"]
    assert _events(e, book) == ["started", "external-change", "paused"]
    ec = [r for r in _rows(e, book, "lifecycle") if r["event"] == "external-change"][0]
    assert ec["reason"] == "kami 263: not in the account's party" and ec["observedTicks"] == 3 and ec["degraded"] == []  # a valid read, not a lens fault
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["state"] == "paused" and st["pausedReason"] == "external-change"
    assert e.keeper.signer.recorded == [] or all(r.system != "system.harvest.start" for r in e.keeper.signer.recorded)
    # lab: resume is refused with PLAN_CONTRADICTED while the kami is absent, naming it
    r = e.keeper.handle("resume", {"book": book.id})
    assert r["ok"] is False and r["error"]["code"] == "PLAN_CONTRADICTED" and "kami 263: not in the account's party" in r["error"]["message"] and book.state == "paused"
    assert e.keeper.tick_due(now=e.clock["now"] + 10_000) == []  # lab never ticks a paused book
    # the row is back (transferred back, resting at full HP): the resume re-adopts and the book runs
    row = dict(row, state=RESTING, hp={"current": 240, "total": 240, "percent": 100})
    row.pop("node", None)
    e.lens.party[CAROL]["kamis"].append(row)
    r = e.keeper.handle("resume", {"book": book.id})
    assert r["ok"], r
    assert book.state == "running" and book.plan["263"] == {"state": RESTING, "node": None} and e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0
    s4 = _tick(e, book)
    assert "contradiction" not in s4 and book.state == "running"


def test_f1_an_absence_that_clears_before_confirmation_never_pauses(cp):
    e = cp
    book = _start(e, BOOK_43[:3])
    row = _remove_from_party(e, 508)
    _tick(e, book)
    _tick(e, book)
    e.lens.party[CAROL]["kamis"].append(row)  # a lens gap, healed
    s = _tick(e, book)
    assert "contradiction cleared" in s.get("note", "") and book.state == "running" and _events(e, book) == ["started"]
    _remove_from_party(e, 508)
    _tick(e, book)
    _tick(e, book)
    assert book.state == "running"  # the streak restarted from zero


def test_f1_fleet_stays_paused_while_the_kami_is_absent_and_readopts_after_the_span_once_it_returns(make_keeper):
    e = make_keeper(profile="fleet", cap=0)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    e.books = []  # type: ignore[attr-defined]
    _seed(e.lens)
    e.grant()
    book = _start(e, BOOK_43[:4], readoptAfterBlocks=10)
    row = _remove_from_party(e, 263)
    for _ in range(3):
        _tick(e, book)
    assert book.state == "paused" and book.pausedReason == "external-change"
    # re-checking every tick: while the kami is absent the world cannot match (no lens flag: the read is valid)
    for _ in range(3):
        e.lens.advance(5)
        assert e.keeper.tick_due(now=e.clock["now"] + 60) == [book.id]
        e.clock["now"] += 60
        s = e.keeper.runtime[book.id]["lastSummary"]
        assert s["reason"] == "paused: external change; world does not match plan" and s["contradiction"] == "kami 263: not in the account's party"
    assert _rows(e, book, "tick")[-1]["degraded"] == [] and book.state == "paused"
    # the row returns RESTING: the matching span starts, and after readoptAfterBlocks the book re-adopts and runs
    row = dict(row, state=RESTING, hp={"current": 240, "total": 240, "percent": 100})
    row.pop("node", None)
    e.lens.party[CAROL]["kamis"].append(row)
    s = _tick(e, book)
    assert book.state == "paused" and s["matchingBlocks"] == 0
    e.lens.advance(10)
    s = _tick(e, book)
    assert book.state == "running" and _events(e, book) == ["started", "external-change", "paused", "readopted"]
    assert book.plan["263"]["state"] in (RESTING, HARVESTING)  # adopted, then possibly deployed in the same tick


# ------------------------------------------------------------ 3. the battles seed


def _battles_for(kami: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    d = copy.deepcopy(BATTLES)
    d["kami"] = {"index": kami, "name": f"kami-{kami}"}
    d["kills"] = [dict(r, victim={"index": kami}) if r.get("isDeath") else r for r in rows]
    d["stats"] = {"kills": sum(1 for r in rows if not r.get("isDeath")), "deaths": sum(1 for r in rows if r.get("isDeath")), "pnl": 0}
    return d


def _battle_reads(e: Env) -> list[str]:
    return [r["args"][0] for r in e.lens.requests if r["query"] == "battles"]


def _at_T(rows: list[dict[str, Any]], hours: float = 2.0) -> list[dict[str, Any]]:
    """The battles fixture is dated before the synthetic clock T of these
    tests, and a SEEDED row is judged by its own `timestamp` — so re-date the
    rows inside the window and keep the fixture's blocks, which is what the
    assertions below read."""
    return [dict(r, timestamp=T - hours * 3600) for r in rows]


def test_f2_start_seeds_killed_accounts_from_battles_add_only_and_marks_the_kamis_read(cp):
    e = cp
    deaths = [r for r in BATTLES["kills"] if r["isDeath"]]
    assert [r["account"]["index"] for r in deaths] == [2481, 392]
    e.lens.battles[263] = _battles_for(263, _at_T(BATTLES["kills"]))
    e.lens.battles[508] = _battles_for(508, _at_T([dict(deaths[0], blockNumber=12158391)], 3.0))  # the same owner again, earlier (the LAST block, 12168391, is what is kept)
    e.lens.requests.clear()
    book = _start(e, [263, 508, 699])
    started = [r for r in _rows(e, book, "lifecycle") if r["event"] == "started"][0]
    assert started["seedFromBattles"] == {"status": "seeded", "kamisRead": 3, "deaths": 3, "ownersAdded": 2, "pending": 0}
    assert _battle_reads(e) == ["263", "508", "699"]
    o = e.keeper.owners
    assert o.killed_accounts == {2481: {CAROL: 12168391}, 392: {CAROL: 12173391}} and o.kills == {} and o.casts == {}  # ADD only; the window class untouched
    assert o.killed_ours_in_window(2481, CAROL, 48, None) and o.killed_ours_in_window(392, CAROL, 48, None)  # the window check, not an ever-killed flag
    assert e.keeper.strategies[book.id].render_owner_class(2481) == "killed-here-48h"
    doc = json.loads((e.data_dir / "owners.json").read_text())["owners"]
    assert doc["seeded"][str(CAROL)]["kamis"] == [263, 508, 699] and doc["killedAccounts"] == {"2481": {str(CAROL): 12168391}, "392": {str(CAROL): 12173391}}
    assert any("battles seed for account 2849: 3 kamis read, 3 deaths, 2 owner(s) added" in l["line"] for l in e.keeper.logs)
    # a second book of the same account reads only the kamis not yet read
    e.lens.requests.clear()
    book2 = _start(e, [263, 1008, 1248])
    started2 = [r for r in _rows(e, book2, "lifecycle") if r["event"] == "started"][0]
    assert started2["seedFromBattles"]["kamisRead"] == 2 and started2["seedFromBattles"]["status"] == "seeded" and _battle_reads(e) == ["1008", "1248"]
    assert json.loads((e.data_dir / "owners.json").read_text())["owners"]["seeded"][str(CAROL)]["kamis"] == [263, 508, 699, 1008, 1248]
    # a restart re-reads none: the resumed rows say `already seeded`
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    e.lens.requests.clear()
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    assert _battle_reads(e) == [] and k2.owners.killed_ours_in_window(2481, CAROL, 48, None)
    rows = k2.handle("ledger", {"book": book.id, "kind": "lifecycle"})["data"]["rows"]
    assert rows[-1]["event"] == "resumed" and rows[-1]["seedFromBattles"]["status"] == "already seeded"
    # an already-seeded owner keeps its LATEST block — a later death raises it, an earlier one does not lower it
    assert o.seed_killed(2481, CAROL, 12148391) is False and o.killed_accounts[2481][CAROL] == 12168391
    assert o.seed_killed(2481, CAROL, 12178390) is False and o.killed_accounts[2481][CAROL] == 12178390


def test_f2_the_seed_is_skipped_and_stated_under_degraded_feeds_or_a_lens_refusal_and_never_gates(cp, monkeypatch):
    e = cp
    e.lens.status_data["feedsDegraded"] = ["kamiden-stream"]
    book = _start(e, [263, 508])
    started = [r for r in _rows(e, book, "lifecycle") if r["event"] == "started"][0]
    assert started["seedFromBattles"] == {"status": "skipped", "reason": "kamiden feeds degraded"} and book.state == "running" and _battle_reads(e) == []
    assert "kamiden-unavailable" in started["degraded"]
    e.lens.status_data["feedsDegraded"] = []
    # a lens that does not serve the query: skipped, the kamis stay unread (the next start tries again)
    e.lens.battles_mode = "bad-args"
    book2 = _start(e, [699])
    s2 = [r for r in _rows(e, book2, "lifecycle") if r["event"] == "started"][0]["seedFromBattles"]
    assert s2["status"] == "skipped" and s2["reason"] == "lens BAD_ARGS" and s2["kamisRead"] == 0 and e.keeper.owners.seeded_kamis(CAROL) == set()
    e.lens.battles_mode = "ok"
    # a per-kami NOT_FOUND is an empty answer, the pass goes on; a death row by the book's own account is ignored
    e.lens.hooks["battles"] = lambda args: {"__error__": "NOT_FOUND", "message": "no battles"} if args[0] == "1008" else _battles_for(int(args[0]), [dict(r, account={"index": CAROL}) for r in BATTLES["kills"] if r["isDeath"]][:1] + [r for r in BATTLES["kills"] if r["isDeath"]][1:])
    book3 = _start(e, [1008, 1248])
    s3 = [r for r in _rows(e, book3, "lifecycle") if r["event"] == "started"][0]["seedFromBattles"]
    assert s3 == {"status": "seeded", "kamisRead": 2, "deaths": 1, "ownersAdded": 1, "pending": 0} and e.keeper.owners.killed_accounts == {392: {CAROL: 12173391}}
    assert e.keeper.owners.seeded_kamis(CAROL) == {1008, 1248}
    e.lens.hooks.pop("battles")
    # the per-pass cap: the rest is pending and read by the next start of the account
    monkeypatch.setattr(coremod, "SEED_MAX_READS", 2)
    e.lens.requests.clear()
    book4 = _start(e, BOOK_43[:5])
    s4 = [r for r in _rows(e, book4, "lifecycle") if r["event"] == "started"][0]["seedFromBattles"]
    assert s4["kamisRead"] == 2 and s4["pending"] == 1 and _battle_reads(e) == ["263", "508"]  # two of the kamis were read by book3
    e.lens.requests.clear()
    _start(e, BOOK_43[:5])
    assert _battle_reads(e) == ["699"]  # the one pending


def test_f2_boot_seeds_a_persisted_auto_book_and_skips_when_the_lens_is_down(cp):
    e = cp
    e.lens.battles[263] = _battles_for(263, _at_T(BATTLES["kills"]))
    book = _start(e, [263, 508])
    # forget the seed as if the book were stored without it, restart with the lens live: the resumed row seeds
    e.keeper.owners.seeded = {}
    e.keeper.owners.killed_accounts = {}
    e.keeper.owners._dirty = True
    e.keeper.owners.save()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    e.lens.requests.clear()
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    rows = k2.handle("ledger", {"book": book.id, "kind": "lifecycle"})["data"]["rows"]
    assert rows[-1]["event"] == "resumed" and rows[-1]["seedFromBattles"]["status"] == "seeded" and rows[-1]["seedFromBattles"]["kamisRead"] == 2 and _battle_reads(e) == ["263", "508"]
    assert k2.owners.killed_ours_in_window(2481, CAROL, 48, None) and k2.owners.killed_ours_in_window(392, CAROL, 48, None)
    # the lens down at boot: skipped and stated, the book still resumes; the kamis stay unread
    k2.owners.seeded = {}
    k2.owners._dirty = True
    k2.owners.save()
    e.lens.stop()
    k3 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k3.boot()
    rows = k3.handle("ledger", {"book": book.id, "kind": "lifecycle"})["data"]["rows"]
    assert rows[-1]["event"] == "resumed" and rows[-1]["seedFromBattles"] == {"status": "skipped", "reason": "lens not answering"}
    assert k3.store.get(book.id).state == "running" and k3.owners.seeded_kamis(CAROL) == set()


def test_f2_a_harvest_and_feed_start_carries_no_seed(env):
    env.grant()
    s = env.start_haf()
    rows = env.keeper.handle("ledger", {"book": s["data"]["book"]["id"], "kind": "lifecycle"})["data"]["rows"]
    assert "seedFromBattles" not in rows[0] and not any(r["query"] == "battles" for r in env.lens.requests)
