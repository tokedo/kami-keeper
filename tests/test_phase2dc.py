"""A BOOT-closed landed act advances the PLAN, not only the ledger
(otherwise a book killed inside a `stage: broadcast` row self-pauses
`external-change` after the restart, its plan contradicting the world):

  §A the join: `Ledger.decision_of_tx()` — each broadcast row claims its
     OWN decision row (the system, the perception basis, the members or
     the count, in ledger order), a single re-offered after a batch
     matches the decision it was split from, and a step with no decision
     row has no answer; `Ledger.receipt_row_hashes()`.
  §B the advance at boot: a batched start reads HARVESTING for every kami
     of its decision and a batched stop reads RESTING, the world no longer
     contradicts the plan, `openTx` 0, `planAdvance` on the row.
  §C what must NOT advance: a `reverted` close, a close whose decision row
     is absent (the row and the log say so), an `origin == "act"` late
     close, and a boot-REOPENED row whose receipt row the act path already
     counted as landed.
  §D the two clocks the same lines own: `lastActBlock` on a
     boot-closed `success` row and not on a `not_sent` one, and the act
     clocks (`lastActAt`, `lastFeedAt`) at the row's own time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, Env
from evm_stubs import BatchingTesterProvider, deploy_world
from keeper.chain import Chain
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.ledger import Ledger
from keeper.lens import LensClient
from keeper.protocol import Basis
from keeper.signer import LiveSigner
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import BOOK_43, CAROL, NODE, T, _add_occupant, _party_row, _rows, _seed, _set_party, _start, _tick_all
from test_phase2c import TEST_GAS_PRICE
from test_phase2d import _external_tx

H1 = "0x" + "a1" * 32
H2 = "0x" + "b2" * 32
H3 = "0x" + "c3" * 32
BASIS = Basis(lensBlock=500, lensReconciledThrough=498)


# ------------------------------------------------------------ helpers


def _auto_env(make_keeper, tmp_path):
    """A lab keeper ARMED on the tester chain over the recorded fixtures (as
    `test_phase2c._live_env`), plus a `keeper()` factory that boots a FRESH
    keeper over the SAME dataDir — the restart the open rows are closed on."""
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper(cap=6)
    flags = {"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "armed": True}
    clock = {"now": T}

    def keeper() -> Keeper:
        signer = LiveSigner(chain, gas_price=TEST_GAS_PRICE, receipt_timeout_sec=5, receipt_poll_sec=0.01, quiescence_wait_sec=2, settle_sec=0, sleep=lambda s: None)
        k = Keeper(load_config(flags=flags, env={}), lens=LensClient(e.lens.socket_path, 5), chain=chain, signer=signer)
        k.clock = lambda: clock["now"]
        k.boot()
        return k

    k = keeper()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(flags["dataDir"])
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    assert k.handle("grant", {"account": "lab"})["ok"] and k.dry_run is False
    return e, provider, w3, chain, keeper


def _led(e: Env, book) -> Ledger:
    return Ledger(e.data_dir / "books" / book.id, book.id, "lab")


def _seed_act(e: Env, book, kamis: list[int], kind: str, system: str, tx_hash: str, nonce: int, basis: Basis = BASIS, with_decision: bool = True, **tx_over: Any) -> dict[str, Any]:
    """The two rows a book leaves behind when the process dies inside the
    burst: the `decision` row of the tick and the `stage: broadcast` row of
    its one intent — same basis, as `_act` writes them. A batched row
    carries `count`, never the indices."""
    led = _led(e, book)
    if with_decision:
        params: dict[str, Any] = {"kamis": list(kamis), "batchSize": len(kamis), "detail": [{"kami": int(k)} for k in kamis]}
        if kind == "start":
            params["node"] = NODE
        else:
            params["regenMethod"] = "REST"
        led.decision(basis, kind, params)
    single = len(kamis) == 1
    tx = {"txHash": tx_hash, "nonce": nonce, "status": "unconfirmed", "kami": int(kamis[0]) if single else None, "system": system, "stage": "broadcast", "fn": "executeTyped", "count": len(kamis), "batchSize": None if single else len(kamis), "precheck": "ok", "gasLimit": 3_500_000, "broadcast": "batch"}
    tx.update(tx_over)
    return led.tx(basis, tx)


def _tx(e: Env, book) -> list[dict[str, Any]]:
    return [r["tx"] for r in _rows(e, book, "tx")]


def _world_harvesting(e: Env, kamis: list[int]) -> None:
    for k in kamis:
        _set_party(e.lens, k, state=HARVESTING, node=NODE)
        if not any(h["kami"]["index"] == k for h in e.lens.nodes[NODE]["harvests"]):
            r = _party_row(e.lens, k)
            _add_occupant(e.lens, k, CAROL, hp=(r["hp"]["current"], r["hp"]["total"]), level=r["level"])


def _world_resting(e: Env, kamis: list[int]) -> None:
    for k in kamis:
        _set_party(e.lens, k, state=RESTING, node=None, hp=(240, 240))
        e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != k]


def _plan(book, kamis: list[int]) -> dict[int, dict[str, Any]]:
    return {int(k): book.plan[str(k)] for k in kamis}


# ------------------------------------------------------------ §A the join


def test_decision_of_tx_claims_each_broadcast_row_for_its_own_decision_row_in_ledger_order(tmp_path):
    led = Ledger(tmp_path / "b1", "b1", "lab")
    b1, b2 = Basis(lensBlock=100, lensReconciledThrough=98), Basis(lensBlock=140, lensReconciledThrough=138)
    base = {"nonce": 1, "status": "unconfirmed", "system": "system.harvest.stop", "stage": "broadcast", "kami": None}
    # one tick, two chunks of the SAME shape: each broadcast row takes its own
    d1 = led.decision(b1, "rest", {"kamis": [11, 12], "batchSize": 2})
    d2 = led.decision(b1, "rest", {"kamis": [21, 22], "batchSize": 2})
    r1 = led.tx(b1, {**base, "txHash": H1, "count": 2})
    r2 = led.tx(b1, {**base, "txHash": H2, "nonce": 2, "count": 2})
    claims = led.decision_of_tx()
    assert claims[H1]["decision"] == d1["decision"] and claims[H2]["decision"] == d2["decision"]
    # a LATER tick's row never claims an older tick's decision: the basis differs
    led.tx(b2, {**base, "txHash": H3, "nonce": 3, "count": 2})
    assert H3 not in led.decision_of_tx()
    # a pooled stop row names its own members: the match is the set, not the count
    led2 = Ledger(tmp_path / "b2", "b2", "lab")
    led2.decision(b1, "rest", {"kamis": [31, 32], "batchSize": 2})
    dd = led2.decision(b1, "rest", {"kamis": [41, 42, 263], "batchSize": 3})
    led2.tx(b1, {**base, "txHash": H1, "kamis": [41, 42, 263], "count": 5, "batchSize": 5})
    assert led2.decision_of_tx()[H1]["decision"] == dd["decision"]
    # a single re-offered after a batch that did not land has no decision row of
    # its own: it matches the decision it was split from, which stays claimed
    led3 = Ledger(tmp_path / "b3", "b3", "lab")
    d = led3.decision(b1, "rest", {"kamis": [51, 52], "batchSize": 2})
    led3.tx(b1, {**base, "txHash": H1, "count": 2})
    led3.tx(b1, {**base, "txHash": H2, "nonce": 2, "kami": 52, "count": 1, "batchFallback": True})
    claims = led3.decision_of_tx()
    assert claims[H1]["decision"] == d["decision"] and claims[H2]["decision"] == d["decision"]
    # a step with no decision row has no answer, and a `receipt` row is never claimed
    led4 = Ledger(tmp_path / "b4", "b4", "lab")
    led4.tx(b1, {**base, "txHash": H1, "count": 2})
    led4.tx(b1, {**base, "txHash": H1, "count": 2, "stage": "receipt", "block": 101, "status": "success"})
    assert led4.decision_of_tx() == {} and led4.receipt_row_hashes() == {H1}


def test_receipt_row_hashes_is_every_hash_with_a_receipt_row_of_any_status(tmp_path):
    led = Ledger(tmp_path / "b", "b", "lab")
    b = Basis(lensBlock=100, lensReconciledThrough=98)
    base = {"nonce": 1, "status": "unconfirmed", "system": "system.harvest.start", "kami": 7}
    led.tx(b, {**base, "txHash": H1, "stage": "broadcast"})
    assert led.receipt_row_hashes() == set()  # only broadcast so far: the act path never reached its receipt stage
    led.tx(b, {**base, "txHash": H1, "stage": "receipt", "reconciled": "receipt read unavailable (…)"})
    led.tx(b, {**base, "txHash": H2, "nonce": 2, "stage": "receipt", "status": "success", "block": 2})
    assert led.receipt_row_hashes() == {H1, H2}  # `unconfirmed` counts: the act path applied that plan


# ------------------------------------------------------------ §B the advance at boot


def test_a_boot_closed_batched_start_advances_the_plan_to_harvesting_for_every_kami_of_its_decision(make_keeper, tmp_path):
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_resting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    assert _plan(book, kamis) == {k: {"state": RESTING, "node": None} for k in kamis}  # the pre-act intent
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)  # the batched start the chain took while the daemon was dead
    _seed_act(e, book, kamis, "start", "system.harvest.start", h, n)
    _world_harvesting(e, kamis)  # the world the restarted keeper reads
    e.keeper.stop_engine()

    k2 = keeper()
    b2 = k2.store.get(book.id)
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "success")]
    assert rows[1]["reconciled"] == "receipt read after restart" and rows[1]["block"] > 0 and rows[1]["count"] == 4
    assert rows[1]["planAdvance"] == "start applied to 4 kami"
    assert _plan(b2, kamis) == {k: {"state": HARVESTING, "node": NODE} for k in kamis}  # the plan the act path would have applied
    strat = k2.strategies[b2.id]
    obs = strat.observe(k2.lens)
    assert strat.external_change(obs) is None and strat.world_matches_plan(obs) is True  # no external-change pause
    assert k2._ledger(b2).open_tx() == {} and k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    assert any("plan start applied to 4 kami" in l["line"] for l in k2.logs)
    # and it survives a SECOND restart: the plan was saved, not only held
    k2.stop_engine()
    k3 = keeper()
    assert _plan(k3.store.get(book.id), kamis) == {k: {"state": HARVESTING, "node": NODE} for k in kamis}
    k3.stop_engine()


def test_a_boot_closed_batched_stop_advances_the_plan_to_resting_for_every_kami_of_its_decision(make_keeper, tmp_path):
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    assert _plan(book, kamis) == {k: {"state": HARVESTING, "node": NODE} for k in kamis}  # the pre-act intent
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_act(e, book, kamis, "rest", "system.harvest.stop", h, n)
    _world_resting(e, kamis)
    e.keeper.stop_engine()

    k2 = keeper()
    b2 = k2.store.get(book.id)
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "success")]
    assert rows[1]["planAdvance"] == "rest applied to 4 kami"
    # RESTING on node None — `pending` until a valid read settles it, exactly
    # what the act path writes for a stop (an AllowFailure member can fail
    # inside a receipt that succeeded)
    assert _plan(b2, kamis) == {k: {"state": RESTING, "node": None, "pending": True} for k in kamis}
    strat = k2.strategies[b2.id]
    obs = strat.observe(k2.lens)
    assert strat.external_change(obs) is None and strat.world_matches_plan(obs) is True
    assert k2._ledger(b2).open_tx() == {} and k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    k2.stop_engine()


# ------------------------------------------------------------ §C what must not advance


def test_a_boot_closed_reverted_row_leaves_the_plan_unchanged(make_keeper, tmp_path):
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_resting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    before = _plan(book, kamis)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_act(e, book, kamis, "start", "system.harvest.start", h, n)
    e.keeper.stop_engine()
    real = chain.receipt
    chain.receipt = lambda hash_: (None if real(hash_) is None else {**real(hash_), "status": 0})  # the chain took it and it REVERTED

    k2 = keeper()
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "reverted")]
    assert "planAdvance" not in rows[1]  # nothing was decided about the plan
    assert _plan(k2.store.get(book.id), kamis) == before == {k: {"state": RESTING, "node": None} for k in kamis}
    assert k2.runtime[book.id].get("lastActBlock") is None  # a reverted step is not a bar either
    k2.stop_engine()
    chain.receipt = real


def test_a_boot_closed_row_whose_decision_row_is_absent_leaves_the_plan_unchanged_and_says_so(make_keeper, tmp_path):
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    before = _plan(book, kamis)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_act(e, book, kamis, "rest", "system.harvest.stop", h, n, with_decision=False)  # the broadcast row alone
    e.keeper.stop_engine()

    k2 = keeper()
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "success")]
    assert rows[1]["planAdvance"] == "not applied: no decision row in the ledger for this step"
    assert _plan(k2.store.get(book.id), kamis) == before  # never a guess
    assert any("not applied: no decision row in the ledger for this step" in l["line"] for l in k2.logs)
    assert k2.runtime[book.id]["lastActBlock"] == rows[1]["block"]  # the row still landed: the lastActBlock bar moves
    k2.stop_engine()


def test_an_act_origin_late_close_does_not_advance_the_plan_a_second_time(make_keeper, tmp_path):
    """A step whose receipt read did not answer is registered by the
    ACT path, which already applied that decision's plan (`_landed` counts
    `unconfirmed` as landed). Its late close must not apply it again — the
    book has decided on, and the stale intent would fight the world."""
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    row = _seed_act(e, book, kamis, "rest", "system.harvest.stop", h, n)
    rt = e.keeper.runtime[book.id]
    e.keeper._register_open_tx(rt, h, row)  # as `_receipt_rows` does for an unconfirmed receipt row
    assert rt["_openTx"][h]["_opened"] == "act"
    before = dict(book.plan)

    e.keeper._probe_open_tx(book, rt, "tick")
    rows = _tx(e, book)
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "success")]
    assert rows[1]["reconciled"] == "receipt read at tick" and "planAdvance" not in rows[1]
    assert book.plan == before  # byte for byte: the act path owns that plan
    assert rt["lastActBlock"] == rows[1]["block"] and rt["_openTx"] == {}


def test_a_boot_reopened_row_whose_receipt_row_exists_does_not_advance_the_plan_a_second_time(make_keeper, tmp_path):
    """The same step across a RESTART: the ledger holds the broadcast row
    AND an `unconfirmed` receipt row, so the act path reached its receipt
    stage and applied the plan before the process went. The boot close
    reconciles the ledger and leaves the plan alone."""
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:4]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    before = _plan(book, kamis)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_act(e, book, kamis, "rest", "system.harvest.stop", h, n)
    _led(e, book).tx(BASIS, {"txHash": h, "nonce": n, "status": "unconfirmed", "kami": None, "system": "system.harvest.stop", "stage": "receipt", "fn": "executeTyped", "count": 4, "batchSize": 4, "precheck": "ok", "gasLimit": 3_500_000, "broadcast": "batch", "reconciled": "receipt read unavailable (eth_getTransactionReceipt: HTTPError)"})
    e.keeper.stop_engine()

    k2 = keeper()
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert [(t["stage"], t["status"]) for t in rows] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed"), ("receipt", "success")]
    assert "planAdvance" not in rows[2] and _plan(k2.store.get(book.id), kamis) == before
    assert k2._ledger(k2.store.get(book.id)).open_tx() == {}
    k2.stop_engine()


# ------------------------------------------------------------ §D the bar and the act clocks


def test_last_act_block_moves_on_a_boot_closed_success_row_and_not_on_a_not_sent_one(make_keeper, tmp_path):
    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:2]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_act(e, book, [kamis[0]], "collect", "system.harvest.collect", h, n)
    e.keeper.stop_engine()

    k2 = keeper()  # the bar of an act that demonstrably landed, after a restart
    rows = [r["tx"] for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    assert rows[-1]["status"] == "success" and rows[-1]["block"] > 0
    assert k2.runtime[book.id]["lastActBlock"] == rows[-1]["block"]
    bar = rows[-1]["block"]
    k2.stop_engine()

    # a hash the chain never took, whose nonce another sender has passed:
    # closed `not_sent`, and a `not_sent` row is no bar
    n2 = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _external_tx(w3, n2)
    _seed_act(e, book, [kamis[1]], "collect", "system.harvest.collect", H1, n2)
    k3 = keeper()
    assert k3.handle("grant", {"account": "lab"})["ok"]
    k3.tick_book(k3.store.get(book.id))
    rows = [r["tx"] for r in k3.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]
    closed = next(t for t in rows if t["txHash"] == H1 and t["stage"] == "receipt")
    assert closed["status"] == "not_sent" and closed["block"] is None and "planAdvance" not in closed
    assert k3.runtime[book.id]["lastActBlock"] >= bar  # never moved by a step that was not sent
    k3.stop_engine()


def test_the_act_clocks_of_a_boot_closed_landed_step_move_to_the_rows_own_time_and_never_backwards(make_keeper, tmp_path):
    import datetime as _dt

    e, provider, w3, chain, keeper = _auto_env(make_keeper, tmp_path)
    kamis = BOOK_43[:2]
    _world_harvesting(e, kamis)
    book = _start(e, kamis, previewsPerTick=50)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    bcast = _seed_act(e, book, [kamis[0]], "feed", "system.kami.use.item", h, n)
    at = _dt.datetime.fromisoformat(bcast["at"].replace("Z", "+00:00")).timestamp()
    e.keeper.stop_engine()

    k2 = keeper()
    rt = k2.runtime[book.id]
    assert rt["lastActAt"] == at and rt["lastFeedAt"] == at  # the feed's own time, not the restart's
    # it is written to runtime.json, so a second restart keeps it
    k2.stop_engine()
    k3 = keeper()
    assert k3.runtime[book.id]["lastActAt"] == at and k3.runtime[book.id]["lastFeedAt"] == at
    # and it never moves a clock BACKWARDS: a newer clock in the runtime stays
    k3.runtime[book.id]["lastActAt"] = at + 10_000
    k3.runtime[book.id]["lastFeedAt"] = at + 10_000
    n2 = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h2 = _external_tx(w3, n2)
    row = _seed_act(e, book, [kamis[1]], "feed", "system.kami.use.item", h2, n2)
    b3 = k3.store.get(book.id)
    k3.runtime[book.id]["_openTx"] = {h2: row}
    k3._probe_open_tx(b3, k3.runtime[book.id], "boot")
    assert k3.runtime[book.id]["lastActAt"] == at + 10_000 and k3.runtime[book.id]["lastFeedAt"] == at + 10_000
    k3.stop_engine()
