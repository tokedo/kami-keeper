"""SPEC P6 / P8 — LiveSigner on the tester chain: the nonce lane (pending
read ONCE per burst, consecutive nonces, receipts before the next burst),
the whitelist at SEND with no RPC, the eth_call pre-check refusing one
item and the burst going on, batch broadcast with ids = nonces,
reconciliation on a simulated transport failure (a mined step is never
reported not_sent), a node refusal mid-body, the stop abort between
chunks, the quiescence proof from the chain, and no key in any output."""

from __future__ import annotations

import json

import pytest
from web3 import Web3

from conftest import FIXTURE_KEY_HEX, FIXTURE_OPERATOR_ADDRESS, FIXTURE_OPERATOR_KEY
from evm_stubs import BatchingTesterProvider, deploy, deploy_world, reverter
from keeper.chain import Chain, RpcUnavailable
from keeper.secrets import OperatorKey
from keeper.signer import GAS_CEILINGS, GAS_PER_ITEM, MAX_TX_GAS, Intent, LiveSigner, NotGranted, WhitelistRefused, gas_for

TEST_GAS_PRICE = {"maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 0}  # the tester's base fee is ~1 gwei; Yominet's flat fee is the keeper's default
CFG = {"foodItem": 11301, "reviveItems": [21301]}


def _feed(k: int) -> Intent:
    return Intent("system.kami.use.item", "lab", k, {"itemIndex": 11301, "source": "inventory"}, "b1")


@pytest.fixture
def live():
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]
    world = deploy_world(w3, sink)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("", world, w3.eth.chain_id, web3=w3)
    signer = LiveSigner(chain, gas_price=TEST_GAS_PRICE, receipt_timeout_sec=5, receipt_poll_sec=0.01, quiescence_wait_sec=2, settle_sec=0, sleep=lambda s: None)
    signer.grant("lab", OperatorKey("LAB_OPERATOR_KEY", FIXTURE_OPERATOR_KEY))
    return provider, w3, chain, signer, sink


def test_gas_is_the_cited_shape_and_refuses_over_the_lane_cap():
    assert gas_for("system.kami.use.item") == GAS_CEILINGS["system.kami.use.item"] == 3_500_000
    assert gas_for("system.craft") == 2_200_000
    assert gas_for("system.harvest.start", 1) == 1_300_000 + 950_000
    assert gas_for("system.harvest.stop", 12) == 1_600_000 + 12 * 1_950_000
    assert gas_for("system.harvest.collect", 17) == 1_600_000 + 17 * 1_700_000 <= MAX_TX_GAS
    assert gas_for("system.harvest.start", 31) <= MAX_TX_GAS
    with pytest.raises(WhitelistRefused):
        gas_for("system.harvest.start", 32)  # the harness's own derivation: 31 fit
    with pytest.raises(WhitelistRefused):
        gas_for("system.harvest.stop", 16)  # 15 fit (harness); the SPEC cap 12 is applied earlier
    assert set(GAS_PER_ITEM) == {"system.harvest.start", "system.harvest.stop", "system.harvest.collect"}


def test_nonce_lane_consecutive_pending_read_once_per_burst_receipts_before_next(live):
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    n0 = provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending"))
    recs = signer.submit([_feed(1), _feed(2), _feed(3)], CFG)
    assert [r.nonce for r in recs] == [start, start + 1, start + 2]
    assert provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending")) == n0 + 1  # ONCE
    assert all(r.status == "success" and r.txHash and r.block and r.gasUsed and r.effectiveGasPrice for r in recs)
    assert all(r.broadcast == "batch" and r.precheck == "ok" and r.gasLimit == 3_500_000 and r.fn == "executeTyped" for r in recs)
    assert provider.batch_calls == 1
    lane = signer.lane("lab")
    assert lane.confirmed == start + 3 and lane.pending == start + 3 and lane.last_hash == recs[-1].txHash
    recs2 = signer.submit([_feed(4)], CFG)
    assert recs2[0].nonce == start + 3 and recs2[0].status == "success"
    q = signer.quiescence("lab")
    assert q.quiescent and q.source == "chain" and q.pendingNonce == start + 4 and q.lastTxHash == recs2[0].txHash
    # the chain agrees with the receipts
    for r in recs + recs2:
        assert w3.eth.get_transaction_receipt(r.txHash).status == 1
        assert w3.eth.get_transaction(r.txHash)["to"] == sink


def test_whitelist_at_send_refuses_without_any_rpc_and_no_grant_signs_nothing(live):
    provider, w3, chain, signer, sink = live
    before = len(provider.log)
    for bad in ("system.harvest.liquidate", "system.item.transfer", "system.listing.buy", "system.kami.onyx.revive"):
        with pytest.raises(WhitelistRefused):
            signer.submit([Intent(bad, "lab", 1, {"itemIndex": 11301})], CFG)
    with pytest.raises(WhitelistRefused):
        signer.submit([Intent("system.kami.use.item", "lab", 1, {"itemIndex": 19001})], CFG)  # not the book's item
    assert len(provider.log) == before and signer.sent == []
    signer.revoke("lab")
    with pytest.raises(NotGranted):
        signer.submit([_feed(1)], CFG)
    assert len(provider.log) == before


def test_precheck_revert_refuses_that_item_and_the_burst_goes_on(live):
    provider, w3, chain, signer, sink = live
    chain._systems["system.craft"] = deploy(w3, reverter("craft: not enough stamina 0x" + "cd" * 32))
    recs = signer.submit([_feed(1), Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 1}), _feed(2)], CFG)
    assert [r.status for r in recs] == ["success", "not_sent", "success"]
    assert recs[1].precheck == "reverted" and "not enough stamina" in recs[1].reason and "<hash>" in recs[1].reason and "cd" * 32 not in recs[1].reason
    assert recs[1].nonce is None and recs[1].txHash is None
    assert [r.nonce for r in (recs[0], recs[2])] == [recs[0].nonce, recs[0].nonce + 1]  # consecutive across the refused item
    row = recs[1].as_ledger_tx("b1")
    assert row["status"] == "not_sent" and row["reason"] and "precheck" in row


def test_batch_answers_map_by_nonce_never_by_position(live):
    provider, w3, chain, signer, sink = live
    provider.reverse = True
    recs = signer.submit([_feed(1), _feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success"] * 3
    for r in recs:
        assert w3.eth.get_transaction(r.txHash)["nonce"] == r.nonce


def test_transport_failure_after_the_node_admitted_the_body_is_reconciled_not_resent(live):
    """The node holds the items, the client saw a timeout (the harness's
    reconciliation rule). Every row is rescued by the pending-nonce read and carries the
    evidence; nothing is re-signed or re-offered."""
    provider, w3, chain, signer, sink = live
    provider.fail_batch_after_forward = 1
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    recs = signer.submit([_feed(1), _feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success"] * 3
    assert all(r.reconciled and "pending count" in r.reconciled for r in recs)
    assert provider.batch_calls == 1  # not re-offered
    assert provider.count("eth_sendRawTransaction") == 3  # forwarded once each
    assert w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending") == start + 3
    assert [r.nonce for r in recs] == [start, start + 1, start + 2]


def test_transport_failure_before_the_node_saw_it_is_re_offered_once_then_serial(live):
    provider, w3, chain, signer, sink = live
    provider.fail_batch_times = 1
    recs = signer.submit([_feed(1), _feed(2)], CFG)
    assert [r.status for r in recs] == ["success", "success"] and all(r.broadcast == "batch" for r in recs)
    assert provider.batch_calls == 2 and provider.count("eth_sendRawTransaction") == 2
    provider.fail_batch_times = 2
    recs = signer.submit([_feed(3)], CFG)
    assert recs[0].status == "success" and recs[0].broadcast == "serial"


def test_a_node_refusal_mid_body_stops_the_burst_and_names_the_reason(live):
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    provider.refuse_nonce = start + 1
    recs = signer.submit([_feed(1), _feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success", "not_sent", "not_sent"]
    assert "no error message" in recs[1].reason  # an empty message never yields an empty reason
    assert recs[1].nonceRetry is None and recs[1].nonce == start + 1  # refused on its own: not a nonce race, not re-signed
    # the third item's refusal was a sequence mismatch behind the refused one; the node
    # does not know its hash, so it was re-signed ONCE at the fresh nonce (start + 1), which the
    # fake refuses too -> not_sent, the race named, the first refusal kept
    assert recs[2].nonceRetry is True and "sequence mismatch" in recs[2].broadcastError and recs[2].reason.startswith("nonce race: ")
    assert "no error message" in recs[2].reason and recs[2].nonce == start + 1
    assert provider.batch_calls == 2 and provider.count("eth_getTransactionByHash") == 2  # probed twice (~2 s, ~5 s), then the one retry
    assert recs[1].txHash is None and recs[2].txHash is None
    assert w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending") == start + 1
    assert signer.lane("lab").cursor == start + 1  # never past a refused step
    # the next burst re-reads the pending nonce and continues from the chain's truth
    provider.refuse_nonce = None
    recs2 = signer.submit([_feed(4)], CFG)
    assert recs2[0].nonce == start + 1 and recs2[0].status == "success"


def test_stop_abort_between_chunks_leaves_later_chunks_unoffered(live):
    provider, w3, chain, signer, sink = live
    signer.chunk = 2
    offered = []
    signer.abort = lambda: len(offered) >= 1
    real = signer._offer_chunk

    def spy(chunk, addr):
        offered.append(len(chunk))
        return real(chunk, addr)

    signer._offer_chunk = spy  # type: ignore[assignment]
    recs = signer.submit([_feed(k) for k in range(1, 6)], CFG)
    assert [r.status for r in recs] == ["success", "success", "not_sent", "not_sent", "not_sent"]
    assert offered == [2] and all("stop requested" in r.reason for r in recs[2:])
    assert signer.quiescence("lab").quiescent


def test_rpc_unavailable_on_the_nonce_read_raises_before_anything_is_signed(live):
    provider, w3, chain, signer, sink = live
    def flaky(method, params):
        if method == "eth_getTransactionCount":
            raise ConnectionError("down")

    provider.inject = flaky
    with pytest.raises(RpcUnavailable):
        signer.submit([_feed(1)], CFG)
    assert provider.count("eth_sendRawTransaction") == 0
    q = signer.quiescence("lab")
    assert q.quiescent is False and q.source == "chain"


def test_no_key_value_in_records_requests_or_descriptions(live):
    provider, w3, chain, signer, sink = live
    recs = signer.submit([_feed(1), _feed(2)], CFG)
    blob = json.dumps([r.as_ledger_tx("b1") for r in recs]) + json.dumps(signer.describe()) + json.dumps(signer.grants()) + json.dumps(signer.quiescence("lab").as_dict())
    assert FIXTURE_KEY_HEX not in blob
    for method, params in provider.log:
        assert FIXTURE_KEY_HEX not in json.dumps(params, default=str), method


# ---- the nonce cursor and the sequence-mismatch retry (SPEC P6) ----


def _external_send(w3, sink, nonce: int) -> str:
    """A transaction of the SAME operator signed outside the keeper (another
    process on the key): what moves the node's count under the keeper."""
    from eth_account import Account

    tx = {"to": sink, "from": FIXTURE_OPERATOR_ADDRESS, "data": b"", "value": 0, "gas": 100_000, "nonce": nonce, "chainId": w3.eth.chain_id, **TEST_GAS_PRICE}
    return w3.eth.send_raw_transaction(bytes(Account.sign_transaction(tx, FIXTURE_OPERATOR_KEY).raw_transaction)).hex()


def _freeze_pending(provider, at: int) -> None:
    """The node's pending count stops at `at` (it has not caught up with its
    own admissions), whatever the chain really holds."""
    def mutate(method, params, resp):
        if method == "eth_getTransactionCount" and len(params) > 1 and params[1] == "pending" and str(params[0]).lower() == FIXTURE_OPERATOR_ADDRESS.lower():
            real = int(resp["result"], 16) if isinstance(resp["result"], str) else int(resp["result"])
            return {**resp, "result": min(real, at)}
        return None

    provider.mutate = mutate


def test_two_bursts_of_one_operator_under_a_frozen_pending_count_take_consecutive_nonces_from_the_cursor(live):
    """Two books of one account ticked in one pass, both reading the same
    pending count (the node had not caught up with the first burst): the
    second would get `account sequence mismatch`. The lane's cursor is the
    lower bound: the second burst signs at the next nonce whatever the count
    says; the count is still read once per burst (it catches an external
    sender)."""
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _freeze_pending(provider, start)
    n0 = provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending"))
    first = signer.submit([_feed(1), _feed(2)], CFG)
    second = signer.submit([_feed(3)], CFG)
    third = signer.submit([_feed(4), _feed(5)], CFG)
    recs = first + second + third
    assert [r.nonce for r in recs] == [start, start + 1, start + 2, start + 3, start + 4]
    assert all(r.status == "success" and r.nonceRetry is None and r.broadcastError is None and r.reconciled is None for r in recs)
    assert provider.count("eth_getTransactionCount", (FIXTURE_OPERATOR_ADDRESS, "pending")) == n0 + 3  # once per burst
    assert provider.count("eth_getTransactionByHash") == 0  # nothing to prove: no mismatch
    assert signer.lane("lab").cursor == start + 5 and signer.lane("lab").confirmed == start + 5


def test_a_burst_refused_pre_send_does_not_move_the_cursor(live):
    provider, w3, chain, signer, sink = live
    chain._systems["system.craft"] = deploy(w3, reverter("craft: not enough stamina"))
    signer.submit([_feed(1)], CFG)
    c = signer.lane("lab").cursor
    recs = signer.submit([Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 1})], CFG)
    assert recs[0].status == "not_sent" and recs[0].precheck == "reverted" and recs[0].nonce is None
    assert signer.lane("lab").cursor == c

    def down(method, params):
        if method == "eth_getTransactionCount":
            raise ConnectionError("down")

    provider.inject = down
    with pytest.raises(RpcUnavailable):
        signer.submit([_feed(2)], CFG)
    assert signer.lane("lab").cursor == c
    provider.inject = None
    assert signer.submit([_feed(3)], CFG)[0].nonce == c


def test_a_stale_pending_read_behind_an_external_sender_is_the_mismatch_path_re_signed_once(live):
    """The cursor covers the keeper's own bursts; an EXTERNAL sender on the
    same operator (the web client, another process) still makes a stale
    read possible. The node's `account sequence mismatch` on a hash it does
    not know is re-signed ONCE at the fresh nonce and lands on the same
    row set: `nonceRetry`, the first refusal kept, one row per step."""
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    signer.submit([_feed(1)], CFG)  # cursor start + 1
    _external_send(w3, sink, start + 1)  # someone else spends start + 1 and start + 2
    _external_send(w3, sink, start + 2)
    _freeze_pending(provider, start + 1)  # the node still answers the old count
    n0 = provider.count("eth_sendRawTransaction")
    b0 = provider.batch_calls
    recs = signer.submit([_feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success", "success"]
    assert [r.nonce for r in recs] == [start + 3, start + 4]  # re-signed at the fresh nonces (the node's own `expected`)
    assert all(r.nonceRetry is True and "account sequence mismatch" in r.broadcastError and r.reason is None for r in recs)
    assert recs[0].broadcastError.startswith(f"account sequence mismatch, expected {start + 3}, got {start + 1}")
    assert provider.batch_calls == b0 + 2 and provider.count("eth_sendRawTransaction") == n0 + 4  # two refused attempts on the first body, two taken on the one retry body
    assert provider.count("eth_getTransactionByHash") == 4  # two rows x two probes, all unknown
    for r in recs:
        assert w3.eth.get_transaction(r.txHash)["nonce"] == r.nonce and w3.eth.get_transaction_receipt(r.txHash).status == 1
    rows = [r.as_ledger_tx("b1") for r in recs]
    assert all(row["nonceRetry"] is True and row["broadcastError"] and "reason" not in row for row in rows)
    assert signer.lane("lab").cursor == start + 5
    # the next burst is plain again
    provider.mutate = None
    nxt = signer.submit([_feed(4)], CFG)
    assert nxt[0].nonce == start + 5 and nxt[0].status == "success" and nxt[0].nonceRetry is None


def test_an_off_by_one_mismatch_lets_the_step_behind_it_land_and_re_signs_only_the_refused_one(live):
    """A JSON-RPC batch is admitted item by item: when the first step's
    nonce is one behind, the second step's nonce IS the one the node
    expects and lands on the first body. The retry then starts after the
    burst's own taken steps, not at the node's first `expected`."""
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    signer.submit([_feed(1)], CFG)
    _external_send(w3, sink, start + 1)
    _freeze_pending(provider, start + 1)
    recs = signer.submit([_feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success", "success"]
    assert recs[1].nonce == start + 2 and recs[1].nonceRetry is None and recs[1].broadcastError is None  # landed on the first body
    assert recs[0].nonce == start + 3 and recs[0].nonceRetry is True and f"expected {start + 2}, got {start + 1}" in recs[0].broadcastError
    assert signer.lane("lab").cursor == start + 4 and w3.eth.get_transaction(recs[0].txHash)["nonce"] == start + 3


def test_a_mismatch_on_a_hash_the_node_knows_is_a_held_step_not_a_retry(live):
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    provider.mismatch_after_forward = {start + 1}
    n0 = provider.count("eth_sendRawTransaction")
    b0 = provider.batch_calls
    recs = signer.submit([_feed(1), _feed(2), _feed(3)], CFG)
    assert [r.status for r in recs] == ["success"] * 3 and [r.nonce for r in recs] == [start, start + 1, start + 2]
    assert recs[1].reconciled == "transaction known to the node after a sequence mismatch" and "sequence mismatch" in recs[1].broadcastError
    assert recs[1].nonceRetry is None and recs[0].nonceRetry is None
    # the node TOOK the mismatched step, so the step behind it was in sequence and landed on the same body
    assert recs[2].nonceRetry is None and recs[2].broadcastError is None and recs[2].reconciled is None
    assert provider.batch_calls == b0 + 1 and provider.count("eth_sendRawTransaction") == n0 + 3  # nothing re-signed
    assert provider.count("eth_getTransactionByHash") == 1  # known at the first probe
    assert signer.lane("lab").cursor == start + 3


def test_a_sequence_mismatch_is_never_waited_on_and_a_refused_retry_is_not_sent(live):
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    signer.submit([_feed(1)], CFG)
    _external_send(w3, sink, start + 1)
    _freeze_pending(provider, start + 1)
    signer.receipt_timeout_sec = 60  # a wait on an unknown hash would show as 60 s here
    provider.refuse_nonce = start + 2  # the retry at the fresh nonce is refused too
    import time as _t

    rc0 = provider.count("eth_getTransactionReceipt")
    t0 = _t.monotonic()
    recs = signer.submit([_feed(2)], CFG)
    assert _t.monotonic() - t0 < 5
    r = recs[0]
    assert r.status == "not_sent" and r.txHash is None and r.nonceRetry is True and r.nonce == start + 2
    assert r.reason.startswith("nonce race: ") and "account sequence mismatch" in r.broadcastError
    # never a receipt WAIT on a hash the node does not know: at most the one reconciliation lookup of the retry
    assert provider.count("eth_getTransactionReceipt") - rc0 <= 1
    assert signer.lane("lab").cursor == start + 2  # the fresh count read, never past a refused step
    provider.refuse_nonce = None
    provider.mutate = None
    assert signer.submit([_feed(3)], CFG)[0].nonce == start + 2


def test_an_rpc_failure_during_receipt_collection_keeps_every_broadcast_row_unconfirmed_with_its_hash(live):
    """The one keeper-side path that could sign and broadcast without a
    ledger row: `wait_receipt` raising after the body was admitted. The
    rows are returned `unconfirmed` with hash and nonce instead."""
    provider, w3, chain, signer, sink = live
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")

    def flaky(method, params):
        if method == "eth_getTransactionReceipt":
            raise ConnectionError("down")

    provider.inject = flaky
    recs = signer.submit([_feed(1), _feed(2)], CFG)
    assert [r.status for r in recs] == ["unconfirmed", "unconfirmed"] and [r.nonce for r in recs] == [start, start + 1]
    assert all(r.txHash and "receipt read unavailable" in r.reconciled for r in recs)
    provider.inject = None
    for r in recs:
        assert w3.eth.get_transaction_receipt(r.txHash).status == 1
    assert signer.lane("lab").cursor == start + 2
    assert signer.quiescence("lab").quiescent
