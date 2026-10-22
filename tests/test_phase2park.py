"""The `park` verb:

  a. the whole park on one book: `park` marks it, the next tick writes ONE
     `rest` decision (`parking: true`, every member `parked: true`) and ONE
     `system.harvest.stop`, no start / feed / collect, and the tick after
     the world applied it stops the book itself with the completion facts;
  b. packs and the per-tick cap: PARK_PACK_MAX kamis a pack,
     PARK_PACKS_PER_TICK packs a tick, all of them in ONE burst;
  c. a kami on the chain's action cooldown is deferred, and the park does
     not complete while its stop is owed;
  d. a pack that did not land is re-offered as singles in the same tick; a
     refused single backs off and is offered again after startBackoffSec;
  e. a DEAD kami never completes a park: with a revive item the revive
     rides the same tick as the stops, without one the book stays parking;
  f. no start while parking, whatever a resting kami's HP;
  g. refusals and idempotence: twice = `alreadyParking`; a harvestAndFeed,
     a craft, a stopped and a paused book are all BAD_ARGS;
  h. the mark survives a keeper restart and the park goes on;
  i. a stale lens gates the tick (`parking: true`) and the stale / chain
     assessments are never called while parking;
  j. the protocol and the CLI carry `park` with its one required argument.

The book is the four kamis of `tests/test_phase2ib.py` (`_pack_env`:
RESTING_FOUR put HARVESTING on the recorded node, every attacker synthesized at
threshold 0 so every bound is 0 and nothing but the park ever stops them).
They sit at 200 HP, far above their rest floors of 25 / 24 / 23 / 25, so a
tick decides NOTHING until the park is asked for.

Note on the burst: the dry-run signer assigns no nonces, so "consecutive
nonces on the account's one lane" is asserted here as ONE `_send` of every
intent of the tick — which is what the lane turns into consecutive nonces
(the live-signer side of that is `tests/test_phase2c.py` §1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import CRAFT_CONFIG
from keeper import cli
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.protocol import KeeperError, validate_request
from keeper.signer import DryRunSigner
from keeper.strategies.base import DEAD, HARVESTING, RESTING
from test_phase2a import CAROL, NODE, _apply_plans_to_world, _decisions, _last_tick, _party_row, _rows, _set_party
from test_phase2ib import A, B, C, D, _pack_env

FULL = {A: (200, 30.0), B: (200, 30.0), C: (200, 30.0), D: (200, 30.0)}


# ------------------------------------------------------------ helpers


def _park(e, book) -> dict[str, Any]:
    return e.keeper.handle("park", {"book": book.id})


def _tick(e, book, dt: float = 30.0) -> dict[str, Any]:
    """One tick of this book alone, returning the summary the core built —
    the completion facts ride the RETURNED summary (`parked`), the ledger
    tick row is written before the book stops itself."""
    e.clock["now"] += dt
    return e.keeper.tick_book(book, now=e.clock["now"])


def _kinds(e, book) -> list[str]:
    return [d["kind"] for d in _decisions(e, book)]


def _rest_params(e, book) -> list[dict[str, Any]]:
    return [d["parameters"] for d in _decisions(e, book, "rest")]


def _stop_txs(e, book) -> list[dict[str, Any]]:
    return [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["system"] == "system.harvest.stop"]


def _events(e, book) -> list[str]:
    return [r["event"] for r in _rows(e, book, "lifecycle")]


def _status(e, book) -> dict[str, Any]:
    return e.keeper.handle("status", {"book": book.id})["data"]["books"][0]


def _count_bursts(e) -> list[int]:
    """Replace the signer with one that counts each `_send` and how many
    intents it carried: one entry per burst."""
    bursts: list[int] = []
    ok = e.keeper.signer

    class Counting(DryRunSigner):
        def _send(self, grant, intents):
            bursts.append(len(intents))
            return super()._send(grant, intents)

    s = Counting()
    s._grants = ok._grants
    e.keeper.signer = s
    return bursts


def _revive_balance(e, balance: int) -> None:
    for row in e.lens.inventory[CAROL]["items"]:
        if row["item"]["index"] == 11001:
            row["balance"] = balance


def _kill(e, kami: int) -> None:
    """The world reads the kami DEAD: off the node, no harvest row."""
    _set_party(e.lens, kami, state=DEAD, node=None)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


# ------------------------------------------------------------ a. the whole park


def test_park_marks_the_book_stops_every_harvest_and_then_stops_itself(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    r = _park(e, book)
    assert r["ok"], r
    mark = r["data"]["parking"]
    assert set(mark) == {"since", "by"} and mark["by"] == "agent" and isinstance(mark["since"], str)
    assert r["data"]["book"]["parking"] == mark and r["data"]["book"]["state"] == "running"
    assert _status(e, book)["parking"] == mark
    assert _events(e, book) == ["started", "parking"]
    parking_row = _rows(e, book, "lifecycle")[-1]
    assert parking_row["reason"] == "agent: park" and parking_row["kamis"] == 4 and parking_row["node"] == NODE

    # the next tick: ONE pack of four, nothing else
    s = _tick(e, book)
    assert _kinds(e, book) == ["rest"]
    p = _rest_params(e, book)[0]
    assert p["kamis"] == [A, B, C, D] and p["batchSize"] == 4 and p["parking"] is True
    assert all(m["parked"] is True for m in p["detail"])
    assert [m["kami"] for m in p["detail"]] == [A, B, C, D]
    assert [(t["fn"], t["count"]) for t in _stop_txs(e, book)] == [("executeBatchedAllowFailure", 4)]
    assert s["parking"] is True and s["parkOffered"] == 4 and s["parkPacks"] == 1 and s["parkRemaining"] == 0
    assert s["kamis"]["harvesting"] == 4 and s["kamis"]["resting"] == 0 and "parked" not in s
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True} for k in (A, B, C, D))

    # the world applies it, and the next VALID read completes the park
    _apply_plans_to_world(e)
    s2 = _tick(e, book)
    assert s2["parked"] == {"kamis": 4, "resting": 4, "dead": 0, "elsewhere": 0, "lensBlock": s2["parked"]["lensBlock"], "lensAppliedThrough": None}
    assert isinstance(s2["parked"]["lensBlock"], int) and s2["parked"]["lensBlock"] > 0
    assert s2["parkOffered"] == 0 and s2["parkPacks"] == 0 and s2["parkRemaining"] == 0
    assert book.state == "stopped" and book.reason == {"stopped_by": "agent", "reason": "park complete"}
    assert _events(e, book) == ["started", "parking", "stopped_by"]
    last = _rows(e, book, "lifecycle")[-1]
    assert last["parked"] == s2["parked"] and last["reason"] == "agent: park complete"
    # the mark stays on the stopped book as the history of why it stopped
    assert book.parking == mark and _status(e, book)["parking"] == mark
    # and a stopped book is not ticked again
    e.clock["now"] += 30
    assert e.keeper.tick_due(now=e.clock["now"]) == []
    assert _kinds(e, book) == ["rest"]


# ------------------------------------------------------------ b. the packs and the per-tick cap


def test_the_pack_size_and_the_packs_per_tick_bound_what_leaves_each_tick(make_keeper, monkeypatch):
    e, book = _pack_env(make_keeper, FULL)
    monkeypatch.setattr("keeper.strategies.auto.PARK_PACK_MAX", 2)
    monkeypatch.setattr("keeper.strategies.auto.PARK_PACKS_PER_TICK", 1)
    assert _park(e, book)["ok"]

    s1 = _tick(e, book)
    assert s1["parkOffered"] == 2 and s1["parkPacks"] == 1 and s1["parkRemaining"] == 2
    assert _rest_params(e, book)[0]["kamis"] == [A, B]
    assert [(t["count"]) for t in _stop_txs(e, book)] == [2]
    _apply_plans_to_world(e)

    s2 = _tick(e, book)
    assert s2["parkOffered"] == 2 and s2["parkRemaining"] == 0 and s2["kamis"]["harvesting"] == 2
    assert _rest_params(e, book)[1]["kamis"] == [C, D]
    _apply_plans_to_world(e)

    s3 = _tick(e, book)
    assert s3["parked"]["kamis"] == 4 and s3["parked"]["resting"] == 4
    assert book.state == "stopped"


def test_two_packs_in_one_tick_leave_as_one_burst(make_keeper, monkeypatch):
    e, book = _pack_env(make_keeper, FULL)
    monkeypatch.setattr("keeper.strategies.auto.PARK_PACK_MAX", 2)
    monkeypatch.setattr("keeper.strategies.auto.PARK_PACKS_PER_TICK", 2)
    assert _park(e, book)["ok"]
    bursts = _count_bursts(e)
    s = _tick(e, book)
    assert s["parkOffered"] == 4 and s["parkPacks"] == 2 and s["parkRemaining"] == 0
    assert [p["kamis"] for p in _rest_params(e, book)] == [[A, B], [C, D]]
    assert [t["count"] for t in _stop_txs(e, book)] == [2, 2]
    # ONE burst of both intents: the lane numbers them consecutively
    assert bursts == [2]


# ------------------------------------------------------------ c. the chain's action cooldown


def test_a_kami_on_cooldown_is_deferred_and_holds_the_completion(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    _party_row(e.lens, A)["cooldownSec"] = 120
    assert _park(e, book)["ok"]

    s1 = _tick(e, book)
    assert s1["cooldownDeferred"] == [{"kami": A, "action": "rest", "cooldownSec": 120, "hp": 200}]
    assert s1["kamis"]["cooldown"] == 1 and s1["parkOffered"] == 3 and s1["parkRemaining"] == 1
    assert _rest_params(e, book)[0]["kamis"] == [B, C, D]
    _apply_plans_to_world(e)

    # the cooldown is still on: nothing is offered and the park cannot finish
    s2 = _tick(e, book)
    assert s2["parkOffered"] == 0 and s2["kamis"]["cooldown"] == 1 and "parked" not in s2
    assert book.state == "running" and book.parking is not None

    _party_row(e.lens, A)["cooldownSec"] = 0
    s3 = _tick(e, book)
    assert s3["parkOffered"] == 1 and "cooldownDeferred" not in s3
    assert _rest_params(e, book)[1]["kamis"] == [A]
    _apply_plans_to_world(e)
    assert _tick(e, book)["parked"]["kamis"] == 4
    assert book.state == "stopped"


# ------------------------------------------------------------ d. a pack that did not land


def test_a_pack_that_did_not_land_is_re_offered_as_singles_and_a_refusal_backs_off(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    assert _park(e, book)["ok"]

    class RevertStops(DryRunSigner):
        """Every `harvest stop` reverts on chain: the pack does not land, the
        core re-offers each member as a single, and each single's refusal IS
        the chain's verdict on the kami — the full startBackoffSec wait."""

        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system == "system.harvest.stop":
                    r.status, r.reason = "reverted", "revert: harvest not active"
            return recs

    ok = e.keeper.signer
    bad = RevertStops()
    bad._grants = ok._grants
    e.keeper.signer = bad
    s1 = _tick(e, book)
    assert s1["batchFallbackTx"] == 4
    assert [(t["count"], t.get("batchFallback", False), t["kami"]) for t in _stop_txs(e, book)] == [(4, False, None), (1, True, A), (1, True, B), (1, True, C), (1, True, D)]
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (A, B, C, D))
    assert sorted(int(k) for k in e.keeper.runtime[book.id]["backoff"]) == sorted([A, B, C, D])
    assert {r["deviation"]["action"] for r in _rows(e, book, "deviation")} == {"rest"}
    assert {r["deviation"]["backoffSec"] for r in _rows(e, book, "deviation")} == {3600}

    # inside the backoff: nothing is offered and the park cannot finish
    e.keeper.signer = ok
    s2 = _tick(e, book)
    assert s2["parkOffered"] == 0 and s2["kamis"]["backoff"] == 4 and s2["backoffKamis"] == sorted([A, B, C, D])
    assert "parked" not in s2 and book.state == "running"

    # after startBackoffSec the whole pack is offered again, and it lands
    s3 = _tick(e, book, dt=3601)
    assert s3["parkOffered"] == 4 and "backoffKamis" not in s3
    _apply_plans_to_world(e)
    assert _tick(e, book)["parked"]["kamis"] == 4
    assert book.state == "stopped"


# ------------------------------------------------------------ e. a DEAD kami


def test_a_dead_kami_is_revived_in_the_same_tick_and_the_park_completes_after(make_keeper):
    e, book = _pack_env(make_keeper, {A: (200, 30.0), B: (200, 30.0), C: (200, 30.0)})
    _kill(e, D)
    assert _park(e, book)["ok"]

    s1 = _tick(e, book)
    assert _kinds(e, book) == ["revive", "rest"]
    assert _decisions(e, book, "revive")[0]["parameters"]["item"] == 11001
    assert _rest_params(e, book)[0]["kamis"] == [A, B, C]
    assert s1["kamis"]["dead"] == 1 and s1["parkOffered"] == 3 and "parked" not in s1

    # the world applies both: the three rest, the revived kami is RESTING
    _apply_plans_to_world(e)
    _set_party(e.lens, D, state=RESTING, node=None)
    s2 = _tick(e, book)
    assert s2["parked"] == {"kamis": 4, "resting": 4, "dead": 0, "elsewhere": 0, "lensBlock": s2["parked"]["lensBlock"], "lensAppliedThrough": None}
    assert book.state == "stopped"


def test_without_a_revive_item_the_book_stays_parking(make_keeper):
    e, book = _pack_env(make_keeper, {A: (200, 30.0), B: (200, 30.0), C: (200, 30.0)})
    _revive_balance(e, 0)
    _kill(e, D)
    assert _park(e, book)["ok"]

    s1 = _tick(e, book)
    assert _kinds(e, book) == ["rest"] and s1["kamis"]["dead"] == 1
    assert [r["death"]["outcome"] for r in _rows(e, book, "death")] == ["revive-skipped: no-item"]
    _apply_plans_to_world(e)
    for _ in range(3):
        s = _tick(e, book)
        assert s["kamis"]["dead"] == 1 and s["parkRemaining"] == 0 and s["parkOffered"] == 0
        assert "parked" not in s
        assert book.state == "running" and book.parking is not None
    assert _events(e, book) == ["started", "parking"]


# ------------------------------------------------------------ f. no start while parking


def test_a_resting_kami_at_full_hp_is_never_started_while_parking(make_keeper):
    e, book = _pack_env(make_keeper, {A: (200, 30.0), B: (200, 30.0), C: (200, 30.0)})
    _set_party(e.lens, D, state=RESTING, node=None, hp=(250, 250))
    assert _park(e, book)["ok"]

    s1 = _tick(e, book)
    assert s1["kamis"]["resting"] == 1 and s1["kamis"]["harvesting"] == 3
    assert _kinds(e, book) == ["rest"]
    _apply_plans_to_world(e)
    s2 = _tick(e, book)
    assert _kinds(e, book) == ["rest"]  # still no start, on this tick or the last
    assert s2["parked"]["resting"] == 4 and s2["parked"]["kamis"] == 4


# ------------------------------------------------------------ g. refusals and idempotence


def test_park_is_idempotent_on_a_book_already_parking(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    first = _park(e, book)["data"]
    second = _park(e, book)
    assert second["ok"] and second["data"]["alreadyParking"] is True
    assert second["data"]["book"]["parking"] == first["parking"]
    assert _events(e, book) == ["started", "parking"]  # one row, not two


def test_park_refuses_every_other_book(env):
    env.grant()
    haf = env.start_haf()["data"]["book"]["id"]
    r = env.keeper.handle("park", {"book": haf})
    assert r["error"]["code"] == "BAD_ARGS" and "only an auto book parks" in r["error"]["message"]
    craft = env.keeper.handle("start", {"account": "lab", "strategyType": "craft", "config": dict(CRAFT_CONFIG)})["data"]["book"]["id"]
    assert env.keeper.handle("park", {"book": craft})["error"]["code"] == "BAD_ARGS"
    assert env.keeper.handle("park", {"book": "nope"})["error"]["code"] == "NOT_FOUND"


def test_park_refuses_a_stopped_and_a_paused_auto_book(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    assert e.keeper.handle("pause", {"book": book.id})["ok"]
    r = e.keeper.handle("park", {"book": book.id})
    assert r["error"]["code"] == "BAD_ARGS" and "resume it first" in r["error"]["message"] and "paused" in r["error"]["message"]
    assert book.parking is None
    assert e.keeper.handle("resume", {"book": book.id})["ok"]
    assert e.keeper.handle("stop", {"book": book.id})["ok"]
    r2 = e.keeper.handle("park", {"book": book.id})
    assert r2["error"]["code"] == "BAD_ARGS" and "is stopped" in r2["error"]["message"]
    assert book.parking is None


def test_a_parking_book_still_pauses_stops_and_resumes(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    assert _park(e, book)["ok"]
    mark = book.parking
    assert e.keeper.handle("pause", {"book": book.id})["ok"] and book.parking == mark
    assert e.keeper.handle("resume", {"book": book.id})["ok"] and book.parking == mark
    assert e.keeper.handle("stop", {"book": book.id})["data"]["stopped"] == [book.id]
    assert book.reason == {"stopped_by": "agent", "reason": "stop"} and book.parking == mark


# ------------------------------------------------------------ h. the restart


def test_the_parking_mark_survives_a_keeper_restart(make_keeper):
    e, book = _pack_env(make_keeper, FULL)
    assert _park(e, book)["ok"]
    s1 = _tick(e, book)
    assert s1["parkOffered"] == 4
    _apply_plans_to_world(e)

    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.clock = lambda: e.clock["now"]
    k2.boot()
    e.keeper.stop_engine()
    e.keeper = k2
    assert Path(cfg.dataDir) == e.data_dir
    b2 = k2.store.get(book.id)
    assert b2.state == "running" and b2.parking == book.parking
    assert k2.handle("status", {"book": b2.id})["data"]["books"][0]["parking"] == book.parking
    assert k2.handle("grant", {"account": "lab"})["ok"]
    e.books = [b2]  # type: ignore[attr-defined]
    s2 = _tick(e, b2)
    assert s2["parked"]["kamis"] == 4 and b2.state == "stopped"
    assert [r["event"] for r in _rows(e, b2, "lifecycle")] == ["started", "parking", "resumed", "stopped_by"]


# ------------------------------------------------------------ i. a stale lens gates


def test_a_stale_lens_gates_a_parking_tick_and_the_stale_branch_is_never_called(make_keeper, monkeypatch):
    e, book = _pack_env(make_keeper, FULL)
    assert _park(e, book)["ok"]

    def never(*_a, **_k):
        raise AssertionError("a parking book must not take the stale or chain assessment")

    monkeypatch.setattr("keeper.strategies.auto.Auto.assess_under_stale", never)
    monkeypatch.setattr("keeper.strategies.auto.Auto.assess_under_chain", never)
    e.lens.mode = "stale"
    e.lens.lag = 100
    s = _tick(e, book)
    assert s["acted"] is False and s["reason"] == "perception gated" and s["parking"] is True
    assert any(f.startswith("lens-stale:") for f in s["degraded"])
    assert _decisions(e, book) == [] and _stop_txs(e, book) == []
    assert _last_tick(e, book)["parking"] is True
    assert book.state == "running" and book.parking is not None

    # the lens comes back and the park runs
    e.lens.mode = "live"
    e.lens.lag = 0
    assert _tick(e, book)["parkOffered"] == 4


def test_an_outage_gates_a_parking_tick_with_no_chain_fallback(make_keeper, monkeypatch):
    e, book = _pack_env(make_keeper, FULL, chainFallbackAfterSec=0)
    assert _park(e, book)["ok"]

    def never(*_a, **_k):
        raise AssertionError("a parking book must not take the stale or chain assessment")

    monkeypatch.setattr("keeper.strategies.auto.Auto.assess_under_chain", never)
    e.lens.mode = "not-ready"
    s = _tick(e, book)
    assert s["acted"] is False and s["reason"] == "perception gated" and s["parking"] is True
    assert "chainFallback" not in s and _decisions(e, book) == []


# ------------------------------------------------------------ j. the protocol and the CLI


def test_the_protocol_and_the_cli_carry_park(env, capsys):
    cmds = {c["name"]: c for c in env.keeper.handle("describe")["data"]["commands"]}
    assert cmds["park"]["args"] == [{"name": "book", "type": "string", "required": True, "description": "book id"}]
    assert cmds["park"]["description"].startswith("park a running auto book")
    with pytest.raises(KeeperError) as ei:
        validate_request("park", {"bogus": 1})
    assert ei.value.code == "BAD_ARGS" and "undeclared argument 'bogus'" in ei.value.message
    with pytest.raises(KeeperError):
        validate_request("park", {})  # `book` is required
    rc = cli.main(["park", "--socket", "/nonexistent.sock", "--book", "auto_x"])
    assert rc == cli.EXIT_NO_DAEMON  # the sub-command parsed; only the socket is missing
    assert capsys.readouterr().out
