"""The cross-book STOP pool (`poolStopsAcrossBooks`) and the kill-forced
tick (`killForcesTick`):

  a. both keys absent or `false` reproduce the key-off behaviour byte for
     byte;
  b. the pool, on: one stop transaction, one `tx` row per book sharing
     the hash, `stopPooled` on each tick;
  c. chunking: whole decisions, first fit, 12 kamis a chunk;
  d. two keys, two pools: a different node or a different account never
     shares a transaction;
  e. a book stopped between its tick and the flush leaves the pool;
  f. a chunk that did not land re-offers each pooled DECISION once, and a
     start-join joiner is never re-offered as a single;
  g. no false P7: every book applies its OWN plan and takes the
     read-past-last-act bar from the shared receipt, so its next tick does
     not pause;
  h. degraded paths are never pooled (the chain-backed floor rest, and
     the `under_stale` guard at the seam);
  i. the forced tick: a binding kill pulls the book's clock to `now`;
     a listed owner, a `standing`-only owner and an unattributed kill
     force nothing, and an earlier clock is kept;
  j. the order: the books' own bursts, then the pooled stops, then the
     collects;
  k. the acceptance test — a recorded eight-kill strike replayed with no
     chain and no lens: every one of the 62 exposed kamis' stop in the
     FIRST burst.
"""

from __future__ import annotations

import copy
from typing import Any

from conftest import FIXTURE_OPERATOR_KEY, FIXTURE_OWNER_KEY, HAF_CONFIG
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.chainhp import ChainHpReading
from keeper.protocol import Basis
from keeper.signer import DryRunSigner
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import BOOK_43, CAROL, FRIENDS, NODE, T, _add_occupant, _apply_plans_to_world, _chain_env, _decisions, _last_tick, _preview_threshold, _rows, _seed, _set_party, _start, _tick_all

NODE62 = 62
OTHER = 63
BOTH_ON = {"poolStopsAcrossBooks": True, "killForcesTick": True}


# --------------------------------------------------------------- helpers


def _haf(e, kamis: list[int], node: int = NODE62, account: str = "lab", hp: tuple[int, int] = (40, 200), **over: Any) -> list[Any]:
    """One `harvestAndFeed` book per kami, all HARVESTING on `node` below
    their feed floor. With the food balance 0 (set by `_starve`) each book
    decides the `failsafe-stop` — one stop decision of one kami: the shape
    of many one-kami books of one account sharing a node."""
    books = []
    for k in kamis:
        e.lens.set_kami(k, state=HARVESTING, node=node, hp=hp, account=4242)
        cfg = dict(HAF_CONFIG, kamiIndex=k, nodeIndex=node, **dict({"collectEvery": 100000}, **over))
        r = e.keeper.handle("start", {"account": account, "strategyType": "harvestAndFeed", "config": cfg})
        assert r["ok"], r
        books.append(e.keeper.store.get(r["data"]["book"]["id"]))
    return books


def _starve(e) -> None:
    """No food in the account's inventory: every HARVESTING kami below its
    feed floor decides the failsafe stop instead of a feed."""
    e.lens.set_inventory(4242, int(HAF_CONFIG["foodItem"]), 0)


def _stops(e) -> list[Any]:
    return [r for r in e.keeper.signer.recorded if r.system == "system.harvest.stop"]


def _tx(e, book, system: str = "system.harvest.stop") -> list[dict[str, Any]]:
    return [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["system"] == system]


def _signature(e, books) -> list[Any]:
    """Every book's rows, with what moves between two keeper processes
    (the row clock, the book id, the lens basis) taken out — so two runs
    of the same world can be compared row for row."""
    out = []
    for b in books:
        for r in _rows(e, b):
            row = {k: v for k, v in r.items() if k not in ("at", "basis", "bookId")}
            if isinstance(row.get("tx"), dict):
                row["tx"] = {k: v for k, v in row["tx"].items() if k != "bookId"}
            out.append(row)
    return out


class _Stamping(DryRunSigner):
    """A dry-run signer that reports a hash, a block and a receipt, in the
    LIVE backend's two stages — so the shared hash, the `stage` rows and
    the read-past-last-act bar of a pooled stop are exercised without a
    chain."""

    def __init__(self, block: int = 12278391) -> None:
        super().__init__()
        self.block = block
        self.n = 0

    def _send(self, grant, intents):
        recs = super()._send(grant, intents)
        self.n += 1
        for r in recs:
            r.status, r.txHash, r.block, r.nonce, r.gasUsed = "success", "0x" + f"{self.n:02x}" * 32, self.block, self.n, 1_000_000
        self._report_broadcast(recs)
        return recs


def _refusing(stop_refused):
    """A signer that refuses the stops `stop_refused(kamis)` selects
    pre-send (the `_RefusingSigner` shape of test_phase1d.py)."""

    class S(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for i, r in zip(intents, recs):
                if r.system == "system.harvest.stop" and stop_refused(i.kamis):
                    r.status, r.precheck, r.reason = "not_sent", "reverted", "Harvest: kami starving [code 3]"
            return recs

    return S()


def _swap_signer(e, signer) -> Any:
    signer._grants = e.keeper.signer._grants
    e.keeper.signer = signer
    e.keeper.signer.abort = lambda: False
    return signer


# --------------------------------------------- a. the defaults are key-off


def test_a_the_defaults_off_are_today_three_books_three_stops_and_no_new_keys(make_keeper):
    """Three books, one account, one node, a stop due on each: three
    separate stop transactions in the books' own bursts, no `stopPooled`,
    no `killForcedTick` — and row for row what the same world gives a
    keeper whose books never heard of either key."""
    runs = []
    for over in ({}, {"poolStopsAcrossBooks": False, "killForcesTick": False}):
        e = make_keeper(cap=6)
        e.grant()
        _starve(e)
        books = _haf(e, [1001, 1002, 1003], **over)
        e.keeper.tick_due(now=T)
        stops = _stops(e)
        assert [(r.kami, r.count, r.params, r.fn) for r in stops] == [(k, 1, {"count": 1}, "executeTyped") for k in (1001, 1002, 1003)]
        for b in books:
            rows = _tx(e, b)
            assert len(rows) == 1 and rows[0]["kami"] == b.config["kamiIndex"] and rows[0]["count"] == 1
            assert "batchSize" not in rows[0] and "kamis" not in rows[0]
            s = _last_tick(e, b)
            assert "stopPooled" not in s and "killForcedTick" not in s and s["acted"] is True and s["decisions"] == ["failsafe-stop"]
        assert e.keeper._stop_pool == {}
        runs.append(_signature(e, books))
    assert runs[0] == runs[1]


# ------------------------------------------------------------ b. the pool


def test_b_the_pool_on_sends_one_stop_for_three_books_with_one_row_each_on_the_shared_hash(make_keeper):
    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    signer = _swap_signer(e, _Stamping(block=12278502))
    books = _haf(e, [1001, 1002, 1003], **BOTH_ON)
    e.keeper.tick_due(now=T)
    stops = _stops(e)
    assert len(stops) == 1
    rec = stops[0]
    assert rec.params == {"kamiIndices": [1001, 1002, 1003], "count": 3} and rec.fn == "executeBatchedAllowFailure"
    assert rec.count == 3 and rec.kami is None and rec.batchSize == 3
    for b in books:
        own = b.config["kamiIndex"]
        rows = _tx(e, b)
        assert [r["stage"] for r in rows] == ["broadcast", "receipt"]  # one record, two stages
        for r in rows:
            assert r["txHash"] == rec.txHash and r["batchSize"] == 3 and r["count"] == 3 and r["kami"] is None
            assert r["kamis"] == [own] and r["bookId"] == b.id and "batchFallback" not in r
        s = _last_tick(e, b)
        assert s["stopPooled"] == 1 and s["acted"] is True and s["decisions"] == ["failsafe-stop"]
        rt = e.keeper.runtime[b.id]
        assert rt["ticksSinceAct"] == 0 and rt["lastActAt"] == T and rt["lastTxHash"] == rec.txHash and rt["lastError"] is None
        assert rt["lastActBlock"] == 12278502  # the read-past-last-act bar, per book, from the shared receipt
        assert b.plan[str(own)]["state"] == RESTING  # each book applied its OWN decision's plan
    assert any("1 stop(s) pooled for the account+node burst" in l["line"] for l in e.keeper.logs)
    assert any("pooled chunk of 3" in l["line"] for l in e.keeper.logs)
    assert e.keeper._stop_pool == {} and signer.n == 1


# -------------------------------------------------------------- c. chunks


def test_c_whole_decisions_are_packed_first_fit_at_twelve_kamis_a_chunk(make_keeper):
    """Fourteen one-kami books: 12 + 2, no decision split, every kami
    exactly once and in arrival order."""
    e = make_keeper(cap=20)
    e.grant()
    _starve(e)
    kamis = list(range(2001, 2015))
    _haf(e, kamis, **BOTH_ON)
    e.keeper.tick_due(now=T)
    stops = _stops(e)
    assert [r.count for r in stops] == [12, 2]
    assert [r.params["kamiIndices"] for r in stops] == [kamis[:12], kamis[12:]]
    assert [r.batchSize for r in stops] == [12, 2]
    assert sorted(k for r in stops for k in r.params["kamiIndices"]) == kamis


class _StubStrategy:
    """What `_record_stop` asks of a strategy, and nothing else — so the
    PACKER can be driven with decisions of arbitrary sizes."""

    def __init__(self) -> None:
        self.applied: list[Any] = []
        self.recorded: list[Any] = []

    def apply_plan(self, decision) -> None:
        self.applied.append(decision)

    def on_records(self, decisions, records, now_sec, runtime) -> None:
        self.recorded.append((decisions, records))


def test_c2_a_mixed_group_packs_whole_decisions_first_fit_and_never_splits_one(make_keeper):
    """A strike's arithmetic through the real packer: decisions of 12,
    12, 12, 1 then 11 then 12, 2 pack into SIX chunks of at most 12 — the
    1 and the 11 share one, which is what FIRST fit buys over a fresh
    chunk per book — and no decision is ever split."""
    from keeper.core import PooledStop
    from keeper.strategies.base import Decision
    from keeper.whitelist import Intent

    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    (book,) = _haf(e, [1001])
    strategy = _StubStrategy()
    obs_basis = e.keeper.strategies[book.id].observe(e.keeper.lens).basis
    nxt = iter(range(3001, 3100))
    sizes = [12, 12, 12, 1, 11, 12, 2]
    groups: list[list[int]] = []
    for n in sizes:
        kamis = [next(nxt) for _ in range(n)]
        groups.append(kamis)
        d = Decision("rest", {"kamis": kamis, "batchSize": n, "detail": [{"kami": k} for k in kamis]}, [Intent("system.harvest.stop", "lab", None if n > 1 else kamis[0], {"kamiIndices": kamis, "count": n} if n > 1 else {"count": 1}, book.id)])
        e.keeper._stop_pool.setdefault(("lab", NODE62), []).append(PooledStop(book, strategy, d, obs_basis, [], T, e.keeper.runtime.setdefault(book.id, {})))
    e.keeper.signer.recorded.clear()
    e.keeper._flush_stops()
    burst = _stops(e)
    assert [r.count for r in burst] == [12, 12, 12, 12, 12, 2] and len(burst) == 6 == -(-62 // 12)
    assert burst[3].params["kamiIndices"] == groups[3] + groups[4]  # the 1 and the 11 share chunk 4
    assert sorted(k for r in burst for k in r.params["kamiIndices"]) == sorted(k for g in groups for k in g)
    # no decision was split: every chunk's kamis are a concatenation of whole groups
    for r in burst:
        rest = list(r.params["kamiIndices"])
        while rest:
            g = next(g for g in groups if g[0] == rest[0])
            assert rest[: len(g)] == g
            rest = rest[len(g):]
    assert len(strategy.applied) == len(sizes) and e.keeper._stop_pool == {}


# ------------------------------------------------------- d. two key parts


def test_d_a_different_node_or_a_different_account_never_shares_a_transaction(make_keeper, tmp_path):
    """The pool key is `(account, nodeIndex)`: two books of `lab` on node
    62, two on node 63, and two of a second account on node 62 — three
    pools, three transactions, never one."""
    keys = tmp_path / "two.env"
    keys.write_text(f"LAB_OPERATOR_KEY={FIXTURE_OPERATOR_KEY}\nLAB_OWNER_KEY={FIXTURE_OWNER_KEY}\nLAB2_OPERATOR_KEY={FIXTURE_OPERATOR_KEY}\n", encoding="utf-8")
    e = make_keeper(cap=8, keys_file=keys)
    assert e.grant("lab")["ok"] and e.grant("lab2")["ok"]
    _starve(e)
    here = _haf(e, [1001, 1002], node=NODE62, **BOTH_ON)
    there = _haf(e, [1011, 1012], node=OTHER, **BOTH_ON)
    other_account = _haf(e, [1021, 1022], node=NODE62, account="lab2", **BOTH_ON)
    e.keeper.tick_due(now=T)
    stops = _stops(e)
    assert [(r.account, r.params["kamiIndices"]) for r in stops] == [("lab", [1001, 1002]), ("lab", [1011, 1012]), ("lab2", [1021, 1022])]
    assert all(r.count == 2 and r.batchSize == 2 for r in stops)
    for group in (here, there, other_account):
        hashes = {tuple(_tx(e, b)[0].get("txHash") or ()) for b in group}
        assert len(hashes) == 1
    for b in here + there + other_account:
        assert _last_tick(e, b)["stopPooled"] == 1


# --------------------------------------------- e. a book leaves the pool


def test_e_a_book_stopped_between_its_tick_and_the_flush_leaves_the_pool(make_keeper):
    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    b1, b2, b3 = _haf(e, [1001, 1002, 1003], **BOTH_ON)
    e.keeper._pooling = True
    for b in (b1, b2, b3):
        e.keeper.tick_book(b, now=T)
    assert e.keeper.handle("stop", {"book": b3.id})["ok"]
    e.keeper._pooling = False
    e.keeper._flush_stops()
    stops = _stops(e)
    assert len(stops) == 1 and stops[0].params["kamiIndices"] == [1001, 1002] and stops[0].count == 2
    assert _tx(e, b3) == []
    assert any("stop not sent: the book is stopped" in l["line"] for l in e.keeper.logs if l["book"] == b3.id)
    assert [_tx(e, b)[0]["kamis"] for b in (b1, b2)] == [[1001], [1002]]


# -------------------------------------------------------- f. the fallback


def test_f_a_chunk_that_did_not_land_re_offers_each_decision_once_and_never_a_joiner(make_keeper):
    """Two books, one pooled chunk of two kamis, the CHUNK refused and
    1002's single refused too: 1001's single lands and moves its plan,
    1002 stays due, both singles carry `batchFallback`."""
    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    signer = _swap_signer(e, _refusing(lambda ks: 1002 in ks))
    b1, b2 = _haf(e, [1001, 1002], **BOTH_ON)
    e.keeper.tick_due(now=T)
    stops = _stops(e)
    assert [(r.count, r.status, r.batchSize) for r in stops] == [(2, "not_sent", 2), (1, "dry_run", 1), (1, "not_sent", 1)]
    assert [r.kami for r in stops[1:]] == [1001, 1002]
    for b, landed in ((b1, True), (b2, False)):
        chunk, single = _tx(e, b)
        assert chunk["batchSize"] == 2 and chunk["precheck"] == "reverted" and "batchFallback" not in chunk and chunk["kamis"] == [b.config["kamiIndex"]]
        assert single["batchSize"] == 1 and single["batchFallback"] is True and single["kami"] == b.config["kamiIndex"]
        assert (b.plan[str(b.config["kamiIndex"])]["state"] == RESTING) is landed
        assert (e.keeper.runtime[b.id]["lastError"] is None) is landed
    assert any("re-offering each pooled decision as its own stop once" in l["line"] for l in e.keeper.logs)


def test_f2_a_2ib_joiner_of_a_pooled_pack_that_did_not_land_is_not_re_offered_as_a_single(make_keeper):
    """The start-join pack through the pool: an ANCHOR at its limit plus a
    JOINER ten minutes from its own. The pooled chunk is refused, and
    `split_batch` re-offers the ANCHOR alone — a joiner was never due, so
    it simply joins the next pack."""
    from test_phase2ib import A, B, C, D, _pack_env, _rests

    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (38, 30), D: (27, None)}, stopJoinWindowMin=10, **BOTH_ON)
    _swap_signer(e, _refusing(lambda ks: len(ks) > 1))  # the CHUNK is refused; every single lands
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A, B] and r[0]["detail"][1]["pulled"] is True
    assert _last_tick(e, book)["stopPooled"] == 2
    stops = _stops(e)
    assert [(r.count, r.status, r.kami) for r in stops] == [(2, "not_sent", None), (1, "dry_run", A)]
    assert stops[0].params["kamiIndices"] == [A, B] and stops[0].batchSize == 2
    chunk, single = _tx(e, book)
    assert chunk["kamis"] == [A, B] and chunk["batchSize"] == 2 and "batchFallback" not in chunk
    assert single["kami"] == A and single["kamis"] == [A] and single["batchFallback"] is True and single["batchSize"] == 1
    assert book.plan[str(A)] == {"state": RESTING, "node": None, "pending": True}
    assert book.plan[str(B)] == {"state": HARVESTING, "node": NODE}  # the joiner was not re-offered
    assert any("re-offering each pooled decision as its own stop once" in l["line"] for l in e.keeper.logs)


# ------------------------------------------------------------- g. no P7


def test_g_a_pooled_stop_is_the_books_own_act_and_the_next_tick_does_not_pause(make_keeper):
    """The row carries the book's own id, the book applies its OWN
    decision's plan, and the read-past-last-act bar is set per book from the shared
    receipt — so every book's next tick reads its own plan and stays
    `running`, with no `external-change` lifecycle row."""
    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    _swap_signer(e, _Stamping(block=12278613))
    books = _haf(e, [1001, 1002, 1003], **BOTH_ON)
    e.keeper.tick_due(now=T)
    for b in books:
        assert b.plan[str(b.config["kamiIndex"])] == {"state": RESTING, "node": None}
        assert e.keeper.runtime[b.id]["lastActBlock"] == 12278613
    # the world applies what the chunk signed, and the lens is past the bar
    for b in books:
        e.lens.set_kami(b.config["kamiIndex"], state=RESTING, node=None, hp=(40, 200))
    e.lens.block = 12278691
    e.keeper.tick_due(now=T + 60)
    for b in books:
        assert e.keeper.store.get(b.id).state == "running"
        assert [r for r in _rows(e, b, "lifecycle") if r["event"] != "started"] == []
        s = _last_tick(e, b)
        assert "contradiction" not in s and s.get("reason") != "perception gated"
    assert len(_stops(e)) == 1  # nothing was re-asserted


# ---------------------------------------------- h. degraded is never pooled


def test_h_the_chain_backed_floor_rest_is_never_pooled_even_with_the_keys_on(make_keeper, tmp_path):
    """An `auto` REST book that rests from the CHAIN under a lens
    outage is a degraded-mode act — it goes out in the book's own burst,
    with no `stopPooled` and nothing left in the pool."""
    from test_chainhp import FakeHpReader, _chain_env, _tick

    reader = FakeHpReader({263: ("HARVESTING", 24, 240, NODE), 508: ("HARVESTING", 12, 220, NODE)})
    e = _chain_env(make_keeper, tmp_path, reader)
    _seed(e.lens)
    book = _start(e, [263, 508], chainFallbackAfterSec=0, **BOTH_ON)
    e.lens.mode = "not-ready"
    s = _tick(e, book)
    assert s["underChain"] is True and s["chainRested"] == [263, 508] and "stopPooled" not in s
    assert e.keeper._stop_pool == {}
    assert [(r.system, r.count) for r in e.keeper.signer.recorded] == [("system.harvest.stop", 2)]
    assert _tx(e, book)[0]["underChain"] is True


def test_h2_a_stop_decided_under_a_stale_lens_is_never_pooled(make_keeper):
    """No strategy decides a stop on the stale-lens path (`auto`'s
    REST arm does nothing there and `harvestAndFeed` only feeds), so the
    guard is asserted where it lives: `_act` with `under_stale` pools
    nothing and sends the stop at once."""
    e = make_keeper(cap=6)
    e.grant()
    _starve(e)
    (book,) = _haf(e, [1001], **BOTH_ON)
    strategy = e.keeper.strategies[book.id]
    obs = strategy.observe(e.keeper.lens)
    decisions = strategy.assess(obs, T, e.keeper.runtime.setdefault(book.id, {}))
    assert [d.kind for d in decisions] == ["failsafe-stop"]
    reading = ChainHpReading(block=12278724, at=T, calls=3, bytes=900)
    for stale, chain in ((True, None), (False, reading)):
        e.keeper.signer.recorded.clear()
        s = e.keeper._act(book, strategy, obs, obs.basis, ["lens-stale:200" if stale else "lens-not-live"], [decisions[0]], T, e.keeper.runtime[book.id], under_stale=stale, under_chain=chain)
        assert e.keeper._stop_pool == {} and "stopPooled" not in s
        assert [r.system for r in e.keeper.signer.recorded] == ["system.harvest.stop"]
    # and with neither flag the same decision IS pooled
    e.keeper.signer.recorded.clear()
    s = e.keeper._act(book, strategy, obs, obs.basis, [], [decisions[0]], T, e.keeper.runtime[book.id])
    assert s["stopPooled"] == 1 and e.keeper.signer.recorded == []
    assert list(e.keeper._stop_pool) == [("lab", NODE62)]




# ------------------------------------------------------- i. the forced tick


def _kill_env(make_keeper, tmp_path, chain: FakeChain, kamis: list[int], **over: Any):
    e = _chain_env(make_keeper, tmp_path, chain)
    for b in list(e.keeper.store.all()):
        assert e.keeper.handle("stop", {"book": b.id})["ok"]
    book = _start(e, kamis, **over)
    e.books = [book]  # type: ignore[attr-defined]
    return e, book


def test_i_a_binding_kill_pulls_the_books_clock_into_the_same_pass(make_keeper, tmp_path):
    """`killForcesTick` on, a book due in 300 s, a kill on its node by
    a foreign account: `nextTickAt` becomes `now`, the book assesses in the
    SAME pass, and the tick carries `killForcedTick`."""
    chain = FakeChain(head=12278391)
    e, book = _kill_env(make_keeper, tmp_path, chain, [263, 508], tickSeconds=300, **BOTH_ON)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    _add_occupant(e.lens, 42823, 2481, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(12278381)
    _tick_all(e)
    _apply_plans_to_world(e)
    assert e.keeper.runtime[book.id]["nextTickAt"] == e.clock["now"] + 300
    ticks_before = len(_rows(e, book, "tick"))
    # the kill lands, 30 s into the 300 s window
    chain.head = 12278392
    chain.logs = [liquidate_log(12278392, "0xaa", kami_entity_id(5946), 0)]
    chain.txs["0xaa"] = liquidate_tx("0xaa", harvest_entity_id(5946), kami_entity_id(42823))
    e.clock["now"] += 30
    assert e.keeper.tick_due(now=e.clock["now"]) == [book.id]  # it was NOT due; the kill made it
    assert len(_rows(e, book, "tick")) > ticks_before
    k = _rows(e, book, "kill")[-1]["kill"]
    assert k["attackerAccount"] == 2481 and k["ownerClassAfter"] == "killed-here-48h"
    assert _last_tick(e, book)["killForcedTick"] is True
    assert any("assessing in this pass (was due in 270 s)" in l["line"] for l in e.keeper.logs)
    assert "killForcedTick" not in e.keeper.runtime[book.id]  # popped onto the row, once
    # the next tick is the book's own clock again, and carries nothing
    e.clock["now"] += 300
    e.keeper.tick_due(now=e.clock["now"])
    assert "killForcedTick" not in _last_tick(e, book)


def test_i2_a_listed_owner_an_unattributed_kill_and_an_earlier_clock_force_nothing(make_keeper, tmp_path):
    chain = FakeChain(head=12278391)
    e, book = _kill_env(make_keeper, tmp_path, chain, [263, 508], tickSeconds=300, friends=FRIENDS + [2481], **BOTH_ON)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    _add_occupant(e.lens, 42823, 2481, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(12278381)
    _tick_all(e)
    _apply_plans_to_world(e)
    due = e.keeper.runtime[book.id]["nextTickAt"]
    # a kill by a LISTED owner: the row says `listed`, the clock does not move
    chain.head = 12278392
    chain.logs = [liquidate_log(12278392, "0xaa", kami_entity_id(5946), 0)]
    chain.txs["0xaa"] = liquidate_tx("0xaa", harvest_entity_id(5946), kami_entity_id(42823))
    e.clock["now"] += 30
    assert e.keeper.tick_due(now=e.clock["now"]) == []
    assert e.keeper.runtime[book.id]["nextTickAt"] == due
    assert _rows(e, book, "kill")[-1]["kill"]["ownerClassAfter"] == "listed"
    # an UNATTRIBUTED kill (no attacker account): nothing to classify, nothing forced
    chain.head = 12278393
    chain.logs = [liquidate_log(12278393, "0xbb", kami_entity_id(5946), 0)]
    chain.txs["0xbb"] = liquidate_tx("0xbb", harvest_entity_id(5946), kami_entity_id(42823), "other")
    e.clock["now"] += 30
    assert e.keeper.tick_due(now=e.clock["now"]) == []
    assert e.keeper.runtime[book.id]["nextTickAt"] == due
    assert _rows(e, book, "kill")[-1]["kill"]["attackerAccount"] is None
    # and a book already due EARLIER keeps its earlier clock: forward only
    strategy = e.keeper.strategies[book.id]
    e.keeper.runtime[book.id]["nextTickAt"] = e.clock["now"] - 1000
    e.keeper.owners.record_kill({"block": 12278394, "txHash": "0xcc", "logIndex": 0, "node": NODE, "victimKami": 5946, "victimAccount": CAROL, "attackerKami": 42823, "attackerAccount": 2222, "attribution": "occupant"})
    assert strategy.owner_binds_now(2222, 12278394) is True
    e.keeper._kill_rows({"block": 12278394, "txHash": "0xcc", "logIndex": 0, "node": NODE, "victimKami": 5946, "victimAccount": CAROL, "attackerKami": 42823, "attackerAccount": 2222, "attribution": "occupant"}, {NODE: [book]}, Basis(), [], 12278394, e.clock["now"])
    assert e.keeper.runtime[book.id]["nextTickAt"] == e.clock["now"] - 1000


def test_i3_a_standing_only_owner_does_not_bind_the_predicate_and_forces_no_tick(make_keeper, tmp_path):
    """`owner_binds_now` is the class that renders `killed-here-<window>`
    and nothing else: a `standing` owner already bound the book before any
    kill, so it raises no line and exposes no kami. (Its OWN kill on the
    node makes it `killed-here` by definition, so the exclusion is
    asserted where it is reachable: at the `_kill_rows` seam, with the
    owner on the standing list and no kill of its in the ledger.)"""
    chain = FakeChain(head=12278391)
    e, book = _kill_env(make_keeper, tmp_path, chain, [263, 508], tickSeconds=300, **BOTH_ON)
    e.keeper.context.standing_owners = [1743]  # keeper-level config, read through the context
    strategy = e.keeper.strategies[book.id]
    _tick_all(e)
    due = e.keeper.runtime[book.id]["nextTickAt"]
    assert strategy.render_owner_class(1743, 12278391) == "standing"
    assert strategy.owner_binds_now(1743, 12278391) is False
    kill = {"block": 12278392, "txHash": "0xdd", "logIndex": 0, "node": NODE, "victimKami": 5946, "victimAccount": 9999, "attackerKami": 777, "attackerAccount": 1743, "attribution": "occupant"}
    e.keeper._kill_rows(kill, {NODE: [book]}, Basis(), [], 12278392, e.clock["now"])
    assert _rows(e, book, "kill")[-1]["kill"]["ownerClassAfter"] == "standing"
    assert e.keeper.runtime[book.id]["nextTickAt"] == due and "killForcedTick" not in e.keeper.runtime[book.id]


# ----------------------------------------------------------- j. the order


def test_j_the_order_the_books_own_bursts_then_the_pooled_stops_then_the_collects(make_keeper):
    """With both keys on and four books of one account on one node: the
    revive goes out in its own book's burst FIRST, then the account+node
    stop burst, then the account's batched collect: pooled stops are sent
    before pooled collects."""
    e = make_keeper(cap=8)
    e.grant()
    dead, stop_a, stop_b, collector = 1001, 1002, 1003, 1004
    books = _haf(e, [dead, stop_a, stop_b, collector], hp=(150, 200), reviveItems=[21301], collectEvery=100, **BOTH_ON)
    e.keeper.tick_due(now=T)  # above the feed floor: nothing to do but start the clocks
    assert e.keeper.signer.recorded == []
    e.lens.set_kami(dead, state="DEAD", hp=(0, 200))
    for k in (stop_a, stop_b):
        e.lens.set_kami(k, state=HARVESTING, node=NODE62, hp=(40, 200))
    _starve(e)  # the two below the floor can no longer be fed: the failsafe stop
    e.keeper.tick_due(now=T + 200)
    assert [(r.system, r.kami, r.count) for r in e.keeper.signer.recorded] == [
        ("system.kami.use.item", dead, 1),         # the book's own burst
        ("system.harvest.stop", None, 2),          # then the pooled stops
        ("system.harvest.collect", collector, 1),  # then the collect
    ]
    assert e.keeper.signer.recorded[1].params["kamiIndices"] == [stop_a, stop_b]
    assert [_last_tick(e, b).get("stopPooled") for b in books] == [None, 1, 1, None]
    assert [_last_tick(e, b).get("collectPooled") for b in books] == [None, None, None, 1]
    assert e.keeper._stop_pool == {} and e.keeper._collect_pool == {}


# ------------------------------------------- k. the acceptance test


# eight kills in 70 s by six kamis of one account on the node: (block,
# attacker kami, tx hash prefix), in block order — synthetic values.
RECORDED_KILLS: tuple[tuple[int, int, str], ...] = (
    (12278237, 70001, "0xaa000001"),
    (12278242, 70002, "0xaa000002"),
    (12278244, 70003, "0xaa000003"),
    (12278258, 70004, "0xaa000004"),
    (12278292, 70005, "0xaa000005"),
    (12278294, 70001, "0xaa000006"),
    (12278304, 70003, "0xaa000007"),
    (12278306, 70006, "0xaa000008"),
)
# the kamis each of the three books has exposed: 37 / 11 / 14
EXPOSED: tuple[list[int], ...] = (BOOK_43[:37], BOOK_43[37:48], BOOK_43[48:50] + [12000 + i for i in range(12)])
# the distinct attacker kamis of the eight kills, all owned by one account
ATTACKERS: tuple[int, ...] = (70001, 70002, 70003, 70004, 70005, 70006)


def _hash(prefix: str) -> str:
    """The recorded hash prefix, padded to a 32-byte hash."""
    return prefix + "11" * 28


def test_recorded_kills_replay_every_exposed_kami_stops_in_the_first_burst(make_keeper, tmp_path):
    """The acceptance test of the stop pool and the kill-forced tick: the
    eight kills of `RECORDED_KILLS` — eight kills in 70 s by six kamis of
    one account — replayed with no chain, no lens and no oracle.

    Every attacker is owned by one account, every victim is a kami of the
    books' account on the node. With both keys off the three books assess
    at their own next ticks and stop in separate transactions. With both
    keys on, the kill rows pull all three books into the pass that
    scanned them and their 62 exposed kamis leave in the FIRST burst:
    one `_flush_stops`, ceil(62 / 12) = 6 stop transactions, and nothing
    left for a second pass.
    """
    chain = FakeChain(head=12278231)
    e = _chain_env(make_keeper, tmp_path, chain)
    for b in list(e.keeper.store.all()):
        assert e.keeper.handle("stop", {"book": b.id})["ok"]
    assert sum(len(k) for k in EXPOSED) == 62
    # the attacker account's six kamis, each served ABOVE every exposed kami's HP:
    # with `passiveOwnersBind` false they bind nothing while the owner is clean,
    # and every exposed kami the moment the owner reads killed-here
    for k in ATTACKERS:
        _add_occupant(e.lens, k, 2481, hp=(250, 300), level=45)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))  # the victim of every replayed kill
    for kamis in EXPOSED:
        for k in kamis:
            if not any(r["index"] == k for r in e.lens.party[CAROL]["kamis"]):
                row = copy.deepcopy(e.lens.party[CAROL]["kamis"][0])
                row["index"], row["name"] = k, f"kami-{k}"
                e.lens.party[CAROL]["kamis"].append(row)
            _set_party(e.lens, k, state=HARVESTING, node=NODE, hp=(200, 240), level=40)
            _add_occupant(e.lens, k, CAROL, hp=(200, 240), level=40)
            for att in ATTACKERS:
                _preview_threshold(e.lens, att, k, 220)
    # the three books, one account, one node, both keys on
    books = [_start(e, list(kamis), tickSeconds=300, passiveOwnersBind=False, previewsPerTick=50, **BOTH_ON) for kamis in EXPOSED]
    e.books = books  # type: ignore[attr-defined]
    e.keeper.owners.set_last_scanned(12278231)
    for _ in range(4):  # settle the previews; nothing binds while the owner is clean
        _tick_all(e, 300)  # the books' own clock: one assessment per 300 s
        _apply_plans_to_world(e)
    for b in books:
        s = _last_tick(e, b)
        assert s["previewsPending"] == 0 and s["owners"]["killed-here"] == 0
        assert _decisions(e, b, "rest") == []
        assert e.keeper.runtime[b.id]["nextTickAt"] == e.clock["now"] + 300  # none of them is due
    stops_before = len(_stops(e))
    flushes = {"n": 0}
    real_flush = e.keeper._flush_stops

    def counting_flush():
        flushes["n"] += 1
        return real_flush()

    e.keeper._flush_stops = counting_flush  # type: ignore[method-assign]
    # the eight recorded kills, all scanned in ONE pass, 30 s into the window
    chain.head = RECORDED_KILLS[-1][0] + 1
    chain.logs = [liquidate_log(blk, _hash(h), kami_entity_id(5946), i) for i, (blk, _k, h) in enumerate(RECORDED_KILLS)]
    for blk, att, h in RECORDED_KILLS:
        chain.txs[_hash(h)] = liquidate_tx(_hash(h), harvest_entity_id(5946), kami_entity_id(att))
    e.clock["now"] += 30
    ticked = e.keeper.tick_due(now=e.clock["now"])
    # cause A: all three books assessed in the pass that scanned the kills,
    # 270 s before any of them was due
    assert set(ticked) == {b.id for b in books}
    for b in books:
        assert _last_tick(e, b)["killForcedTick"] is True
        assert _last_tick(e, b)["owners"]["killed-here"] == 1
        assert len(_rows(e, b, "kill")) == len(RECORDED_KILLS)
    # cause B: ONE flush, six transactions, every one of the 62 kamis in it
    assert flushes["n"] == 1
    burst = _stops(e)[stops_before:]
    assert len(burst) == 6 == -(-62 // 12)
    assert [r.count for r in burst] == [12, 12, 12, 12, 12, 2]
    assert sorted(k for r in burst for k in r.params["kamiIndices"]) == sorted(k for ks in EXPOSED for k in ks)
    assert all(r.batchSize == r.count and r.fn == "executeBatchedAllowFailure" for r in burst)
    # and nothing is left for a second pass: every exposed kami is planned
    # RESTING and every book has its own rows on the shared transactions
    assert e.keeper._stop_pool == {}
    for b, kamis in zip(books, EXPOSED):
        assert all(b.plan[str(k)]["state"] == RESTING for k in kamis)
        rows = _tx(e, b)
        assert sorted(k for r in rows for k in r["kamis"]) == sorted(kamis)
        assert all(r["bookId"] == b.id and r["batchSize"] == r["count"] for r in rows)
