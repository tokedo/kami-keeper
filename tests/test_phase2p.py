"""A refused or failed revive is offered again (a glued kami stays dead).

A kami an attacker CAST Spirit Glue on (item 19001, `NEXT_COOLDOWN+180`)
before killing it refuses a revive pre-send with `revert: kami on
cooldown`. `mark_dead` has already written `dead: True`, so `newly_dead`
never returns it again. The plan entry therefore carries `reviveRetryAt`
— set at every OFFER, whatever became of it — and the tick that reaches
it offers the revive again while the world still reads DEAD.

The books here are `auto` books of TWO kamis on the recorded fixtures
(one kami dies, the book keeps running).

The last two tests: the pause right after the death block (`every book
kami is DEAD`) requires every entry's `reviveRetryAt` to be None (revives
disabled); a pending timer keeps an ALL-dead book running, so the retry
can still fire.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from conftest import Env
from fakechain import FakeChain
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.signer import DryRunSigner
from test_phase2a import CAROL, NODE, T, _decisions, _rows, _seed, _set_party, _start

DEAD_KAMI = 263  # HARVESTING on the node in the fixtures, 171/240 HP
ALIVE_KAMI = 508  # 212/220: healthy, so the book decides nothing for it
RETRY_SEC = 200  # the schema default
COOLDOWN_REVERT = "revert: kami on cooldown: Reverted [code -32000]"


# ------------------------------------------------------------ helpers


def _env(make_keeper, tmp_path=None, chain: FakeChain | None = None, **over: Any) -> tuple[Env, Any]:
    """A lab keeper on the recorded fixtures with ONE two-kami `auto` book,
    the clock at T. With a chain the dry-run signer pre-checks every
    intent, so `chain.refuse` can refuse a revive the way the glue did."""
    e = make_keeper(cap=4)
    if chain is not None:
        cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "kk2p"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "requestTimeoutSec": 5}, env={})
        k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
        clock: dict[str, float] = {"now": T}
        k.clock = lambda: clock["now"]
        k.boot()
        e.keeper.stop_engine()
        e.keeper = k
        e.data_dir = Path(cfg.dataDir)
    else:
        clock = {"now": T}
        e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    assert e.keeper.handle("grant", {"account": "lab"})["ok"]
    book = _start(e, [DEAD_KAMI, ALIVE_KAMI], **over)
    return e, book


def _kill(e: Env, kami: int = DEAD_KAMI) -> None:
    """The world reads the kami DEAD: off the node, no harvest row."""
    _set_party(e.lens, kami, state="DEAD", node=None)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    return e.keeper.tick_book(book, now=e.clock["now"])


def _deaths(e: Env, book) -> list[str]:
    return [r["death"]["outcome"] for r in _rows(e, book, "death")]


def _kinds(e: Env, book) -> list[tuple[str, Any]]:
    """Every row as (kind, the one thing these tests assert on it) — the
    row-ordering assertion, with the decision and tx rows in between."""
    out = []
    for r in e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]:
        if r["kind"] == "death":
            out.append(("death", r["death"]["outcome"]))
        elif r["kind"] == "decision":
            out.append(("decision", r["decision"]["kind"]))
        elif r["kind"] == "tx":
            out.append(("tx", r["tx"]["system"]))
        else:
            out.append((r["kind"], None))
    return out


def _plan(book, kami: int = DEAD_KAMI) -> dict[str, Any]:
    return book.plan[str(kami)]


def _persisted_plan(e: Env, book) -> dict[str, Any]:
    return json.loads((e.keeper.store.book_dir(book.id) / "book.json").read_text(encoding="utf-8"))["plan"]


class _NonceRace(DryRunSigner):
    """Every record comes back `not_sent` with a nonce-race reason — what
    the core sees when the operator's nonce was taken by another sender
    and the one retry was refused too."""

    def _send(self, grant, intents):
        recs = super()._send(grant, intents)
        for r in recs:
            r.status, r.reason = "not_sent", "nonce race: account sequence mismatch, expected 9, got 8"
        return recs


# ------------------------------------------------------------ the retry


def test_refused_revive_is_offered_again_after_retry_sec(make_keeper, tmp_path):
    """The glue's cooldown refuses the revive pre-send; the keeper offers
    it again once `reviveRetrySec` has passed, and it lands."""
    chain = FakeChain(head=12143991)
    e, book = _env(make_keeper, tmp_path, chain=chain)
    chain.refuse = lambda call: COOLDOWN_REVERT if call.system == "system.kami.use.item" else None
    _kill(e)
    t0 = e.clock["now"] + 30
    _tick(e, book)
    rows = _kinds(e, book)
    i_obs = rows.index(("death", "observed"))
    i_dec = rows.index(("decision", "revive"))
    i_tx = next(i for i, r in enumerate(rows) if r == ("tx", "system.kami.use.item"))
    i_ref = next(i for i, r in enumerate(rows) if r[0] == "death" and str(r[1]).startswith("revive-refused: "))
    assert i_obs < i_dec < i_tx < i_ref
    assert rows[i_ref][1] == "revive-refused: " + COOLDOWN_REVERT[:120]
    tx = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["system"] == "system.kami.use.item"]
    assert tx[0]["precheck"] == "reverted"
    assert _plan(book) == {"state": "DEAD", "node": None, "dead": True, "reviveRetryAt": t0 + RETRY_SEC}

    # not due yet (+100 s): no revive decision, no death row
    before = len(_kinds(e, book))
    _tick(e, book, dt=100)
    added = _kinds(e, book)[before:]
    assert not any(k == "death" or (k == "decision" and v == "revive") for k, v in added)
    assert _deaths(e, book) == ["observed", "revive-refused: " + COOLDOWN_REVERT[:120]]
    assert len(_decisions(e, book, "revive")) == 1

    # the glue expires: due, still DEAD -> offered again, and it lands
    chain.refuse = lambda call: None
    _tick(e, book, dt=100)
    assert _deaths(e, book) == ["observed", "revive-refused: " + COOLDOWN_REVERT[:120], "revive-retry", "revived"]
    revives = _decisions(e, book, "revive")
    assert len(revives) == 2 and revives[1]["parameters"]["retry"] is True and "retry" not in revives[0]["parameters"]
    rows = _kinds(e, book)
    i_retry = next(i for i, r in enumerate(rows) if r == ("death", "revive-retry"))
    i_dec2 = [i for i, r in enumerate(rows) if r == ("decision", "revive")][1]
    i_tx2 = [i for i, r in enumerate(rows) if r == ("tx", "system.kami.use.item")][-1]
    i_rev = next(i for i, r in enumerate(rows) if r == ("death", "revived"))
    assert i_retry < i_dec2 < i_tx2 < i_rev


def test_legacy_dead_entry_is_offered_once(make_keeper):
    """A plan entry written without `reviveRetryAt` (an older plan): it is
    offered once more on the first tick that reads the kami DEAD, and the
    offer gives it the key (so it fires once, not every tick)."""
    e, book = _env(make_keeper)
    book.plan[str(DEAD_KAMI)] = {"state": "DEAD", "node": None, "dead": True}
    e.keeper.store.save(book)
    _kill(e)
    t0 = e.clock["now"] + 30
    _tick(e, book)
    assert _deaths(e, book) == ["revive-retry", "revived"]
    assert _plan(book) == {"state": "DEAD", "node": None, "dead": True, "reviveRetryAt": t0 + RETRY_SEC}
    # …and not again on the next tick: the key is there now
    _tick(e, book)
    assert _deaths(e, book) == ["revive-retry", "revived"]


def test_no_item_then_ribbons_arrive(make_keeper):
    """`revive-skipped: no-item` sets the timer too — ribbons shipped
    after the death are used at the next due tick."""
    e, book = _env(make_keeper)
    e.lens.set_inventory(CAROL, 11001, 0)
    _kill(e)
    t0 = e.clock["now"] + 30
    _tick(e, book)
    assert _deaths(e, book) == ["revive-skipped: no-item"]
    assert _plan(book)["reviveRetryAt"] == t0 + RETRY_SEC
    assert _decisions(e, book, "revive") == []
    e.lens.set_inventory(CAROL, 11001, 3)
    _tick(e, book, dt=RETRY_SEC)
    assert _deaths(e, book) == ["revive-skipped: no-item", "revive-retry", "revived"]


def test_landed_revive_with_world_still_dead_is_rechecked(make_keeper):
    """The revive LANDED but the world still reads DEAD (a revert the
    receipt did not tell us about, a lost tx): offered again at the
    timer, and not once the kami is alive."""
    e, book = _env(make_keeper)
    _kill(e)
    _tick(e, book)
    assert _deaths(e, book) == ["observed", "revived"]
    _tick(e, book, dt=RETRY_SEC)
    assert _deaths(e, book) == ["observed", "revived", "revive-retry", "revived"]
    # the kami is alive now: nothing more is offered
    _set_party(e.lens, DEAD_KAMI, state="RESTING", node=None)
    _tick(e, book, dt=RETRY_SEC)
    assert _deaths(e, book) == ["observed", "revived", "revive-retry", "revived"]
    assert len(_decisions(e, book, "revive")) == 2


def test_nonce_race_retries_next_tick(make_keeper):
    """A nonce race is not the chain's verdict on the kami (the nonce-race rule):
    the timer is pulled to `now` and the revive is re-decided next tick."""
    e, book = _env(make_keeper)
    ok = e.keeper.signer
    race = _NonceRace()
    race._grants = ok._grants
    e.keeper.signer = race
    _kill(e)
    _tick(e, book)
    t = e.clock["now"]
    assert _deaths(e, book) == ["observed", "revive-refused: nonce race: account sequence mismatch, expected 9, got 8"]
    assert _plan(book)["reviveRetryAt"] == t
    e.keeper.signer = ok
    _tick(e, book)
    assert _deaths(e, book)[-2:] == ["revive-retry", "revived"]


def test_retry_at_survives_restart(make_keeper, tmp_path):
    """The timer is part of the plan, which is persisted in book.json —
    a keeper restart does not rebuild the plan, so it must be there."""
    chain = FakeChain(head=12143991)
    e, book = _env(make_keeper, tmp_path, chain=chain)
    chain.refuse = lambda call: COOLDOWN_REVERT if call.system == "system.kami.use.item" else None
    _kill(e)
    t0 = e.clock["now"] + 30
    _tick(e, book)
    assert _persisted_plan(e, book)[str(DEAD_KAMI)] == {"state": "DEAD", "node": None, "dead": True, "reviveRetryAt": t0 + RETRY_SEC}


# ------------------------------------------------------------ the config key


def test_revive_retry_sec_defaults_and_bounds(env):
    """200 s on both strategy types; 29 and 3601 are config refusals."""
    env.grant()
    for t in ("auto", "harvestAndFeed"):
        d = env.keeper.handle("describe", {"type": t})["data"]
        prop = d["schema"]["properties"]["reviveRetrySec"]
        assert prop["default"] == 200 and prop["minimum"] == 30 and prop["maximum"] == 3600
    bid = env.start_haf()["data"]["book"]["id"]
    assert env.keeper.store.get(bid).config["reviveRetrySec"] == 200
    for bad in (29, 3601):
        r = env.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(kamiIndex=1002, nodeIndex=62, foodItem=11301, feedBelowHp=100, reviveRetrySec=bad)})
        assert r["error"]["code"] == "BAD_ARGS" and "reviveRetrySec" in r["error"]["message"]
    r = env.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": {"kamiIndices": [1003], "nodeIndex": 62, "accountIndex": 4242, "reviveRetrySec": 29}})
    assert r["error"]["code"] == "BAD_ARGS" and "reviveRetrySec" in r["error"]["message"]


# ------------------------------------------------------------ the all-dead pause


def test_all_dead_book_keeps_running_until_the_retry_lands(make_keeper, tmp_path):
    """The `kami-dead` pause requires that NO revive retry is pending.
    A book whose kamis ALL died in one burst and whose revives were ALL
    refused (a whole book glued) stays RUNNING, so the retry timer can fire —
    a paused book never ticks its deaths again."""
    chain = FakeChain(head=12143991)
    e, book = _env(make_keeper, tmp_path, chain=chain)
    chain.refuse = lambda call: COOLDOWN_REVERT if call.system == "system.kami.use.item" else None
    _kill(e)
    _kill(e, ALIVE_KAMI)
    t0 = e.clock["now"] + 30
    _tick(e, book)
    refused = "revive-refused: " + COOLDOWN_REVERT[:120]
    assert _deaths(e, book) == ["observed", "observed", refused, refused]
    assert len(_decisions(e, book, "revive")) == 2
    for k in (DEAD_KAMI, ALIVE_KAMI):
        assert _plan(book, k) == {"state": "DEAD", "node": None, "dead": True, "reviveRetryAt": t0 + RETRY_SEC}
    assert book.state == "running"  # not paused `kami-dead`: the retry is pending

    # every kami DEAD and nothing due yet: still running, no new death row
    before = len(_kinds(e, book))
    _tick(e, book, dt=100)
    assert book.state == "running"
    assert not any(k == "death" for k, _ in _kinds(e, book)[before:])
    assert _deaths(e, book) == ["observed", "observed", refused, refused]

    # the glue expires: both are offered again, and both land
    chain.refuse = lambda call: None
    _tick(e, book, dt=100)
    assert _deaths(e, book) == ["observed", "observed", refused, refused, "revive-retry", "revive-retry", "revived", "revived"]
    assert book.state == "running"


def test_all_dead_with_revives_disabled_still_pauses(make_keeper):
    """The one path that writes `reviveRetryAt: None` — revives disabled —
    is the one that still pauses: nothing is pending, so no tick could ever
    bring the book back on its own."""
    e, book = _env(make_keeper, reviveItems=[])
    _kill(e)
    _kill(e, ALIVE_KAMI)
    _tick(e, book)
    assert _deaths(e, book) == ["revive-skipped: disabled", "revive-skipped: disabled"]
    assert _decisions(e, book, "revive") == []
    for k in (DEAD_KAMI, ALIVE_KAMI):
        assert _plan(book, k) == {"state": "DEAD", "node": None, "dead": True, "reviveRetryAt": None}
    assert book.state == "paused" and book.pausedReason == "kami-dead"
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["state"] == "paused" and st["pausedReason"] == "kami-dead"
    paused = [r for r in _rows(e, book, "lifecycle") if r["event"] == "paused"]
    assert paused[-1]["reason"] == "every book kami is DEAD"
