"""Craft stamina need (SPEC P2 craft row, P5 runtime).

A recipe whose cost the book does not know would be offered every tick
from stamina 1 and refused by the pre-check until stamina reaches the
cost. A stamina refusal must raise a persisted floor past the reading it
was refused at; a declared `staminaCost` is a floor from the
first tick; the park lasts the regen time to the need.
"""

from __future__ import annotations

from conftest import write_keys_file  # noqa: F401
from evm_stubs import deploy, reverter
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.strategies.craft import STAMINA_REGEN_SEC
from test_core_1b import _tester_chain


def _craft_keeper(make_keeper, tmp_path, name="craft"):
    provider, w3, chain = _tester_chain()
    chain._systems["system.craft"] = deploy(w3, reverter("Account: insufficient stamina"))
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / name), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    return e, w3, chain, k, cfg


def test_a_stamina_refusal_raises_a_learned_floor_and_parks_for_the_regen_time(make_keeper, tmp_path):
    e, w3, chain, k, _ = _craft_keeper(make_keeper, tmp_path)
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}]}})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    book = k.store.get(bid)
    e.lens.set_stamina(4242, 67)
    t0 = 1_000_000.0
    s1 = k.tick_book(book, now=t0)
    assert s1["decisions"] == ["craft"] and k.signer.recorded[-1].precheck == "reverted"
    rt = k.runtime[bid]
    assert rt["staminaMin"] == {"29": 68}
    park = rt["parked"]["29"]
    assert park["stamina"] == 67 and park["staminaMin"] == 68 and park["retryAt"] == t0 + STAMINA_REGEN_SEC
    # the same reading after the park: no offer, no pre-check — a staminaWait note
    s2 = k.tick_book(book, now=t0 + 300)
    assert s2["acted"] is False and s2["decisions"] == []
    assert s2["staminaWait"] == [{"recipe": 29, "stamina": 67, "need": 68, "staminaFloor": 0, "retryInSec": STAMINA_REGEN_SEC}]
    assert len(k.signer.recorded) == 1
    # stamina reaches the floor: offered again, refused again (cost is 75) -> floor 73
    e.lens.set_stamina(4242, 72)
    s3 = k.tick_book(book, now=t0 + 600)
    assert s3["decisions"] == ["craft"] and rt["staminaMin"] == {"29": 73}
    # the chain accepts at 77: crafted moves, the floor stays (a landed reading is not the cost)
    chain._systems["system.craft"] = w3.eth.accounts[1]
    e.lens.set_stamina(4242, 77)
    s4 = k.tick_book(book, now=t0 + 1200)
    assert s4["decisions"] == ["craft"] and k.signer.recorded[-1].precheck == "ok"
    assert rt["crafted"] == {"29": 1} and rt["staminaMin"] == {"29": 73} and "29" not in rt["parked"]
    # below the learned floor, nothing is offered even though the chain would now accept
    e.lens.set_stamina(4242, 40)
    s5 = k.tick_book(book, now=t0 + 1500)
    assert s5["decisions"] == [] and s5["staminaWait"][0]["retryInSec"] == 33 * STAMINA_REGEN_SEC
    rows = k.handle("ledger", {"book": bid})["data"]["rows"]
    assert sum(1 for r in rows if r["kind"] == "tx") == 3


def test_a_declared_stamina_cost_is_a_floor_from_the_first_tick_and_the_unspent_floor_sits_on_top(make_keeper, tmp_path):
    e, w3, chain, k, _ = _craft_keeper(make_keeper, tmp_path, "declared")
    chain._systems["system.craft"] = w3.eth.accounts[1]  # the chain would accept
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29, "staminaCost": 75}], "staminaFloor": 10}})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    book = k.store.get(bid)
    e.lens.set_stamina(4242, 80)
    s1 = k.tick_book(book, now=1.0)
    assert s1["decisions"] == [] and s1["staminaWait"] == [{"recipe": 29, "stamina": 80, "need": 75, "staminaFloor": 10, "retryInSec": 5 * STAMINA_REGEN_SEC}]
    assert k.signer.recorded == []
    e.lens.set_stamina(4242, 85)
    s2 = k.tick_book(book, now=2.0)
    assert s2["decisions"] == ["craft"] and k.signer.recorded[-1].params["recipeIndex"] == 29
    assert k.runtime[bid]["crafted"] == {"29": 1}
    # a second recipe without a declared cost is offered when the first waits
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29, "staminaCost": 75}, {"recipeIndex": 23, "priority": 1}]}})
    bid2 = s["data"]["book"]["id"]
    e.lens.set_stamina(4242, 30)
    s3 = k.tick_book(k.store.get(bid2), now=3.0)
    assert s3["decisions"] == ["craft"] and k.signer.recorded[-1].params["recipeIndex"] == 23
    assert s3["staminaWait"][0]["recipe"] == 29


def test_the_learned_floor_survives_a_restart(make_keeper, tmp_path):
    e, w3, chain, k, cfg = _craft_keeper(make_keeper, tmp_path, "restart")
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}]}})
    bid = s["data"]["book"]["id"]
    e.lens.set_stamina(4242, 67)
    k.tick_book(k.store.get(bid), now=100.0)
    assert k.runtime[bid]["staminaMin"] == {"29": 68}
    k.stop_engine()
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k2.boot()
    assert k2.handle("grant", {"account": "lab"})["ok"]
    assert k2.runtime[bid]["staminaMin"] == {"29": 68}
    chain._systems["system.craft"] = w3.eth.accounts[1]
    s2 = k2.tick_book(k2.store.get(bid), now=200.0)
    assert s2["decisions"] == [] and s2["staminaWait"][0]["need"] == 68
    k2.stop_engine()


def test_the_act_log_says_refused_pre_send_when_nothing_was_signed(make_keeper, tmp_path):
    e, w3, chain, k, _ = _craft_keeper(make_keeper, tmp_path, "log")
    s = k.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}]}})
    bid = s["data"]["book"]["id"]
    e.lens.set_stamina(4242, 67)
    k.tick_book(k.store.get(bid), now=100.0)
    lines = k.handle("logs", {"book": bid})["data"]["lines"]
    text = "\n".join(l if isinstance(l, str) else l.get("line", "") for l in lines)
    assert "acted: craft (1 tx, dry run; 0 landed; 1 refused pre-send)" in text


def test_account_key_names_the_lens_account_when_the_label_is_not_the_in_game_name(make_keeper, tmp_path):
    """A label whose in-game account name differs from it: the label names the grant, the
    lane and the ledger; `accountKey` names the lens `account` key for the
    stamina read. The fake lens registers the account under "lab" and "4242"
    only, so label `a` is not in the mirror."""
    kf = tmp_path / "two.env"
    kf.write_text("A_OPERATOR_KEY=0x" + "11" * 32 + "\nB_OPERATOR_KEY=0x" + "22" * 32 + "\n")
    provider, w3, chain = _tester_chain()
    chain._systems["system.craft"] = deploy(w3, reverter("Account: insufficient stamina"))
    e = make_keeper(keys_file=kf)
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "accountkey"), "lensSocket": e.lens.socket_path, "keysFile": str(kf), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    assert k.handle("grant", {"account": "a"})["ok"]
    # (a) without the key the label IS the lens key, and the label is not in
    # the mirror: the read is refused, nothing is offered
    s = k.handle("start", {"account": "a", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}]}})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    s1 = k.tick_book(k.store.get(bid), now=1_000_000.0)
    assert s1["acted"] is False and s1["reason"] == "lens read refused: NOT_FOUND"
    assert k.runtime[bid]["lastError"] == "lens NOT_FOUND"
    assert len(k.signer.recorded) == 0
    # (b) with the key the read lands on the registered account and the craft
    # is offered (the chain refuses it pre-send; the point is the offer)
    s2 = k.handle("start", {"account": "a", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}], "accountKey": "4242"}})
    assert s2["ok"], s2
    bid2 = s2["data"]["book"]["id"]
    e.lens.set_stamina(4242, 90)
    t2 = k.tick_book(k.store.get(bid2), now=1_000_100.0)
    assert t2["decisions"] == ["craft"] and k.signer.recorded[-1].params["recipeIndex"] == 29
    assert k.runtime[bid2].get("lastError") is None
    # (c) the key is in the served schema
    d = k.handle("describe", {"type": "craft"})
    assert d["ok"], d
    assert "accountKey" in d["data"]["schema"]["properties"]
    assert "- accountKey (" in d["data"]["description"]
    # (d) an integer key is accepted too (the schema takes integer or string)
    s3 = k.handle("start", {"account": "a", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 29}], "accountKey": 4242}})
    assert s3["ok"], s3
    assert k.store.get(s3["data"]["book"]["id"]).config["accountKey"] == 4242
    k.stop_engine()
