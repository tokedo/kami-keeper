"""SPEC P6 — the signer contract: the interface, the DryRunSigner and the
LiveSigner.

Order of gates on `submit`, for every backend: grant gate (an account
with no active grant signs nothing) -> whitelist (keeper/whitelist.py,
the same check chain.py applies at resolution) -> backend.

LiveSigner, per burst (one account = one operator = one nonce lane):
  1. encode every intent against keeper/abi (chain.encode) and derive its
     gas from the cited ceilings — base + per_item x count for the three
     harvest families, flat for feed and craft; over MAX_TX_GAS is refused
     before signing (server.py L323 _batch_gas);
  2. eth_call the exact calldata from the operator address; a revert
     refuses THAT item with the node's message (status not_sent) and the
     burst goes on without it;
  3. read the pending nonce ONCE (server.py L1864: pending, never latest)
     and take the larger of it and the lane's CURSOR (the nonce after the
     last step this process broadcast — the node's pending count lags its
     own admission by a block or so, so two bursts of one operator in one
     tick pass would both read the count the first had already used), sign
     the survivors on consecutive nonces,
     compute each hash at sign time (keccak of the signed bytes, L10218
     _seq_tx_hash);
  4. broadcast in chunks of <= 32 (L10171 _SEQ_BATCH_CHUNK), each chunk
     a JSON-RPC batch whose ids are the nonces, each request carrying a
     timeout of chunk x 0.5 s x 2 with a 30 s floor (L10172-10173),
     chunks offered SEQUENTIALLY and each reconciled before the next: a
     transport failure is reconciled against the node's pending nonce
     (a nonce below it is held, so it was sent), the rest re-offered
     once, then a serial fallback marked `broadcast: serial`
     (L10317-10379 _seq_offer_chunk); a refused item stops the offer of
     later chunks (their nonces sit behind a gap);
  5. before any row is REPORTED not_sent it is reconciled: pending nonce,
     a settle and a re-read when part of a body was held, then the
     receipt for the pre-computed hash (L10489-10538 _seq_reconcile) —
     a mined step is never reported not_sent. A refusal whose
     text is a SEQUENCE MISMATCH (`account sequence mismatch`, `nonce too
     low`, `incorrect account sequence`) is the node saying it did NOT
     take the step, and the pending count cannot confirm otherwise (the
     count moved because another step landed — the row's nonce is below
     it either way); the "sent" claim is proved by
     eth_getTransactionByHash after a grace (~2 s, then ~5 s): known ->
     `unconfirmed` as any held step; unknown -> the step and every step
     behind it are re-signed ONCE at fresh nonces (the larger of a
     re-read pending count and the nonce the node's text says it
     expected), broadcast and reconciled again on the same rows
     (`nonceRetry: true`, the first refusal kept as `broadcastError`); a
     retry the node refuses too is `not_sent` with `reason: nonce race:
     <text>`. A hash the node does not know is never waited on;
  6. receipts are collected serially (timeout per tx); success / reverted
     carry gasUsed, effectiveGasPrice and block; a timeout is
     `unconfirmed` with its hash, and so is a receipt read the RPC did not
     answer (the row keeps its hash and nonce and is returned —
     a broadcast step always gets its ledger row).

`submit` reports in TWO stages. `submit(intents, config,
on_broadcast=...)` takes an optional callback; the LiveSigner calls it
ONCE per burst, after step 5 (reconcile + the nonce-race retry) and
before step 6 (receipts), with every record of the burst in its
post-broadcast state — `unconfirmed` with `txHash` and `nonce` (plus
`reconciled` / `broadcastError` / `nonceRetry` when they apply) or
`not_sent` with its `reason` — and also when nothing survived the
pre-check (every record `not_sent`). The caller writes the `broadcast`
ledger rows from the callback and the `receipt` rows from the returned
records, so a keeper restart inside the receipt wait cannot lose the
row of a step the chain took. The
DryRunSigner never calls it: nothing is broadcast.

The lane's cursor after a burst is the highest broadcast nonce + 1, or
the last pending count read when nothing was broadcast; it never runs
ahead of a step the node refused (the next burst would sign behind a gap
the node fills with nothing).

Gas price is the flat Yominet fee (server.py L102): maxFeePerGas
2,500,000 wei, priority 0 — charged AS OFFERED, so it is a floor and is
never read from chain. Tests on the tester chain override it.

The operator key value is touched exactly once, inside the signing call
(the reveal call on the grant's key, passed straight to eth_account); it is never
bound to a name here (the ast-scan test).
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from eth_account import Account
from web3 import Web3

from . import chain as chainmod
from .chain import Call, Chain, PrecheckReverted, RpcUnavailable, TransportError
from .scrub import KEY_SHAPED_RE, find_forbidden
from .secrets import OperatorKey
from .whitelist import BATCH_CAPS, WHITELIST, Intent, NotGranted, SignerRefused, WhitelistRefused, check_system, check_whitelist

__all__ = [
    "BATCH_CAPS", "WHITELIST", "Intent", "NotGranted", "SignerRefused", "WhitelistRefused", "check_system", "check_whitelist",
    "GAS_PRICE", "MAX_TX_GAS", "GAS_CEILINGS", "GAS_PER_ITEM", "BATCH_CHUNK", "gas_for",
    "Grant", "TxRecord", "Quiescence", "NonceLane", "Signer", "DryRunSigner", "LiveSigner",
    "NONCE_MISMATCH_RE", "MISMATCH_PROBE_S", "is_nonce_mismatch", "expected_nonce",
]

# -- cited constants ---------------------------------------------------------
# Every number below is copied from kami-harness 3.7.0 (commit a2c22c1),
# executor/server.py, at the line named. None is re-measured here (SPEC P6
# / D3).
#
#   L102  _GAS_PRICE = {"maxFeePerGas": 2_500_000, "maxPriorityFeePerGas": 0}
#         (Yominet charges maxFeePerGas AS OFFERED; the constant is the
#         floor and is not read from chain — a base-fee rise fails loudly)
#   L149  MAX_TX_GAS = 31_500_000 (the per-transaction lane cap; the block
#         gas limit is 45,000,000)
#   L181  "feed_kami": 3_500_000      (system.kami.use.item, feed path: p95
#         2,185,084 / max 2,639,799 over 329,709 successful tx; 1.33x max)
#   L226  "craft_item": 2_200_000     (system.craft: p99 1,408,180 / max
#         1,701,712)
#   L281  "harvest_start_base": 1_300_000, L282 "harvest_start_per_item":
#         950_000 (n=1 p95 1,636,037; n=12 p95 9,339,266; n=25 p95
#         18,385,855; the pair is ~1.36x p95 across n)
#   L286  "harvest_stop_base": 1_600_000, L287 "harvest_stop_per_item":
#         1_950_000 (n=1 p95 2,645,724; n=12 p95 19,029,290; ~1.31x)
#   L294  "harvest_collect_base": 1_600_000, L295 "harvest_collect_per_item":
#         1_700_000 (n=1 p95 2,465,867; n=12 p95 16,844,165; ~1.31x)
#   L314  _harvest_max_per_call: (MAX_TX_GAS - base) // per_item -> start 31,
#         stop 15, collect 17; L323 _batch_gas refuses base + per_item*n >
#         MAX_TX_GAS before signing.
#   L10171-10173  chunk 32, 0.5 s per item, 30 s floor; L10176 settle 1.0 s.
GAS_PRICE: dict[str, int] = {"maxFeePerGas": 2_500_000, "maxPriorityFeePerGas": 0}  # server.py L102
MAX_TX_GAS = 31_500_000  # server.py L149
GAS_CEILINGS: dict[str, int] = {
    "system.harvest.start": 1_300_000,  # server.py L281 harvest_start_base
    "system.harvest.stop": 1_600_000,  # server.py L286 harvest_stop_base
    "system.harvest.collect": 1_600_000,  # server.py L294 harvest_collect_base
    "system.kami.use.item": 3_500_000,  # server.py L181 feed_kami (flat)
    "system.craft": 2_200_000,  # server.py L226 craft_item (flat)
}
GAS_PER_ITEM: dict[str, int] = {
    "system.harvest.start": 950_000,  # server.py L282 harvest_start_per_item
    "system.harvest.stop": 1_950_000,  # server.py L287 harvest_stop_per_item
    "system.harvest.collect": 1_700_000,  # server.py L295 harvest_collect_per_item
}
BATCH_CHUNK = 32  # server.py L10171 _SEQ_BATCH_CHUNK
BATCH_ITEM_S = 0.5  # server.py L10172
BATCH_TIMEOUT_FLOOR_S = 30  # server.py L10173
RECONCILE_SETTLE_S = 1.0  # server.py L10176

# A node refusal whose text says the STEP'S NONCE was wrong —
# Cosmos SDK / minievm (Yominet): "account sequence mismatch, expected N+1,
# got N [code -32000]" / "incorrect account sequence"; geth: "nonce too
# low". Such a
# refusal is a claim the node did NOT take the step.
NONCE_MISMATCH_RE = re.compile(r"account sequence mismatch|incorrect account sequence|nonce too low", re.IGNORECASE)
_EXPECTED_NONCE_RE = re.compile(r"expected[:\s]+(\d+)", re.IGNORECASE)
# the grace before each eth_getTransactionByHash probe of a mismatch row:
# one poll at ~2 s, then once more at ~5 s
MISMATCH_PROBE_S: tuple[float, ...] = (2.0, 3.0)
NOT_OFFERED_AFTER_REFUSAL = "not offered: an earlier item of the burst was refused"
NOT_OFFERED_STOP = "not offered: stop requested before this chunk"


def is_nonce_mismatch(text: str | None) -> bool:
    return bool(text) and NONCE_MISMATCH_RE.search(str(text)) is not None


def expected_nonce(text: str | None) -> int | None:
    """The nonce the node's refusal text says it expected, when it says."""
    m = _EXPECTED_NONCE_RE.search(str(text or ""))
    return int(m.group(1)) if m else None


def gas_for(system: str, count: int = 1) -> int:
    """base + per_item x count (harvest families) or the flat ceiling; refuses
    over MAX_TX_GAS before signing (server.py L323 _batch_gas)."""
    base = GAS_CEILINGS[system]
    per = GAS_PER_ITEM.get(system, 0)
    gas = base + per * max(1, int(count)) if per else base
    if gas > MAX_TX_GAS:
        fits = max(1, (MAX_TX_GAS - base) // per) if per else 1
        raise WhitelistRefused(f"{system}: {count} kamis need {gas:,} gas, over the {MAX_TX_GAS:,} lane cap; at most {fits} per transaction")
    return gas


def batch_timeout(items: int) -> float:
    return max(float(BATCH_TIMEOUT_FLOOR_S), items * BATCH_ITEM_S * 2)


@dataclass(frozen=True)
class Grant:
    account: str
    key: OperatorKey
    grantedAt: str

    def public(self) -> dict[str, Any]:
        return {"account": self.account, "operatorAddress": self.key.address, "grantedAt": self.grantedAt}


@dataclass
class TxRecord:
    """What `submit` reports per intent — the ledger `tx` row's fields."""

    system: str
    account: str
    kami: int | None
    status: str  # success | reverted | unconfirmed | not_sent | dry_run
    txHash: str | None = None
    nonce: int | None = None
    block: int | None = None
    gasUsed: int | None = None
    effectiveGasPrice: int | None = None
    params: dict[str, Any] = field(default_factory=dict)
    # evidence fields (all optional on the row)
    reason: str | None = None  # why not_sent (pre-check revert, node refusal, not offered)
    reconciled: str | None = None  # the evidence that rescued a row the broadcast could not report
    broadcastError: str | None = None  # what the node said before the rescue
    broadcast: str | None = None  # batch | serial
    precheck: str | None = None  # ok | reverted | rpc-unavailable | skipped
    gasLimit: int | None = None
    fn: str | None = None
    count: int | None = None
    # the receipt's component-write logs (hex), kept on the record
    # for the collect decoder and NEVER written to the ledger row
    logs: list[dict[str, Any]] | None = None
    # a batched collect — how many kamis the shared tx carried
    batchSize: int | None = None
    # the step was re-signed once at a fresh nonce after a
    # sequence-mismatch refusal of a hash the node did not know
    nonceRetry: bool | None = None

    def as_ledger_tx(self, book_id: str, stage: str | None = None) -> dict[str, Any]:
        """The ledger `tx` fields. `stage` is `broadcast` or
        `receipt` on a live row and absent on a dry-run row."""
        out: dict[str, Any] = {
            "txHash": self.txHash, "nonce": self.nonce, "block": self.block, "status": self.status,
            "gasUsed": self.gasUsed, "effectiveGasPrice": self.effectiveGasPrice, "account": self.account,
            "kami": self.kami, "bookId": book_id, "cycleOpenedBy": "keeper", "system": self.system,
        }
        for k in ("reason", "reconciled", "broadcastError", "broadcast", "precheck", "gasLimit", "fn", "count", "batchSize", "nonceRetry"):
            v = getattr(self, k)
            if v is not None:
                out[k] = v
        if stage is not None:
            out["stage"] = stage
        return out


@dataclass
class Quiescence:
    account: str
    pendingNonce: int
    confirmedNonce: int
    lastTxHash: str | None
    lastBlock: int | None
    source: str = "lane"  # lane (dry run) | chain

    @property
    def quiescent(self) -> bool:
        return self.pendingNonce == self.confirmedNonce

    def as_dict(self) -> dict[str, Any]:
        return {"account": self.account, "pendingNonce": self.pendingNonce, "confirmedNonce": self.confirmedNonce, "quiescent": self.quiescent, "lastTxHash": self.lastTxHash, "lastBlock": self.lastBlock, "source": self.source}


class NonceLane:
    """One lane per operator (SPEC P6): the pending nonce is read once per
    burst, consecutive nonces within a burst, receipts reconciled before
    the next burst. `address` survives a revoke so the quiescence proof can
    still be read from the chain."""

    def __init__(self, account: str):
        self.account = account
        self.address: str | None = None
        self.pending = 0
        self.confirmed = 0
        self.last_hash: str | None = None
        self.last_block: int | None = None
        self.bursts = 0
        self.stuck_since: float | None = None
        # the nonce after the last step this process broadcast on
        # this lane; a lower bound on the next burst's start nonce (None
        # until the first burst)
        self.cursor: int | None = None
        self.lock = threading.Lock()


class Signer:
    """The interface. `submit` is the only entry to signing; it applies the
    grant gate, then the whitelist, then the backend."""

    kind = "interface"

    def __init__(self) -> None:
        self._grants: dict[str, Grant] = {}
        self._lanes: dict[str, NonceLane] = {}
        self._lock = threading.RLock()
        # set by the core: True when the current burst should stop offering
        # further chunks (stop --immediate); read between chunks only
        self.abort: Callable[[], bool] = lambda: False
        # the caller's broadcast-stage callback for the burst in
        # flight, held here (under the submit lock) so `_send` keeps its
        # signature for every backend and test double
        self._on_broadcast: Callable[[list[TxRecord]], None] | None = None

    # -- grants ----------------------------------------------------------

    def grant(self, account: str, key: OperatorKey) -> Grant:
        with self._lock:
            g = Grant(account=account, key=key, grantedAt=_now_iso())
            self._grants[account] = g
            lane = self._lanes.setdefault(account, NonceLane(account))
            if lane.address != key.address:
                lane.cursor = None  # another operator address: the cursor was about the old one
            lane.address = key.address
            return g

    def revoke(self, account: str) -> Grant | None:
        with self._lock:
            return self._grants.pop(account, None)

    def has_grant(self, account: str) -> bool:
        return account in self._grants

    def grants(self) -> list[dict[str, Any]]:
        return [g.public() for g in self._grants.values()]

    def lane(self, account: str) -> NonceLane:
        with self._lock:
            return self._lanes.setdefault(account, NonceLane(account))

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind}

    # -- the send path ---------------------------------------------------

    def submit(self, intents: list[Intent], book_config: dict[str, Any] | None, on_broadcast: Callable[[list[TxRecord]], None] | None = None) -> list[TxRecord]:
        """Sign and send one burst. `on_broadcast` is called once by a
        live backend with every record in its post-broadcast state, before
        any receipt is awaited; None is the default one-stage report and the
        dry-run backend never calls it."""
        if not intents:
            return []
        accounts = {i.account for i in intents}
        if len(accounts) != 1:
            raise SignerRefused("a burst is one account (one nonce lane)")
        account = intents[0].account
        with self._lock:
            grant = self._grants.get(account)
            if grant is None:
                raise NotGranted(f"account {account} has no active grant; nothing is signed")
            for i in intents:
                check_whitelist(i, book_config)
            self._on_broadcast = on_broadcast
            try:
                return self._send(grant, intents)
            finally:
                self._on_broadcast = None

    def _report_broadcast(self, records: list[TxRecord]) -> None:
        """The broadcast-stage report, once per burst."""
        cb = self._on_broadcast
        if cb is not None:
            cb(list(records))

    def _send(self, grant: Grant, intents: list[Intent]) -> list[TxRecord]:
        raise NotImplementedError

    def quiescence(self, account: str) -> Quiescence:
        lane = self.lane(account)
        with lane.lock:
            return Quiescence(account, lane.pending, lane.confirmed, lane.last_hash, lane.last_block)


class DryRunSigner(Signer):
    """Records what WOULD be sent; signs nothing; pending == confirmed
    always. Used for `armed: false`. With a `chain`, every intent is still
    encoded and pre-checked by eth_call (a READ) so a dry-run ledger says
    whether the chain would have accepted the call; without one the intent
    is encoded only."""

    kind = "dry-run"

    def __init__(self, chain: Chain | None = None) -> None:
        super().__init__()
        self.recorded: list[TxRecord] = []
        self.chain = chain

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "precheck": self.chain is not None}

    def _send(self, grant: Grant, intents: list[Intent]) -> list[TxRecord]:
        out = []
        for i in intents:
            rec = TxRecord(system=i.system, account=i.account, kami=i.kami, status="dry_run", params=dict(i.params))
            try:
                call = chainmod.encode(i)
                rec.fn, rec.count, rec.gasLimit = call.fn, call.count, gas_for(i.system, call.count)
            except (KeyError, ValueError, TypeError) as e:
                rec.reason = f"encoding refused: {type(e).__name__}"
                rec.precheck = "skipped"
                self.recorded.append(rec)
                out.append(rec)
                continue
            if self.chain is not None:
                try:
                    self.chain.precheck(call, grant.key.address)
                    rec.precheck = "ok"
                except PrecheckReverted as e:
                    rec.precheck = "reverted"
                    rec.reason = clean_text(e.reason)
                except (RpcUnavailable, chainmod.ChainError):
                    rec.precheck = "rpc-unavailable"
            self.recorded.append(rec)
            out.append(rec)
        return out


class LiveSigner(Signer):
    """The chain send path. See the module docstring for the burst."""

    kind = "live"

    def __init__(self, chain: Chain, gas_price: dict[str, int] | None = None, receipt_timeout_sec: float = 120.0, receipt_poll_sec: float = 0.5, chunk: int = BATCH_CHUNK, quiescence_wait_sec: float = 30.0, settle_sec: float = RECONCILE_SETTLE_S, sleep: Callable[[float], None] = time.sleep, mismatch_probe_sec: tuple[float, ...] = MISMATCH_PROBE_S):
        super().__init__()
        self.chain = chain
        self.gas_price = dict(gas_price or GAS_PRICE)
        self.receipt_timeout_sec = receipt_timeout_sec
        self.receipt_poll_sec = receipt_poll_sec
        self.chunk = max(1, min(int(chunk), BATCH_CHUNK))
        self.quiescence_wait_sec = quiescence_wait_sec
        self.settle_sec = settle_sec
        self.mismatch_probe_sec = tuple(mismatch_probe_sec)
        self._sleep = sleep
        self.sent: list[TxRecord] = []

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "maxFeePerGas": self.gas_price["maxFeePerGas"], "maxPriorityFeePerGas": self.gas_price["maxPriorityFeePerGas"], "chunk": self.chunk, "receiptTimeoutSec": self.receipt_timeout_sec, "chain": self.chain.describe()}

    # -- the burst ------------------------------------------------------

    def _send(self, grant: Grant, intents: list[Intent]) -> list[TxRecord]:
        account = grant.account
        addr = grant.key.address
        lane = self.lane(account)
        with lane.lock:
            self.chain.verify_chain_id()
            records: list[TxRecord] = []
            calls: dict[int, tuple[Call, int]] = {}  # row -> (call, gas)
            order: list[int] = []
            # 1-2: encode, gas, pre-check
            for i in intents:
                rec = TxRecord(system=i.system, account=account, kami=i.kami, status="not_sent", params=dict(i.params))
                records.append(rec)
                row = len(records) - 1
                try:
                    call = chainmod.encode(i)
                    gas = gas_for(i.system, call.count)
                except (KeyError, ValueError, TypeError) as e:
                    rec.reason = f"encoding refused: {type(e).__name__}"
                    rec.precheck = "skipped"
                    continue
                rec.fn, rec.count, rec.gasLimit = call.fn, call.count, gas
                try:
                    self.chain.precheck(call, addr)
                    rec.precheck = "ok"
                except PrecheckReverted as e:
                    rec.precheck = "reverted"
                    rec.reason = clean_text(e.reason)
                    continue
                calls[row] = (call, gas)
                order.append(row)
            if not calls:
                # nothing survived the pre-check: the burst is known in full
                # at the broadcast stage (one row per record)
                self._report_broadcast(records)
                return records
            # 3: nonce once, consecutive, from the larger of the pending count
            #    and the lane's cursor; hashes at sign time
            read = self.chain.pending_nonce(addr)
            start_nonce = read if lane.cursor is None else max(read, lane.cursor)
            lane.pending = start_nonce
            signed = self._sign_steps(grant, addr, [(row, *calls[row]) for row in order], start_nonce, records)
            lane.pending = start_nonce + len(signed)
            lane.bursts += 1
            # 4: chunks, sequential, reconciled
            accepted = self._offer_all(signed, records, addr)
            # 5: reconcile every not_sent row against the node before reporting
            #    it; a sequence-mismatch row the node does not know is the
            #    nonce race — re-signed ONCE at fresh nonces
            race = self._reconcile(records, signed, accepted, addr, retry=True)
            last_read = read
            if race:
                last_read = self._retry_nonce_race(grant, addr, records, race, calls, accepted, last_read)
            # the cursor: after the last broadcast step, or the last count read
            sent_nonces = [int(records[row].nonce) for row in accepted if records[row].nonce is not None]
            lane.cursor = (max(sent_nonces) + 1) if sent_nonces else last_read
            lane.pending = lane.cursor
            # every record is in its post-broadcast state — report
            # it BEFORE any receipt is awaited (a restart inside the wait
            # must not lose the row of a step the chain took)
            self._report_broadcast(records)
            # 6: receipts, serially
            self._collect_receipts(records, order, accepted, lane)
            self.sent.extend(records)
            return records

    def _sign_steps(self, grant: Grant, addr: str, steps: list[tuple[int, Call, int]], start_nonce: int, records: list[TxRecord]) -> list[tuple[int, int, bytes, str]]:
        """Sign `steps` on consecutive nonces from `start_nonce`; the ONE
        signing call of the process. Returns (row, nonce, rawtx, hash)."""
        signed: list[tuple[int, int, bytes, str]] = []
        for offset, (row, call, gas) in enumerate(steps):
            nonce = start_nonce + offset
            to = self.chain.resolve_system(call.system)
            tx = {"to": to, "from": addr, "data": call.calldata, "value": 0, "gas": gas, "nonce": nonce, "chainId": self.chain.chain_id, **self.gas_price}
            stx = Account.sign_transaction(tx, grant.key.reveal_bytes())
            rawtx = bytes(stx.raw_transaction)
            signed.append((row, nonce, rawtx, _hex(Web3.keccak(rawtx))))
            records[row].nonce = nonce
        return signed

    def _offer_all(self, signed: list[tuple[int, int, bytes, str]], records: list[TxRecord], addr: str) -> dict[int, str]:
        """Step 4: offer in chunks of <= self.chunk, sequentially, stopping
        at a refusal (later nonces sit behind a gap) or a stop request.
        Returns {row: hash} for the steps the node took."""
        accepted: dict[int, str] = {}
        stopped = False
        for start in range(0, len(signed), self.chunk):
            if stopped:
                break
            if self.abort():
                for row, _n, _r, _h in signed[start:]:
                    records[row].reason = NOT_OFFERED_STOP
                break
            chunk = signed[start:start + self.chunk]
            mode, outcomes = self._offer_chunk(chunk, addr)
            for row, ok, payload, evidence in outcomes:
                rec = records[row]
                rec.broadcast = mode
                if ok:
                    accepted[row] = payload
                    rec.txHash = payload
                    rec.status = "unconfirmed"
                    if evidence:
                        rec.reconciled = evidence
                else:
                    rec.reason = clean_text(payload)
                    stopped = True
            if stopped:
                offered = {row for row, *_ in outcomes}
                for row, _n, _r, _h in signed[start:]:
                    if row not in offered:
                        records[row].reason = NOT_OFFERED_AFTER_REFUSAL
        return accepted

    def _retry_nonce_race(self, grant: Grant, addr: str, records: list[TxRecord], race: list[int], calls: dict[int, tuple[Call, int]], accepted: dict[int, str], last_read: int) -> int:
        """The steps of `race` were refused for their nonce and the
        node does not know their hashes — re-sign them ONCE from the larger
        of a re-read pending count and the nonce the node said it expected,
        offer, reconcile without another retry. Each row keeps the first
        refusal as `broadcastError` and reads `nonceRetry: true`; a row the
        node refuses again is `not_sent` with `reason: nonce race: <text>`.
        Returns the pending count read (the cursor's floor when nothing of
        the burst was taken)."""
        texts = {row: str(records[row].reason or "") for row in race}
        try:
            fresh = self.chain.pending_nonce(addr)
        except RpcUnavailable:
            for row in race:
                rec = records[row]
                rec.broadcastError = texts[row]
                rec.reason = clean_text(f"nonce race: {texts[row]}")
                rec.reconciled = "pending nonce unreadable; not re-signed"
            return last_read
        # the fresh start: the count re-read, the nonce the node's text said it
        # expected, and the nonce after any step of THIS burst the node took
        # (a step behind an off-by-one mismatch lands on the first body: its
        # nonce was the one the node expected)
        expected = [n for n in (expected_nonce(texts[row]) for row in race) if n is not None]
        taken_already = [int(records[row].nonce) + 1 for row in accepted if records[row].nonce is not None]
        fresh = max([fresh] + expected + taken_already)
        for row in race:
            rec = records[row]
            rec.broadcastError = texts[row]
            rec.reason, rec.txHash, rec.status, rec.broadcast, rec.reconciled = None, None, "not_sent", None, None
            rec.nonceRetry = True
        retried = self._sign_steps(grant, addr, [(row, *calls[row]) for row in race], fresh, records)
        taken = self._offer_all(retried, records, addr)
        self._reconcile(records, retried, taken, addr, retry=False)
        for row, _n, _r, _h in retried:
            if row in taken:
                accepted[row] = taken[row]
            else:
                rec = records[row]
                rec.reason = clean_text(f"nonce race: {rec.reason or texts[row]}")
        return fresh

    def _collect_receipts(self, records: list[TxRecord], order: list[int], accepted: dict[int, str], lane: NonceLane) -> None:
        """Step 6: one receipt wait per broadcast step, in step order. A
        receipt read the RPC does not answer leaves the row `unconfirmed`
        with its hash and nonce: the step was broadcast, so it
        gets its row whatever the read said."""
        for row in order:
            if row not in accepted:
                continue
            rec = records[row]
            h = accepted[row]
            try:
                r = self.chain.wait_receipt(h, self.receipt_timeout_sec, self.receipt_poll_sec)
            except RpcUnavailable as e:
                rec.status = "unconfirmed"
                note = f"receipt read unavailable ({e})"
                rec.reconciled = f"{rec.reconciled}; {note}" if rec.reconciled else note
                continue
            if r is None:
                rec.status = "unconfirmed"
                continue
            rec.status = "success" if r["status"] == 1 else "reverted"
            rec.block, rec.gasUsed, rec.effectiveGasPrice = r["block"], r["gasUsed"], r["effectiveGasPrice"]
            rec.logs = list(r.get("logs") or [])
            nonce = int(rec.nonce) if rec.nonce is not None else lane.confirmed
            lane.confirmed = max(lane.confirmed, nonce + 1)
            lane.last_hash, lane.last_block = h, r["block"]

    def _offer_chunk(self, chunk: list[tuple[int, int, bytes, str]], addr: str) -> tuple[str, list[tuple[int, bool, str, str | None]]]:
        """Offer ONE chunk; outcomes in step order as (row, accepted, hash or
        node text, reconciliation evidence). Mirrors server.py L10317-10379."""
        results: dict[int, tuple[bool, str, str | None]] = {}
        remaining = list(chunk)
        mode = "batch"
        if not self.chain.supports_batch:
            mode = "serial"
        else:
            timeout_s = batch_timeout(len(chunk))
            for _attempt in (1, 2):
                try:
                    by_nonce = self.chain.send_raw_batch([(n, r) for _row, n, r, _h in remaining], timeout_s)
                except TransportError as e:
                    held = self._pending_or_none(addr)
                    still = []
                    for row, nonce, rawtx, h in remaining:
                        if held is not None and nonce < held:
                            results[row] = (True, h, f"nonce {nonce} < pending count {held} after {e}")
                        else:
                            still.append((row, nonce, rawtx, h))
                    remaining = still
                    if not remaining:
                        break
                    continue
                offered = len(remaining)
                for row, nonce, _rawtx, h in remaining:
                    resp = by_nonce.get(nonce)
                    if resp is None:
                        results[row] = (False, f"no response for nonce {nonce} in batch of {offered}", None)
                    elif resp.get("error") is not None:
                        results[row] = (False, _error_text(resp["error"], nonce), None)
                    else:
                        results[row] = (True, _hex(resp.get("result")) if resp.get("result") else h, None)
                remaining = []
                break
            if remaining:
                mode = "serial"  # two transport failures and the node holds none of these
        for row, nonce, rawtx, h in remaining:
            try:
                results[row] = (True, self.chain.send_raw(rawtx), None)
            except TransportError as e:
                held = self._pending_or_none(addr)
                if held is not None and nonce < held:
                    results[row] = (True, h, f"nonce {nonce} < pending count {held} after {e}")
                    continue
                results[row] = (False, f"transport failed: {e}", None)
                break
            except ValueError as e:
                results[row] = (False, (str(e) or f"node refused nonce {nonce} with no error message")[:300], None)
                break
        return mode, [(row, *results[row]) for row, _n, _r, _h in chunk if row in results]

    def _pending_or_none(self, addr: str) -> int | None:
        try:
            return self.chain.pending_nonce(addr)
        except RpcUnavailable:
            return None

    def _reconcile(self, records: list[TxRecord], signed: list[tuple[int, int, bytes, str]], accepted: dict[int, str], addr: str, retry: bool = True) -> list[int]:
        """server.py L10489-10538: `not_sent` is a claim about the NODE, so ask
        the node — pending nonce, settle + re-read when part of a body was
        held, then the receipt for the pre-computed hash.

        A row refused with a SEQUENCE-MISMATCH text is the node saying it
        did not take the step; the pending count cannot rescue it (the
        count moved because another step landed, and the row's nonce is
        below it either way — otherwise the row waits out a receipt timeout
        on a hash the node never saw). Such a row, and every row never offered
        behind it, is proved by eth_getTransactionByHash after a grace:
        known -> adopted as held; unknown -> returned as the nonce race
        (re-signed once by the caller when `retry`; left `not_sent` when
        this is the retry's own pass)."""
        unsent = [(row, nonce, h) for row, nonce, _r, h in signed if row not in accepted]
        if not unsent:
            return []

        def adopt(row: int, h: str, evidence: str) -> None:
            rec = records[row]
            if rec.reason:
                rec.broadcastError = rec.reason
                rec.reason = None
            rec.txHash, rec.status, rec.reconciled = h, "unconfirmed", evidence
            accepted[row] = h

        # the race set: from the first sequence-mismatch row on, every
        # mismatch row (proved by hash below) and every row never offered
        # because of it (unknown to the node by construction)
        race: list[int] = []
        probe: list[tuple[int, int, str]] = []
        first = next((i for i, (row, _n, _h) in enumerate(unsent) if is_nonce_mismatch(records[row].reason)), None)
        if first is not None:
            for row, nonce, h in unsent[first:]:
                reason = records[row].reason or ""
                if is_nonce_mismatch(reason):
                    probe.append((row, nonce, h))
                elif reason == NOT_OFFERED_AFTER_REFUSAL:
                    race.append(row)
            for grace in self.mismatch_probe_sec:
                if not probe:
                    break
                self._sleep(grace)
                still: list[tuple[int, int, str]] = []
                for row, nonce, h in probe:
                    try:
                        known = self.chain.get_transaction(h) is not None
                    except RpcUnavailable:
                        known = False
                    if known:
                        adopt(row, h, "transaction known to the node after a sequence mismatch")
                    else:
                        still.append((row, nonce, h))
                probe = still
            race.extend(row for row, _n, _h in probe)
            position = {row: i for i, (row, *_rest) in enumerate(signed)}
            race.sort(key=lambda row: position[row])
        in_race = set(race)
        unsent = [u for u in unsent if u[0] not in accepted and u[0] not in in_race]

        def below(count: int | None) -> list[int]:
            if count is None:
                return []
            taken = [row for row, nonce, h in unsent if nonce < count]
            for row, nonce, h in unsent:
                if nonce < count:
                    adopt(row, h, f"nonce {nonce} < pending count {count}")
            return taken

        if unsent:
            taken = set(below(self._pending_or_none(addr)))
            unsent = [u for u in unsent if u[0] not in taken]
            if unsent and taken:
                self._sleep(self.settle_sec)
                again = set(below(self._pending_or_none(addr)))
                unsent = [u for u in unsent if u[0] not in again]
            for row, _nonce, h in unsent:
                try:
                    if self.chain.receipt(h) is not None:
                        adopt(row, h, "receipt found for the pre-computed hash")
                except RpcUnavailable:
                    pass
        return race if retry else []

    # -- quiescence -----------------------------------------------------

    def quiescence(self, account: str) -> Quiescence:
        """pending nonce == latest nonce ON THE CHAIN (SPEC P8): polled up to
        quiescence_wait_sec; an unreadable chain answers not-quiescent."""
        lane = self.lane(account)
        addr = lane.address
        if addr is None:
            return Quiescence(account, lane.pending, lane.confirmed, lane.last_hash, lane.last_block)
        deadline = time.monotonic() + self.quiescence_wait_sec
        pending = confirmed = -1
        while True:
            try:
                pending = self.chain.pending_nonce(addr)
                confirmed = self.chain.latest_nonce(addr)
            except RpcUnavailable:
                return Quiescence(account, -1, -2, lane.last_hash, lane.last_block, source="chain")
            if pending == confirmed or time.monotonic() >= deadline:
                break
            self._sleep(min(0.5, self.receipt_poll_sec))
        with lane.lock:
            lane.pending, lane.confirmed = pending, confirmed
            if pending != confirmed:
                lane.stuck_since = lane.stuck_since or time.time()
            else:
                lane.stuck_since = None
        return Quiescence(account, pending, confirmed, lane.last_hash, lane.last_block, source="chain")


# ---- helpers --------------------------------------------------------------


def _hex(h: Any) -> str:
    if isinstance(h, (bytes, bytearray)):
        return "0x" + bytes(h).hex()
    s = str(h)
    return s if s.startswith("0x") else "0x" + s


def _error_text(err: Any, nonce: int) -> str:
    """A refused item's payload, verbatim and never empty (server.py
    L10229-10262 _seq_error_text)."""
    text = ""
    if isinstance(err, dict):
        message = err.get("message")
        if message not in (None, ""):
            parts = [str(message)]
            if err.get("code") is not None:
                parts.append(f"[code {err['code']}]")
            if err.get("data") not in (None, ""):
                parts.append(f"[data {err['data']}]")
            text = " ".join(parts)
    elif err is not None:
        text = str(err)
    if not text.strip():
        text = f"node refused nonce {nonce} with no error message: {err!r}"
    return text[:300]


def clean_text(text: str) -> str:
    """External text (a node message) bound for a ledger row or a status
    field: 32-byte hex runs are replaced (the frame rule), forbidden words
    withhold the text (the scrub rule), length is bounded."""
    t = KEY_SHAPED_RE.sub("<hash>", str(text))[:300]
    if find_forbidden(t):
        return "node message withheld: it carried forbidden language"
    return t


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
