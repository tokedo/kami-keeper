"""SPEC P6 / D2 — chain.py: entity ids, ABI files, encoding asserted
against keeper/abi/*.json (selectors = the harness 3.7.0 literals), the
whitelist at RESOLUTION with zero RPC calls, resolution and the eth_call
pre-check on a real EVM (tester chain + hand-assembled stubs)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS
from evm_stubs import BatchingTesterProvider, deploy, deploy_world, reverter
from keeper import chain as chainmod
from keeper.chain import ABI_DIR, Call, Chain, ChainError, PrecheckReverted, RpcUnavailable, SYSTEM_ABI, SYSTEM_FUNCTIONS, account_entity_id, decode, encode, harvest_entity_id, inventory_entity_id, kami_entity_id, load_abi, system_hash
from keeper.whitelist import WHITELIST, Intent, WhitelistRefused

# The function signatures kami-harness 3.7.0 sends live (executor/server.py
# L4351 _ABI_FEED, L4391 _ABI_HARVEST_START, L4401 _ABI_HARVEST_STOP, L7937
# _ABI_HARVEST_STOP_BATCH, L8433 _ABI_CRAFT). The keeper's ABI files must
# produce exactly these selectors.
HARNESS_SIGNATURES = {
    "HarvestStartSystem": ["executeTyped(uint256,uint32,uint256,uint256)", "executeBatched(uint256[],uint32,uint256,uint256)"],
    "HarvestStopSystem": ["executeTyped(uint256)", "executeBatched(uint256[])", "executeBatchedAllowFailure(uint256[])"],
    "HarvestCollectSystem": ["executeTyped(uint256)", "executeBatched(uint256[])"],
    "KamiUseItemSystem": ["executeTyped(uint256,uint32)"],
    "CraftSystem": ["executeTyped(uint32,uint256)"],
}
# The KamiUseItemSystem methodIdentifiers selector of the game's published
# ABI: the one that matches the live shape.
INTEGRATION_FEED_SELECTOR = "e60f3a76"


def _sig(fn: dict) -> str:
    return fn["name"] + "(" + ",".join(i["type"] for i in fn["inputs"]) + ")"


def test_abi_files_exist_name_their_source_and_carry_the_harness_selectors():
    for contract, sigs in HARNESS_SIGNATURES.items():
        doc = json.loads((ABI_DIR / f"{contract}.json").read_text())
        assert "server.py" in doc["source"] and doc["contract"] == contract
        assert [_sig(f) for f in doc["abi"] if f["type"] == "function"] == sigs
        for s in sigs:
            assert Web3.keccak(text=s)[:4].hex()  # selector derivable
    feed = load_abi("KamiUseItemSystem")[0]
    assert Web3.keccak(text=_sig(feed))[:4].hex() == INTEGRATION_FEED_SELECTOR
    assert {SYSTEM_ABI[s] for s in WHITELIST} == set(HARNESS_SIGNATURES)
    assert set(SYSTEM_FUNCTIONS) == WHITELIST
    for name in ("World", "SystemsComponent"):
        doc = json.loads((ABI_DIR / f"{name}.json").read_text())
        assert doc["abi"] and doc["source"]


def test_entity_ids_follow_the_cited_formulas():
    assert kami_entity_id(42) == int.from_bytes(Web3.solidity_keccak(["string", "uint32"], ["kami.id", 42]), "big")
    assert harvest_entity_id(42) == int.from_bytes(Web3.solidity_keccak(["string", "uint256"], ["harvest", kami_entity_id(42)]), "big")
    assert account_entity_id(FIXTURE_OPERATOR_ADDRESS) == int(FIXTURE_OPERATOR_ADDRESS, 16)
    assert inventory_entity_id(7, 11301) == int.from_bytes(Web3.solidity_keccak(["string", "uint256", "uint32"], ["inventory.instance", 7, 11301]), "big")
    assert system_hash("system.craft") == int.from_bytes(Web3.keccak(text="system.craft"), "big")
    assert kami_entity_id(1) != kami_entity_id(2) and kami_entity_id(1) < 2**256


@pytest.mark.parametrize("intent,contract,fn,expect", [
    (Intent("system.harvest.start", "lab", 7, {"nodeIndex": 62}), "HarvestStartSystem", "executeTyped", {"kamiID": kami_entity_id(7), "nodeIndex": 62, "taxerID": 0, "taxAmt": 0}),
    (Intent("system.harvest.start", "lab", None, {"nodeIndex": 62, "kamiIndices": [7, 8]}), "HarvestStartSystem", "executeBatched", {"kamiIDs": [kami_entity_id(7), kami_entity_id(8)], "nodeIndex": 62, "taxerID": 0, "taxAmt": 0}),
    (Intent("system.harvest.stop", "lab", 7, {}), "HarvestStopSystem", "executeTyped", {"id": harvest_entity_id(7)}),
    (Intent("system.harvest.stop", "lab", None, {"kamiIndices": [7, 8, 9]}), "HarvestStopSystem", "executeBatchedAllowFailure", {"ids": [harvest_entity_id(k) for k in (7, 8, 9)]}),
    (Intent("system.harvest.collect", "lab", 7, {}), "HarvestCollectSystem", "executeTyped", {"id": harvest_entity_id(7)}),
    (Intent("system.harvest.collect", "lab", None, {"kamiIndices": [7, 8]}), "HarvestCollectSystem", "executeBatched", {"ids": [harvest_entity_id(7), harvest_entity_id(8)]}),
    (Intent("system.kami.use.item", "lab", 7, {"itemIndex": 11301}), "KamiUseItemSystem", "executeTyped", {"kamiID": kami_entity_id(7), "itemIndex": 11301}),
    (Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 3}), "CraftSystem", "executeTyped", {"recipeIndex": 29, "amount": 3}),
])
def test_encoding_matches_the_abi_json_and_round_trips(intent, contract, fn, expect):
    call = encode(intent)
    assert isinstance(call, Call) and call.contract == contract and call.fn == fn
    sig = next(_sig(f) for f in load_abi(contract) if f["name"] == fn)
    assert call.calldata[:4] == Web3.keccak(text=sig)[:4], "selector != keccak(signature) from the ABI file"
    name, args = decode(contract, call.calldata)
    assert name == fn and args == expect
    assert call.count == max(1, len(intent.kamis))
    if intent.system == "system.kami.use.item":
        assert call.selector == "0x" + INTEGRATION_FEED_SELECTOR


def test_encode_refuses_a_non_whitelisted_system_before_touching_any_abi(monkeypatch):
    opened = []
    monkeypatch.setattr(chainmod, "load_abi", lambda c: opened.append(c) or [])
    with pytest.raises(WhitelistRefused):
        encode(Intent("system.harvest.liquidate", "lab", 7, {}))
    with pytest.raises(WhitelistRefused):
        encode(Intent("system.kami.onyx.revive", "lab", 7, {}))
    assert opened == []


@pytest.fixture
def tester():
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]  # no code: any call returns empty, any tx lands
    world = deploy_world(w3, sink)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})  # the tester checks the caller's balance on eth_call
    chain = Chain("", world, w3.eth.chain_id, web3=w3)
    return provider, w3, chain, sink


def test_resolution_refuses_a_non_whitelisted_id_with_zero_rpc_calls(tester):
    provider, w3, chain, sink = tester
    before = len(provider.log)
    for bad in ("system.harvest.liquidate", "system.item.transfer", "system.account.move", "system.trade.create", "system.kami.cast.item", "system.listing.buy", "system.pool.swap", "system.kami.onyx.revive"):
        with pytest.raises(WhitelistRefused):
            chain.resolve_system(bad)
    assert len(provider.log) == before
    assert chain.describe()["resolvedSystems"] == {}


def test_resolution_on_a_real_evm_and_the_cache(tester):
    provider, w3, chain, sink = tester
    addr = chain.resolve_system("system.harvest.start")
    assert addr == sink
    calls = provider.count("eth_call")
    assert chain.resolve_system("system.harvest.start") == sink and provider.count("eth_call") == calls  # cached
    assert chain.resolve_system("system.craft") == sink
    assert set(chain.describe()["resolvedSystems"]) == {"system.harvest.start", "system.craft"}
    assert chain.verify_chain_id() == w3.eth.chain_id
    with pytest.raises(ChainError):
        Chain("", chain.world_address, chain.chain_id + 1, web3=w3).verify_chain_id()


def test_precheck_passes_on_the_sink_and_refuses_with_the_node_message_on_a_revert(tester):
    provider, w3, chain, sink = tester
    call = encode(Intent("system.kami.use.item", "lab", 7, {"itemIndex": 11301}))
    chain.precheck(call, FIXTURE_OPERATOR_ADDRESS)  # sink: empty return, no revert
    rev = deploy(w3, reverter("harvest: kami not owned"))
    chain._systems["system.craft"] = rev
    with pytest.raises(PrecheckReverted) as ei:
        chain.precheck(encode(Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 1})), FIXTURE_OPERATOR_ADDRESS)
    assert "kami not owned" in ei.value.reason
    assert chain.code_size(rev) > 0 and chain.code_size(sink) == 0


def test_rpc_failures_read_as_unavailable_with_a_class_name_only(tester):
    provider, w3, chain, sink = tester

    def boom(method, params):
        raise ConnectionError("secret-looking text 0x" + "ab" * 32)

    provider.inject = boom
    with pytest.raises(RpcUnavailable) as ei:
        chain.pending_nonce(FIXTURE_OPERATOR_ADDRESS)
    assert "ConnectionError" in str(ei.value) and "ab" * 32 not in str(ei.value)
    with pytest.raises(RpcUnavailable):
        chain.block_number()
