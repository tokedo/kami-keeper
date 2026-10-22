"""Kill evidence from the chain: the `component.state`
DEAD writes on the World, attributed through the liquidate calldata.

Why the chain and not Kamiden: SPEC D1 — kill evidence is chain logs
(this module) until a lens `deaths` query is served; Kamiden is never
load-bearing. Why calldata: the victim's DEAD write carries no attacker,
so the attacker is read from the liquidate transaction's calldata.

What a liquidate leaves in the World's logs (verified on a recorded
liquidate receipt in tests/fixtures/receipts/, kami <killer> killing kami
<victim>):

  * a `ComponentValueSet(uint256,address,uint256,bytes)` log with
    topic[1] = uint256(keccak256("component.state")), topic[2] = the
    writing system (address, left-padded), topic[3] = the VICTIM'S KAMI
    ENTITY (`kami_entity_id(<victim>)`), and a `bytes` payload that is the
    ABI encoding of the string `DEAD` — decoded as ABI `bytes` then
    `string` (`abi_decode(["bytes"], data)[0]`, then
    `abi_decode(["string"], inner)[0]`); the same block also writes
    `INACTIVE` on the victim's harvest entity, which this module ignores;
  * the transaction's calldata: selector `0x915657fb` =
    `executeTyped(uint256 victimHarvID, uint256 killerID)` with
    victimHarvID == keccak("harvest", keccak("kami.id", <victim>)) and
    killerID == keccak("kami.id", <killer>) (asserted by
    tests/test_kills.py); the `execute(bytes)` form carries the same two
    words ABI-encoded inside the bytes argument.

KAMI_CAST world events (a cast on a kami of the book's account) are
decoded from the Emitter's (`world._emitter()`)
`WorldEvent(string,uint8[],bytes)` log with topic[1] =
keccak("KAMI_CAST"); data = abi(uint8[] schema, bytes value);
value = abi(uint256 accountId, uint256 timestamp, uint256 targetKamiId,
uint32 itemIndex, uint32 nodeIndex). The caster's ACCOUNT ENTITY is
uint256(owner address) (chain.py `account_entity_id`); its account index
is resolved through the lens `account <address>` query by the owner
ledger's caller, never guessed here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from web3 import Web3

from .chain import Chain, RpcUnavailable
from .receipts import COMPONENT_VALUE_SET_TOPIC0

if TYPE_CHECKING:  # blockclock imports this module for BLOCK_SECONDS
    from .blockclock import BlockClock

_codec = Web3().codec  # provider-less: ABI decoding only (the web3 dependency, not eth_abi directly)


def abi_decode(types: list[str], data: bytes) -> tuple:
    return tuple(_codec.decode(types, data))

# uint256(keccak256("component.state")) — the state component's id (the
# same derivation as receipts.py VALUE_COMPONENT_ID for `component.value`).
STATE_COMPONENT_ID: int = int.from_bytes(Web3.keccak(text="component.state"), "big")
STATE_COMPONENT_TOPIC: str = "0x" + STATE_COMPONENT_ID.to_bytes(32, "big").hex()
COMPONENT_VALUE_SET_TOPIC: str = "0x" + COMPONENT_VALUE_SET_TOPIC0.hex()

# `executeTyped(uint256,uint256)` of the liquidate system — the selector on
# the recorded receipt; and `execute(bytes)`.
LIQUIDATE_SELECTOR: str = "0x915657fb"
EXECUTE_BYTES_SELECTOR: str = "0x" + Web3.keccak(text="execute(bytes)")[:4].hex()

# the Emitter's world-event log and the KAMI_CAST event name
WORLD_EVENT_TOPIC: str = "0x" + Web3.keccak(text="WorldEvent(string,uint8[],bytes)").hex()
KAMI_CAST_TOPIC: str = "0x" + Web3.keccak(text="KAMI_CAST").hex()

# SPEC D2: assumed block time ~2.5 s -> 1,440 blocks an hour. This is the
# FALLBACK rate only — the window and the first-boot scan are measured in
# wall-clock time by keeper/blockclock.py, which uses these constants until
# it has samples of its own (Yominet's block time varies around this
# value).
BLOCK_SECONDS = 2.5
BLOCKS_PER_HOUR = int(3600 / BLOCK_SECONDS)

# eth_getLogs calls per tick pass while catching up (one call covers at
# most `logScanBlocks`, default 40; the rest of the gap is
# `ws-logs-gap:<N>` and never gates).
SCAN_MAX_CALLS_PER_PASS = 25

# Block-header reads per scan pass, one per DISTINCT
# kill block; the rest of the pass records rows without a stamp — the
# fallback (the block clock, then the 2.5 s arithmetic) dates them.
TS_READS_PER_PASS = 50

REATTRIBUTE_HEAD_BLOCKS = 400          # how far behind the pass head a stored unattributed row is still retried (~15 min at 2.3 s/block)
REATTRIBUTE_MAX_CALLS_PER_PASS = 4     # transaction reads a pass may spend on re-attribution


def _bytes(v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    s = str(v)
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    return bytes.fromhex(s) if s else b""


def _hex(v: Any) -> str:
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    s = str(v)
    return s if s.startswith("0x") else "0x" + s


def decode_state_write(data: Any) -> str | None:
    """The string a `component.state` ComponentValueSet payload carries
    (ABI `bytes` wrapping an ABI `string`); None for any other layout."""
    try:
        inner = abi_decode(["bytes"], _bytes(data))[0]
        return str(abi_decode(["string"], inner)[0])
    except Exception:  # noqa: BLE001 — a malformed payload is not a state write
        return None


@dataclass(frozen=True)
class DeadWrite:
    block: int
    tx_hash: str
    log_index: int
    entity: int  # the kami entity written DEAD


def dead_writes(logs: list[dict[str, Any]]) -> list[DeadWrite]:
    """Every `component.state = DEAD` write among World logs, in log order."""
    out: list[DeadWrite] = []
    for log in logs:
        topics = [_hex(t) for t in (log.get("topics") or [])]
        if len(topics) < 4 or topics[0] != COMPONENT_VALUE_SET_TOPIC or topics[1] != STATE_COMPONENT_TOPIC:
            continue
        if decode_state_write(log.get("data")) != "DEAD":
            continue
        block = log.get("blockNumber")
        out.append(DeadWrite(int(block) if block is not None else 0, _hex(log.get("transactionHash") or ""), int(log.get("logIndex") or 0), int.from_bytes(_bytes(topics[3]), "big")))
    return out


def decode_liquidate_calldata(calldata: Any) -> tuple[int, int] | None:
    """(victimHarvID, killerID) from a liquidate transaction's input:
    `executeTyped(uint256,uint256)` (0x915657fb) or `execute(bytes)` with
    the same two words inside; None for any other selector or shape."""
    raw = _bytes(calldata)
    if len(raw) < 4:
        return None
    sel = "0x" + raw[:4].hex()
    try:
        if sel == LIQUIDATE_SELECTOR:
            victim, killer = abi_decode(["uint256", "uint256"], raw[4:])
            return int(victim), int(killer)
        if sel == EXECUTE_BYTES_SELECTOR:
            inner = abi_decode(["bytes"], raw[4:])[0]
            victim, killer = abi_decode(["uint256", "uint256"], inner)
            return int(victim), int(killer)
    except Exception:  # noqa: BLE001 — a payload that is not the shape
        return None
    return None


@dataclass(frozen=True)
class CastEvent:
    block: int
    tx_hash: str
    log_index: int
    account_entity: int
    target_kami_entity: int
    item_index: int
    node_index: int


def cast_events(logs: list[dict[str, Any]]) -> list[CastEvent]:
    """Every KAMI_CAST WorldEvent among Emitter logs, in log order; a log
    that does not decode is skipped."""
    out: list[CastEvent] = []
    for log in logs:
        topics = [_hex(t) for t in (log.get("topics") or [])]
        if len(topics) < 2 or topics[0] != WORLD_EVENT_TOPIC or topics[1] != KAMI_CAST_TOPIC:
            continue
        try:
            _schema, value = abi_decode(["uint8[]", "bytes"], _bytes(log.get("data")))
            acc_id, _ts, target_id, item_index, node_index = abi_decode(["uint256", "uint256", "uint256", "uint32", "uint32"], value)
        except Exception:  # noqa: BLE001
            continue
        block = log.get("blockNumber")
        out.append(CastEvent(int(block) if block is not None else 0, _hex(log.get("transactionHash") or ""), int(log.get("logIndex") or 0), int(acc_id), int(target_id), int(item_index), int(node_index)))
    return out


@dataclass
class OccupantRef:
    """What the scanner knows about a kami on a watched node, from the
    previous pass's node occupants (never a chain read)."""

    node: int
    kami_index: int
    account_index: int | None


@dataclass
class ScanResult:
    from_block: int | None
    to_block: int | None
    head: int | None
    last_scanned: int | None
    logCalls: int = 0
    dead_writes: int = 0
    kills: list[dict[str, Any]] = field(default_factory=list)
    casts: list[dict[str, Any]] = field(default_factory=list)
    ignored: int = 0  # DEAD writes on entities that are not occupants of a watched node
    casts_on_others: int = 0  # KAMI_CAST events on foreign occupants of a watched node (not counted)
    # DEAD writes / cast events in range the owner ledger ALREADY holds
    # (the overlap re-read behind the cursor) — skipped before any occupant
    # lookup or transaction read, so a re-read costs one eth_getLogs and
    # nothing else
    overlap_skipped: int = 0
    gap: int = 0  # blocks still unscanned after this pass (ws-logs-gap:<N>)
    rpc_error: str | None = None
    tx_lookups: int = 0
    # kills / casts recorded this pass whose block header could not be
    # read — one per DISTINCT block (the pass cache answers the rest), whether
    # the read cap was reached or the read failed; those rows carry `blockTs` null
    # and the block floor dates them
    ts_unread: int = 0


def resolve_kami_index(chain: Chain, kami_entity: int) -> int | None:
    """Chain fallback for a killer that is not an occupant we knew:
    `component.index.kami.safeGet(killerID)`."""
    try:
        return chain.kami_index_of(kami_entity)
    except RpcUnavailable:
        return None


class KillScanner:
    """One keeper-level scanner: once per tick pass, before any
    auto book assesses, over [lastScannedBlock + 1, head] in calls of at
    most `log_scan_blocks` blocks, at most SCAN_MAX_CALLS_PER_PASS per
    pass. DEAD writes are matched against `occupants` (kami entity id ->
    OccupantRef) — the previous pass's node rows; anything else is ignored.
    A matched death is attributed through `eth_getTransactionByHash`.

    `kami_entity_of` maps a kami index to its entity id (chain.py
    `kami_entity_id`); `lookup_index` resolves a killer entity that is not
    an occupant (the chain fallback), `lookup_account` an index to an
    account index (the lens `kami` read) — both injected so the scanner
    itself makes no read but eth_getLogs / eth_getTransactionByHash."""

    def __init__(self, chain: Chain, log_scan_blocks: int = 40, max_calls: int = SCAN_MAX_CALLS_PER_PASS, lookup_index: Callable[[int], int | None] | None = None, lookup_account: Callable[[int], int | None] | None = None, lookup_account_entity: Callable[[int], int | None] | None = None, emitter: Callable[[], str | None] | None = None, block_clock: "BlockClock | None" = None):
        self.chain = chain
        self.log_scan_blocks = max(1, int(log_scan_blocks))
        self.max_calls = max(1, int(max_calls))
        self.lookup_index = lookup_index if lookup_index is not None else getattr(chain, "kami_index_of", None)
        self.lookup_account = lookup_account
        self.lookup_account_entity = lookup_account_entity
        self.emitter = emitter
        # the first-boot window in wall-clock hours; None = blocks
        # at BLOCK_SECONDS
        self.block_clock = block_clock
        # per-PASS block-header cache (block -> timestamp, None =
        # unread) and the pass's header-read count, both reset by `scan`
        self._block_ts_cache: dict[int, int | None] = {}
        self._ts_reads = 0

    def scan(self, last_scanned: int | None, occupants: dict[int, OccupantRef], window_hours: int, scan_casts: bool = True, our_accounts: set[int] | None = None, overlap: int = 0, known: Callable[[str, int], bool] | None = None) -> ScanResult:
        """`overlap` / `known`: the pass re-reads `overlap`
        blocks BEHIND the cursor — `start = max(0, last_scanned + 1 -
        overlap)` — because an `eth_getLogs` at the head block can be served
        before the node's log index has caught up with its own
        `eth_blockNumber` (a head read can report no DEAD write that the
        chain already holds one block below). First boot is unchanged, and
        the cursor is never moved backwards: the overlap is spent at the
        NEXT pass's start only. A row the caller already holds — `known(tx
        hash, log index)`, the owner ledger's `(txHash, logIndex)` dedup —
        is skipped before any occupant lookup or transaction read and
        counted in `overlap_skipped`; a row `known` does not name is
        scanned normally and absorbed by `record_kill` / `record_cast` as
        before.

        `our_accounts`: a KAMI_CAST counts only when its TARGET is a kami
        of one of these accounts. Without the filter a cast by anyone on any
        occupant of the node — including a cast by the book's own account
        on a foreign occupant — would be recorded as a hostile cast. A
        kill of a foreign occupant on the node still counts (policy: a
        liquidate by the owner on this node). None = every occupant (the
        pre-fix behaviour, kept for callers that pass nothing)."""
        self._block_ts_cache, self._ts_reads = {}, 0  # the cache is per pass
        try:
            head = self.chain.block_number()
        except RpcUnavailable as e:
            return ScanResult(None, None, None, last_scanned, rpc_error=str(e))
        if last_scanned is None:
            # first boot: the window is populated from the block the chain was
            # at `window_hours` of wall-clock time ago (head - window
            # blocks at BLOCK_SECONDS without a clock)
            last_scanned = max(0, (self.block_clock.floor(window_hours, head) if self.block_clock is not None else head - int(window_hours) * BLOCKS_PER_HOUR)) - 1
            start = last_scanned + 1
        else:
            start = max(0, last_scanned + 1 - int(overlap))
        res = ScanResult(start if start <= head else None, None, head, last_scanned)
        if start > head:
            return res
        emitter_addr: str | None = None
        if scan_casts and self.emitter is not None:
            try:
                emitter_addr = self.emitter()
            except RpcUnavailable as e:
                res.rpc_error = str(e)
                return res
        cursor = start
        while cursor <= head and res.logCalls < self.max_calls:
            to = min(head, cursor + self.log_scan_blocks - 1)
            try:
                logs = self.chain.get_logs(cursor, to, topics=[COMPONENT_VALUE_SET_TOPIC, STATE_COMPONENT_TOPIC])
                casts = self.chain.get_logs(cursor, to, address=emitter_addr, topics=[WORLD_EVENT_TOPIC, KAMI_CAST_TOPIC]) if emitter_addr else []
            except RpcUnavailable as e:
                res.rpc_error = str(e)
                break
            res.logCalls += 1
            for dw in dead_writes(logs):
                if known is not None and known(dw.tx_hash, dw.log_index):
                    res.overlap_skipped += 1  # the ledger holds this row already
                    continue
                res.dead_writes += 1
                victim = occupants.get(dw.entity)
                if victim is None:
                    res.ignored += 1
                    continue
                kill = self._attribute(dw, victim, occupants, res)
                # the stamp is not an attribution fact — it is read AFTER
                # attribution, so an unattributed row is dated too (it prunes by it)
                kill["blockTs"] = self._block_ts(dw.block, res)
                res.kills.append(kill)
            for ce in cast_events(casts):
                if known is not None and known(ce.tx_hash, ce.log_index):
                    res.overlap_skipped += 1
                    continue
                target = occupants.get(ce.target_kami_entity)
                if target is None:
                    continue
                if our_accounts is not None and (target.account_index is None or int(target.account_index) not in our_accounts):
                    res.casts_on_others += 1
                    continue
                caster_account = self.lookup_account_entity(ce.account_entity) if self.lookup_account_entity is not None else None
                res.casts.append({"block": ce.block, "txHash": ce.tx_hash, "logIndex": ce.log_index, "node": target.node, "targetKami": target.kami_index, "targetAccount": target.account_index, "casterAccount": caster_account, "item": ce.item_index, "eventNode": ce.node_index, "blockTs": self._block_ts(ce.block, res)})
            # the overlap is spent at the START of the pass only — a pass
            # whose call cap was reached inside the re-read must never carry
            # the cursor BACKWARDS (it would re-read further every pass)
            res.last_scanned = max(to, last_scanned)
            res.to_block = to
            cursor = to + 1
        if res.rpc_error is None and cursor <= head:
            res.gap = head - (res.last_scanned if res.last_scanned is not None else last_scanned)
        return res

    def _block_ts(self, block: int, res: ScanResult) -> int | None:
        """The block's OWN header timestamp (`eth_getBlockByNumber`),
        cached once per pass per block and capped at TS_READS_PER_PASS reads a
        pass. None = the row is recorded UNDATED (the cap was reached, or the
        node did not answer) and the block floor dates it. The `eth_getLogs`
        cap (`max_calls`) is not touched by these reads."""
        blk = int(block)
        if blk in self._block_ts_cache:
            return self._block_ts_cache[blk]
        if self._ts_reads >= TS_READS_PER_PASS:
            self._block_ts_cache[blk] = None
            res.ts_unread += 1
            return None
        self._ts_reads += 1  # the count grows on every ATTEMPT
        try:
            ts = int(self.chain.block_timestamp(blk))
        except RpcUnavailable:
            self._block_ts_cache[blk] = None
            res.ts_unread += 1
            return None
        self._block_ts_cache[blk] = ts
        return ts

    def _attribute(self, dw: DeadWrite, victim: OccupantRef, occupants: dict[int, OccupantRef], res: ScanResult) -> dict[str, Any]:
        kill: dict[str, Any] = {"block": dw.block, "txHash": dw.tx_hash, "logIndex": dw.log_index, "node": victim.node, "victimKami": victim.kami_index, "victimAccount": victim.account_index, "attackerKami": None, "attackerAccount": None, "attribution": "unattributed"}
        try:
            tx = self.chain.get_transaction(dw.tx_hash) if dw.tx_hash else None
        except RpcUnavailable:
            tx = None
            kill["attribution"] = "tx read unavailable"
        res.tx_lookups += 1
        if not tx:
            if kill["attribution"] == "unattributed":
                kill["attribution"] = "no transaction for the log"
            return kill
        kill["attackerKami"], kill["attackerAccount"], kill["attribution"] = self._attacker_from_tx(tx, occupants)
        return kill

    def _attacker_from_tx(self, tx: dict[str, Any], occupants: dict[int, OccupantRef]) -> tuple[int | None, int | None, str]:
        """(attackerKami, attackerAccount, attribution) from the
        liquidate transaction — the body `_attribute` used to hold inline, so
        the re-attribution pass resolves a stored row by exactly the same
        rules."""
        decoded = decode_liquidate_calldata(tx.get("input"))
        if decoded is None:
            return None, None, "selector not a liquidate"
        _victim_harv, killer_id = decoded
        killer = occupants.get(killer_id)
        if killer is not None:
            return killer.kami_index, killer.account_index, "occupant"
        idx = self.lookup_index(killer_id) if self.lookup_index is not None else None
        if idx is None:
            return None, None, "killer not an occupant; index unresolved"
        acct = self.lookup_account(idx) if self.lookup_account is not None else None
        if acct is None:
            return idx, None, "killer resolved by index; account unresolved"
        return idx, acct, "chain fallback"

    def reattribute(self, row: dict[str, Any], occupants: dict[int, OccupantRef], res: "ScanResult | None" = None) -> tuple[int | None, int, str] | None:
        """Re-read the transaction of a stored unattributed kill row and
        resolve its attacker. None when the row is STILL unattributed — the
        transaction unreadable (no answer, or `RpcUnavailable`), the selector
        not a liquidate, or the account unresolved — so the caller leaves the
        row exactly where it is. Otherwise (attackerKami, attackerAccount,
        attribution) with a non-None account."""
        tx_hash = row.get("txHash")
        if not tx_hash:
            return None
        try:
            tx = self.chain.get_transaction(str(tx_hash))
        except RpcUnavailable:
            tx = None
        if res is not None:
            res.tx_lookups += 1
        if not tx:
            return None
        kami, acct, attribution = self._attacker_from_tx(tx, occupants)
        if acct is None:
            return None
        return kami, int(acct), attribution
