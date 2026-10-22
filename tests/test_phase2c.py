"""The nonce lane through the core (§1) and start batching (§2).

  §1 on the tester chain with a LiveSigner behind the core: two `auto`
     books of one account in one tick pass take consecutive nonces under a
     pending count the node has not caught up (the cursor); a nonce race
     whose retry is refused too leaves the row `not_sent`, the plan not
     applied, no backoff — the book re-decides next tick.
  §2 on the recorded fixtures with the dry-run signer: `startBatchWaitSec` /
     `startBatchMin` hold a ready kami for company — three ready at t,
     t+60, t+120 with wait 300 / min 4 go out as one batch at t+300; four
     ready by t+90 go out at t+90; wait 0 is the key-off behaviour; the queue
     note, the status count, the runtime persistence, a kami whose bound
     rises leaves the queue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, FIXTURE_OPERATOR_KEY, Env
from evm_stubs import BatchingTesterProvider, deploy_world
from keeper.chain import Chain
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.signer import LiveSigner
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import BOOK_43, CAROL, NODE, T, _apply_plans_to_world, _decisions, _last_tick, _rows, _seed, _set_party, _start, _tick_all

TEST_GAS_PRICE = {"maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 0}


# ------------------------------------------------------------ §1 the nonce lane through the core


def _live_env(make_keeper, tmp_path) -> tuple[Env, BatchingTesterProvider, Any, Chain]:
    """A lab keeper ARMED on the tester chain (every whitelisted system
    resolves to an EOA sink: pre-checks pass, txs land), the recorded fixtures
    on the fake lens, the clock at T."""
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]
    world = deploy_world(w3, sink)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper(cap=6)
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "armed": True}, env={})
    signer = LiveSigner(chain, gas_price=TEST_GAS_PRICE, receipt_timeout_sec=5, receipt_poll_sec=0.01, quiescence_wait_sec=2, settle_sec=0, sleep=lambda s: None)
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain, signer=signer)
    clock = {"now": T}
    k.clock = lambda: clock["now"]
    k.boot()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(cfg.dataDir)
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    assert k.handle("grant", {"account": "lab"})["ok"] and k.dry_run is False
    return e, provider, w3, chain


def _freeze_pending(provider: BatchingTesterProvider, at: int) -> None:
    def mutate(method, params, resp):
        if method == "eth_getTransactionCount" and len(params) > 1 and params[1] == "pending" and str(params[0]).lower() == FIXTURE_OPERATOR_ADDRESS.lower():
            real = int(resp["result"], 16) if isinstance(resp["result"], str) else int(resp["result"])
            return {**resp, "result": min(real, at)}
        return None

    provider.mutate = mutate


def _external_send(w3, nonce: int) -> None:
    from eth_account import Account

    tx = {"to": w3.eth.accounts[1], "from": FIXTURE_OPERATOR_ADDRESS, "data": b"", "value": 0, "gas": 100_000, "nonce": nonce, "chainId": w3.eth.chain_id, **TEST_GAS_PRICE}
    w3.eth.send_raw_transaction(bytes(Account.sign_transaction(tx, FIXTURE_OPERATOR_KEY).raw_transaction))


def test_two_books_of_one_operator_in_one_pass_take_consecutive_nonces_under_a_lagging_pending_count(make_keeper, tmp_path):
    """Two `auto` books of one account, one pass, each burst reading the
    node's pending count — which has not
    moved since the first burst. The lane's cursor makes the second burst
    sign at the next nonce; no mismatch, no retry, no 120 s wait."""
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    a = _start(e, BOOK_43[:3], previewsPerTick=50)
    b = _start(e, [25814, 27850, 28532], previewsPerTick=50)  # the first kami is HARVESTING on the node in the fixture
    e.books = [a, b]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))  # below the floor: a single stop from each book
    _set_party(e.lens, 25814, hp=(20, 230))
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _freeze_pending(provider, start)
    n0 = provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending"))
    ticked = _tick_all(e)
    assert ticked == [a.id, b.id]
    ra = [r["tx"] for r in _rows(e, a, "tx") if r["tx"]["stage"] == "receipt"]  # the final row per step
    rb = [r["tx"] for r in _rows(e, b, "tx") if r["tx"]["stage"] == "receipt"]  # the stop of one kami and the start of the other (resting at 96 %): one burst, two steps
    assert [t["system"] for t in ra] == ["system.harvest.stop"] and [t["system"] for t in rb] == ["system.harvest.stop", "system.harvest.start"]
    both = ra + rb
    assert [t["nonce"] for t in both] == [start, start + 1, start + 2] and all(t["status"] == "success" for t in both)
    assert all("nonceRetry" not in t and "broadcastError" not in t and "reconciled" not in t for t in both)
    assert provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending")) == n0 + 2  # once per burst, still
    assert provider.count("eth_getTransactionByHash") == 0
    assert e.keeper.signer.lane("lab").cursor == start + 3
    assert [w3.eth.get_transaction(t["txHash"])["nonce"] for t in both] == [start, start + 1, start + 2]
    assert a.plan[str(BOOK_43[0])] == {"state": RESTING, "node": None, "pending": True} and b.plan["25814"] == {"state": RESTING, "node": None, "pending": True}
    assert _last_tick(e, b)["txStatus"] == ["success", "success"] and e.keeper.runtime[b.id]["lastError"] is None


def test_a_nonce_race_whose_retry_is_refused_leaves_the_plan_and_is_re_decided_next_tick_without_a_backoff(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    k = BOOK_43[0]
    _set_party(e.lens, k, hp=(20, 240))
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _external_send(w3, start)  # another process on the operator spends `start`
    _freeze_pending(provider, start)  # the node still answers the old count
    provider.refuse_nonce = start + 1  # and refuses the one retry at the fresh nonce
    rc0 = provider.count("eth_getTransactionReceipt")
    _tick_all(e)
    tx = [r["tx"] for r in _rows(e, book, "tx")]
    assert len(tx) == 1 and tx[0]["status"] == "not_sent" and tx[0]["nonceRetry"] is True and tx[0]["nonce"] == start + 1 and tx[0]["txHash"] is None
    assert tx[0]["stage"] == "broadcast"  # a not_sent record's one row, at the broadcast stage
    assert tx[0]["reason"].startswith("nonce race: ") and "account sequence mismatch" in tx[0]["broadcastError"]
    assert book.plan[str(k)] == {"state": HARVESTING, "node": NODE}  # the plan is not applied
    assert _rows(e, book, "deviation") == [] and e.keeper.runtime[book.id].get("backoff", {}) == {}  # not the chain's verdict on the kami: no backoff
    assert "nonce race" in e.keeper.runtime[book.id]["lastError"] and _last_tick(e, book)["txStatus"] == ["not_sent"]
    assert provider.count("eth_getTransactionReceipt") - rc0 <= 1  # no WAIT on a hash the node does not know: at most the reconciliation lookup
    # next tick: the kami is still below the floor -> the rest is decided again and lands at the fresh nonce
    provider.refuse_nonce = None
    provider.mutate = None
    _tick_all(e)
    d = _decisions(e, book, "rest")
    assert len(d) == 2 and d[1]["parameters"]["kamis"] == [k]
    tx = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["stage"] == "receipt" or r["tx"]["status"] == "not_sent"]  # the final row per step
    assert tx[1]["status"] == "success" and tx[1]["nonce"] == start + 1 and "nonceRetry" not in tx[1]
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}
    assert e.keeper.signer.lane("lab").cursor == start + 2 and e.keeper.runtime[book.id]["lastError"] is None


# ------------------------------------------------------------ §2 start batching: hold a ready kami for company

RESTING_FOUR = [24454, 25133, 28532, 29213]  # RESTING in the party fixture at 68 / 55 / 74 / 60 %
TOTALS = {24454: 250, 25133: 240, 28532: 230, 29213: 250}


def _batch_env(make_keeper, **over: Any) -> tuple[Env, Any]:
    """A lab dry-run keeper on the recorded fixtures with ONE auto book over
    the four resting kamis: every foreign occupant previewed on the first
    tick (previewsPerTick 50), rest to full (resumeHpPct 100)."""
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    e.grant()
    book = _start(e, RESTING_FOUR, previewsPerTick=50, resumeHpPct=100, **over)
    e.books = [book]  # type: ignore[attr-defined]
    return e, book


def _full(e: Env, kami: int) -> None:
    _set_party(e.lens, kami, hp=(TOTALS[kami], TOTALS[kami]))


def _starts(e: Env, book) -> list[dict[str, Any]]:
    return [d["parameters"] for d in _decisions(e, book, "start")]


def _tick_summary(e: Env, book) -> dict[str, Any]:
    """One direct tick 30 s on, and its LIVE summary — a steady wait writes
    no tick row (`startWaiting` is ignored by the change key), so the last
    row cannot show the clock moving."""
    e.clock["now"] += 30
    return e.keeper.tick_book(book, now=e.clock["now"])


def test_three_kamis_ready_at_t_t60_t120_go_out_as_one_batch_when_the_oldest_has_waited_the_window(make_keeper):
    e, book = _batch_env(make_keeper, startBatchWaitSec=300, startBatchMin=4)
    _full(e, 24454)
    _tick_all(e)  # T+30: the first read that sees the first kami ready = t
    t = e.clock["now"]
    s = _last_tick(e, book)
    assert _starts(e, book) == [] and s["kamis"]["waiting"] == 1 and s["kamis"]["resting"] == 4
    assert s["startWaiting"] == [{"kami": 24454, "readySinceSec": 0}]
    _tick_all(e)
    _full(e, 25133)
    _tick_all(e)  # t+60: the second ready
    s = _last_tick(e, book)
    assert s["kamis"]["waiting"] == 2 and s["startWaiting"] == [{"kami": 24454, "readySinceSec": 60}, {"kami": 25133, "readySinceSec": 0}]
    _tick_all(e)
    _full(e, 28532)
    _tick_all(e)  # t+120: the third ready
    assert _last_tick(e, book)["kamis"]["waiting"] == 3 and _starts(e, book) == []
    # the status count and the persisted queue
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert st["kamis"]["waiting"] == 3
    rt = json.loads((e.data_dir / "books" / book.id / "runtime.json").read_text())["runtime"]
    assert rt["startQueue"] == {"24454": t, "25133": t + 60, "28532": t + 120}
    # a steady wait does not write a tick row every tick (readySinceSec is ignored by the change key)
    n = len(_rows(e, book, "tick"))
    while e.clock["now"] < t + 270:
        _tick_all(e)
        assert _starts(e, book) == [] and _last_tick(e, book)["kamis"]["waiting"] == 3
    assert len(_rows(e, book, "tick")) == n
    _tick_all(e)  # t+300: the oldest has waited the window -> one batch of the three
    assert e.clock["now"] == t + 300
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [24454, 25133, 28532] and st[0]["batchSize"] == 3 and st[0]["node"] == NODE
    assert [m["readySinceSec"] for m in st[0]["detail"]] == [300, 240, 180] and st[0]["detail"][0]["resumeHpPct"] == 100
    s = _last_tick(e, book)
    assert s["kamis"]["waiting"] == 0 and "startWaiting" not in s and s["decisions"] == ["start"]
    tx = [r["tx"] for r in _rows(e, book, "tx")]
    assert [(t_["system"], t_["fn"], t_["count"]) for t_ in tx] == [("system.harvest.start", "executeBatched", 3)]
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (24454, 25133, 28532)) and book.plan["29213"] == {"state": RESTING, "node": None}


def test_four_kamis_ready_by_t90_go_out_at_t90(make_keeper):
    e, book = _batch_env(make_keeper, startBatchWaitSec=300, startBatchMin=4)
    _full(e, 24454)
    _tick_all(e)  # t
    t = e.clock["now"]
    _full(e, 25133)
    _full(e, 28532)
    _tick_all(e)  # t+30: three ready, under the minimum
    assert _starts(e, book) == [] and _last_tick(e, book)["kamis"]["waiting"] == 3
    _tick_all(e)  # t+60
    _full(e, 29213)
    _tick_all(e)  # t+90: the fourth -> the batch, 210 s before the oldest's window
    assert e.clock["now"] == t + 90
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == RESTING_FOUR and st[0]["batchSize"] == 4
    assert [m["readySinceSec"] for m in st[0]["detail"]] == [90, 60, 60, 0]
    assert _last_tick(e, book)["kamis"]["waiting"] == 0


def test_wait_zero_is_todays_behaviour_a_ready_kami_starts_on_its_tick(make_keeper):
    e, book = _batch_env(make_keeper)  # the schema defaults: startBatchWaitSec 0, startBatchMin 4
    assert book.config["startBatchWaitSec"] == 0 and book.config["startBatchMin"] == 4
    _full(e, 24454)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [24454] and st[0]["batchSize"] == 1 and st[0]["detail"][0]["readySinceSec"] == 0
    s = _last_tick(e, book)
    assert s["kamis"]["waiting"] == 0 and "startWaiting" not in s
    assert "startQueue" not in json.loads((e.data_dir / "books" / book.id / "runtime.json").read_text())["runtime"]
    _apply_plans_to_world(e)  # the dry-run start applied (else P7 notes the contradiction and the tick does not assess)
    _full(e, 25133)
    _tick_all(e)
    assert _starts(e, book)[-1]["kamis"] == [25133]  # one single per ready kami


def test_a_kami_whose_bound_rises_above_its_hp_leaves_the_queue_and_re_enters_afresh(make_keeper):
    e, book = _batch_env(make_keeper, startBatchWaitSec=300, startBatchMin=4)
    _full(e, 24454)
    _tick_all(e)
    t = e.clock["now"]
    assert _last_tick(e, book)["startWaiting"] == [{"kami": 24454, "readySinceSec": 0}]
    # the attacker's threshold against the first kami rises to 249: bound 249 + 2 % x 250 = 254 > 250 -> held, out of the queue
    e.lens.synth_thresholds[(360, 24454)] = 249
    for h in e.lens.previews[NODE][360]["harvests"]:
        if h["kami"]["index"] == 24454:
            h["liquidation"]["threshold"] = 249
    e.lens.previews[NODE][360]["harvests"].append({"id": "0x" + "ab" * 32, "state": "ACTIVE", "kami": {"id": "0x" + "cd" * 32, "name": "kami-24454", "index": 24454}, "account": {"index": CAROL, "name": "carol"}, "vitals": {"hp": {"current": 250, "total": 250, "percent": 100}, "hpRatePerHr": "0", "musuAccrued": 0, "cooldownSec": 0, "cooldownUntil": 0, "level": 30, "xp": 0, "xpRequired": 1, "levelUpReady": False, "levelUpBlockedBy": "EXPERIENCE", "skillPoints": 0}, "liquidation": {"eligible": False, "reason": "TARGET_HP_ABOVE_THRESHOLD", "threshold": 249, "margin": 1, "spoils": 0, "salvage": 0, "recoil": 0}})
    e.keeper.previews.entries(NODE, CAROL)[360].fetched_at = 0.0  # force the refresh
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["kamis"]["waiting"] == 0 and "startWaiting" not in s and s["kamis"]["held"] == 1 and s["held"][0]["kami"] == 24454 and s["held"][0]["reason"] == "bound above resume HP"
    assert e.keeper.runtime[book.id]["startQueue"] == {} and _starts(e, book) == []
    # the threshold drops again: ready afresh, the wait starts over
    e.lens.previews[NODE][360]["harvests"] = [h for h in e.lens.previews[NODE][360]["harvests"] if h["kami"]["index"] != 24454]
    e.keeper.previews.entries(NODE, CAROL)[360].fetched_at = 0.0
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["startWaiting"] == [{"kami": 24454, "readySinceSec": 0}] and e.keeper.runtime[book.id]["startQueue"] == {"24454": e.clock["now"]}
    assert e.clock["now"] == t + 60


def test_a_restart_keeps_the_wait_and_a_start_that_does_not_land_keeps_its_place(make_keeper):
    from keeper.signer import DryRunSigner

    e, book = _batch_env(make_keeper, startBatchWaitSec=300, startBatchMin=4)
    _full(e, 24454)
    _tick_all(e)
    t = e.clock["now"]
    for _ in range(3):
        _tick_all(e)
    assert _tick_summary(e, book)["startWaiting"] == [{"kami": 24454, "readySinceSec": 120}]  # t+120
    # a keeper restart mid-wait restores the queue: the wait continues, it does not start over
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.clock = lambda: e.clock["now"]
    k2.boot()
    k2.handle("grant", {"account": "lab"})
    assert k2.runtime[book.id]["startQueue"] == {"24454": t}
    e.keeper.stop_engine()
    e.keeper = k2
    b2 = k2.store.get(book.id)
    e.books = [b2]  # type: ignore[attr-defined]
    assert _tick_summary(e, b2)["startWaiting"] == [{"kami": 24454, "readySinceSec": 150}]  # t+150: the wait continued
    # the window elapses but the start does not land: the kami keeps its place and is offered again next tick
    class Refuse(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                r.status, r.reason = "not_sent", "nonce race: account sequence mismatch, expected 9, got 8"
            return recs

    refuse = Refuse()
    refuse._grants = k2.signer._grants
    ok = k2.signer
    k2.signer = refuse
    while e.clock["now"] < t + 300:
        _tick_all(e)
    st = _starts(e, b2)
    assert len(st) == 1 and st[0]["kamis"] == [24454] and st[0]["detail"][0]["readySinceSec"] == 300
    assert [r["tx"]["status"] for r in _rows(e, b2, "tx")] == ["not_sent"] and b2.plan["24454"] == {"state": RESTING, "node": None}
    assert _rows(e, b2, "deviation") == [] and k2.runtime[b2.id].get("backoff", {}) == {}  # a nonce race is not a backoff
    k2.signer = ok
    _tick_all(e)
    st = _starts(e, b2)
    assert len(st) == 2 and st[1]["kamis"] == [24454] and st[1]["detail"][0]["readySinceSec"] == 330  # released at once: its place was kept
    assert b2.plan["24454"] == {"state": HARVESTING, "node": NODE}
    k2.stop_engine()
