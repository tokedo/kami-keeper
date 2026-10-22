"""Core wiring: `armed` selects the signer (refused without the
chain facts; LiveSigner end-to-end on the tester chain); the revive path
(death row first, then `revived` with its tx); craft parks an exhausted
recipe on the pre-check verdict; `rpc-unavailable` is a flag, never a
silent skip; books declared in config start under fleet only, once,
and wait for the lens; stop immediate vs afterBurst."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, HAF_CONFIG, write_keys_file
from evm_stubs import BatchingTesterProvider, deploy, deploy_world, reverter
from keeper.chain import Chain, RpcUnavailable
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.signer import DryRunSigner

TEST_GAS_PRICE = {"maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 0}


def _tester_chain():
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]
    world = deploy_world(w3, sink)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    return provider, w3, Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)


def test_armed_without_the_chain_facts_refuses_to_run_books(make_keeper):
    e = make_keeper(armed=True)
    assert e.keeper.dry_run is True and e.keeper.signer.kind == "dry-run"
    assert any("rpcUrl" in r and "worldAddress" in r and "chainId" in r for r in e.keeper.engine_refusals)
    e.grant()
    s = e.start_haf()
    assert s["error"]["code"] == "BAD_ARGS" and "rpcUrl" in s["error"]["message"]
    st = e.keeper.handle("status")["data"]
    assert st["dryRun"] is True and st["signer"]["kind"] == "dry-run" and st["chain"] is None


def test_armed_with_the_chain_facts_selects_the_live_signer_and_a_feed_lands_on_the_tester_chain(make_keeper, tmp_path):
    provider, w3, chain = _tester_chain()
    e = make_keeper()  # a fake lens to borrow
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "armed": True, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    assert k.engine_refusals == [] and k.dry_run is False and k.signer.kind == "live"
    k.signer.gas_price = TEST_GAS_PRICE  # the tester's base fee is above Yominet's flat fee
    assert k.handle("grant", {"account": "lab"})["ok"]
    s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": HAF_CONFIG})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    book = k.store.get(bid)
    e.lens.set_kami(1001, hp=(40, 200))  # below feedBelowHp 100 -> feed
    summary = k.tick_book(book)
    assert summary["acted"] is True and summary["decisions"] == ["feed"] and summary["txStatus"] == ["success"]
    rows = k.handle("ledger", {"book": bid})["data"]["rows"]
    txs = [r for r in rows if r["kind"] == "tx"]
    assert [r["tx"]["stage"] for r in txs] == ["broadcast", "receipt"] and txs[0]["tx"]["status"] == "unconfirmed" and txs[0]["tx"]["txHash"] == txs[1]["tx"]["txHash"]  # the two-stage row
    tx = txs[-1]
    assert tx["dryRun"] is False and tx["tx"]["status"] == "success" and tx["tx"]["txHash"] and tx["tx"]["gasUsed"] > 0 and tx["tx"]["block"] > 0 and tx["tx"]["effectiveGasPrice"] > 0
    assert tx["tx"]["nonce"] == 0 and tx["tx"]["system"] == "system.kami.use.item" and tx["tx"]["precheck"] == "ok"
    assert w3.eth.get_transaction_receipt(tx["tx"]["txHash"]).status == 1
    st = k.handle("status", {"book": bid})["data"]
    assert st["dryRun"] is False and st["signer"]["kind"] == "live" and st["chain"]["resolvedSystems"]["system.kami.use.item"]
    assert st["books"][0]["lastTxHash"] == tx["tx"]["txHash"] and st["books"][0]["dryRun"] is False
    q = k.handle("stop", {"book": bid})["data"]["quiescence"]
    assert q["quiescent"] is True and q["source"] == "chain" and q["lastTxHash"] == tx["tx"]["txHash"]
    for f in sorted(Path(cfg.dataDir).rglob("*")):
        if f.is_file():
            assert "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80" not in f.read_text(errors="replace")


def test_death_row_then_revive_from_inventory_then_revived_row(env):
    env.grant()
    bid = env.start_haf(reviveItems=[21301])["data"]["book"]["id"]  # inventory fixture holds 2 Red Ribbon Gummy
    book = env.keeper.store.get(bid)
    env.lens.set_kami(1001, state="DEAD", node=None)
    summary = env.keeper.tick_book(book)
    assert summary["decisions"] == ["revive"] and summary["txStatus"] == ["dry_run"]
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    kinds = [(r["kind"], r.get("death", {}).get("outcome"), r.get("decision", {}).get("kind")) for r in rows]
    i_obs = kinds.index(("death", "observed", None))
    i_dec = kinds.index(("decision", None, "revive"))
    i_tx = [i for i, k in enumerate(kinds) if k[0] == "tx"][0]
    i_rev = kinds.index(("death", "revived", None))
    assert i_obs < i_dec < i_tx < i_rev  # the death is a row of its own, before anything else
    tx = rows[i_tx]["tx"]
    assert tx["system"] == "system.kami.use.item" and tx["status"] == "dry_run" and tx["kami"] == 1001
    assert book.state == "running"
    # a purchase is never signed: the intent's item must be one of reviveItems
    assert env.keeper.signer.recorded[-1].params["itemIndex"] == 21301 and env.keeper.signer.recorded[-1].params["source"] == "inventory"


def test_death_without_a_revive_item_in_inventory_is_recorded_and_the_book_keeps_running(env):
    """A `no-item` death keeps its revive retry timer, so the book
    stays RUNNING (the `kami-dead` pause needs every entry's
    `reviveRetryAt` None) and uses ribbons shipped later."""
    env.grant()
    bid = env.start_haf(reviveItems=[21301])["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    env.lens.set_inventory(4242, 21301, 0)
    env.lens.set_kami(1001, state="DEAD", node=None)
    env.keeper.tick_book(book)
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    assert [r["death"]["outcome"] for r in rows if r["kind"] == "death"] == ["revive-skipped: no-item"]
    assert env.keeper.signer.recorded == [] and book.state == "running"
    assert isinstance(book.plan["1001"]["reviveRetryAt"], (int, float))


def test_craft_parks_a_recipe_the_precheck_refuses_and_retries_after_the_window(make_keeper, tmp_path):
    provider, w3, chain = _tester_chain()
    chain._systems["system.craft"] = deploy(w3, reverter("Craft: insufficient inputs"))
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "craft"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    assert k.signer.kind == "dry-run" and k.signer.describe()["precheck"] is True
    assert k.handle("grant", {"account": "lab"})["ok"]
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29, "amount": 0}, {"recipeIndex": 23, "amount": 0, "priority": 1}]}})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    book = k.store.get(bid)
    t0 = 1_000_000.0
    s1 = k.tick_book(book, now=t0)
    assert s1["decisions"] == ["craft"] and s1["txStatus"] == ["dry_run"]
    rec = k.signer.recorded[-1]
    assert rec.precheck == "reverted" and "insufficient inputs" in rec.reason and rec.params["recipeIndex"] == 29
    rt = k.runtime[bid]
    assert "29" in rt["parked"] and rt["crafted"] == {} and "did not land" not in (rt.get("lastError") or "")
    rows = k.handle("ledger", {"book": bid})["data"]["rows"]
    assert [r for r in rows if r["kind"] == "tx"][-1]["tx"]["precheck"] == "reverted"
    # next tick: recipe 29 is parked, the next recipe (23) is tried (and parked too)
    s2 = k.tick_book(book, now=t0 + 60)
    assert s2["decisions"] == ["craft"] and k.signer.recorded[-1].params["recipeIndex"] == 23
    s3 = k.tick_book(book, now=t0 + 120)
    assert s3["acted"] is False and s3["decisions"] == []
    # after the exhausted window the pre-check is tried again (still refused, parked again)
    s4 = k.tick_book(book, now=t0 + 3601)
    assert s4["decisions"] == ["craft"] and k.signer.recorded[-1].params["recipeIndex"] == 29
    # a recipe the chain accepts: crafted count moves, park cleared
    chain._systems["system.craft"] = w3.eth.accounts[1]
    s5 = k.tick_book(book, now=t0 + 7300)
    assert s5["decisions"] == ["craft"] and k.signer.recorded[-1].precheck == "ok"
    assert k.runtime[bid]["crafted"] == {"29": 1} and "29" not in k.runtime[bid]["parked"]


def test_rpc_unavailable_is_a_degraded_flag_and_the_book_keeps_running(env):
    class Down(DryRunSigner):
        def _send(self, grant, intents):
            raise RpcUnavailable("eth_getTransactionCount(pending): ConnectionError")

    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    down = Down()
    down._grants = env.keeper.signer._grants
    env.keeper.signer = down
    env.lens.set_kami(1001, hp=(40, 200))
    summary = env.keeper.tick_book(book)
    assert summary["acted"] is False and "rpc-unavailable" in summary["degraded"] and book.state == "running"
    st = env.keeper.handle("status", {"book": bid})["data"]["books"][0]
    assert "rpc-unavailable" in st["degraded"] and "rpc unavailable" in st["lastError"]
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    assert "rpc-unavailable" in rows[-1]["degraded"] and rows[-1]["kind"] == "tick"
    assert book.plan["1001"]["state"] == "HARVESTING"  # the plan did not move


def test_books_declared_in_config_start_under_fleet_once_and_never_under_lab(make_keeper, tmp_path):
    books = [
        {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}},
        {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1002, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 90}},
        {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}]}},
        {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}},  # a duplicate declaration
        {"account": "lab", "strategyType": "craft", "config": {"recipes": []}},  # schema-invalid: failed, never retried
    ]
    f = tmp_path / "fleet.yaml"
    f.write_text(json.dumps({"books": books}))
    e = make_keeper(profile="fleet", cap=0, grants=["lab"], config_file=str(f))
    st = e.keeper.handle("status")["data"]
    assert st["configBooks"]["declared"] == 5 and len(st["configBooks"]["started"]) == 3 and st["configBooks"]["pending"] == 0
    assert len(st["configBooks"]["failed"]) == 1 and st["configBooks"]["failed"][0]["error"] == "BAD_ARGS"
    running = [b for b in e.keeper.store.all() if b.state == "running"]
    assert len(running) == 3 and {b.strategyType for b in running} == {"harvestAndFeed", "craft"}
    # a restart on the same data dir holds the same three books and starts nothing new
    cfg = load_config(flags={"profile": "fleet", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "grants": ["lab"]}, env={}, config_file=str(f))
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    assert len([b for b in k2.store.all() if b.state == "running"]) == 3 and k2.config_books_started == [] and k2.config_books_pending == []
    # lab: refused as a config error; nothing starts
    lab = make_keeper(config_file=str(f))
    assert any("books" in r for r in lab.keeper.engine_refusals) and lab.keeper.store.all() == []


def test_config_books_wait_for_the_lens_and_start_when_it_is_live(make_keeper, tmp_path):
    f = tmp_path / "fleet.yaml"
    f.write_text(json.dumps({"books": [{"account": "lab", "strategyType": "harvestAndFeed", "config": HAF_CONFIG}]}))
    e = make_keeper(profile="fleet", cap=0, grants=["lab"], lens_mode="not-ready", config_file=str(f))
    assert e.keeper.config_books_pending and e.keeper.store.all() == []
    e.lens.mode = "live"
    e.keeper._start_config_books()
    assert e.keeper.config_books_pending == [] and len(e.keeper.config_books_started) == 1
    assert e.keeper.store.all()[0].state == "running"


def test_stop_immediate_asks_the_burst_to_abort_and_after_burst_does_not(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    assert env.keeper.signer.abort() is False
    env.keeper._ticking = bid
    env.keeper._abort.add(bid)
    assert env.keeper.signer.abort() is True
    env.keeper._abort.clear()
    env.keeper._ticking = None
    r = env.keeper.handle("stop", {"book": bid, "afterBurst": True})
    assert r["ok"] and env.keeper._abort == set()
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    assert rows[-1]["event"] == "stopped_by" and rows[-1]["afterBurst"] is True
