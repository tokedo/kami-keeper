"""The tx row at broadcast and at receipt, the nonce gap closed after a
restart (open broadcast rows), the ledger readers.

  §A the signer's two-phase report: `on_broadcast` once per burst, after
     reconcile + retry and before any receipt read, every record in its
     post-broadcast state; a burst refused in full by the pre-check still
     reports; the DryRunSigner never calls it; `Ledger.open_tx()` and the
     `stage` schema rule.
  §B the core writes a `broadcast` row then a `receipt` row per live step
     (the burst, the batch-fallback singles, the pooled collect), one
     `not_sent` row at the broadcast stage, a receipt-read failure as
     the second row, `nonceRetry` on the broadcast row, `lastTxHash` at
     broadcast.
  §C open broadcast rows probed at boot and per tick; the receipt
     row with `reconciled`, the `not_sent` close on a passed nonce, the
     expiry, `openTx` in status, dry run has none.
  §E an `unconfirmed` RECEIPT row is still an open step.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import ValidationError
from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, FIXTURE_OPERATOR_KEY
from keeper.ledger import Ledger, validate_row
from keeper.protocol import Basis
from keeper.secrets import OperatorKey
from keeper.signer import DryRunSigner, Intent
from test_live_signer import CFG, TEST_GAS_PRICE, _feed, live  # noqa: F401 — the tester-chain signer fixture

H1 = "0x" + "ab" * 32
H2 = "0x" + "cd" * 32


# ------------------------------------------------------------ §A the signer's two-phase report


def test_on_broadcast_is_called_once_after_reconcile_and_before_any_receipt_read_with_every_record(live):
    provider, w3, chain, signer, sink = live
    seen: list[list[dict[str, Any]]] = []
    receipt_reads_at_callback: list[int] = []

    def cb(records):
        receipt_reads_at_callback.append(provider.count("eth_getTransactionReceipt"))
        seen.append([{"status": r.status, "txHash": r.txHash, "nonce": r.nonce, "block": r.block} for r in records])

    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    rc0 = provider.count("eth_getTransactionReceipt")
    recs = signer.submit([_feed(1), _feed(2)], CFG, on_broadcast=cb)
    assert len(seen) == 1 and receipt_reads_at_callback == [rc0]  # once, and no receipt had been read yet
    assert seen[0] == [{"status": "unconfirmed", "txHash": recs[0].txHash, "nonce": start, "block": None}, {"status": "unconfirmed", "txHash": recs[1].txHash, "nonce": start + 1, "block": None}]
    assert [r.status for r in recs] == ["success", "success"] and all(r.block is not None for r in recs)
    assert signer._on_broadcast is None  # cleared after the burst
    # a caller with no callback gets the one-phase report
    assert [r.status for r in signer.submit([_feed(3)], CFG)] == ["success"] and len(seen) == 1


def test_a_burst_refused_in_full_by_the_precheck_still_reports_at_the_broadcast_stage(live):
    from evm_stubs import deploy, reverter

    provider, w3, chain, signer, sink = live
    chain._systems["system.craft"] = deploy(w3, reverter("Craft: no stamina"))
    seen: list[list[Any]] = []
    recs = signer.submit([Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 1}, "b1")], CFG, on_broadcast=lambda rs: seen.append([(r.status, r.reason, r.txHash) for r in rs]))
    assert [r.status for r in recs] == ["not_sent"] and recs[0].precheck == "reverted"
    assert seen == [[("not_sent", recs[0].reason, None)]]  # known in full at the broadcast stage: one report, no receipt stage
    assert provider.count("eth_sendRawTransaction") == 0


def test_a_mixed_burst_reports_the_refused_row_not_sent_and_the_others_unconfirmed(live):
    from evm_stubs import deploy, reverter

    provider, w3, chain, signer, sink = live
    chain._systems["system.craft"] = deploy(w3, reverter("Craft: no stamina"))
    seen: list[list[tuple[str, str | None]]] = []
    recs = signer.submit([_feed(1), Intent("system.craft", "lab", None, {"recipeIndex": 29, "amount": 1}, "b1"), _feed(2)], CFG, on_broadcast=lambda rs: seen.append([(r.status, r.txHash) for r in rs]))
    assert [s for s, _h in seen[0]] == ["unconfirmed", "not_sent", "unconfirmed"] and seen[0][1][1] is None
    assert [r.status for r in recs] == ["success", "not_sent", "success"]


def test_the_dry_run_signer_never_calls_on_broadcast():
    signer = DryRunSigner(None)
    signer.grant("lab", OperatorKey("LAB_OPERATOR_KEY", FIXTURE_OPERATOR_KEY))
    called: list[Any] = []
    recs = signer.submit([_feed(1)], CFG, on_broadcast=called.append)
    assert [r.status for r in recs] == ["dry_run"] and called == []
    assert "stage" not in recs[0].as_ledger_tx("b1") and recs[0].as_ledger_tx("b1", stage="broadcast")["stage"] == "broadcast"


def test_open_tx_is_the_broadcast_rows_without_a_receipt_row_and_stageless_rows_are_never_open(tmp_path):
    led = Ledger(tmp_path / "b1", "b1", "lab")
    b = Basis(lensBlock=100, lensReconciledThrough=98)
    base = {"nonce": 5, "block": None, "status": "unconfirmed", "gasUsed": None, "effectiveGasPrice": None, "kami": 1, "system": "system.kami.use.item"}
    led.tx(b, {**base, "txHash": H1, "stage": "broadcast"})
    led.tx(b, {**base, "txHash": H2, "nonce": 6, "stage": "broadcast"})
    led.tx(b, {**base, "txHash": H1, "block": 101, "status": "success", "gasUsed": 1, "effectiveGasPrice": 2, "stage": "receipt"})
    led.tx(b, {**base, "txHash": "0x" + "ef" * 32, "nonce": 7, "block": 101, "status": "success"})  # a row with no stage (the one-phase shape)
    led.tx(b, {**base, "txHash": None, "nonce": 8, "status": "not_sent", "reason": "pre-check: reverted", "stage": "broadcast"})
    led.tx(b, {**base, "txHash": None, "nonce": None, "status": "dry_run"}, dry_run=True)
    assert set(led.open_tx()) == {H2} and led.open_tx()[H2]["tx"]["nonce"] == 6
    assert led.tx_hashes() == {H1, H2, "0x" + "ef" * 32}  # a set: the two-row step counts once


def test_the_stage_is_broadcast_or_receipt_and_a_dry_run_row_carries_none(tmp_path):
    led = Ledger(tmp_path / "b1", "b1", "lab")
    b = Basis()
    base = {"txHash": H1, "nonce": 5, "block": None, "gasUsed": None, "effectiveGasPrice": None, "kami": 1, "system": "system.kami.use.item"}
    with pytest.raises(ValidationError):
        led.tx(b, {**base, "status": "unconfirmed", "stage": "sent"})
    with pytest.raises(ValidationError):
        led.tx(b, {**base, "txHash": None, "nonce": None, "status": "dry_run", "stage": "broadcast"}, dry_run=True)
    validate_row(led._base("tx", b, None, False) | {"tx": {**base, "status": "unconfirmed", "account": "lab", "bookId": "b1", "cycleOpenedBy": "keeper", "stage": "broadcast"}})
    assert led.open_tx() == {}


# ------------------------------------------------------------ §B the core: broadcast row, then receipt row

from conftest import HAF_CONFIG  # noqa: E402
from evm_stubs import BatchingTesterProvider, deploy, deploy_world, reverter  # noqa: E402
from keeper.chain import Chain  # noqa: E402
from keeper.config import load as load_config  # noqa: E402
from keeper.core import Keeper  # noqa: E402
from keeper.lens import LensClient  # noqa: E402
from keeper.strategies.base import HARVESTING, RESTING  # noqa: E402
from test_phase1d import T as T1D, _rows as _krows  # noqa: E402 — _krows takes the keeper, _rows the Env
from test_phase2a import BOOK_43, NODE, _apply_plans_to_world, _decisions, _last_tick, _rows, _set_party, _start, _tick_all  # noqa: E402
from test_phase2c import _external_send, _freeze_pending, _live_env  # noqa: E402

SAME_KEYS = ("txHash", "nonce", "account", "kami", "system", "bookId", "fn", "count", "gasLimit", "precheck", "cycleOpenedBy")


def _pairs(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    """(broadcast row, receipt row or None) per step, in ledger order."""
    out: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    for r in rows:
        t = r["tx"]
        if t.get("stage") == "broadcast":
            out.append((t, None))
        elif t.get("stage") == "receipt":
            i = next(i for i, (b, rc) in enumerate(out) if rc is None and b["txHash"] == t["txHash"])
            out[i] = (out[i][0], t)
    return out


def _same(b: dict[str, Any], r: dict[str, Any]) -> None:
    for k in SAME_KEYS:
        assert b.get(k) == r.get(k), (k, b.get(k), r.get(k))


def test_a_normal_burst_writes_a_broadcast_row_then_a_receipt_row_per_step_in_order_with_the_same_hash(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, [25814, 27850, 28532], previewsPerTick=50)  # the first kami HARVESTING on the node, the second RESTING at 96 %
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, 25814, hp=(20, 230))  # below the floor: a stop, plus the start of the second — one burst, two steps
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _tick_all(e)
    rows = _rows(e, book, "tx")
    assert [r["tx"]["stage"] for r in rows] == ["broadcast", "broadcast", "receipt", "receipt"]  # the burst's broadcast rows first, then its receipts
    pairs = _pairs(rows)
    assert [(b["system"], b["nonce"], b["status"], b["block"]) for b, _r in pairs] == [("system.harvest.stop", start, "unconfirmed", None), ("system.harvest.start", start + 1, "unconfirmed", None)]
    for b, r in pairs:
        assert r is not None and r["status"] == "success" and r["block"] > 0 and r["gasUsed"] > 0 and r["effectiveGasPrice"] > 0
        _same(b, r)
        assert b["txHash"] == r["txHash"] and w3.eth.get_transaction_receipt(r["txHash"]).status == 1
    assert all(r["dryRun"] is False and r["lensBlock"] == rows[0]["lensBlock"] for r in rows)
    assert book.plan["25814"] == {"state": RESTING, "node": None, "pending": True} and book.plan["27850"] == {"state": HARVESTING, "node": NODE}  # the plan follows the returned records
    assert _last_tick(e, book)["txStatus"] == ["success", "success"] and _last_tick(e, book)["txCount"] == 2  # the tick counts steps, not rows
    assert e.keeper.runtime[book.id]["lastTxHash"] == pairs[1][0]["txHash"]


def test_a_pre_send_refusal_is_one_not_sent_row_at_the_broadcast_stage(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    chain._systems["system.harvest.stop"] = deploy(w3, reverter("Harvest: not active"))
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))
    _tick_all(e)
    rows = [r["tx"] for r in _rows(e, book, "tx")]
    assert len(rows) == 1 and rows[0]["stage"] == "broadcast" and rows[0]["status"] == "not_sent" and rows[0]["precheck"] == "reverted" and "not active" in rows[0]["reason"]
    assert rows[0]["txHash"] is None and rows[0]["nonce"] is None and provider.count("eth_sendRawTransaction") == 0
    assert _rows(e, book, "deviation")[0]["deviation"]["kami"] == BOOK_43[0]  # the backoff rule applies
    assert e.keeper.runtime[book.id].get("lastTxHash") is None


def test_a_receipt_read_that_raises_writes_the_broadcast_row_and_an_unconfirmed_receipt_row(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))

    def flaky(method, params):
        if method == "eth_getTransactionReceipt":
            raise ConnectionError("down")

    provider.inject = flaky
    _tick_all(e)
    provider.inject = None
    pairs = _pairs(_rows(e, book, "tx"))
    assert len(pairs) == 1
    b, r = pairs[0]
    assert b["status"] == "unconfirmed" and "reconciled" not in b
    assert r is not None and r["status"] == "unconfirmed" and "receipt read unavailable" in r["reconciled"] and r["block"] is None  # the receipt row says what the receipt read said
    _same(b, r)
    assert w3.eth.get_transaction_receipt(r["txHash"]).status == 1  # the chain took it; the row does not claim so
    assert book.plan[str(BOOK_43[0])] == {"state": RESTING, "node": None, "pending": True}  # unconfirmed lands the plan


def test_the_nonce_race_retry_puts_nonce_retry_and_the_retried_nonce_on_the_broadcast_row(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _external_send(w3, start)  # another process on the operator spends `start`
    _freeze_pending(provider, start)  # the node still answers the old count -> mismatch -> one re-sign at start + 1
    _tick_all(e)
    pairs = _pairs(_rows(e, book, "tx"))
    assert len(pairs) == 1
    b, r = pairs[0]
    assert b["nonceRetry"] is True and b["nonce"] == start + 1 and "account sequence mismatch" in b["broadcastError"] and b["status"] == "unconfirmed" and "reason" not in b
    assert r is not None and r["nonceRetry"] is True and r["nonce"] == start + 1 and r["broadcastError"] == b["broadcastError"] and r["status"] == "success"
    _same(b, r)
    assert w3.eth.get_transaction(r["txHash"])["nonce"] == start + 1


def _batch_only_reverter(limit: int) -> bytes:
    """Runtime code: STOP when CALLDATASIZE <= limit, else REVERT with an
    Error(string) — a system that takes singles and refuses the batch."""
    body = reverter("Harvest: batch refused")
    # CALLDATASIZE, PUSH2 limit, LT (limit < size), PUSH1 dest, JUMPI, STOP, JUMPDEST, <revert>
    return b"\x36" + b"\x61" + int(limit).to_bytes(2, "big") + b"\x10" + b"\x60" + bytes([9]) + b"\x57" + b"\x00" + b"\x5b" + body


def test_the_batch_fallback_singles_each_get_a_broadcast_row_and_a_receipt_row_after_the_batch_one_not_sent_row(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    chain._systems["system.harvest.stop"] = deploy(w3, _batch_only_reverter(100))  # a single stop is 36 bytes of calldata, a batch of two 132
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))
    _set_party(e.lens, BOOK_43[1], hp=(20, 240))  # two below the floor: one batched stop of two
    start = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _tick_all(e)
    rows = [r["tx"] for r in _rows(e, book, "tx")]
    assert [(t["stage"], t["status"], t.get("batchFallback"), t.get("batchSize"), t["count"]) for t in rows] == [
        ("broadcast", "not_sent", None, None, 2),  # the batch: refused pre-send, one row
        ("broadcast", "unconfirmed", True, 1, 1), ("broadcast", "unconfirmed", True, 1, 1),  # the two singles, broadcast
        ("receipt", "success", True, 1, 1), ("receipt", "success", True, 1, 1),  # their receipts
    ]
    assert rows[0]["precheck"] == "reverted" and "batch refused" in rows[0]["reason"] and rows[0]["txHash"] is None
    pairs = _pairs(_rows(e, book, "tx"))
    assert [b["nonce"] for b, _r in pairs] == [None, start, start + 1]
    for b, r in pairs[1:]:
        _same(b, r)
    assert _last_tick(e, book)["batchFallbackTx"] == 2 and _last_tick(e, book)["txStatus"] == ["not_sent", "success", "success"]
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True} for k in BOOK_43[:2])
    assert _rows(e, book, "deviation") == []  # the singles landed: no backoff


def test_the_pooled_collect_writes_one_broadcast_row_per_book_then_one_receipt_row_per_book_and_last_tx_hash_moves_at_broadcast(make_keeper, tmp_path):
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "armed": True, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    k.signer.gas_price = TEST_GAS_PRICE
    k.signer.receipt_timeout_sec, k.signer.receipt_poll_sec = 5, 0.01
    assert k.handle("grant", {"account": "lab"})["ok"]
    books = []
    for kami in (1001, 1002):
        e.lens.set_kami(kami, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(HAF_CONFIG, kamiIndex=kami, collectEvery=1000)})
        assert s["ok"], s
        books.append(k.store.get(s["data"]["book"]["id"]))
    # at the receipt stage the broadcast rows and lastTxHash must already be there
    seen_at_receipts: list[tuple[list[list[str]], list[str | None]]] = []
    orig = k.signer._collect_receipts

    def spy(records, order, accepted, lane):
        seen_at_receipts.append(([[r["tx"]["stage"] for r in _krows(k, b, "tx")] for b in books], [k.runtime[b.id].get("lastTxHash") for b in books]))
        return orig(records, order, accepted, lane)

    k.signer._collect_receipts = spy
    k.tick_due(now=T1D)
    k.tick_due(now=T1D + 1000)
    per_book = [_pairs(_krows(k, b, "tx")) for b in books]
    assert [len(p) for p in per_book] == [1, 1]
    (ba, ra), (bb, rb) = per_book[0][0], per_book[1][0]
    assert ba["txHash"] == bb["txHash"] == ra["txHash"] == rb["txHash"] and ba["batchSize"] == bb["batchSize"] == ra["batchSize"] == 2
    assert (ba["kami"], bb["kami"]) == (1001, 1002) and ba["bookId"] == books[0].id and bb["bookId"] == books[1].id and ba["status"] == bb["status"] == "unconfirmed"
    assert "musuCollected" not in {k_: v for k_, v in ba.items() if v is not None} and ra["status"] == rb["status"] == "success" and ra["decodeError"] == "the receipt carried no logs"
    _same(ba, ra)
    _same(bb, rb)
    assert seen_at_receipts == [([["broadcast"], ["broadcast"]], [ba["txHash"], ba["txHash"]])]
    k.stop_engine()


# ------------------------------------------------------------ §C open broadcast rows after a restart

from pathlib import Path  # noqa: E402

from keeper.core import OPEN_TX_MAX_AGE_SEC  # noqa: E402


def _armed_env(make_keeper, tmp_path, armed: bool = True):
    """A keeper on the tester chain with ONE harvestAndFeed book whose kami
    sits above the feed floor (a tick acts on nothing); returns the pieces
    to restart it over the same dataDir."""
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper()
    flags = {"profile": "lab", "dataDir": str(tmp_path / "data"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}
    if armed:
        flags["armed"] = True

    def keeper() -> Keeper:
        k = Keeper(load_config(flags=flags, env={}), lens=LensClient(e.lens.socket_path, 5), chain=chain)
        k.boot()
        if armed:
            k.signer.gas_price = TEST_GAS_PRICE
            k.signer.receipt_timeout_sec, k.signer.receipt_poll_sec = 5, 0.01
        return k

    k = keeper()
    assert k.handle("grant", {"account": "lab"})["ok"]
    e.lens.set_kami(1001, state="HARVESTING", node=62, hp=(120, 200), account=4242)
    s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(HAF_CONFIG, collectEvery=100000)})
    assert s["ok"], s
    book = k.store.get(s["data"]["book"]["id"])
    k.tick_book(book)  # nothing to do at 120/200
    k.stop_engine()
    return e, provider, w3, chain, keeper, book


def _external_tx(w3, nonce: int) -> str:
    from eth_account import Account

    tx = {"to": w3.eth.accounts[1], "from": FIXTURE_OPERATOR_ADDRESS, "data": b"", "value": 0, "gas": 100_000, "nonce": nonce, "chainId": w3.eth.chain_id, **TEST_GAS_PRICE}
    return "0x" + w3.eth.send_raw_transaction(bytes(Account.sign_transaction(tx, FIXTURE_OPERATOR_KEY).raw_transaction)).hex()


def _seed_open_row(book, data_dir: Path, tx_hash: str, nonce: int, system: str = "system.kami.use.item", at: str | None = None) -> dict[str, Any]:
    """A `broadcast` row as the core writes it, with no receipt row —
    what a restart inside the receipt wait leaves behind."""
    led = Ledger(data_dir / "books" / book.id, book.id, "lab")
    row = led.tx(Basis(lensBlock=500, lensReconciledThrough=498), {"txHash": tx_hash, "nonce": nonce, "status": "unconfirmed", "kami": 1001, "system": system, "stage": "broadcast", "fn": "executeTyped", "count": 1, "precheck": "ok", "gasLimit": 3_500_000, "broadcast": "batch"})
    if at is not None:
        lines = led.path.read_text(encoding="utf-8").splitlines()
        last = json.loads(lines[-1])
        last["at"] = at
        lines[-1] = json.dumps(last, separators=(",", ":"), sort_keys=True)
        led.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        row = last
    return row


def _tx_rows(k, book) -> list[dict[str, Any]]:
    return [r for r in k.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "tx"]


def test_an_open_broadcast_row_whose_receipt_the_chain_knows_gets_its_receipt_row_at_boot(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h_feed, h_collect = _external_tx(w3, n), _external_tx(w3, n + 1)  # two steps the chain took whose receipts a restart never read
    data_dir = tmp_path / "data"
    _seed_open_row(book, data_dir, h_feed, n)
    _seed_open_row(book, data_dir, h_collect, n + 1, system="system.harvest.collect")
    plan_before = dict(book.plan)
    rc0 = provider.count("eth_getTransactionReceipt")
    k2 = keeper()
    rows = _tx_rows(k2, book)
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("broadcast", "unconfirmed"), ("receipt", "success"), ("receipt", "success")]
    for b, r in zip(rows[:2], rows[2:]):
        _same(b["tx"], r["tx"])
        assert r["tx"]["reconciled"] == "receipt read after restart" and r["tx"]["block"] > 0 and r["tx"]["gasUsed"] > 0 and r["tx"]["effectiveGasPrice"] > 0
        assert r["lensBlock"] is None and r["lensReconciledThrough"] is None and r["degraded"] == [] and r["dryRun"] is False  # a chain read: null basis, like a kill row
    assert rows[3]["tx"]["musuCollected"] is None and rows[3]["tx"]["decodeError"] == "the receipt carried no logs"  # the collect's drain decode, never a guess
    assert provider.count("eth_getTransactionReceipt") - rc0 == 2  # one read per open hash
    assert k2._ledger(k2.store.get(book.id)).open_tx() == {} and k2.runtime[book.id]["_openTx"] == {}
    assert k2.store.get(book.id).plan == plan_before  # the plan is not re-applied from a receipt found after a restart
    resumed = [r for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "lifecycle" and r["event"] == "resumed"]
    assert resumed[-1]["openTx"] == 0 and rows[-1]["at"] <= resumed[-1]["at"]  # closed before the resumed row
    assert k2.handle("grant", {"account": "lab"})["ok"]
    st = k2.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["openTx"] == 0 and "openTx" in st
    k2.stop_engine()


def test_an_unknown_hash_whose_nonce_the_operator_has_passed_is_closed_not_sent_naming_the_count(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _external_tx(w3, n)  # another sender took nonce n; the keeper's own step at n was never admitted
    _seed_open_row(book, tmp_path / "data", H1, n)
    k2 = keeper()  # no grant yet at boot: the receipt read finds nothing, no operator address to count against -> open
    assert k2.runtime[book.id]["_openTx"].keys() == {H1}
    assert k2.handle("grant", {"account": "lab"})["ok"]
    assert k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 1
    k2.tick_book(k2.store.get(book.id))
    rows = _tx_rows(k2, book)
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "not_sent")]
    t = rows[1]["tx"]
    assert t["reason"] == "nonce passed without a receipt after restart" and t["txHash"] == H1 and t["nonce"] == n and t["block"] is None
    assert t["reconciled"] == f"confirmed nonce count {n + 1} read at tick is above nonce {n}; the hash is unknown to the chain"
    assert k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    k2.stop_engine()


def test_an_unknown_hash_whose_nonce_has_not_passed_stays_open_and_is_probed_once_per_tick(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _seed_open_row(book, tmp_path / "data", H2, n)  # the next nonce: nothing has taken it
    k2 = keeper()
    assert k2.handle("grant", {"account": "lab"})["ok"]
    b2 = k2.store.get(book.id)
    for i in range(3):
        rc = provider.count("eth_getTransactionReceipt")
        k2.tick_book(b2)
        assert provider.count("eth_getTransactionReceipt") == rc + 1  # one read per open hash per pass
        assert k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 1 and len(_tx_rows(k2, book)) == 1
    _external_tx(w3, n)  # now something else took the nonce
    k2.tick_book(b2)
    rows = _tx_rows(k2, book)
    assert [r["tx"]["status"] for r in rows] == ["unconfirmed", "not_sent"] and rows[1]["tx"]["reason"] == "nonce passed without a receipt after restart"
    assert k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    k2.stop_engine()


def test_a_chain_read_that_fails_leaves_the_row_open_for_the_next_pass(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)
    _seed_open_row(book, tmp_path / "data", h, n)

    def down(method, params):
        if method == "eth_getTransactionReceipt":
            raise ConnectionError("down")

    provider.inject = down
    k2 = keeper()  # the boot probe: the read fails, the row stays open, nothing is written
    assert k2.runtime[book.id]["_openTx"].keys() == {h} and len(_tx_rows(k2, book)) == 1
    assert any("receipt read unavailable" in l["line"] and "next pass" in l["line"] for l in k2.logs)
    assert k2.handle("grant", {"account": "lab"})["ok"]
    provider.inject = None
    k2.tick_book(k2.store.get(book.id))
    rows = _tx_rows(k2, book)
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "success")] and rows[1]["tx"]["reconciled"] == "receipt read after restart"
    k2.stop_engine()


def test_an_open_row_older_than_the_expiry_is_closed_not_sent_as_expired(make_keeper, tmp_path):
    import datetime as _dt

    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    old = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=OPEN_TX_MAX_AGE_SEC + 60)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    _seed_open_row(book, tmp_path / "data", H1, n + 50, at=old)  # a nonce far ahead: the count rule cannot close it
    assert OPEN_TX_MAX_AGE_SEC == 86400
    k2 = keeper()
    rows = _tx_rows(k2, book)
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "not_sent")]
    assert rows[1]["tx"]["reason"] == "open broadcast row expired" and rows[1]["tx"]["txHash"] == H1 and rows[1]["tx"]["nonce"] == n + 50
    assert k2.runtime[book.id]["_openTx"] == {}
    k2.stop_engine()


def test_a_dry_run_keeper_probes_nothing_and_reads_open_tx_zero(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path, armed=False)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    _seed_open_row(book, tmp_path / "data", _external_tx(w3, n), n)
    rc0 = provider.count("eth_getTransactionReceipt")
    k2 = keeper()
    assert k2.dry_run and provider.count("eth_getTransactionReceipt") == rc0 and "_openTx" not in k2.runtime[book.id]
    assert k2.handle("grant", {"account": "lab"})["ok"]
    k2.tick_book(k2.store.get(book.id))
    assert provider.count("eth_getTransactionReceipt") == rc0 and len(_tx_rows(k2, book)) == 1
    assert k2.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    k2.stop_engine()


def test_a_dry_run_keeper_writes_one_stageless_row_per_record_as_before(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    env.lens.set_kami(1001, hp=(40, 200))
    env.keeper.tick_book(book)
    txs = [r for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tx"]
    assert len(txs) == 1 and txs[0]["dryRun"] is True and "stage" not in txs[0]["tx"] and txs[0]["tx"]["status"] == "dry_run"
    assert env.keeper._ledger(book).open_tx() == {}


# ------------------------------------------------------------ §E an `unconfirmed` RECEIPT row is still an open step


def _seed_unconfirmed_receipt(book, data_dir: Path, tx_hash: str, nonce: int, system: str = "system.kami.use.item") -> dict[str, Any]:
    """The receipt row of a step whose receipt read did not answer —
    `stage: receipt`, `unconfirmed`, no block / gas, the RPC's text."""
    led = Ledger(data_dir / "books" / book.id, book.id, "lab")
    return led.tx(Basis(), {"txHash": tx_hash, "nonce": nonce, "status": "unconfirmed", "kami": 1001, "system": system, "stage": "receipt", "fn": "executeTyped", "count": 1, "precheck": "ok", "gasLimit": 3_500_000, "broadcast": "batch", "reconciled": "receipt read unavailable (eth_getTransactionReceipt: HTTPError)"})


def test_a_receipt_row_that_is_unconfirmed_leaves_the_hash_open_and_any_other_status_closes_it(tmp_path):
    led = Ledger(tmp_path / "b1", "b1", "lab")
    b = Basis(lensBlock=100, lensReconciledThrough=98)
    base = {"nonce": 5, "block": None, "status": "unconfirmed", "gasUsed": None, "effectiveGasPrice": None, "kami": 1, "system": "system.kami.use.item"}
    bcast = led.tx(b, {**base, "txHash": H1, "stage": "broadcast"})
    led.tx(b, {**base, "txHash": H1, "stage": "receipt", "reconciled": "receipt read unavailable (eth_getTransactionReceipt: HTTPError)"})
    assert set(led.open_tx()) == {H1}  # the receipt read did not answer: the step is still open
    assert led.open_tx()[H1] == bcast and led.open_tx()[H1]["tx"]["stage"] == "broadcast"  # the entry stays the FIRST broadcast row
    led.tx(b, {**base, "txHash": H1, "block": 101, "status": "success", "gasUsed": 1, "effectiveGasPrice": 2, "stage": "receipt"})
    assert led.open_tx() == {} and led.tx_hashes() == {H1}  # three rows, one hash, closed
    # a reverted and a not_sent receipt row close too
    led.tx(b, {**base, "txHash": H2, "nonce": 6, "stage": "broadcast"})
    led.tx(b, {**base, "txHash": H2, "nonce": 6, "stage": "receipt", "reconciled": "x"})
    assert set(led.open_tx()) == {H2}
    led.tx(b, {**base, "txHash": H2, "nonce": 6, "block": 102, "status": "reverted", "stage": "receipt"})
    assert led.open_tx() == {}
    h3 = "0x" + "12" * 32
    led.tx(b, {**base, "txHash": h3, "nonce": 7, "stage": "broadcast"})
    led.tx(b, {**base, "txHash": h3, "nonce": 7, "stage": "receipt", "reconciled": "y"})
    led.tx(b, {**base, "txHash": h3, "nonce": 7, "status": "not_sent", "reason": "open broadcast row expired", "stage": "receipt"})
    assert led.open_tx() == {}
    # a receipt `unconfirmed` row with NO broadcast row of its own: nothing to repeat, never open
    led2 = Ledger(tmp_path / "b2", "b2", "lab")
    led2.tx(b, {**base, "txHash": H1, "stage": "receipt", "reconciled": "z"})
    assert led2.open_tx() == {}


def test_an_unconfirmed_receipt_row_of_the_act_path_is_open_and_the_next_tick_closes_it_with_the_receipt(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    _set_party(e.lens, BOOK_43[0], hp=(20, 240))

    def flaky(method, params):
        if method == "eth_getTransactionReceipt":
            raise ConnectionError("down")

    provider.inject = flaky
    _tick_all(e)
    provider.inject = None
    rows = _rows(e, book, "tx")
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed")]
    h = rows[0]["tx"]["txHash"]
    rt = e.keeper.runtime[book.id]
    assert rt["_openTx"].keys() == {h} and rt["_openTx"][h]["tx"]["stage"] == "receipt"  # the step the receipt read could not settle
    assert e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 1
    assert e.keeper._ledger(book).open_tx().keys() == {h}  # and the ledger says so too, for the next boot
    plan_before = dict(book.plan)
    _apply_plans_to_world(e)
    rc = provider.count("eth_getTransactionReceipt")
    _tick_all(e)
    assert provider.count("eth_getTransactionReceipt") - rc == 1  # one read for the open hash
    rows = _rows(e, book, "tx")
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows[:3]] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed"), ("receipt", "success")]
    final = rows[2]["tx"]
    assert final["reconciled"] == f"{rows[1]['tx']['reconciled']}; receipt read at tick" and "receipt read unavailable" in final["reconciled"]
    assert final["block"] > 0 and final["gasUsed"] > 0 and final["effectiveGasPrice"] > 0
    assert w3.eth.get_transaction_receipt(h).status == 1 and final["txHash"] == h
    _same(rows[0]["tx"], final)
    assert "_opened" not in final and "_opened" not in rows[2]  # a runtime-only key, never in the ledger
    assert rows[2]["lensBlock"] is None and rows[2]["dryRun"] is False  # a chain read: null basis
    assert e.keeper._ledger(book).open_tx() == {} and rt["_openTx"] == {}
    assert e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["openTx"] == 0
    assert rt["lastActBlock"] == final["block"]  # the bar moves with the late receipt
    # the close re-applies nothing: the act path already counted the unconfirmed
    # record as landed, and no new step was taken this tick (only the `pending`
    # mark clears, as it does on any tick the lens confirms the planned state)
    assert len(rows) == 3 and {k: v["state"] for k, v in book.plan.items()} == {k: v["state"] for k, v in plan_before.items()}
    assert book.plan[str(BOOK_43[0])] == {"state": RESTING, "node": None}


def test_the_pooled_collects_unconfirmed_receipt_row_is_open_in_every_book_and_each_book_closes_its_own(make_keeper, tmp_path):
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "armed": True, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    k.signer.gas_price = TEST_GAS_PRICE
    k.signer.receipt_timeout_sec, k.signer.receipt_poll_sec = 5, 0.01
    assert k.handle("grant", {"account": "lab"})["ok"]
    books = []
    for kami in (1001, 1002):
        e.lens.set_kami(kami, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(HAF_CONFIG, kamiIndex=kami, collectEvery=1000)})
        assert s["ok"], s
        books.append(k.store.get(s["data"]["book"]["id"]))

    def flaky(method, params):
        if method == "eth_getTransactionReceipt":
            raise ConnectionError("down")

    k.tick_due(now=T1D)
    provider.inject = flaky
    k.tick_due(now=T1D + 1000)  # the pooled collect: one hash, one row per book, the receipt read down
    provider.inject = None
    h = _krows(k, books[0], "tx")[0]["tx"]["txHash"]
    for b in books:
        rows = _krows(k, b, "tx")
        assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed")]
        assert rows[0]["tx"]["txHash"] == h and k.runtime[b.id]["_openTx"].keys() == {h}  # the same hash, one open entry per BOOK
        assert k.handle("status", {"book": b.id})["data"]["books"][0]["openTx"] == 1
    for i, b in enumerate(books):
        k.tick_book(b)
        rows = _krows(k, b, "tx")
        assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows[:3]] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed"), ("receipt", "success")]
        final = rows[2]["tx"]
        assert final["reconciled"].endswith("; receipt read at tick") and final["kami"] == (1001, 1002)[i] and final["batchSize"] == 2
        assert final["block"] > 0 and final["gasUsed"] > 0 and k.runtime[b.id]["_openTx"] == {}
        assert k.runtime[b.id]["lastActBlock"] == final["block"]
        # the OTHER book is untouched by this book's close
        other = books[1 - i]
        assert (len(_krows(k, other, "tx")) == 3) is (i == 1)
    k.stop_engine()


def test_an_unconfirmed_receipt_row_in_the_ledger_is_open_at_boot_and_closed_with_the_restart_note(make_keeper, tmp_path):
    e, provider, w3, chain, keeper, book = _armed_env(make_keeper, tmp_path)
    n = w3.eth.get_transaction_count(FIXTURE_OPERATOR_ADDRESS, "pending")
    h = _external_tx(w3, n)  # the step the chain took; the keeper restarted before it learned the receipt
    data_dir = tmp_path / "data"
    _seed_open_row(book, data_dir, h, n)
    _seed_unconfirmed_receipt(book, data_dir, h, n)
    rc0 = provider.count("eth_getTransactionReceipt")
    k2 = keeper()
    rows = _tx_rows(k2, book)
    assert [(r["tx"]["stage"], r["tx"]["status"]) for r in rows] == [("broadcast", "unconfirmed"), ("receipt", "unconfirmed"), ("receipt", "success")]
    assert provider.count("eth_getTransactionReceipt") - rc0 == 1  # one read for the one open hash
    final = rows[2]["tx"]
    assert final["reconciled"] == "receipt read after restart" and final["block"] > 0 and final["gasUsed"] > 0  # opened at boot: the boot note
    _same(rows[0]["tx"], final)
    assert k2._ledger(k2.store.get(book.id)).open_tx() == {} and k2.runtime[book.id]["_openTx"] == {}
    resumed = [r for r in k2.handle("ledger", {"book": book.id})["data"]["rows"] if r["kind"] == "lifecycle" and r["event"] == "resumed"]
    assert resumed[-1]["openTx"] == 0
    k2.stop_engine()
