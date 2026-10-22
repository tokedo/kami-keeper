"""The keeper: config + profile + book store + grants + lens + chain +
signer, the command handlers behind the Router, and the tick engine.

Reading order for a reviewer: `Keeper.__init__` (what is wired, and how
`armed` selects the signer), `_handlers` (SPEC P1/P8 commands),
`tick_book` (SPEC P4/P5/P7 per tick), `_start_config_books` (fleet books
declared at deploy), `run_daemon` (process start-up).

SPEC P2 under `lab`: with no `maxBooksPerAccount` the process still
serves `describe` / `status` (so the refusal is visible over the socket)
but the ENGINE never starts and `start` / `resume` answer BAD_ARGS
naming the key. `config.errors` is the list of such refusals.

Signer selection (SPEC P8): `armed: false` (the default) is
the DryRunSigner — full perception and decisions, nothing signed, rows
marked dry_run; with a chain configured it still encodes and pre-checks
every intent by eth_call (a read) so the dry-run ledger says whether the
chain would have accepted the call. `armed: true` selects the LiveSigner
and requires rpcUrl, worldAddress and chainId; without them the keeper
refuses to run books (a config refusal, never a silent dry run).
"""

from __future__ import annotations

import collections
import datetime as _dt
from dataclasses import dataclass
import json
import logging
import math
import signal
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import KEEPER_VERSION, SCHEMA_VERSION, profiles
from .blockclock import BlockClock
from .books import Book, BookError, BookStore, IllegalTransition, ProfileForbids, defaults_digest, load_schema, new_book_id, schema_defaults, validate_config
from .chain import Chain, ChainError, RpcUnavailable
from .chainhp import ChainHpReader, reads_note
from .config import KeeperConfig, load as load_config
from .describe import describe_type
from .kills import BLOCK_SECONDS, REATTRIBUTE_HEAD_BLOCKS, REATTRIBUTE_MAX_CALLS_PER_PASS, KillScanner, OccupantRef
from .ledger import Ledger, tx_row_kamis
from .lens import LensClient, LensNotApplied, LensNotReady, LensQueryError, LensUnavailable, PassLens, Perception, perceive
from .owners import OwnerLedger
from .census import CensusCache, entity_id
from .previews import PreviewCache
from .protocol import STRATEGY_TYPES, Answer, Basis, KeeperError, Router, describe_protocol
from .receipts import decode_musu_collected
from .runtime import iso as rt_iso, load_runtime, restore_into, save_runtime
from .scrub import ScrubError, assert_clean
from .secrets import SecretError, SecretStore, store_from_config
from .signer import DryRunSigner, LiveSigner, NotGranted, Signer, SignerRefused, WhitelistRefused
from .status import book_status, lens_incomplete as incomplete_flag, outage_only, rpc_unavailable as rpc_flag, stale_only, ws_logs_gap
from .strategies import REGISTRY as STRATEGIES
from .strategies.auto import PARK_PACK_MAX
from .strategies.base import DEAD, Decision, Observation, Strategy, StrategyContext, absent_contradiction
from .watchlist import WatchCache, Watchlist
from .whitelist import BATCH_CAPS, Intent

log = logging.getLogger("keeper")

# Keeper-level `status` gate when no book is running: the schema default
# of `maxStalenessBlocks`.
STATUS_STALENESS_DEFAULT = 90


@dataclass
class PooledCollect:
    """A `collect` decision held back from its book's own burst so every
    book of one account due in the same tick pass shares ONE
    `executeBatched` collect (capped at BATCH_CAPS collect). Feeds are never pooled: use-item has no batch form."""

    book: Book
    strategy: Strategy
    decision: Decision
    basis: Basis
    degraded: list[str]
    now: float
    rt: dict[str, Any]


@dataclass
class PooledStop:
    """A `rest` / `failsafe-stop` decision held back from its book's
    own burst so every book of one account+node due in the same tick pass
    shares the stop transactions (cap BATCH_CAPS stop = 12). The unit is
    the DECISION, never a kami: a decision is already capped at 12 by
    `auto._batch`, so it is packed whole into one chunk and its plan
    applies exactly as it does in a book's own burst."""

    book: Book
    strategy: Strategy
    decision: Decision
    basis: Basis
    degraded: list[str]
    now: float
    rt: dict[str, Any]


def _stop_kamis(d: Decision) -> list[int]:
    """The kamis one pooled stop decision holds — `kamis[]` on a batched
    `rest` (`auto._batch`), the single `kami` on a `failsafe-stop`."""
    ks = d.parameters.get("kamis")
    if ks:
        return [int(k) for k in ks]
    return [int(d.parameters["kami"])]


STATUS_LENS_TIMEOUT_SEC = 6.0  # during a preview burst the daemon can take more than 2 s to answer `status`; a shorter timeout reads as a false `lens-unavailable`
LOG_RING = 2000
CONFIG_BOOKS_RETRY_SEC = 30.0
# Seconds after a revive ATTEMPT before a still-DEAD kami is
# offered a revive again, for a book stored before the key existed (the
# schema default is the same 200)
REVIVE_RETRY_DEFAULT_SEC = 200

# Tick-summary keys that are MEASUREMENTS (they move every tick by nature —
# bytes, call counts) or the lens's own text; the tick-row change key
# ignores them so a steady state does not write a row per tick.
# `startWaiting` carries a per-kami `readySinceSec` clock that moves every
# tick; the waiting COUNT rides `kamis.waiting`, which is compared.
# `nodeReadBytes`, `attackerVitalsCalls` (measurements) and `censusAgeSec`
# (a clock: 0, 30, 60 … between census reads).
TICK_KEY_IGNORED: frozenset[str] = frozenset({"lensDegraded", "lensCalls", "lensCallsServedFromPass", "lensBytes", "previewCalls", "previewBytes", "previewChangedThreshold", "chainReads", "chainHp", "chainBlock", "outageSec", "startWaiting", "nodeReadBytes", "attackerVitalsCalls", "censusAgeSec"})

# The `killScan` note is compared on THESE keys only — the scan's cursor
# and per-pass counters (`head`, `lastScanned`, `logCalls`, `deadWrites`,
# `ignored`) move every pass and are CARRIED on the row, not compared
# (compared, they would write a row on nearly every pass of every
# node-watching book). It is an ALLOW list, so `overlapSkipped`,
# `remembered`, `own` and `tsUnread` (the block headers this pass could not
# read), which move every pass the same way, are carried and not compared
# by construction. `reattributed` IS compared: a re-attribution is an
# event, so it must write a row.
KILLSCAN_KEY_COMPARED: tuple[str, ...] = ("kills", "casts", "castsOnOthers", "unattributed", "reattributed", "gap", "rpcError")

# Degraded flags that gate no tick are CARRIED on the row, not compared by
# the change key — `kamiden-unavailable` (enrichment only, read by the
# optional battles seed alone) can flap on and off from tick to tick, which
# would write a row per tick on every node-watching book.
DEGRADED_KEY_IGNORED: frozenset[str] = frozenset({"kamiden-unavailable"})

# SPEC P3: `status.lens.passCalls` / `passBytes` are the last
# pass and the p95 over this many passes that ticked at least one book.
PASS_TOTALS_KEPT = 100

# The lens `battles` seed reads at most this many kamis per start / boot
# pass; a larger book is seeded over several passes.
SEED_MAX_READS = 150


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# SPEC P5: a `broadcast` row with no `receipt` row is OPEN after
# a restart; it is probed at boot and on every tick of its book until the
# receipt is found or the operator's confirmed nonce has passed it. A row
# older than this is closed as `not_sent` (`open broadcast row expired`).
# A module constant, not a config key: a default is MAJOR and this is not
# a policy — the receipt of a step the chain took is on chain within
# seconds, never a day later.
OPEN_TX_MAX_AGE_SEC = 86400

# The systems whose landed step moves a PLAN entry — the plan holds
# a harvest state and a node, and only a start / stop writes one. A boot
# close of any other system (a feed, a collect, a revive, a craft) has no
# plan effect to apply, so it is not joined to a decision at all.
PLAN_ADVANCE_SYSTEMS: frozenset[str] = frozenset({"system.harvest.start", "system.harvest.stop"})


def _parse_iso(s: str) -> float | None:
    try:
        t = str(s).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=_dt.timezone.utc)
        return d.timestamp()
    except (ValueError, TypeError):
        return None


def _single(rec: Any) -> None:
    """A batch-fallback single: `batchSize: 1` on the record before either
    of its rows is written (the broadcast row comes first)."""
    rec.batchSize = 1


def build_chain(cfg: KeeperConfig) -> Chain | None:
    """A Chain when the three facts are configured, else None. Constructing
    one makes no RPC call."""
    if cfg.rpcUrl and cfg.worldAddress and cfg.chainId:
        return Chain(cfg.rpcUrl, cfg.worldAddress, cfg.chainId, ws_url=cfg.wsRpcUrl, request_timeout_sec=float(cfg.requestTimeoutSec))
    return None


class Keeper:
    def __init__(
        self,
        cfg: KeeperConfig,
        lens: LensClient | None = None,
        signer: Signer | None = None,
        secrets: SecretStore | None = None,
        chain: Chain | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.cfg = cfg
        self.profile = cfg.profile
        self.clock = clock
        self.data_dir = Path(cfg.dataDir)
        self.store = BookStore(self.data_dir)
        self.secrets = secrets or store_from_config(cfg)
        self.lens = lens or LensClient(cfg.lensSocket, cfg.requestTimeoutSec)
        self.status_lens = LensClient(self.lens.socket_path, STATUS_LENS_TIMEOUT_SEC)
        self.chain = chain if chain is not None else build_chain(cfg)
        self.dry_run = not cfg.armed
        if signer is None:
            if cfg.armed:
                if self.chain is None:
                    cfg.errors.append("signing requested, but rpcUrl / worldAddress / chainId are not all set; nothing runs until they are")
                    self.dry_run = True
                    signer = DryRunSigner(None)
                else:
                    signer = LiveSigner(self.chain)
            else:
                signer = DryRunSigner(self.chain)
        else:
            self.dry_run = signer.kind != "live"
        self.signer = signer
        self.engine_refusals: list[str] = list(cfg.errors)
        self.engine_started = False
        self._engine_stop = threading.Event()
        self._engine_thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.runtime: dict[str, dict[str, Any]] = {}
        self.strategies: dict[str, Strategy] = {}
        self.ledgers: dict[str, Ledger] = {}
        self.logs: collections.deque[dict[str, Any]] = collections.deque(maxlen=LOG_RING)
        self.grant_errors: dict[str, str] = {}
        self.last_perception: Perception | None = None
        self.started_at = _now_iso()
        # stop --immediate: the book being ticked and the set asked to abort
        # between chunks (read by the signer between chunks only)
        self._ticking: str | None = None
        self._abort: set[str] = set()
        # collects pooled per account during a tick pass, flushed
        # as one batched tx per account at the end of the pass (or of a
        # direct tick_book when no pass is open)
        self._pooling = False
        self._collect_pool: dict[str, list[PooledCollect]] = {}
        # stops pooled per account+node during a tick pass, flushed
        # BEFORE the collects at the end of the pass
        self._stop_pool: dict[tuple[str, int], list[PooledStop]] = {}
        self.signer.abort = lambda: self._ticking is not None and self._ticking in self._abort
        # the per-pass lens read cache, the threshold preview cache and the
        # owner ledger fed by the chain kill scan — keeper-level, shared by
        # every book on a node
        self._pass_lens: PassLens | None = None
        self.previews = PreviewCache(self.data_dir, lambda: self.clock())
        # the wall-clock hostility window — (time, head) samples
        # the kill scan feeds once a pass, so `hostileWindowHours` is real
        # hours at any block rate (2.5 s/block is only the fallback)
        self.block_clock = BlockClock(self.data_dir, lambda: self.clock())
        self.owners = OwnerLedger(self.data_dir, block_clock=self.block_clock)
        # the node census cache (per node, shared by the books on
        # it, never persisted) and the lens totals of the last passes
        self.census = CensusCache()
        # the kill scan's occupant MEMORY — kami entity id -> (the ref a
        # census gave it, the scan head of the pass that last listed it). A
        # kami killed between the previous scan head and the census read is
        # already gone from the node when the census is taken, so the census
        # alone cannot resolve its DEAD write; the memory covers that gap. Never
        # persisted: a restart starts empty and the books' OWN kamis are
        # resolved from the books themselves.
        self._occupant_memory: dict[int, tuple[OccupantRef, int]] = {}
        # the world killer watchlist, written by the operator's tooling into
        # <dataDir>/watchlist.json (the keeper only ever reads it), and the
        # thresholds its kamis serve per node —
        # keeper-level like the previews, shared by every book on a node
        self.watchlist = Watchlist(self.data_dir, lambda: self.clock())
        self.watch = WatchCache(self.data_dir, lambda: self.clock())
        # the operator's standing list, keeper-level config (not
        # a book key: a live book is matched to its declaration by type,
        # account, kami set and node, so a book key would need a new book)
        self.context = StrategyContext(previews=self.previews, owners=self.owners, census=self.census, watchlist=self.watchlist, watch=self.watch, standing_owners=frozenset(int(a) for a in self.cfg.standingOwners), clock=lambda: self.clock(), profile=self.profile)
        self.pass_totals: collections.deque[tuple[int, int]] = collections.deque(maxlen=PASS_TOTALS_KEPT)
        self.scan_runs = 0
        # the chain-backed HP fallback — the reader is
        # built on first use from the configured chain (tests inject one),
        # and the keeper-level clock of the current lens OUTAGE (first tick
        # that read lens-unavailable / lens-not-live / lens-degraded:*;
        # None while the lens answers) gates it by chainFallbackAfterSec
        self.chainhp: Any | None = None
        self.lens_outage_since: float | None = None
        # fleet: books declared in config (SPEC P8 "declared at deploy")
        self.config_books_pending: list[dict[str, Any]] = []
        self.config_books_started: list[str] = []
        self.config_books_failed: list[dict[str, Any]] = []
        self._config_books_next = 0.0
        self.router = Router(self.profile, self._handlers())

    # ------------------------------------------------------------------ boot

    def boot(self) -> None:
        """Load books; grant from config where the profile allows; resume
        running books (SPEC P8 restart semantics) if the engine may run;
        start the books declared in config (fleet)."""
        restore = self.owners.load()
        if restore != "missing":
            self._log(None, f"owners.json {restore}" + (f"; kill scan resumes after block {self.owners.last_scanned_block}" if restore.startswith("restored") else "; the ledger starts empty and the kill scan starts at head minus the window"))
        if self.block_clock.load() == "restored":
            bc = self.block_clock.describe()
            self._log(None, f"block clock: {bc['samples']} samples, {bc['blockSeconds']:.2f} s/block over {bc['spanSec'] / 3600.0:.1f} h")
        for b in self.store.load_all():
            self._attach(b, restore=True)
        if self.cfg.grants and profiles.allows("grant-in-config", self.profile):
            for label in self.cfg.grants:
                try:
                    self._grant(label)
                except (SecretError, KeeperError) as e:
                    self.grant_errors[label] = type(e).__name__
                    self._log(None, f"grant {label} from config failed: {type(e).__name__}")
        # SPEC P5: every broadcast row without a receipt row is
        # probed once now, whatever the book's state; what stays open is
        # probed again on each tick of a running book
        for b in self.store.all():
            self._open_tx_boot(b)
        if self.engine_refusals:
            self._log(None, "engine not started: " + "; ".join(self.engine_refusals))
            return
        boot_per: Perception | None = None
        for b in self.store.all():
            if b.state == "running":
                rt = self.runtime.get(b.id, {})
                # the row states what runtime was restored
                restored = {"restore": rt.get("_restore", "missing"), "lastCollectAt": rt_iso(rt.get("lastCollectAt")), "lastFeedAt": rt_iso(rt.get("lastFeedAt")), "lastActAt": rt_iso(rt.get("lastActAt"))}
                extra: dict[str, Any] = {"openTx": len(rt.get("_openTx") or {})}
                if self.strategies[b.id].watches_node():
                    # a persisted node-watching book whose
                    # kamis were never seeded reads their `battles` now (one
                    # status read for the Kamiden gate, shared by the pass)
                    if boot_per is None:
                        boot_per = perceive(self.lens, STATUS_STALENESS_DEFAULT)
                    extra["seedFromBattles"] = self._seed_from_battles(self.strategies[b.id], boot_per)
                self._ledger(b).lifecycle(Basis(), "resumed", reason="keeper-restart", dry_run=self.dry_run, runtime=restored, **extra)
                self._log(b.id, f"resumed after keeper restart (runtime {restored['restore']}; lastCollectAt {restored['lastCollectAt']})" + (f"; battles seed {extra['seedFromBattles'].get('status')}" if "seedFromBattles" in extra else "") + (f"; {extra['openTx']} open broadcast row(s)" if extra.get("openTx") else ""))
        if self.cfg.standingOwners:
            self._log(None, f"standing owners: {sorted(int(a) for a in self.cfg.standingOwners)}")
        if self.cfg.books and profiles.allows("books-in-config", self.profile):
            self.config_books_pending = [dict(b) for b in self.cfg.books]
            self._start_config_books()

    def start_engine(self) -> None:
        if self.engine_refusals or self.engine_started:
            return
        self.engine_started = True
        self._engine_stop.clear()
        self._engine_thread = threading.Thread(target=self._engine_loop, name="keeper-engine", daemon=True)
        self._engine_thread.start()

    def stop_engine(self) -> None:
        self._engine_stop.set()
        if self._engine_thread is not None:
            self._engine_thread.join(timeout=5)
        self.engine_started = False

    def _engine_loop(self) -> None:
        while not self._engine_stop.is_set():
            try:
                if self.config_books_pending and self.clock() >= self._config_books_next:
                    self._start_config_books()
                self.tick_due()
            except Exception as e:  # noqa: BLE001 — the loop must survive
                self._log(None, f"engine tick raised {type(e).__name__}")
            self._engine_stop.wait(1.0)

    # ------------------------------------------------------- config books

    @staticmethod
    def config_book_identity(stype: str, config: dict[str, Any], account: str) -> tuple[Any, ...]:
        """What makes two book declarations the same book: the account, the
        type and the bound subject (kami / kami set + node / recipe set)."""
        if stype == "harvestAndFeed":
            return (stype, account, int(config.get("kamiIndex", -1)))
        if stype == "auto":
            return (stype, account, tuple(sorted(int(k) for k in config.get("kamiIndices", []))), int(config.get("nodeIndex", -1)))
        return (stype, account, tuple(sorted(int(r.get("recipeIndex", -1)) for r in config.get("recipes", []) if isinstance(r, dict))))

    def _start_config_books(self) -> None:
        """Start every pending declaration that has no live twin; keep the
        ones the lens refuses for a retry, drop the ones the schema refuses."""
        self._config_books_next = self.clock() + CONFIG_BOOKS_RETRY_SEC
        live = {self.config_book_identity(b.strategyType, b.config, b.account) for b in self.store.all() if not b.terminal}
        still: list[dict[str, Any]] = []
        for decl in self.config_books_pending:
            account, stype, config = decl.get("account"), decl.get("strategyType"), decl.get("config")
            if not isinstance(account, str) or stype not in STRATEGY_TYPES or not isinstance(config, dict):
                self.config_books_failed.append({"declaration": decl, "error": "BAD_ARGS", "message": "a config book needs account, strategyType and config"})
                continue
            ident = self.config_book_identity(stype, config, account)
            if ident in live:
                continue  # already held (a restart, or a duplicate declaration)
            frame = self.handle("start", {"account": account, "strategyType": stype, "config": config})
            if frame.get("ok"):
                bid = frame["data"]["book"]["id"]
                self.config_books_started.append(bid)
                live.add(ident)
                continue
            err = frame.get("error") or {}
            if err.get("code") in ("BAD_ARGS", "PROFILE_FORBIDS", "BOOK_CAP"):
                self.config_books_failed.append({"declaration": decl, "error": err.get("code"), "message": err.get("message")})
                self._log(None, f"config book refused ({err.get('code')}): {stype} for {account}")
            else:
                still.append(decl)  # NOT_GRANTED / LENS_* : retry
        if still and not self.config_books_pending == still:
            self._log(None, f"{len(still)} config book(s) waiting (lens or grant); retry in {int(CONFIG_BOOKS_RETRY_SEC)} s")
        self.config_books_pending = still

    # ------------------------------------------------------------- plumbing

    def _attach(self, book: Book, restore: bool = False) -> None:
        cls = STRATEGIES[book.strategyType]
        self.strategies[book.id] = cls(book)
        self.strategies[book.id].attach(self.context)
        self.ledgers[book.id] = Ledger(self.store.book_dir(book.id), book.id, book.account)
        rt = self.runtime.setdefault(book.id, {})
        rt.setdefault("ticksSinceAct", 0)
        rt.setdefault("degraded", [])
        rt.setdefault("expectedTicksBetweenActs", self.strategies[book.id].expected_ticks_between_acts())
        if restore and not book.terminal:
            # the persisted runtime (collect / feed clocks, feed guard, craft
            # parks, P7 counters) comes back before the book is resumed; a
            # missing or unreadable file starts fresh clocks, stated in the
            # `resumed` row
            snap, status = load_runtime(self.store.book_dir(book.id), book.id)
            rt["_restore"] = status
            if snap:
                restore_into(rt, snap)
            elif status != "missing":
                self._log(book.id, f"runtime.json {status}; clocks restart at the next tick")

    # ------------------------------------------------ open broadcast rows

    def _open_tx_boot(self, book: Book) -> None:
        """Collect the book's open broadcast rows (a hash with no
        receipt row) into `rt["_openTx"]` and probe each once. Armed with a
        chain only: a dry run broadcasts nothing, so nothing is open."""
        if self.dry_run or self.chain is None:
            return
        rt = self.runtime.setdefault(book.id, {})
        try:
            opened = self._ledger(book).open_tx()
        except (OSError, ValueError) as e:
            self._log(book.id, f"ledger not readable for open broadcast rows: {type(e).__name__}")
            return
        rt["_openTx"] = opened
        if opened:
            self._log(book.id, f"{len(opened)} broadcast row(s) without a receipt row after restart; probing the chain")
            self._probe_open_tx(book, rt, "boot")

    @staticmethod
    def _register_open_tx(rt: dict[str, Any], tx_hash: str, row: dict[str, Any]) -> None:
        """An `unconfirmed` RECEIPT row is still an OPEN step —
        the step was broadcast and the receipt read did not answer, so it is
        probed at the next tick like an open broadcast row and the receipt
        the chain finally answers becomes its final row. The runtime entry
        is the row that was written (the probe repeats its fields and reads
        its `at` for the expiry) plus `_opened`, a runtime-only key that
        names the pass and is NEVER written to the ledger."""
        rt.setdefault("_openTx", {})[tx_hash] = {**row, "_opened": "act"}

    def _probe_open_tx(self, book: Book, rt: dict[str, Any], when: str) -> None:
        """One receipt read per open hash: found -> its receipt row
        (`reconciled: receipt read after restart` for a row opened at boot,
        `receipt read at tick` for one the act path registered); unknown and older than
        OPEN_TX_MAX_AGE_SEC -> closed `not_sent` (expired); unknown with the
        operator's CONFIRMED nonce above the row's -> closed `not_sent`
        (`nonce passed without a receipt after restart`); otherwise it stays
        open for the next pass. A chain read that fails leaves the row for
        the next pass."""
        opened: dict[str, dict[str, Any]] = rt.get("_openTx") or {}
        if not opened or self.chain is None:
            return
        address = self.signer.lane(book.account).address
        latest: int | None = None
        latest_read = False
        for h, row in list(opened.items()):
            t = dict(row.get("tx") or {})
            origin = str(row.get("_opened") or "boot")  # boot (the ledger) or act (an unconfirmed receipt row)
            at = _parse_iso(row.get("at"))  # the row's own wall-clock time (the expiry, and the act clocks)
            try:
                r = self.chain.receipt(h)
            except RpcUnavailable as e:
                self._log(book.id, f"open broadcast row {t.get('nonce')}: receipt read unavailable ({e}); next pass")
                continue
            if r is not None:
                self._close_open_tx(book, rt, h, t, receipt=r, when=when, origin=origin, at=at)
                continue
            if at is not None and time.time() - at > OPEN_TX_MAX_AGE_SEC:
                self._close_open_tx(book, rt, h, t, reason="open broadcast row expired", reconciled=f"no receipt {int(OPEN_TX_MAX_AGE_SEC)} s after the broadcast row", when=when, origin=origin, at=at)
                continue
            if address is None or t.get("nonce") is None:
                continue  # no operator address to count against yet (no grant): next pass
            if not latest_read:
                try:
                    latest = self.chain.latest_nonce(address)
                except RpcUnavailable as e:
                    self._log(book.id, f"open broadcast row {t.get('nonce')}: nonce read unavailable ({e}); next pass")
                    continue
                latest_read = True
            if latest is not None and latest > int(t["nonce"]):
                self._close_open_tx(book, rt, h, t, reason="nonce passed without a receipt after restart", reconciled=f"confirmed nonce count {latest} read at {when} is above nonce {int(t['nonce'])}; the hash is unknown to the chain", when=when, origin=origin, at=at)

    def _close_open_tx(self, book: Book, rt: dict[str, Any], h: str, t: dict[str, Any], receipt: dict[str, Any] | None = None, reason: str | None = None, reconciled: str | None = None, when: str = "tick", origin: str = "boot", at: float | None = None) -> None:
        """The receipt row of an open broadcast row: everything the
        broadcast row stated, then the final status. Basis null (a chain
        read, like a kill row). The note names the pass the row was opened
        on — `receipt read after restart` for a row the ledger read at boot,
        `receipt read at tick` for one registered by the act path (an
        `unconfirmed` receipt row). A row that LANDED also moves the plan of
        the decision it belongs to (a boot-opened row only — the act path
        applied that plan itself), the act block bar (`lastActBlock`) and
        the act clocks, and `planAdvance` on the row states what was done."""
        t = {k: v for k, v in t.items() if k != "stage"}
        note = ("receipt read at tick" if origin == "act" else "receipt read after restart") if receipt is not None else str(reconciled)
        prior = t.get("reconciled")
        t["reconciled"] = f"{prior}; {note}" if prior else note
        if receipt is not None:
            t["status"] = "success" if int(receipt.get("status", 0)) == 1 else "reverted"
            t["block"], t["gasUsed"], t["effectiveGasPrice"] = receipt.get("block"), receipt.get("gasUsed"), receipt.get("effectiveGasPrice")
            if t.get("system") == "system.harvest.collect" and t["status"] == "success" and t.get("kami") is not None:
                t.pop("decodeError", None)  # the unconfirmed row's "no receipt within the receipt timeout" is stale once the receipt is read
                d = decode_musu_collected(list(receipt.get("logs") or []), [int(t["kami"])])[int(t["kami"])]
                t["musuCollected"] = d["musuCollected"]
                if d["decodeError"]:
                    t["decodeError"] = d["decodeError"]
        else:
            t["status"] = "not_sent"
            t["reason"] = str(reason)
            t["block"], t["gasUsed"], t["effectiveGasPrice"] = None, None, None
        landed = receipt is not None and t["status"] == "success"
        # the plan advance the act path would have run. Decided BEFORE
        # the row is written so the row states it; applied after, so a plan
        # that cannot be stated never stops the row being written.
        advance: tuple[Strategy, Any] | None = None
        if landed and origin != "act" and t.get("system") in PLAN_ADVANCE_SYSTEMS:
            advance, plan_note = self._plan_advance_for(book, h, t)
            if plan_note is not None:
                t["planAdvance"] = plan_note
        t["stage"] = "receipt"
        self._ledger(book).tx(Basis(), t, [], dry_run=False)
        rt["_openTx"].pop(h, None)
        # a step of this keeper's own act path that landed late still moves
        # the bar — a read older than its receipt block must not decide on
        # it — and so does a BOOT-closed one (moving it for `origin == "act"`
        # only would leave the bar behind after a restart). The signer's
        # lane is NOT touched: the next send reads the pending nonce.
        if landed and t.get("block"):
            rt["lastActBlock"] = max(int(rt.get("lastActBlock") or 0), int(t["block"]))
        # the act clocks of a step that landed while this process
        # was not there. `lastActAt` is report-only (`status`, the `resumed`
        # row). `lastFeedAt` is read ONLY by a `harvestAndFeed` book in
        # INTERVAL feed mode (`feedMode != "hp"`), where it is the interval
        # the landed feed started; the HP floor and the feed-idempotence
        # guard read the per-kami `feedGuard`, which this never touches, so
        # no feed the floor needs can be withheld by it. Monotone: a clock
        # already past the row's time is kept.
        if landed and at is not None:
            rt["lastActAt"] = max(float(rt.get("lastActAt") or 0.0), at)
            if t.get("system") == "system.kami.use.item":
                rt["lastFeedAt"] = max(float(rt.get("lastFeedAt") or 0.0), at)
        if advance is not None:
            strategy, decision = advance
            strategy.apply_plan(decision)
            self.store.save(book)
        if landed:
            self._save_runtime(book)
        self._log(book.id, f"open broadcast row at nonce {t.get('nonce')} closed at {when}: {t['status']} ({note})" + (f"; plan {t['planAdvance']}" if t.get("planAdvance") else ""))

    def _plan_advance_for(self, book: Book, h: str, t: dict[str, Any]) -> tuple[tuple[Strategy, Any] | None, str | None]:
        """The plan effect of a BOOT-closed row that landed, and
        the note that goes on the row. The act path applies a decision's
        `plan_after` once every record of it landed; a process killed
        between the broadcast row and the receipt rows never got there, so
        the boot close has to do it — otherwise the plan keeps the pre-act
        intent, the next valid read contradicts it and the external-change
        guard pauses the book.

        The row does not carry the decision (a batched row carries `count`,
        not the indices), so the decision comes from the book's own ledger
        (`Ledger.decision_of_tx`) and the STRATEGY states the effect
        (`decision_from_ledger`): the core never writes `book.plan` itself
        and never adopts the world, which would mask a real external
        change made while the daemon was down. Never a guess — when the
        decision cannot be recovered the plan is left alone and the row and
        the log say why."""
        strategy = self.strategies.get(book.id)
        if strategy is None:
            return None, "not applied: the book has no strategy loaded"
        ledger = self._ledger(book)
        try:
            if h in ledger.receipt_row_hashes():
                # the step reached the act path's receipt stage, where
                # that path already applied the plan of a landed decision
                # (`unconfirmed` counts as landed) — applying it again would
                # re-impose a stale intent over everything decided since.
                return None, None
            claims = ledger.decision_of_tx()
        except (OSError, ValueError) as e:
            return None, f"not applied: the ledger could not be read ({type(e).__name__})"
        row = claims.get(h)
        if row is None:
            return None, "not applied: no decision row in the ledger for this step"
        d = row.get("decision") or {}
        kind = str(d.get("kind"))
        try:
            decision = strategy.decision_from_ledger(kind, dict(d.get("parameters") or {}), tx_row_kamis(t))
        except (TypeError, ValueError, KeyError) as e:
            return None, f"not applied: the {kind} decision could not be read ({type(e).__name__})"
        if decision is None:
            return None, f"not applied: the {kind} decision does not state a plan effect"
        if not decision.plan_after:
            return None, None
        return (strategy, decision), f"{kind} applied to {len(decision.plan_after)} kami"

    def _save_runtime(self, book: Book) -> None:
        """Persist the book's runtime subset when it changed (atomic temp +
        rename). A write failure is logged, never raised into a tick."""
        rt = self.runtime.get(book.id)
        if rt is None:
            return
        try:
            save_runtime(self.store.book_dir(book.id), book.id, rt)
        except OSError as e:
            self._log(book.id, f"runtime.json not written: {type(e).__name__}")

    def _ledger(self, book: Book) -> Ledger:
        return self.ledgers[book.id]

    def _tick_row(self, book: Book, basis: Basis, summary: dict[str, Any], degraded: list[str]) -> None:
        """SPEC P5: a `tick` row when the assessment
        summary changes and at least every `tickHeartbeat` ticks."""
        rt = self.runtime.setdefault(book.id, {})
        # the forced-tick mark is consumed HERE, the one
        # seam every summary passes through — a tick forced by a kill whose
        # perception is then gated (or which ends in a pause) writes a
        # `_gated` / pause row, never `_act`'s, and the mark would otherwise
        # sit in the runtime and surface on a LATER, unrelated tick row.
        if rt.pop("killForcedTick", None):
            summary["killForcedTick"] = True
        heartbeat = int(book.config.get("tickHeartbeat", 20))
        # `lensDegraded` is the lens's own text (e.g. `stream-stalled:158s`),
        # which moves every tick of a stall; it is carried, not compared
        keyed = {k: v for k, v in summary.items() if k not in TICK_KEY_IGNORED}
        scan = keyed.get("killScan")
        if isinstance(scan, dict):  # in the KEY only; the row carries the whole note
            keyed["killScan"] = {k: v for k, v in scan.items() if k in KILLSCAN_KEY_COMPARED}
        # the same for a flag that gates nothing; the row carries them all
        flags = [f for f in degraded if f not in DEGRADED_KEY_IGNORED]
        key = json.dumps({"summary": keyed, "degraded": flags}, sort_keys=True, default=str)
        since = int(rt.get("ticksSinceTickRow", 0)) + 1
        if key != rt.get("lastTickKey") or since >= heartbeat:
            self._ledger(book).tick(basis, summary, degraded, self.dry_run)
            rt["lastTickKey"] = key
            rt["ticksSinceTickRow"] = 0
        else:
            rt["ticksSinceTickRow"] = since

    def _log(self, book_id: str | None, line: str) -> None:
        try:
            assert_clean(line, "log line")
        except ScrubError:
            line = "log line withheld: it carried forbidden language"
        entry = {"at": _now_iso(), "book": book_id, "line": line}
        self.logs.append(entry)
        log.info("%s %s", book_id or "-", line)

    def _book(self, book_id: str) -> Book:
        b = self.store.get(book_id)
        if b is None:
            raise KeeperError("NOT_FOUND", f"no book {book_id}")
        return b

    def _require_engine(self) -> None:
        if self.engine_refusals:
            raise KeeperError("BAD_ARGS", "keeper refuses to run books: " + "; ".join(self.engine_refusals))

    def _perceive_for_command(self, max_staleness: int) -> Perception:
        """Lens gate for start/resume (SPEC P1): refusal codes."""
        per = perceive(self.lens, max_staleness)
        self.last_perception = per
        for f in per.degraded:
            if f == "lens-unavailable":
                raise KeeperError("LENS_UNAVAILABLE", "the lens daemon is unreachable")
            if f == "lens-not-live":
                raise KeeperError("LENS_UNAVAILABLE", "the lens daemon is not LIVE")
            if f.startswith("lens-stale:"):
                raise KeeperError("LENS_STALE", f"lens reconciledThrough lags by {f.split(':', 1)[1]} blocks (max {max_staleness})")
            if f.startswith("lens-degraded:"):
                raise KeeperError("LENS_STALE", "the lens reports its mirror incomplete (" + ", ".join(per.lens_degraded_text or [f.split(':', 1)[1]]) + ")")
        return per

    def _observe_for_command(self, strategy: Strategy, query_error_is_refusal: bool, absent_is_contradiction: bool = False) -> Observation | None:
        """A VALID observation for start/resume, or a refusal: an
        unreachable lens is LENS_UNAVAILABLE; an incomplete answer (SPEC
        P4) is LENS_STALE — retryable, never an observation of
        the world. A refused query (NOT_FOUND, BAD_ARGS) is LENS_STALE
        when the command needs the read (resume re-evaluation), else None
        (`start` adopts nothing for a kami the lens holds no row for)."""
        try:
            obs = strategy.observe(self.lens)
        except LensNotReady:
            raise KeeperError("LENS_UNAVAILABLE", "the lens daemon is not LIVE") from None
        except LensUnavailable:
            raise KeeperError("LENS_UNAVAILABLE", "the lens daemon is unreachable") from None
        except LensQueryError as e:
            if query_error_is_refusal:
                raise KeeperError("LENS_STALE", f"lens read refused: {e.code}") from None
            return None
        missing = obs.incomplete()
        if missing:
            absent = obs.absent_kamis()
            if absent_is_contradiction and absent and not obs.field_incomplete():
                # on `resume` a kami with no row is the
                # contradiction itself, not a lens fault
                raise KeeperError("PLAN_CONTRADICTED", "the world still contradicts the plan (" + "; ".join(absent_contradiction(k) for k in absent) + "); resume refused")
            raise KeeperError("LENS_STALE", "lens answer incomplete: " + "; ".join(missing))
        return obs

    def _config_value(self, book: Book, key: str) -> Any:
        """A book's config value, or the schema default when a book started
        before the key existed (persisted books keep their start-time
        config)."""
        if key in book.config:
            return book.config[key]
        return schema_defaults(book.strategyType).get(key)

    def _rechecks_while_paused(self, book: Book) -> bool:
        """SPEC P7: under fleet a book paused by external-change
        re-checks the world every tick and re-adopts once it has matched
        the plan for readoptAfterBlocks; under lab it waits for `resume`."""
        return book.state == "paused" and book.pausedReason == "external-change" and profiles.allows("external-change-readopt", self.profile)

    # ------------------------------------------------------------- handlers

    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], Answer]]:
        return {
            "describe": self.h_describe,
            "books": self.h_books,
            "start": self.h_start,
            "stop": self.h_stop,
            "park": self.h_park,
            "pause": self.h_pause,
            "resume": self.h_resume,
            "grant": self.h_grant,
            "revoke": self.h_revoke,
            "status": self.h_status,
            "logs": self.h_logs,
            "ledger": self.h_ledger,
        }

    def handle(self, cmd: str, args: dict[str, Any] | None = None, rid: Any = 1) -> dict[str, Any]:
        """In-process entry (tests, CLI-less callers): the same router."""
        return self.router.handle({"id": rid, "cmd": cmd, "args": args or {}})

    def h_describe(self, args: dict[str, Any]) -> Answer:
        t = args.get("type")
        if t is None:
            d = describe_protocol(self.profile)
            d["defaultsSha256"] = defaults_digest()
            d["engine"] = {"started": self.engine_started, "refusals": list(self.engine_refusals)}
            d["dryRun"] = self.dry_run
            d["signer"] = self.signer.describe()
            return Answer(d)
        return Answer(describe_type(t, self.profile))

    def h_books(self, args: dict[str, Any]) -> Answer:
        if args.get("all"):
            if not profiles.allows("books-list-all", self.profile):
                raise KeeperError("PROFILE_FORBIDS", "this profile lists only the caller's own account (pass account)")
            books = self.store.all()
        else:
            account = args.get("account")
            if not account:
                raise KeeperError("BAD_ARGS", "books: pass account (the caller's account label)")
            books = self.store.for_account(account)
        return Answer({"books": [self._book_status(b) for b in books]})

    def h_start(self, args: dict[str, Any]) -> Answer:
        self._require_engine()
        account = args["account"]
        stype = args["strategyType"]
        if not self.signer.has_grant(account):
            raise KeeperError("NOT_GRANTED", f"account {account} has no active grant; grant it first")
        cls = STRATEGIES[stype]
        if not cls.implemented:
            raise KeeperError("BAD_ARGS", f"strategyType {stype}: policy is not served at keeper {KEEPER_VERSION} (describe {stype} serves the schema)")
        try:
            config = validate_config(stype, args["config"], self.profile)
        except ProfileForbids as e:
            raise KeeperError("PROFILE_FORBIDS", str(e)) from None
        except BookError as e:
            raise KeeperError("BAD_ARGS", str(e)) from None
        cap = self.cfg.maxBooksPerAccount
        if cap is not None and cap > 0 and self.store.count_active(account) >= cap:
            raise KeeperError("BOOK_CAP", f"account {account} holds {self.store.count_active(account)} active books; cap {cap}")
        per = self._perceive_for_command(int(config["maxStalenessBlocks"]))
        book = Book(id=new_book_id(stype), account=account, strategyType=stype, config=config)
        # SPEC P2: `start` adopts what is already there (HARVESTING kamis
        # are owned as-is; RESTING ones are deployed by the first tick).
        # The adoption read must be VALID — an incomplete answer
        # refuses the start (LENS_STALE, retried by config books) rather
        # than adopting an unknown; nothing is saved before that.
        # the adoption read is context-free: it adopts states and refuses a
        # contradicted plan; previews and the occupant table are the first
        # tick's work
        probe = cls(book)
        obs = self._observe_for_command(probe, query_error_is_refusal=False)
        if obs is not None:
            # an `auto` kami HARVESTING on another node
            # refuses the whole start — nothing is saved
            why = probe.refuse_start(obs)
            if why:
                raise KeeperError("BAD_ARGS", f"start refused: {why}")
        with self._lock:
            self.store.save(book)
            self._attach(book)
            strategy = self.strategies[book.id]
            if obs is not None:
                strategy.adopt(obs)
                basis = obs.basis
            else:
                basis = per.basis
            event = book.transition("running")
            self.store.save(book)
            extra: dict[str, Any] = {}
            if strategy.watches_node():
                # seed killedAccounts from the lens
                # `battles` of this book's kamis (enrichment only: a refusal
                # is a note on this row, never a gate)
                extra["seedFromBattles"] = self._seed_from_battles(strategy, per)
            self._ledger(book).lifecycle(basis, event, degraded=per.degraded, dry_run=self.dry_run, plan=dict(book.plan), **extra)
            rt = self.runtime[book.id]
            rt["nextTickAt"] = self.clock()
            self._log(book.id, f"started {stype} for {account}" + (f"; battles seed {extra['seedFromBattles'].get('status')}" if extra else ""))
        return Answer({"book": self._book_status(book)}, basis=basis)

    def _stop_book(self, book: Book, stopped_by: str, reason: str, after_burst: bool = False, extra: dict[str, Any] | None = None) -> bool:
        """Idempotent: returns True if this call stopped it. Immediate (the
        default) asks a burst in flight to stop offering further chunks;
        afterBurst lets it finish. Either way the receipts of what was
        offered are collected before the stop lands (the tick holds the
        lock), so the quiescence proof is honest. `extra` rides the
        `stopped_by` row (`park`: the `parked` completion facts)."""
        if not after_burst:
            self._abort.add(book.id)
        try:
            with self._lock:
                if book.terminal:
                    return False
                event = book.transition("stopped", {"stopped_by": stopped_by, "reason": reason})
                self.store.save(book)
                self._ledger(book).lifecycle(Basis(), event, reason=f"{stopped_by}: {reason}", dry_run=self.dry_run, afterBurst=after_burst, **(extra or {}))
                self._log(book.id, f"stopped by {stopped_by}: {reason}" + (" (after the current burst)" if after_burst else ""))
                return True
        finally:
            self._abort.discard(book.id)

    def h_stop(self, args: dict[str, Any]) -> Answer:
        after = bool(args.get("afterBurst"))
        if args.get("all"):
            account = args.get("account")
            if not account:
                raise KeeperError("BAD_ARGS", "stop: `all` needs account")
            stopped = [b.id for b in self.store.for_account(account) if self._stop_book(b, "agent", "stop --all", after)]
            return Answer({"stopped": stopped, "quiescence": self.signer.quiescence(account).as_dict()})
        book_id = args.get("book")
        if not book_id:
            raise KeeperError("BAD_ARGS", "stop: pass book, or all with account")
        book = self._book(book_id)
        did = self._stop_book(book, "agent", "stop", after)
        return Answer({"stopped": [book.id] if did else [], "alreadyStopped": not did, "book": self._book_status(book), "quiescence": self.signer.quiescence(book.account).as_dict()})

    def h_park(self, args: dict[str, Any]) -> Answer:
        """SPEC P8: park a RUNNING `auto` book. From the next tick
        the book offers NO start, feed or collect; on every tick with a
        VALID read it offers `harvest stop` for its kamis HARVESTING on its
        node in packs of at most PARK_PACK_MAX (at most PARK_PACKS_PER_TICK
        packs a tick, one burst on the account's one nonce lane), keeps
        reviving, and stops ITSELF once a valid read past its last landed
        act shows none of them HARVESTING on the node, none DEAD and
        nothing pending. Idempotent: a book already parking changes
        nothing. Nothing new is signed — `system.harvest.stop` and
        `system.kami.use.item` are the whitelist rows the book already uses."""
        self._require_engine()
        book = self._book(args["book"])
        if book.strategyType != "auto":
            raise KeeperError("BAD_ARGS", "park: only an auto book parks (a harvestAndFeed book rests on its failsafe, a craft book harvests nothing)")
        if book.terminal:
            raise KeeperError("BAD_ARGS", f"park: book {book.id} is {book.state}")
        if book.state != "running":
            raise KeeperError("BAD_ARGS", f"park: book {book.id} is {book.state}; only a running book parks — resume it first")
        if book.parking:
            return Answer({"book": self._book_status(book), "alreadyParking": True})
        strategy = self.strategies[book.id]
        with self._lock:
            book.parking = {"since": _now_iso(), "by": "agent"}
            self.store.save(book)
            node = int(book.config["nodeIndex"])
            n = len(strategy.owned_kamis())
            self._ledger(book).lifecycle(Basis(), "parking", reason="agent: park", degraded=None, dry_run=self.dry_run, kamis=n, node=node)
            rt = self.runtime.setdefault(book.id, {})
            rt["nextTickAt"] = self.clock()  # the next engine pass ticks it
            self._log(book.id, f"parking: {n} kamis; no start, feed or collect from now; harvest stop in packs of <= {PARK_PACK_MAX} until none harvests on node {node}")
        return Answer({"book": self._book_status(book), "parking": book.parking})

    def h_pause(self, args: dict[str, Any]) -> Answer:
        book = self._book(args["book"])
        with self._lock:
            try:
                event = book.transition("paused", paused_reason="agent")
            except IllegalTransition as e:
                raise KeeperError("BAD_ARGS", str(e)) from None
            self.store.save(book)
            self._ledger(book).lifecycle(Basis(), event, reason="agent", dry_run=self.dry_run)
            self._log(book.id, "paused by agent")
        return Answer({"book": self._book_status(book)})

    def h_resume(self, args: dict[str, Any]) -> Answer:
        self._require_engine()
        book = self._book(args["book"])
        if book.state != "paused":
            raise KeeperError("BAD_ARGS", f"book {book.id} is {book.state}; only a paused book resumes")
        if not self.signer.has_grant(book.account):
            raise KeeperError("NOT_GRANTED", f"account {book.account} has no active grant")
        per = self._perceive_for_command(int(book.config["maxStalenessBlocks"]))
        with self._lock:
            strategy = self.strategies[book.id]
            basis = per.basis
            reason = "agent"
            plan: dict[str, Any] | None = None
            if book.pausedReason == "external-change":
                # re-evaluate the world on a VALID read
                # first. While the contradiction stands the resume is
                # refused; otherwise the observed state becomes the plan
                # (the agent's explicit re-adopt) and the book runs.
                obs = self._observe_for_command(strategy, query_error_is_refusal=True, absent_is_contradiction=True)
                assert obs is not None
                if not strategy.world_matches_plan(obs):
                    still = strategy.external_change(obs) or "a book kami is not in a state the book can own"
                    raise KeeperError("PLAN_CONTRADICTED", f"book {book.id}: the world still contradicts the plan ({still}); resume refused")
                if strategy.external_change(obs):
                    reason = "agent; plan re-adopted from the world"
                strategy.adopt(obs)
                basis = obs.basis
                plan = dict(book.plan)
            event = book.transition("running")
            book.contradictedAt = None
            self.store.save(book)
            rt = self.runtime[book.id]
            rt.pop("contradictionTicks", None)
            rt.pop("contradiction", None)
            rt.pop("matchingSinceBlock", None)
            extra = {"plan": plan} if plan is not None else {}
            self._ledger(book).lifecycle(basis, event, reason=reason, degraded=per.degraded, dry_run=self.dry_run, **extra)
            rt["nextTickAt"] = self.clock()
            self._save_runtime(book)
            self._log(book.id, "resumed by agent" + ("; plan re-adopted from the world" if plan is not None else ""))
        return Answer({"book": self._book_status(book)}, basis=basis)

    def _grant(self, label: str) -> dict[str, Any]:
        try:
            key = self.secrets.get_operator_key(label)  # load-on-grant, re-read every time
        except SecretError as e:
            raise KeeperError("NOT_FOUND", f"grant {label}: {e}") from None
        g = self.signer.grant(label, key)
        self.grant_errors.pop(label, None)
        self._log(None, f"granted {label} (operator {key.address})")
        return g.public()

    def h_grant(self, args: dict[str, Any]) -> Answer:
        return Answer({"grant": self._grant(args["account"])})

    def h_revoke(self, args: dict[str, Any]) -> Answer:
        account = args["account"]
        stopped = [b.id for b in self.store.for_account(account) if self._stop_book(b, "agent", "grant-revoked")]
        had = self.signer.revoke(account) is not None
        self._log(None, f"revoked {account} ({len(stopped)} book(s) stopped)")
        return Answer({"revoked": had, "stopped": stopped, "quiescence": self.signer.quiescence(account).as_dict()})

    def _status_staleness(self) -> int:
        """The keeper-level gate is the LARGEST
        `maxStalenessBlocks` among the running books — the loosest bound
        any book acts under — or the schema default when none runs."""
        values = [int(self._config_value(b, "maxStalenessBlocks") or STATUS_STALENESS_DEFAULT) for b in self.store.all() if b.state == "running"]
        return max(values) if values else STATUS_STALENESS_DEFAULT

    def h_status(self, args: dict[str, Any]) -> Answer:
        max_st = self._status_staleness()
        per = perceive(self.status_lens, max_st)
        # what the watchlist file said at the last tick pass (it is
        # read there, never here — and never written anywhere)
        lens_block = {"state": (per.lens_status or {}).get("state"), "liveBlockNumber": (per.lens_status or {}).get("liveBlockNumber"), "reconciledThrough": per.basis.lensReconciledThrough, "appliedThrough": per.applied_through, "version": per.lens_version, "stalenessBlocks": per.staleness_blocks, "maxStalenessBlocks": max_st, "outageSince": _iso(self.lens_outage_since), **self._pass_totals(), "watchlist": {"updatedAt": _iso(self.watchlist.updated_at), "killers": len(self.watchlist.killers)}}
        books = self.store.all()
        if args.get("book"):
            books = [self._book(args["book"])]
        data = {
            "keeperVersion": KEEPER_VERSION,
            "schemaVersion": SCHEMA_VERSION,
            "profile": self.profile,
            "startedAt": self.started_at,
            "dryRun": self.dry_run,
            "signer": self.signer.describe(),
            "chain": self.chain.describe() if self.chain is not None else None,
            "engine": {"started": self.engine_started, "refusals": list(self.engine_refusals)},
            "degraded": list(per.degraded),
            "lens": lens_block,
            "blockClock": self.block_clock.describe(),
            "grants": self.signer.grants(),
            "grantErrors": dict(self.grant_errors),
            "secrets": self.secrets.describe(),
            "maxBooksPerAccount": self.cfg.maxBooksPerAccount,
            "configBooks": {"declared": len(self.cfg.books), "pending": len(self.config_books_pending), "started": list(self.config_books_started), "failed": list(self.config_books_failed)},
            "config": {"dataDir": str(self.data_dir), "socketPath": self.cfg.socketPath, "lensSocket": self.cfg.lensSocket, "sources": dict(self.cfg.sources)},
            "books": [self._book_status(b) for b in books],
        }
        return Answer(data, basis=per.basis)

    def _pass_totals(self) -> dict[str, Any]:
        """SPEC P3: what one tick pass asked of the lens — daemon
        round-trips and bytes on the wire over every book of the pass, the
        status reads included — for the last pass and the p95 (nearest
        rank) over the last PASS_TOTALS_KEPT passes that ticked a book;
        null before the first one."""
        rows = list(self.pass_totals)
        if not rows:
            return {"passCalls": None, "passBytes": None}

        def p95(values: list[int]) -> int:
            ordered = sorted(values)
            return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]

        return {"passCalls": {"last": rows[-1][0], "p95": p95([r[0] for r in rows]), "passes": len(rows)}, "passBytes": {"last": rows[-1][1], "p95": p95([r[1] for r in rows]), "passes": len(rows)}}

    def h_logs(self, args: dict[str, Any]) -> Answer:
        tail = int(args.get("tail") or 200)
        book = args.get("book")
        rows = [e for e in self.logs if book is None or e["book"] == book]
        return Answer({"lines": rows[-tail:]})

    def h_ledger(self, args: dict[str, Any]) -> Answer:
        book = self._book(args["book"])
        rows = self._ledger(book).since(args.get("since"), args.get("kind"))
        return Answer({"book": book.id, "rows": rows})

    def _book_status(self, book: Book) -> dict[str, Any]:
        rt = self.runtime.get(book.id, {})
        strategy = self.strategies.get(book.id)
        extra = strategy.status_extra(rt) if strategy is not None else None
        if extra and extra.get("nextPreviewDueAt") is not None:
            extra["nextPreviewDueAt"] = _iso(float(extra["nextPreviewDueAt"]))
        out = book_status(book, {
            "lastTickAt": _iso(rt.get("lastTickAt")), "lastActAt": _iso(rt.get("lastActAt")), "lastActBlock": rt.get("lastActBlock"), "lastTxHash": rt.get("lastTxHash"),
            "nextTickAt": _iso(rt.get("nextTickAt")), "lastError": rt.get("lastError"), "ticksSinceAct": rt.get("ticksSinceAct", 0),
            "expectedTicksBetweenActs": rt.get("expectedTicksBetweenActs"), "degraded": rt.get("degraded", []),
            "openTx": len(rt.get("_openTx") or {}),
        }, self.dry_run, strategy=extra)
        return out

    # --------------------------------------------------------------- engine

    def tick_due(self, now: float | None = None) -> list[str]:
        """Tick every running book whose nextTickAt has passed — and, under
        fleet, every book paused by external-change (it re-checks the world
        each tick; SPEC P7)."""
        now = self.clock() if now is None else now
        ticked = []
        self._pooling = True
        # one read cache for the pass, and the kill scan BEFORE any auto
        # book assesses
        self._pass_lens = PassLens(self.lens)
        self.previews.begin_pass()
        self.census.begin_pass()
        self.watchlist.load()  # the watchlist file may have been rewritten since the last pass
        self.watch.begin_pass()
        before = self.lens.counters()
        try:
            with self._lock:
                self._scan_kills(now)
            for b in self.store.all():
                if b.state != "running" and not self._rechecks_while_paused(b):
                    continue
                rt = self.runtime.setdefault(b.id, {})
                if rt.get("nextTickAt") is None or rt["nextTickAt"] <= now:
                    self.tick_book(b, now)
                    ticked.append(b.id)
        finally:
            self._pooling = False
            self._pass_lens = None
            if ticked:
                after = self.lens.counters()
                self.pass_totals.append((int(after["calls"] - before["calls"]), int(after["bytes"] - before["bytes"])))
            with self._lock:
                self._flush_stops()  # pooled stops are sent before pooled collects
                self._flush_collects()
        return ticked

    def tick_book(self, book: Book, now: float | None = None) -> dict[str, Any]:
        """One assessment of one book. Returns the tick summary written to
        the ledger. Fail-closed at every step (SPEC P4)."""
        now = self.clock() if now is None else now
        with self._lock:
            self._ticking = book.id
            if not self._pooling:
                self.previews.begin_pass()  # a direct tick is its own pass for the preview cap
                self.census.begin_pass()  # … and for the census and the attacker reads
                self.watchlist.load()  # … and for the watchlist and its reads
                self.watch.begin_pass()
            try:
                summary = self._tick_book_locked(book, now)
            finally:
                self._ticking = None
                self._save_runtime(book)
            if not self._pooling:
                # a direct tick (no pass open): this book's pooled stop and
                # collect go out now, alone or with nothing
                self._flush_stops()
                self._flush_collects()
        return summary

    def _gated(self, book: Book, basis: Basis, degraded: list[str], **detail: Any) -> dict[str, Any]:
        self.runtime.setdefault(book.id, {})["degraded"] = degraded
        summary = {"acted": False, "reason": "perception gated", "degraded": degraded, **detail}
        self._tick_row(book, basis, summary, degraded)
        return summary

    def _tick_book_locked(self, book: Book, now: float) -> dict[str, Any]:
        rechecking = self._rechecks_while_paused(book)
        if book.state != "running" and not rechecking:
            return {"acted": False, "skipped": book.state}
        rt = self.runtime.setdefault(book.id, {})
        strategy = self.strategies[book.id]
        ledger = self._ledger(book)
        tick_s = int(book.config["tickSeconds"])
        rt["lastTickAt"] = now
        rt["nextTickAt"] = now + tick_s
        rt["ticksSinceAct"] = int(rt.get("ticksSinceAct", 0)) + 1
        rt["lastError"] = None
        if rt.get("_openTx"):
            self._probe_open_tx(book, rt, "tick")  # open broadcast rows, one read each

        # -- 1. the lens status gate (SPEC P4) ---------------------------------
        per = perceive(self.lens, int(book.config["maxStalenessBlocks"]))
        self.last_perception = per
        degraded = list(per.degraded)
        rt["degraded"] = degraded
        # a PARKING book acts on a VALID read only — no stale-feed
        # branch and no chain fallback: a stop it does not need costs a
        # harvest its intensity clock, and the whole point of the park is
        # that the completion rests on a read the lens stands behind. The
        # keeper-level `lens_outage_since` clock is the non-parking books'
        # and is left exactly as it was.
        if book.parking and not per.can_act:
            return self._gated(book, per.basis, degraded, parking=True)
        # Under a stale lens: `lens-stale:<n>` ALONE lets a running
        # book take the feed branch below; every other gating flag stops the
        # tick here. A re-checking paused book needs a fully valid read.
        under_stale = not per.can_act and stale_only(degraded) and not rechecking
        # The keeper-level lens outage clock (it gates the chain fallback) — set on the first tick that reads a lens OUTAGE (unreachable,
        # not LIVE, mirror known incomplete), cleared by any tick the lens
        # answers (valid or merely stale)
        outage = not per.can_act and outage_only(degraded)
        if outage:
            if self.lens_outage_since is None:
                self.lens_outage_since = now
        elif per.can_act or stale_only(degraded):
            self.lens_outage_since = None
        if not per.can_act and not under_stale:
            detail = {"lensDegraded": list(per.lens_degraded_text)} if per.lens_degraded_text else {}
            if outage and not rechecking and (strategy.chain_fallback_feeds() or strategy.chain_fallback_rests()):
                if self.chain is None:
                    detail["chainFallback"] = "no chain configured"
                else:
                    after = int(self._config_value(book, "chainFallbackAfterSec") or 0)
                    elapsed = now - float(self.lens_outage_since or now)
                    if elapsed < after:
                        detail["chainFallback"] = "waiting: the lens outage is shorter than chainFallbackAfterSec"
                        detail["chainFallbackAfterSec"] = after
                        detail["outageSec"] = int(elapsed)
                    else:
                        return self._tick_under_chain(book, strategy, degraded, now, rt, detail)
            return self._gated(book, per.basis, degraded, **detail)

        # the kill scan's flags ride every node-watching
        # tick of the pass and every row it writes, and GATE NOTHING — the
        # owner ledger is a lag of the class, not an unknown reading of HP
        if strategy.watches_node():
            for f in self.context.scan.get("flags") or []:
                if f not in degraded:
                    degraded.append(f)
            rt["degraded"] = degraded

        # -- 2. the observation, and its validity (SPEC P4) --------------------
        # on lens 1.0.x the observation is taken through a view that carries
        # `--at-least <the book's last act block>`, so the daemon holds each
        # world read until it has APPLIED that block (the lens's act->verify
        # primitive) instead of answering from a mirror that cannot contain
        # the book's own tx. On a 0.6.x lens `freshness_view` returns the
        # client itself and the reads are plain reads.
        bar = rt.get("lastActBlock")
        base_lens = self._pass_lens or self.lens
        lens_for = base_lens.freshness_view(int(bar)) if bar else base_lens
        try:
            obs = strategy.observe(lens_for)
        except LensNotReady:
            degraded.append("lens-not-live")
            return self._gated(book, per.basis, degraded)
        except LensUnavailable:
            degraded.append("lens-unavailable")
            return self._gated(book, per.basis, degraded)
        except LensNotApplied as e:
            # the 3b outcome, not a refusal: the mirror has not reached the
            # book's own receipt block yet, so there is nothing to decide on
            # and nothing is wrong. No `lastError`, no lifecycle row.
            summary = {"acted": False, "reason": "awaiting a read past the last act", "lastActBlock": int(bar), "lensAppliedThrough": e.applied_through, "lensBlock": per.basis.lensBlock, "lensReconciledThrough": per.basis.lensReconciledThrough}
            self._tick_row(book, per.basis, summary, degraded)
            rt["lastSummary"] = summary
            return summary
        except LensQueryError as e:
            rt["lastError"] = f"lens {e.code}"
            summary = {"acted": False, "reason": f"lens read refused: {e.code}"}
            self._tick_row(book, per.basis, summary, degraded)
            return summary
        basis = obs.basis
        missing = obs.incomplete()
        if missing:
            absent = obs.absent_kamis()
            if absent and not obs.field_incomplete():
                # every served row is complete and the lens is
                # not degraded — only a kami has NO ROW: a gated tick that
                # counts toward an external change of that kami
                return self._absent_kamis_tick(book, strategy, obs, basis, degraded, now, rt, absent, rechecking)
            # A missing field is an unknown, never an observation of the
            # world: no decision, no death row, no P7 comparison.
            degraded.append(incomplete_flag())
            return self._gated(book, basis, degraded, incomplete=missing)

        # -- 3. under a stale lens: the feed branch only ------------------------
        if under_stale:
            decisions = strategy.assess_under_stale(obs, now, rt)
            return self._act(book, strategy, obs, basis, degraded, decisions, now, rt, under_stale=True)

        # -- 3b. a read older than the book's own last landed tx decides
        # nothing. Still the check for a read that came back ANYWAY: the
        # whole 0.6.x path, and on 1.0.x the reads that the 5 s `--max-wait`
        # default let through short (a `FreshLens` read is refused
        # NOT_APPLIED above when the mark never arrives).
        if bar and not self._read_past_last_act(basis, int(bar)):
            summary = {"acted": False, "reason": "awaiting a read past the last act", "lastActBlock": int(bar), "lensBlock": basis.lensBlock, "lensReconciledThrough": basis.lensReconciledThrough}
            self._tick_row(book, basis, summary, degraded)
            rt["lastSummary"] = summary
            return summary

        # -- 4. a paused external-change book re-checks the world (fleet) ------
        if rechecking:
            if not self._readopt_check(book, strategy, obs, basis, degraded, rt):
                return rt["lastSummary"]
            # readopted: the book is running again and assesses this tick

        # -- 5. deaths (SPEC P5): a row of its own, BEFORE any revive ---------
        revives: list[Decision] = []
        retry_sec = int(self._config_value(book, "reviveRetrySec") or REVIVE_RETRY_DEFAULT_SEC)
        for k in strategy.newly_dead(obs, now):
            # a RE-offer, read BEFORE the plan is re-marked
            retry = strategy.is_revive_retry(k)
            items = strategy.revive_items()
            have = None
            if items and obs.inventory is not None:
                have = next((i for i in items if obs.inventory.get(i, 0) > 0), None)
            if not items:
                outcome = "revive-skipped: disabled"
                strategy.mark_dead(k, None)
                what = outcome
            elif have is None:
                outcome = "revive-skipped: no-item"
                # ribbons shipped later are used at the next due tick
                strategy.mark_dead(k, now + retry_sec)
                what = outcome
            else:
                outcome = "revive-retry" if retry else "observed"
                # the timer is set at the OFFER, so every attempt — landed,
                # refused, reverted, unconfirmed, lost — is followed by one
                # re-check; a kami alive by then no longer reads DEAD
                strategy.mark_dead(k, now + retry_sec)
                params: dict[str, Any] = {"kami": k, "item": have, "inventoryBalance": obs.inventory.get(have, 0) if obs.inventory else None}
                if retry:
                    params["retry"] = True
                revives.append(Decision(
                    "revive",
                    params,
                    [Intent("system.kami.use.item", book.account, k, {"itemIndex": have, "source": "inventory"}, book.id)],
                ))
                what = ("revive offered again from inventory item " if retry else "revive from inventory item ") + str(have)
            ledger.death(basis, k, None, outcome, degraded=degraded, dry_run=self.dry_run)
            self._log(book.id, f"kami {k} {'still' if retry else 'observed'} DEAD; {what}")
            self.store.save(book)
        if not revives and self._all_dead_no_retry(book, strategy):
            event = book.transition("paused", paused_reason="kami-dead")
            self.store.save(book)
            ledger.lifecycle(basis, event, reason="every book kami is DEAD", degraded=degraded, dry_run=self.dry_run)
            summary = {"acted": False, "reason": "paused: every book kami is DEAD"}
            self._tick_row(book, basis, summary, degraded)
            return summary

        # -- 6. SPEC P7: interference, with persistence ------------------------
        contradiction = strategy.external_change(obs)
        if contradiction:
            streak = int(rt.get("contradictionTicks", 0)) + 1
            rt["contradictionTicks"] = streak
            rt["contradiction"] = contradiction
            need = int(self._config_value(book, "externalChangeTicks") or 3)
            if streak < need:
                # unconfirmed: a tick note, no lifecycle row, and — the
                # unconditional rule — nothing is asserted against it
                summary = {"acted": False, "reason": "external change observed; awaiting confirmation", "contradiction": contradiction, "observedTicks": streak, "confirmAfterTicks": need}
                self._tick_row(book, basis, summary, degraded)
                return summary
            return self._pause_external_change(book, strategy, obs, basis, degraded, rt, contradiction, streak)
        note = None
        if rt.get("contradictionTicks"):
            note = f"contradiction cleared after {rt['contradictionTicks']} tick(s): {rt.get('contradiction')}"
            rt["contradictionTicks"] = 0
            rt.pop("contradiction", None)

        # -- 7. assess and act ---------------------------------------------------
        # a parking book takes the park assessment instead — no start, feed
        # or collect, `harvest stop` in packs, revives as for a running book
        # (they ride `revives` above, unchanged).
        assessed = strategy.assess_parking(obs, now, rt) if book.parking else strategy.assess(obs, now, rt)
        decisions = revives + assessed
        summary = self._act(book, strategy, obs, basis, degraded, decisions, now, rt, note=note)
        # … and the completion: the assessment marks it when THIS read (past
        # the book's last landed act, as every read is) showed none of its
        # kamis HARVESTING on the node, none DEAD and nothing pending.
        # `_stop_book` takes the RLock re-entrantly; the `_abort` mark it
        # sets has no effect after the burst.
        done = rt.pop("parkComplete", None)
        if done is not None and book.parking and not book.terminal:
            self._stop_book(book, "agent", "park complete", extra={"parked": done})
            summary["parked"] = done
        return summary

    def _all_dead_no_retry(self, book: Book, strategy: Strategy) -> bool:
        """The `kami-dead` pause condition — the strategy owns at
        least one kami and EVERY owned kami's plan entry is `dead` with the
        key `reviveRetryAt` present and None, i.e. revives are disabled
        (`revive-skipped: disabled` is the only path that writes None). A
        PENDING timer — an offered, refused, reverted, lost or `no-item`
        revive, or an entry written before the key existed, with no
        `reviveRetryAt` at all — keeps the book RUNNING instead, because a
        paused book never ticks its deaths again and the revive retry would
        be lost."""
        owned = strategy.owned_kamis()
        if not owned:
            return False
        for k in owned:
            entry = book.plan.get(str(k)) or {}
            if not entry.get("dead"):
                return False
            if "reviveRetryAt" not in entry or entry["reviveRetryAt"] is not None:
                return False
        return True

    def _pause_external_change(self, book: Book, strategy: Strategy, obs: Observation, basis: Basis, degraded: list[str], rt: dict[str, Any], contradiction: str, streak: int) -> dict[str, Any]:
        """SPEC P7: the CONFIRMED external change — the lifecycle row, the
        pause (`pausedReason: external-change`, `contradictedAt`), the
        matching span started when the same read already matches."""
        ledger = self._ledger(book)
        ledger.lifecycle(basis, "external-change", reason=contradiction, degraded=degraded, dry_run=self.dry_run, observedTicks=streak)
        self._log(book.id, f"external change on {streak} consecutive valid ticks: {contradiction}")
        event = book.transition("paused", paused_reason="external-change")
        book.contradictedAt = basis.lensBlock if basis.lensBlock is not None else 0
        self.store.save(book)
        rt["contradictionTicks"] = 0
        rt.pop("matchingSinceBlock", None)
        if strategy.world_matches_plan(obs):
            # the same read already shows a state the book can own (an
            # external stop): the matching span counts from this block
            rt["matchingSinceBlock"] = book.contradictedAt
        ledger.lifecycle(basis, event, reason="external-change", degraded=degraded, dry_run=self.dry_run)
        readopts = profiles.allows("external-change-readopt", self.profile)
        summary = {"acted": False, "reason": "external change confirmed; paused" + ("; re-checking every tick" if readopts else "")}
        self._tick_row(book, basis, summary, degraded)
        rt["lastSummary"] = summary
        return summary

    def _absent_kamis_tick(self, book: Book, strategy: Strategy, obs: Observation, basis: Basis, degraded: list[str], now: float, rt: dict[str, Any], absent: list[int], rechecking: bool) -> dict[str, Any]:
        """A kami with NO ROW in the party
        answer on an otherwise VALID read. A single such read is a gated
        tick (`lens-incomplete`: it may be a lens gap) that COUNTS toward
        an external change of that kami; on `externalChangeTicks`
        consecutive such reads the book pauses with a reason naming the
        kami (transferred, sacrificed, listed). A paused fleet book
        re-checking the world cannot match while the kami is absent."""
        contradiction = "; ".join(absent_contradiction(k) for k in absent)
        if rechecking:
            rt.pop("matchingSinceBlock", None)
            summary = {"acted": False, "reason": "paused: external change; world does not match plan", "contradiction": contradiction}
            self._tick_row(book, basis, summary, degraded)
            rt["lastSummary"] = summary
            return summary
        streak = int(rt.get("contradictionTicks", 0)) + 1
        rt["contradictionTicks"] = streak
        rt["contradiction"] = contradiction
        need = int(self._config_value(book, "externalChangeTicks") or 3)
        if streak < need:
            flagged = list(degraded) + [incomplete_flag()]
            rt["degraded"] = flagged
            summary = {"acted": False, "reason": "perception gated", "degraded": flagged, "incomplete": obs.incomplete(), "contradiction": contradiction, "observedTicks": streak, "confirmAfterTicks": need}
            self._tick_row(book, basis, summary, flagged)
            return summary
        return self._pause_external_change(book, strategy, obs, basis, degraded, rt, contradiction, streak)

    # ------------------------------------------------- chain-backed fallback

    def _chain_hp_reader(self) -> Any:
        if self.chainhp is None and self.chain is not None:
            self.chainhp = ChainHpReader(self.chain)
        return self.chainhp

    def _tick_under_chain(self, book: Book, strategy: Strategy, degraded: list[str], now: float, rt: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        """The chain fallback: under a lens outage that has lasted
        `chainFallbackAfterSec`, a feed book reads its OWN kamis from the
        chain (one point read, keeper/chainhp.py) and may sign feeds only —
        or, for a REST `auto` book, floor rests — `assess_under_chain`. An
        RPC failure on the read is a gated tick (`rpc-unavailable`); the rows of anything
        signed carry the gating flags plus `underChain` / `chainBlock`."""
        reader = self._chain_hp_reader()
        try:
            reading = reader.read(strategy.owned_kamis(), int(book.config["nodeIndex"]), now)
        except RpcUnavailable as e:
            flagged = list(degraded) + [rpc_flag()]
            rt["lastError"] = f"rpc unavailable: {e}"
            self._log(book.id, f"chain fallback read unavailable ({e}); nothing signed this tick")
            return self._gated(book, Basis(), flagged, chainFallback="read unavailable", **detail)
        except ChainError as e:
            flagged = list(degraded) + [rpc_flag()]
            rt["lastError"] = f"chain: {type(e).__name__}"
            self._log(book.id, f"chain fallback read failed ({type(e).__name__}); nothing signed this tick")
            return self._gated(book, Basis(), flagged, chainFallback=f"read failed: {type(e).__name__}", **detail)
        decisions = strategy.assess_under_chain(reading, now, rt)
        return self._act(book, strategy, None, Basis(), degraded, decisions, now, rt, under_chain=reading, note=None, detail=detail)

    def _seed_from_battles(self, strategy: Strategy, per: Perception) -> dict[str, Any]:
        """Read `battles <kami>` for the book's kamis not yet seeded for this
        account (at most SEED_MAX_READS per pass) and ADD every `isDeath:
        true` row's `account.index` (the killer's owner) to
        `killedAccounts[owner][the book's account]` — never a removal, never a touch of `kills` (the node
        window stays chain-fed). Kamiden degraded or a lens refusal = skip,
        stated on the lifecycle row; never a gate.

        Only a death INSIDE the window plus one day is seeded (the
        largest `hostileWindowHours` among the running auto books and this
        one — what `prune` keeps): an old death must not re-create an owner
        the ledger has forgiven. The age is the row's `blockNumber` against
        the lens block; a row without one falls back to its `timestamp`
        against the clock; a row with neither is not seeded
        (`deathsUndated`). The block stored is the LAST one, and the
        killer's kami row (`hostileKamis`) is raised from `killer.index`."""
        account = getattr(strategy, "account_index", None)
        if account is None:
            return {"status": "not applicable"}
        account = int(account)
        unread = self.owners.unread_for_seed(account, strategy.owned_kamis())
        if not unread:
            return {"status": "already seeded", "kamisRead": 0, "ownersAdded": 0}
        if any(f == "kamiden-unavailable" for f in per.degraded):
            return {"status": "skipped", "reason": "kamiden feeds degraded"}
        if any(f in ("lens-unavailable", "lens-not-live") for f in per.degraded):
            return {"status": "skipped", "reason": "lens not answering"}
        reads = deaths = added = outside = undated = 0
        read_kamis: list[int] = []
        outcome: dict[str, Any] = {"status": "seeded"}
        keep_hours = self._largest_window(self._auto_books(), at_least=int(getattr(strategy, "window_hours", 0) or 0)) + 24
        now = float(self.clock())
        for k in unread[:SEED_MAX_READS]:
            try:
                ans = self.lens.battles(int(k))
            except LensNotReady:
                outcome = {"status": "skipped", "reason": "lens not LIVE"}
                break
            except LensUnavailable:
                outcome = {"status": "skipped", "reason": "lens unreachable"}
                break
            except LensQueryError as e:
                if e.code == "NOT_FOUND":
                    reads += 1
                    read_kamis.append(int(k))
                    continue
                outcome = {"status": "skipped", "reason": f"lens {e.code}"}
                break
            reads += 1
            read_kamis.append(int(k))
            data = ans.data if isinstance(ans.data, dict) else {}
            for row in data.get("kills") or []:
                if not isinstance(row, dict) or row.get("isDeath") is not True:
                    continue
                acct = row.get("account") if isinstance(row.get("account"), dict) else {}
                owner = acct.get("index")
                if not isinstance(owner, int) or isinstance(owner, bool) or owner == account:
                    continue
                deaths += 1
                head = per.basis.lensBlock if per.basis.lensBlock is not None else ans.block
                blk = row.get("blockNumber")
                blk = int(blk) if isinstance(blk, int) and not isinstance(blk, bool) and blk > 0 else None
                ts = row.get("timestamp")
                if blk is None and head is not None and isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
                    # placed by its timestamp: the block clock when it
                    # has samples, else head minus the age at BLOCK_SECONDS
                    at_ts = self.block_clock.block_at(float(ts))
                    blk = int(at_ts) if at_ts is not None else max(1, int(head) - int(max(0.0, now - float(ts)) / BLOCK_SECONDS))
                if blk is None or head is None:
                    undated += 1
                    continue
                if blk < self.block_clock.floor(keep_hours, head, now):
                    outside += 1
                    continue
                killer = row.get("killer") if isinstance(row.get("killer"), dict) else {}
                killer_kami = killer.get("index") if isinstance(killer.get("index"), int) and not isinstance(killer.get("index"), bool) else None
                room = row.get("roomIndex") if isinstance(row.get("roomIndex"), int) and not isinstance(row.get("roomIndex"), bool) else None
                ts_val = int(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0 else None  # the row's own date
                if self.owners.seed_killed(owner, account, blk, attacker_kami=killer_kami, node=room, ts=ts_val):
                    added += 1
        if read_kamis:
            self.owners.mark_seeded(account, read_kamis, {"at": _now_iso(), "block": per.basis.lensBlock})
        outcome.update({"kamisRead": reads, "deaths": deaths, "ownersAdded": added, "pending": max(0, len(unread) - len(read_kamis))})
        if outside:
            outcome["deathsOutsideWindow"] = outside  # older than the window plus one day: seeds nothing
        if undated:
            outcome["deathsUndated"] = undated
        try:
            self.owners.save()
        except OSError as e:
            self._log(None, f"owners.json not written: {type(e).__name__}")
        if outcome["status"] == "seeded":
            self._log(None, f"battles seed for account {account}: {reads} kamis read, {deaths} deaths, {added} owner(s) added to killedAccounts")
        else:
            self._log(None, f"battles seed for account {account} skipped ({outcome.get('reason')}); {reads} kamis read first")
        return outcome

    def _readopt_check(self, book: Book, strategy: Strategy, obs: Observation, basis: Basis, degraded: list[str], rt: dict[str, Any]) -> bool:
        """SPEC P7 fleet re-adopt: on a VALID read of a book
        paused by external-change, count the blocks over which the world
        has matched the plan (every owned kami on the node or resting, none
        dead); at `readoptAfterBlocks` adopt the observed state, log
        `readopted`, run. A read that does not match resets the count. It
        never re-adopts against a contradiction, however long the quiet.
        Returns True once readopted; else writes the tick row and returns
        False (`rt["lastSummary"]` carries it)."""
        ledger = self._ledger(book)
        need = int(self._config_value(book, "readoptAfterBlocks") or 240)
        block = basis.lensBlock if basis.lensBlock is not None else 0
        if strategy.world_matches_plan(obs):
            since = rt.get("matchingSinceBlock")
            if since is None:
                since = block
                rt["matchingSinceBlock"] = since
            span = block - int(since)
            if span >= need:
                strategy.adopt(obs)
                book.transition("running")
                book.contradictedAt = None
                self.store.save(book)
                rt.pop("matchingSinceBlock", None)
                ledger.lifecycle(basis, "readopted", reason=f"world matched the plan for {span} blocks", degraded=degraded, dry_run=self.dry_run, plan=dict(book.plan))
                self._log(book.id, f"readopted after {span} blocks of a world matching the plan")
                return True
            summary = {"acted": False, "reason": "paused: external change; world matches plan", "matchingBlocks": span, "readoptAfterBlocks": need}
        else:
            rt.pop("matchingSinceBlock", None)
            summary = {"acted": False, "reason": "paused: external change; world does not match plan", "contradiction": strategy.external_change(obs) or "a book kami is not in a state the book can own"}
        self._tick_row(book, basis, summary, degraded)
        rt["lastSummary"] = summary
        return False

    def _fail_book(self, book: Book, basis: Basis, degraded: list[str], rt: dict[str, Any], last_error: str, reason: dict[str, Any], text: str, summary_reason: str) -> dict[str, Any]:
        """A signer / chain failure that ends the book (SPEC P3 `failed`):
        the lifecycle row, the tick row, the summary (kept in
        `rt["lastSummary"]` for the caller)."""
        rt["lastError"] = last_error
        event = book.transition("failed", reason)
        self.store.save(book)
        self._ledger(book).lifecycle(basis, event, reason=text, degraded=degraded, dry_run=self.dry_run)
        summary = {"acted": False, "reason": summary_reason}
        self._tick_row(book, basis, summary, degraded)
        rt["lastSummary"] = summary
        return summary

    def _submit(self, targets: list[tuple[Book, Basis, list[str], dict[str, Any]]], intents: list[Intent], config: dict[str, Any], on_broadcast: Callable[[list[Any]], None] | None = None) -> list[Any] | None:
        """signer.submit with the SPEC P4 / P6 failure handling applied to
        EVERY target book (one for a book's own burst, several for a
        batched collect). Returns the records, or None when nothing was
        signed — each target's rows are written and `rt["lastSummary"]`
        carries its summary. `on_broadcast` is handed to the
        signer: a live backend calls it once with every record in its
        post-broadcast state before any receipt is awaited."""
        try:
            return self.signer.submit(intents, config, on_broadcast=on_broadcast)
        except NotGranted as e:
            for book, basis, degraded, rt in targets:
                self._fail_book(book, basis, degraded, rt, "not granted", {"failed": "signer", "reason": "grant-revoked"}, str(e), "failed: signer (no grant)")
        except (WhitelistRefused, SignerRefused) as e:
            for book, basis, degraded, rt in targets:
                self._fail_book(book, basis, degraded, rt, "signer refused", {"failed": "signer", "reason": "refused"}, str(e), "failed: signer refused")
        except RpcUnavailable as e:
            # SPEC P4: the chain did not answer a read; nothing was signed
            # for this burst, the book stays running and says so.
            for book, basis, degraded, rt in targets:
                flagged = list(degraded) + [rpc_flag()]
                rt["degraded"] = flagged
                rt["lastError"] = f"rpc unavailable: {e}"
                summary = {"acted": False, "reason": "rpc unavailable; nothing signed", "degraded": flagged}
                self._tick_row(book, basis, summary, flagged)
                rt["lastSummary"] = summary
                self._log(book.id, f"rpc unavailable ({e}); nothing signed this tick")
        except ChainError as e:
            for book, basis, degraded, rt in targets:
                self._fail_book(book, basis, degraded, rt, f"chain: {type(e).__name__}", {"failed": "rpc", "reason": "chain"}, f"chain: {type(e).__name__}", "failed: rpc")
        return None

    # ------------------------------------------------- the two-stage tx row

    def _broadcast_writer(self, ledger: Ledger, basis: Basis, degraded: list[str], book_id: str, rt: dict[str, Any], extra_tx: dict[str, Any], seen: set[int], prepare: Callable[[Any], None] | None = None) -> Callable[[list[Any]], None]:
        """SPEC P5: the `on_broadcast` callback for one book's
        burst — a `stage: broadcast` row per record, written before any
        receipt is awaited (`unconfirmed` with hash and nonce, or the ONE
        row of a `not_sent` record), and `lastTxHash` at this stage. `seen`
        collects the records reported, so the receipt stage knows which
        ones already have their broadcast row."""

        def on_broadcast(records: list[Any]) -> None:
            for rec in records:
                if prepare is not None:
                    prepare(rec)
                seen.add(id(rec))
                ledger.tx(basis, {**rec.as_ledger_tx(book_id, stage="broadcast"), **extra_tx}, degraded, dry_run=(rec.status == "dry_run"))
                if rec.txHash:
                    rt["lastTxHash"] = rec.txHash

        return on_broadcast

    def _receipt_rows(self, ledger: Ledger, basis: Basis, degraded: list[str], book_id: str, rt: dict[str, Any], extra_tx: dict[str, Any], seen: set[int], records: list[Any], prepare: Callable[[Any], None] | None = None) -> None:
        """The `stage: receipt` row per record the callback
        reported and that was broadcast (a hash) — what the receipt read
        said, repeating everything the broadcast row stated; a `not_sent`
        record has its one row already. A record the signer reported in one
        phase (a dry run, a test double) gets the single-row form. A
        receipt row written `unconfirmed` (the receipt read did not answer)
        leaves the step OPEN — it is registered in `rt["_openTx"]` and
        probed at the next tick."""
        for rec in records:
            if prepare is not None:
                prepare(rec)
            if id(rec) in seen:
                if rec.txHash:
                    row = ledger.tx(basis, {**rec.as_ledger_tx(book_id, stage="receipt"), **extra_tx}, degraded, dry_run=(rec.status == "dry_run"))
                    if rec.status == "unconfirmed":  # the receipt read did not answer — the step stays open
                        self._register_open_tx(rt, rec.txHash, row)
                continue
            ledger.tx(basis, {**rec.as_ledger_tx(book_id), **extra_tx}, degraded, dry_run=(rec.status == "dry_run"))
            if rec.txHash:
                rt["lastTxHash"] = rec.txHash

    def _act(self, book: Book, strategy: Strategy, obs: Observation | None, basis: Basis, degraded: list[str], decisions: list[Decision], now: float, rt: dict[str, Any], under_stale: bool = False, note: str | None = None, under_chain: Any | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the decisions, submit their intents, record every record,
        apply the plan of every decision whose records landed. A `collect`
        decision is POOLED: it leaves this burst and
        goes out in the account's batched collect at the end of the tick
        pass (`_flush_collects`), which writes its tx row."""
        ledger = self._ledger(book)
        extra_tx: dict[str, Any] = {"underStale": True} if under_stale else {}
        if under_chain is not None:
            extra_tx = {"underChain": True, "chainBlock": under_chain.block}
        for d in decisions:
            ledger.decision(basis, d.kind, d.parameters, degraded, self.dry_run)
        # poolStopsAcrossBooks: a stop decided on the NORMAL assess path leaves
        # this burst and joins every other opted-in book of the same
        # account and node due in this pass (`_flush_stops`). A degraded
        # path is never pooled: a stop decided under a stale lens or from
        # the chain fallback goes out now, as it always has.
        node = book.config.get("nodeIndex")
        pool_stops = (
            bool(self._config_value(book, "poolStopsAcrossBooks"))
            and not under_stale and under_chain is None
            and node is not None
        )
        stop_kinds = ("rest", "failsafe-stop")
        cap = int(BATCH_CAPS["system.harvest.stop"])
        stops: list[Decision] = []
        stop_ids: set[int] = set()
        if pool_stops:
            for d in decisions:
                if d.kind not in stop_kinds or not d.intents or d.intents[0].system != "system.harvest.stop":
                    continue
                n = len(d.parameters.get("kamis") or [d.parameters.get("kami")])
                if n > cap:  # cannot be packed whole: it stays in the book's own burst
                    self._log(book.id, f"stop of {n} kamis exceeds the pool chunk; sent in the book's own burst")
                    continue
                stops.append(d)
                stop_ids.add(id(d))
        pooled = [d for d in decisions if d.kind == "collect"]
        immediate = [d for d in decisions if d.kind != "collect" and id(d) not in stop_ids]
        for d in stops:
            self._stop_pool.setdefault((book.account, int(node)), []).append(PooledStop(book, strategy, d, basis, list(degraded), now, rt))
        for d in pooled:
            self._collect_pool.setdefault(book.account, []).append(PooledCollect(book, strategy, d, basis, list(degraded), now, rt))
        intents = [i for d in immediate for i in d.intents]
        records: list[Any] = []
        fallback_rows = 0
        if intents:
            # the `broadcast` rows are written from the signer's
            # callback before any receipt is awaited; the `receipt` rows from
            # the returned records (SPEC P5, the two-stage row)
            seen: set[int] = set()
            got = self._submit([(book, basis, degraded, rt)], intents, book.config, on_broadcast=self._broadcast_writer(ledger, basis, degraded, book.id, rt, extra_tx, seen))
            if got is None:
                return rt["lastSummary"]
            records = got
            self._receipt_rows(ledger, basis, degraded, book.id, rt, extra_tx, seen, records)
            rt["lastActAt"] = now
            rt["ticksSinceAct"] = 0
            # a decision's plan applies only when every one of its records landed
            # (or would have, in dry run); a refused or reverted one leaves the
            # plan as the world last confirmed it
            pos = 0
            retry: list[Decision] = []
            for d in immediate:
                mine = records[pos:pos + len(d.intents)]
                pos += len(d.intents)
                if mine and all(self._landed(r) for r in mine):
                    strategy.apply_plan(d)
                    if d.kind == "revive":
                        ledger.death(basis, int(d.parameters["kami"]), None, "revived", tx_hash=mine[0].txHash, degraded=degraded, dry_run=self.dry_run)
                elif mine and d.kind == "revive":
                    # the revive did NOT land (refused pre-send, reverted,
                    # not sent). It is never final: the timer set at the
                    # offer stands, and a nonce race is re-decided next tick.
                    k = int(d.parameters["kami"])
                    rec = mine[0]
                    reason = str(getattr(rec, "reason", None) or rec.status or "no reason given")
                    ledger.death(basis, k, None, "revive-refused: " + reason[:120], degraded=degraded, dry_run=self.dry_run)
                    race = rec.status == "not_sent" and str(getattr(rec, "reason", None) or "").startswith("nonce race")
                    if race:
                        strategy.mark_dead(k, now)
                    at = (book.plan.get(str(k)) or {}).get("reviveRetryAt")
                    in_sec = int(float(at) - now) if isinstance(at, (int, float)) and not isinstance(at, bool) else 0
                    self._log(book.id, f"kami {k}: revive refused ({reason[:80]}); offered again in {in_sec} s")
                elif mine and d.kind in ("start", "rest") and len(d.parameters.get("kamis") or []) > 1:
                    retry.append(d)
            strategy.on_records(immediate, records, now, rt)
            # a batched start /
            # stop that did not land is re-offered as singles ONCE in the
            # same tick, each with its own pre-check, so one refused kami
            # does not hold the other members; the refused singles back off
            for d in retry:
                singles = strategy.split_batch(d)
                self._log(book.id, f"batched {d.kind} of {len(singles)} did not land; re-offering each member as a single {d.kind} once")
                fb_extra = {**extra_tx, "batchFallback": True}
                fb_seen: set[int] = set()
                got = self._submit([(book, basis, degraded, rt)], [i for sd in singles for i in sd.intents], book.config, on_broadcast=self._broadcast_writer(ledger, basis, degraded, book.id, rt, fb_extra, fb_seen, prepare=_single))
                if got is None:
                    return rt["lastSummary"]
                self._receipt_rows(ledger, basis, degraded, book.id, rt, fb_extra, fb_seen, got, prepare=_single)
                spos = 0
                for sd in singles:
                    smine = got[spos:spos + len(sd.intents)]
                    spos += len(sd.intents)
                    if smine and all(self._landed(r) for r in smine):
                        strategy.apply_plan(sd)
                strategy.on_records(singles, got, now, rt)
                records = records + got
                fallback_rows += len(got)
            # the bar — the highest receipt block this tick LANDED
            # (burst + fallback singles); a read older than it decides nothing
            landed_blocks = [int(r.block) for r in records if self._landed(r) and getattr(r, "block", None)]
            if landed_blocks:
                rt["lastActBlock"] = max(int(rt.get("lastActBlock") or 0), max(landed_blocks))
            self.store.save(book)
            failed = [r for r in records if r.status in ("not_sent", "reverted")]
            if failed:
                rt["lastError"] = f"{len(failed)} of {len(records)} tx did not land ({failed[0].status}: {failed[0].reason or 'reverted on chain'})"
            # the log counts a pre-check refusal as not landed in dry run too
            # (its record reads `dry_run` with `precheck: reverted`); lastError
            # above does not (a dry-run refusal is not an error)
            refused = [r for r in records if getattr(r, "precheck", None) == "reverted"]
            landed = [r for r in records if r not in failed and r not in refused]
            how = "dry run" if self.dry_run else ("signed" if len(records) > len(refused) else "nothing signed")
            self._log(book.id, f"acted: {', '.join(d.kind for d in immediate)} ({len(records)} tx, {how}; {len(landed)} landed{'; ' + str(len(refused)) + ' refused pre-send' if refused else ''}{'; lens stale' if under_stale else ''}{'; chain fallback' if under_chain is not None else ''})")
        if stops:
            self._log(book.id, f"{sum(len(_stop_kamis(d)) for d in stops)} stop(s) pooled for the account+node burst")
        if pooled:
            self._log(book.id, "collect pooled for the account's batched collect")
        for dev in rt.pop("deviations", None) or []:
            ledger.deviation(basis, dev, degraded, dry_run=self.dry_run)
            if dev.get("backoffSec") is not None:
                self._log(book.id, f"kami {dev.get('kami')}: {dev.get('action')} refused ({dev.get('reason')}); not offered again for {dev.get('backoffSec')} s")
            else:  # a deviation that is an act, not a refusal (the chain-backed floor rest)
                self._log(book.id, f"kami {dev.get('kami')}: {dev.get('action')} — {dev.get('reason')}")
        summary: dict[str, Any] = {"acted": bool(intents) or bool(pooled) or bool(stops), "decisions": [d.kind for d in decisions], "txCount": len(records), "txStatus": [r.status for r in records]}
        if fallback_rows:
            summary["batchFallbackTx"] = fallback_rows
        if stops:
            summary["stopPooled"] = sum(len(_stop_kamis(d)) for d in stops)
        if pooled:
            summary["collectPooled"] = len(pooled)
        if under_stale:
            summary["underStale"] = True
            summary["degraded"] = degraded
        if under_chain is not None:
            summary["underChain"] = True
            summary["chainBlock"] = under_chain.block
            summary["chainReads"] = reads_note(under_chain)
            summary["degraded"] = degraded
            if under_chain.node_affinity_unknown:
                summary["nodeAffinityUnknown"] = list(under_chain.node_affinity_unknown)
            for k, v in (detail or {}).items():
                summary.setdefault(k, v)
        withheld = rt.pop("withheld", None)
        if withheld:
            summary["feedWithheld"] = withheld
        extra = rt.pop("summaryExtra", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                summary.setdefault(k, v)
        scan_note = self.context.scan.get("note") if strategy.watches_node() else None
        if scan_note:
            summary["killScan"] = scan_note
        stamina_wait = rt.pop("staminaWait", None)
        if stamina_wait:
            summary["staminaWait"] = stamina_wait
        if note:
            summary["note"] = note
        self._tick_row(book, basis, summary, degraded)
        return summary

    # ------------------------------------------------------------ kill scan

    def _largest_window(self, books: list[Book], at_least: int = 0) -> int:
        """The largest `hostileWindowHours` among `books` (the prune / seed /
        first-boot scan window; the window itself stays per book)."""
        return max([int(at_least)] + [int(self._config_value(b, "hostileWindowHours") or 48) for b in books])

    def _auto_books(self) -> list[Book]:
        # the running books that WATCH A NODE — every `auto` book and
        # a `harvestAndFeed` book in ladder mode; the name is unchanged
        return [b for b in self.store.all() if b.state == "running" and self.strategies[b.id].watches_node()]

    def _scan_blocks(self, book: Book, key: str) -> int:
        """A book's block-count config value, the schema default
        only when the key is MISSING — a configured 0 is a real 0."""
        v = self._config_value(book, key)
        if v is None:
            v = schema_defaults(book.strategyType).get(key)
        return int(v or 0)

    def _own_occupants(self, books: list[Book]) -> dict[int, OccupantRef]:
        """Every running node-watching book's OWN kamis as
        occupant refs of that book's node — kami entity id -> OccupantRef.
        A census can only list a kami that is still on the node, so a kami
        killed before any census saw it (in the blocks between the previous
        scan head and the census read) has no row to
        resolve against; the books' own kamis are resolvable from the books,
        whatever the census age and across a restart. The node is what the
        book PLANNED for the kami, else the book's node."""
        out: dict[int, OccupantRef] = {}
        for b in books:
            strategy = self.strategies[b.id]
            kamis = [int(k) for k in strategy.owned_kamis()] if hasattr(strategy, "owned_kamis") else [int(b.config["kamiIndex"])]
            account = b.config.get("accountIndex")
            account = int(account) if account is not None else None
            book_node = int(b.config["nodeIndex"])
            for k in kamis:
                planned = (b.plan.get(str(k)) or {}).get("node")
                node = int(planned) if isinstance(planned, int) and not isinstance(planned, bool) else book_node
                out[entity_id(int(k))] = OccupantRef(node, int(k), account)
        return out

    def _scan_kills(self, now: float) -> None:
        """Once per tick pass, before any node-watching
        book assesses — the World's `component.state = DEAD` writes since the
        last scanned block, attributed through the liquidate calldata,
        recorded in the owner ledger, and written as a `kill` row to every
        running node-watching book on that node. RPC failure ->
        `rpc-unavailable`; blocks left unscanned -> `ws-logs-gap:<N>`;
        neither gates a tick.

        Occupant memory: the DEAD writes are resolved against the previous pass's node
        occupants PLUS the occupants remembered from the last
        `occupantMemoryBlocks` and every running book's own kamis — a kami
        killed between the previous scan head and the census read has
        already left the node when the census is taken — and the scan
        re-reads `killScanOverlapBlocks` behind its cursor, because a head
        block's logs can be served before the node's log index caught up.
        A re-read row the ledger already holds is skipped by
        `OwnerLedger.known` before any transaction read.

        Re-attribution: a row the scan could NOT attribute — the
        RPC replica served the log before it would serve the transaction —
        is re-read from the chain on a LATER pass (`KillScanner.reattribute`),
        at most REATTRIBUTE_MAX_CALLS_PER_PASS reads a pass and only while
        the row is within REATTRIBUTE_HEAD_BLOCKS of the head, and a resolved
        row is promoted into `kills` with its OWN date, so the window it
        opens starts at the kill."""
        books = self._auto_books()
        self.context.scan = {}
        if not books:
            return
        if self.chain is None:
            self.context.scan = {"flags": [], "note": "no chain configured; owner classes rest on the persisted ledger only"}
            return
        window = self._largest_window(books)
        span = min(int(self._config_value(b, "logScanBlocks") or 40) for b in books)
        # the widest re-read and the longest occupant memory among the
        # books of the pass (a configured 0 is a real 0; only a MISSING key
        # falls back to the schema default)
        overlap = max(self._scan_blocks(b, "killScanOverlapBlocks") for b in books)
        memory = max(self._scan_blocks(b, "occupantMemoryBlocks") for b in books)
        nodes = {int(b.config["nodeIndex"]) for b in books}
        current: dict[int, OccupantRef] = {}
        for node in nodes:
            current.update(self.context.node_occupants.get(node, {}))
        # the table the scanner resolves DEAD writes against: the remembered
        # occupants of the last `memory` blocks, then every running book's
        # OWN kamis, then the live census — a census row always wins
        cursor = int(self.owners.last_scanned_block or 0)
        for entity in [e for e, (_ref, seen) in self._occupant_memory.items() if cursor - int(seen) >= memory]:
            del self._occupant_memory[entity]
        occupants: dict[int, OccupantRef] = {e: ref for e, (ref, _seen) in self._occupant_memory.items()}
        own = self._own_occupants(books)
        occupants.update(own)
        occupants.update(current)
        remembered = sum(1 for e in occupants if e not in current and e not in own)
        own_only = sum(1 for e in own if e not in current)
        lens = self._pass_lens or self.lens

        def lookup_account(kami_index: int) -> int | None:
            try:
                acct = (lens.kami(int(kami_index)).data or {}).get("account") or {}
                return int(acct["index"]) if isinstance(acct.get("index"), int) else None
            except (LensUnavailable, LensQueryError, ValueError, TypeError):
                return None

        def lookup_account_entity(entity: int) -> int | None:
            try:
                address = "0x" + int(entity).to_bytes(32, "big")[-20:].hex()
                data = lens.account(address, slim=True).data or {}
                return int(data["index"]) if isinstance(data.get("index"), int) else None
            except (LensUnavailable, LensQueryError, ValueError, TypeError, OverflowError):
                return None

        scanner = KillScanner(self.chain, span, lookup_index=self.chain.kami_index_of, lookup_account=lookup_account, lookup_account_entity=lookup_account_entity, emitter=self.chain.emitter_address, block_clock=self.block_clock)
        our_accounts = {int(b.config["accountIndex"]) for b in books if b.config.get("accountIndex") is not None}
        res = scanner.scan(self.owners.last_scanned_block, occupants, window, our_accounts=our_accounts, overlap=overlap, known=self.owners.known)
        self.scan_runs += 1
        if res.head is not None:
            # THE sample source of the block clock — the fresh
            # eth_blockNumber this pass already made, at this pass's time
            if self.block_clock.observe(int(res.head), now):
                self.block_clock.save()
            # a memory entry is refreshed only by a census that LISTS it
            for entity, ref in current.items():
                self._occupant_memory[entity] = (ref, int(res.head))
        flags: list[str] = []
        if res.rpc_error:
            flags.append(rpc_flag())
        if res.gap > span:
            flags.append(ws_logs_gap(res.gap))
        new_kills = new_casts = unattributed = 0
        by_node: dict[int, list[Book]] = {}
        for b in books:
            by_node.setdefault(int(b.config["nodeIndex"]), []).append(b)
        basis = Basis()
        # a kill row the scan could NOT attribute is re-read from the
        # chain on a later pass — never in the pass that recorded it (the RPC
        # replica served the log before it would serve the transaction, and a
        # read microseconds later fails the same way), only while the row is
        # within REATTRIBUTE_HEAD_BLOCKS of this pass's head, at most
        # REATTRIBUTE_MAX_CALLS_PER_PASS transaction reads a pass, oldest
        # block first. A resolved row is PROMOTED with its OWN date, so the
        # window it opens starts at the kill; a row that stays unreadable is
        # left exactly where it is and offered again next pass.
        reattributed = reattributed_calls = 0
        if res.head is not None and REATTRIBUTE_HEAD_BLOCKS > 0:
            for node, row in self.owners.unattributed_rows():
                if reattributed_calls >= REATTRIBUTE_MAX_CALLS_PER_PASS:
                    break
                behind = int(res.head) - int(row.get("block") or 0)
                if behind < 0 or behind > REATTRIBUTE_HEAD_BLOCKS:
                    continue
                reattributed_calls += 1  # every read counts, resolved or not
                resolved = scanner.reattribute(row, occupants, res)
                if resolved is None:
                    continue
                promoted = self.owners.promote_unattributed(int(node), row.get("txHash"), row.get("logIndex"), resolved[0], resolved[1], resolved[2])
                if promoted is None:
                    continue
                reattributed += 1
                self.census.force(int(node))  # an attributed kill on a node forces its census
                self._kill_rows(dict(promoted, node=int(node)), by_node, basis, flags, res.head, now, reattributed_after=behind)
        for kill in res.kills:
            if not self.owners.record_kill(kill):
                continue
            new_kills += 1
            self.census.force(int(kill["node"]))  # a kill row on a node forces its census (a flip needs the present attackers)
            if kill.get("attackerAccount") is None:
                unattributed += 1
            self._kill_rows(kill, by_node, basis, flags, res.head, now)
        for cast in res.casts:
            if not self.owners.record_cast(cast):
                continue
            new_casts += 1
            self.census.force(int(cast["node"]))
            for b in by_node.get(int(cast["node"]), []):
                strategy = self.strategies[b.id]
                cls_after = strategy.render_owner_class(cast["casterAccount"], res.head) if cast.get("casterAccount") is not None and hasattr(strategy, "render_owner_class") else None
                row = {"event": "cast", "victimKami": cast["targetKami"], "victimAccount": cast["targetAccount"], "attackerKami": None, "attackerAccount": cast["casterAccount"], "block": cast["block"], "txHash": cast["txHash"] or None, "ownerClassAfter": cls_after, "item": cast["item"], "blockTs": cast.get("blockTs")}
                self._ledger(b).kill(basis, row, flags, dry_run=self.dry_run)
                self._log(b.id, f"cast on node {cast['node']}: kami {cast['targetKami']} by account {cast['casterAccount']} (item {cast['item']}) at block {cast['block']}; owner class now {cls_after}")
        if res.rpc_error is None:
            self.owners.set_last_scanned(res.last_scanned)
        self.owners.prune(window + 24, res.head)
        try:
            self.owners.save()
        except OSError as e:
            self._log(None, f"owners.json not written: {type(e).__name__}")
        note = {"head": res.head, "lastScanned": res.last_scanned, "logCalls": res.logCalls, "deadWrites": res.dead_writes, "ignored": res.ignored, "kills": new_kills, "casts": new_casts, "castsOnOthers": res.casts_on_others, "unattributed": unattributed, "reattributed": reattributed, "overlapSkipped": res.overlap_skipped, "remembered": remembered, "own": own_only, "gap": res.gap, "tsUnread": res.ts_unread}
        if res.rpc_error:
            note["rpcError"] = res.rpc_error
        self.context.scan = {"flags": flags, "note": note, "head": res.head}
        if new_kills or new_casts or reattributed or res.rpc_error:
            self._log(None, f"kill scan: {res.logCalls} eth_getLogs call(s) to block {res.last_scanned}, {new_kills} kill(s), {new_casts} cast(s), {unattributed} unattributed" + (f"; rpc unavailable ({res.rpc_error})" if res.rpc_error else "") + (f"; {res.gap} blocks still unscanned" if res.gap else "") + (f"; {res.overlap_skipped} known row(s) re-read" if res.overlap_skipped else "") + (f"; {reattributed} re-attributed" if reattributed else ""))

    def _kill_rows(self, kill: dict[str, Any], by_node: dict[int, list[Book]], basis: "Basis", flags: list[str], head: int | None, now: float, reattributed_after: int | None = None) -> None:
        """The per-book half of the kill loop — the ledger row, the kill
        measurement (`learned`) and the daemon line for every running
        node-watching book on the kill's node. The fresh loop calls it with
        `reattributed_after` None (byte-identical to the loop it was
        extracted from); a kill the scan could not attribute on its own pass
        and RESOLVED later carries the one additive key `reattributed` and
        the block distance between the log and the retry — the suffix sits
        AFTER any `measured:` text, so the existing greps keep working. The
        counters and `census.force` stay in the caller."""
        for b in by_node.get(int(kill["node"]), []):
            strategy = self.strategies[b.id]
            cls_after = strategy.render_owner_class(kill["attackerAccount"], head) if kill.get("attackerAccount") is not None and hasattr(strategy, "render_owner_class") else None
            row = {"event": "liquidate", "victimKami": kill["victimKami"], "victimAccount": kill["victimAccount"], "attackerKami": kill["attackerKami"], "attackerAccount": kill["attackerAccount"], "block": kill["block"], "txHash": kill["txHash"] or None, "ownerClassAfter": cls_after, "attribution": kill.get("attribution"), "blockTs": kill.get("blockTs")}
            if reattributed_after is not None:
                row["reattributed"] = True
            # a kill of THIS book's account on this node is a
            # measurement of the killer's line (strategies/auto.py);
            # a kill of another account's kami carries no `learned`
            learned = None
            if kill.get("victimAccount") is not None and b.config.get("accountIndex") is not None and int(kill["victimAccount"]) == int(b.config["accountIndex"]) and hasattr(strategy, "learn_kill"):
                # a RE-ATTRIBUTED kill is never measured.
                # `learn_kill` prices the killer's line off the victim's view
                # from the PREVIOUS pass, which by the time a retry resolves
                # is the kami read DEAD — or, if it had been revived and
                # restarted, a post-revive HP that would LIFT the owner's
                # lines off a number that was never the HP at the kill.
                # At `resumeHpPct` 100 that second case cannot occur; the
                # guard keeps it so under any value. The retry
                # recovers the BIND; the measurement is lost by design, and
                # the row says so instead of blaming a missing view.
                learned = {"skipped": "re-attributed"} if reattributed_after is not None else strategy.learn_kill(kill, now)
            if learned is not None:
                row["learned"] = learned
            self._ledger(b).kill(basis, row, flags, dry_run=self.dry_run)
            line = f"kill on node {kill['node']}: kami {kill['victimKami']} (account {kill['victimAccount']}) by kami {kill['attackerKami']} of account {kill['attackerAccount']} at block {kill['block']}; owner class now {cls_after}" if cls_after else f"kill on node {kill['node']}: kami {kill['victimKami']} (account {kill['victimAccount']}) at block {kill['block']}; attacker unresolved ({kill.get('attribution')}); no class changed"
            if learned is not None:
                if "skipped" in learned:
                    line += f"; measured: skipped ({learned['skipped']})"
                elif "liftPct" in learned:
                    line += f"; measured: victim HP {learned['hpAtKill']} vs served {learned['served']} (+{learned['gapHp']}) -> lift {learned['liftPct']} % on {len(learned['lifted'])} attacker(s) of owner {learned['owner']}"
                else:
                    line += f"; measured: victim HP {learned['hpAtKill']} vs served {learned['served']} ({learned['gapHp']}): a late rest"
            if reattributed_after is not None:
                line += f"; re-attributed {reattributed_after} block(s) after the log"
            self._log(b.id, line)
            # killForcesTick: a kill row is written to every book on the
            # node in the same pass, but without this key each book assesses
            # it only at its own nextTickAt, up to one tickSeconds later. A
            # kill whose owner binds this book raises every exposed kami's
            # line, so with the key on the book assesses in
            # THIS pass instead of at its own next tick. The clock only ever
            # moves FORWARD, and the row and the line above are already
            # written, so a failure here cannot cost either.
            if self._config_value(b, "killForcesTick") and kill.get("attackerAccount") is not None \
                    and hasattr(strategy, "owner_binds_now") and strategy.owner_binds_now(kill["attackerAccount"], head):
                rt = self.runtime.setdefault(b.id, {})
                due = rt.get("nextTickAt")
                if due is None or float(due) > now:
                    rt["nextTickAt"] = now
                    rt["killForcedTick"] = True
                    self._log(b.id, f"kill on node {kill['node']} by account {kill['attackerAccount']}: assessing in this pass (was due in {int(float(due) - now)} s)" if due is not None else f"kill on node {kill['node']} by account {kill['attackerAccount']}: assessing in this pass")

    # ---------------------------------------------------- batched collects

    def _flush_collects(self) -> list[Any]:
        """Every collect pooled since the last flush goes
        out per account as ONE `executeBatched` collect of up to
        BATCH_CAPS collect (12) kamis; one `tx` row per book with the
        shared hash and `batchSize`. Called under the lock. Returns the
        records sent."""
        pool, self._collect_pool = self._collect_pool, {}
        cap = int(BATCH_CAPS["system.harvest.collect"])
        sent: list[Any] = []
        for account, entries in pool.items():
            live: list[PooledCollect] = []
            for pc in entries:
                if pc.book.state == "running":
                    live.append(pc)
                else:
                    self._log(pc.book.id, f"collect not sent: the book is {pc.book.state}")
            for i in range(0, len(live), cap):
                rec = self._send_collect_batch(account, live[i:i + cap])
                if rec is not None:
                    sent.append(rec)
        return sent

    @staticmethod
    def _landed(rec: Any) -> bool:
        """A record the chain accepted (or would have, in dry run): the
        status says sent and the pre-check did not refuse it. A dry-run
        record with `precheck: reverted` is NOT landed (the dry-run ledger
        carries the chain's verdict on the row)."""
        return rec.status in ("success", "unconfirmed", "dry_run") and getattr(rec, "precheck", None) != "reverted"

    @staticmethod
    def _read_past_last_act(basis: Basis, bar: int) -> bool:
        """A read is past the book's own last landed tx only when the lens's
        own lower bound has reached the receipt block of that tx. The mirror
        block (`lensBlock` = the chain head the daemon knows) is NOT an
        applied cursor — a read at receipt block + 1 can still lack the
        book's own transaction — so it never counts.

        Which bound: on lens 1.0.x the APPLIED mark, `lensAppliedThrough >=
        bar` — the lens documents this as the number that answers "is my
        transaction reflected in this answer?", while `reconciledThrough` is
        the slower chain-proof mark that lags up to one reconcile interval
        and starts lower after a boot. On a 0.6.x lens, which serves no
        applied mark at all, it is `lensReconciledThrough >= bar`."""
        if basis.lensAppliedThrough is not None:
            return int(basis.lensAppliedThrough) >= bar
        return basis.lensReconciledThrough is not None and int(basis.lensReconciledThrough) >= bar

    def _collect_intent(self, account: str, kamis: list[int], book_id: str) -> Intent:
        if len(kamis) == 1:
            return Intent("system.harvest.collect", account, kamis[0], {"count": 1}, book_id)
        return Intent("system.harvest.collect", account, None, {"kamiIndices": list(kamis), "count": len(kamis)}, book_id)

    def _send_collect_batch(self, account: str, entries: list[PooledCollect]) -> Any | None:
        """One batched collect for these books; when the BATCH does not
        land (pre-check refused, node refused, reverted) and it had more
        than one member, each member is re-offered ONCE as a single
        collect with its own pre-check in the same tick:
        survivors land, refusals stay due (their clock did not move)."""
        kamis = [int(pc.decision.parameters["kami"]) for pc in entries]
        n = len(kamis)
        first = entries[0]
        targets = [(pc.book, pc.basis, pc.degraded, pc.rt) for pc in entries]
        self._ticking = first.book.id
        seen: set[int] = set()
        try:
            records = self._submit(targets, [self._collect_intent(account, kamis, first.book.id)], first.book.config, on_broadcast=self._collect_broadcast_writer(entries, n, False, seen))
        finally:
            self._ticking = None
        if not records:
            for pc in entries:
                self._save_runtime(pc.book)
            return None
        rec = records[0]
        rec.batchSize = n
        self._record_collect(entries, rec, n, fallback=False, seen=seen)
        if n > 1 and not self._landed(rec):
            self._log(first.book.id, f"batched collect of {n} did not land ({rec.status}{'; pre-check reverted' if rec.precheck == 'reverted' else ''}); re-offering each member as a single collect once")
            for pc in entries:
                if pc.book.state != "running":
                    continue
                k = int(pc.decision.parameters["kami"])
                self._ticking = pc.book.id
                one: set[int] = set()
                try:
                    singles = self._submit([(pc.book, pc.basis, pc.degraded, pc.rt)], [self._collect_intent(account, [k], pc.book.id)], pc.book.config, on_broadcast=self._collect_broadcast_writer([pc], 1, True, one))
                finally:
                    self._ticking = None
                if not singles:
                    self._save_runtime(pc.book)
                    continue
                singles[0].batchSize = 1
                self._record_collect([pc], singles[0], 1, fallback=True, seen=one)
        return rec

    def _collect_broadcast_writer(self, entries: list[PooledCollect], n: int, fallback: bool, seen: set[int]) -> Callable[[list[Any]], None]:
        """The `on_broadcast` callback of a pooled collect — one
        `stage: broadcast` row per BOOK of the batch (its own `kami`,
        `batchSize`, the shared hash), `lastTxHash` at this stage."""

        def on_broadcast(records: list[Any]) -> None:
            for rec in records:
                rec.batchSize = n
                seen.add(id(rec))
                for pc in entries:
                    row = {**rec.as_ledger_tx(pc.book.id, stage="broadcast"), "kami": int(pc.decision.parameters["kami"])}
                    if fallback:
                        row["batchFallback"] = True
                    self._ledger(pc.book).tx(pc.basis, row, pc.degraded, dry_run=(rec.status == "dry_run"))
                    if rec.txHash:
                        pc.rt["lastTxHash"] = rec.txHash

        return on_broadcast

    def _record_collect(self, entries: list[PooledCollect], rec: Any, n: int, fallback: bool, seen: set[int] | None = None) -> None:
        """One `tx` row per book for a collect record (batched or single),
        the per-book runtime, the plan, the strategy's clock rule. When the
        record was reported at the broadcast stage (`seen`) this is its
        `stage: receipt` row — none for a `not_sent` record, whose one row
        is the broadcast one. An `unconfirmed` receipt
        row leaves the step OPEN in every book of the batch, each closed at
        its own book's next tick."""
        decoded: dict[int, dict[str, Any]] = {}
        if rec.status == "success":
            decoded = decode_musu_collected(rec.logs, [int(pc.decision.parameters["kami"]) for pc in entries])
        landed = self._landed(rec)
        staged = seen is not None and id(rec) in seen
        for pc in entries:
            k = int(pc.decision.parameters["kami"])
            row: dict[str, Any] = {**rec.as_ledger_tx(pc.book.id, stage="receipt" if staged else None), "kami": k}
            if fallback:
                row["batchFallback"] = True
            if rec.status == "success":
                row["musuCollected"] = decoded[k]["musuCollected"]
                if decoded[k]["decodeError"]:
                    row["decodeError"] = decoded[k]["decodeError"]
            elif rec.status == "unconfirmed":
                row["decodeError"] = "no receipt within the receipt timeout"
            if not staged or rec.txHash:
                written = self._ledger(pc.book).tx(pc.basis, row, pc.degraded, dry_run=(rec.status == "dry_run"))
                if staged and rec.txHash and rec.status == "unconfirmed":  # one open entry per BOOK of the batch
                    self._register_open_tx(pc.rt, rec.txHash, written)
            if rec.txHash:
                pc.rt["lastTxHash"] = rec.txHash
            pc.rt["lastActAt"] = pc.now
            pc.rt["ticksSinceAct"] = 0
            if landed and getattr(rec, "block", None):  # the bar
                pc.rt["lastActBlock"] = max(int(pc.rt.get("lastActBlock") or 0), int(rec.block))
            if landed:
                pc.strategy.apply_plan(pc.decision)
                if fallback:
                    pc.rt["lastError"] = None
            else:
                pc.rt["lastError"] = f"collect did not land ({rec.status}: {rec.reason or 'reverted on chain'})"
            pc.strategy.on_records([pc.decision], [rec], pc.now, pc.rt)
            self.store.save(pc.book)
            self._save_runtime(pc.book)
            collected = row.get("musuCollected")
            self._log(pc.book.id, f"acted: collect ({'single after the batch' if fallback else 'batch of ' + str(n)}, {'dry run' if self.dry_run else 'signed'}; {rec.status}{'; musu ' + str(collected) if collected is not None else ''})")

    # ------------------------------------------------- the cross-book stops

    def _flush_stops(self) -> list[Any]:
        """Every stop pooled since the last flush goes out per
        account+node as chunked `executeBatchedAllowFailure` stops of up to
        BATCH_CAPS stop (12) kamis — whole decisions packed in arrival
        order, first fit — so the books of one node that a kill made due in
        the same pass react in ONE burst instead of N serial ones. One `tx`
        row per BOOK of each chunk, sharing the hash, each with
        `batchSize`. A book stopped between its tick and the flush leaves
        the pool (logged, no row). Called under the lock, BEFORE
        `_flush_collects`. Returns the records sent."""
        pool, self._stop_pool = self._stop_pool, {}
        cap = int(BATCH_CAPS["system.harvest.stop"])
        sent: list[Any] = []
        for (account, node), entries in pool.items():
            live: list[PooledStop] = []
            for pc in entries:
                if pc.book.state == "running":
                    live.append(pc)
                else:
                    self._log(pc.book.id, f"stop not sent: the book is {pc.book.state}")
            # whole decisions, first fit in arrival order: a decision is
            # already capped at 12, so it is never split across chunks
            chunks: list[list[PooledStop]] = []
            held: list[int] = []
            for pc in live:
                n = len(_stop_kamis(pc.decision))
                for i, h in enumerate(held):
                    if h + n <= cap:
                        chunks[i].append(pc)
                        held[i] = h + n
                        break
                else:
                    chunks.append([pc])
                    held.append(n)
            for chunk in chunks:
                rec = self._send_stop_batch(account, int(node), chunk)
                if rec is not None:
                    sent.append(rec)
        return sent

    def _stop_intent(self, account: str, kamis: list[int], book_id: str) -> Intent:
        if len(kamis) == 1:
            return Intent("system.harvest.stop", account, kamis[0], {"count": 1}, book_id)
        return Intent("system.harvest.stop", account, None, {"kamiIndices": list(kamis), "count": len(kamis)}, book_id)

    def _send_stop_batch(self, account: str, node: int, entries: list[PooledStop]) -> Any | None:
        """One chunked stop for these books' decisions; when the CHUNK does
        not land (pre-check refused, node refused, reverted) and it held
        more than one kami, each pooled DECISION is re-offered ONCE as its
        own stop with its own pre-check in the same tick (as a book's own
        batched start / stop is): survivors land, refusals stay due. A
        multi-kami decision is split through `strategy.split_batch`, so the
        strategy's split rule holds — a JOINER of a pack that did not land is not
        re-offered as a single."""
        kamis = [k for pc in entries for k in _stop_kamis(pc.decision)]
        n = len(kamis)
        first = entries[0]
        targets = [(pc.book, pc.basis, pc.degraded, pc.rt) for pc in entries]
        self._ticking = first.book.id
        seen: set[int] = set()
        try:
            records = self._submit(targets, [self._stop_intent(account, kamis, first.book.id)], first.book.config, on_broadcast=self._stop_broadcast_writer(entries, n, False, seen))
        finally:
            self._ticking = None
        if not records:
            for pc in entries:
                self._save_runtime(pc.book)
            return None
        rec = records[0]
        rec.batchSize = n
        self._record_stop(entries, rec, n, fallback=False, seen=seen)
        if n > 1 and not self._landed(rec):
            self._log(first.book.id, f"batched stop of {n} did not land ({rec.status}{'; pre-check reverted' if rec.precheck == 'reverted' else ''}); re-offering each pooled decision as its own stop once")
            for pc in entries:
                if pc.book.state != "running":
                    continue
                splitter = getattr(pc.strategy, "split_batch", None)
                singles = splitter(pc.decision) if splitter is not None else [pc.decision]
                for sd in singles:
                    sk = _stop_kamis(sd)
                    sub = PooledStop(pc.book, pc.strategy, sd, pc.basis, pc.degraded, pc.now, pc.rt)
                    self._ticking = pc.book.id
                    one: set[int] = set()
                    try:
                        got = self._submit([(pc.book, pc.basis, pc.degraded, pc.rt)], [self._stop_intent(account, sk, pc.book.id)], pc.book.config, on_broadcast=self._stop_broadcast_writer([sub], len(sk), True, one))
                    finally:
                        self._ticking = None
                    if not got:
                        self._save_runtime(pc.book)
                        continue
                    got[0].batchSize = len(sk)
                    self._record_stop([sub], got[0], len(sk), fallback=True, seen=one)
        return rec

    def _stop_broadcast_writer(self, entries: list[PooledStop], n: int, fallback: bool, seen: set[int]) -> Callable[[list[Any]], None]:
        """SPEC P5, the two-stage row: the `on_broadcast` callback
        of a pooled stop — one `stage: broadcast` row per BOOK of the chunk
        (its own `kamis`, the chunk's `batchSize`, the shared hash),
        `lastTxHash` at this stage."""

        def on_broadcast(records: list[Any]) -> None:
            for rec in records:
                rec.batchSize = n
                seen.add(id(rec))
                for pc in entries:
                    row = {**rec.as_ledger_tx(pc.book.id, stage="broadcast"), "kamis": _stop_kamis(pc.decision)}
                    if fallback:
                        row["batchFallback"] = True
                    self._ledger(pc.book).tx(pc.basis, row, pc.degraded, dry_run=(rec.status == "dry_run"))
                    if rec.txHash:
                        pc.rt["lastTxHash"] = rec.txHash

        return on_broadcast

    def _record_stop(self, entries: list[PooledStop], rec: Any, n: int, fallback: bool, seen: set[int] | None = None) -> None:
        """One `tx` row per book for a pooled stop record (the chunk or a
        single re-offered after it), the per-book runtime, the plan, the
        strategy's own record rule. Each row carries that book's own
        `kamis`, the CHUNK's `count` and `batchSize`, and `kami` exactly as
        the intent set it — the index when the chunk is one kami of one
        book, null otherwise. A record reported at the broadcast stage
        (`seen`) gets its `stage: receipt` row here; an `unconfirmed`
        receipt row leaves the step OPEN in every book of the chunk."""
        landed = self._landed(rec)
        staged = seen is not None and id(rec) in seen
        for pc in entries:
            row: dict[str, Any] = {**rec.as_ledger_tx(pc.book.id, stage="receipt" if staged else None), "kamis": _stop_kamis(pc.decision)}
            if fallback:
                row["batchFallback"] = True
            if not staged or rec.txHash:
                written = self._ledger(pc.book).tx(pc.basis, row, pc.degraded, dry_run=(rec.status == "dry_run"))
                if staged and rec.txHash and rec.status == "unconfirmed":  # one open entry per BOOK of the chunk
                    self._register_open_tx(pc.rt, rec.txHash, written)
            if rec.txHash:
                pc.rt["lastTxHash"] = rec.txHash
            pc.rt["lastActAt"] = pc.now
            pc.rt["ticksSinceAct"] = 0
            if landed and getattr(rec, "block", None):  # the bar
                pc.rt["lastActBlock"] = max(int(pc.rt.get("lastActBlock") or 0), int(rec.block))
            if landed:
                pc.strategy.apply_plan(pc.decision)
                if fallback:
                    pc.rt["lastError"] = None
            else:
                pc.rt["lastError"] = f"stop did not land ({rec.status}: {rec.reason or 'reverted on chain'})"
            pc.strategy.on_records([pc.decision], [rec], pc.now, pc.rt)
            self.store.save(pc.book)
            self._save_runtime(pc.book)
            self._log(pc.book.id, f"acted: {pc.decision.kind} ({'single after the chunk' if fallback else 'pooled chunk of ' + str(n)}, {len(_stop_kamis(pc.decision))} of its own, {'dry run' if self.dry_run else 'signed'}; {rec.status})")


# ---------------------------------------------------------------- process


def build_keeper(flag_ns: dict[str, Any], env: dict[str, str] | None = None) -> Keeper:
    flags = {k: v for k, v in flag_ns.items() if k not in ("cmd", "configFile", "noEngine")}
    cfg = load_config(flags=flags, env=env, config_file=flag_ns.get("configFile"))
    return Keeper(cfg)


def run_daemon(flag_ns: dict[str, Any]) -> int:
    from .server import KeeperServer

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    keeper = build_keeper(flag_ns)
    keeper.boot()
    server = KeeperServer(keeper.cfg.socketPath, keeper.router, keeper.cfg.requestTimeoutSec)
    server.serve_in_thread()
    log.info("kami-keeper %s profile=%s socket=%s dataDir=%s dryRun=%s signer=%s", KEEPER_VERSION, keeper.profile, keeper.cfg.socketPath, keeper.cfg.dataDir, keeper.dry_run, keeper.signer.kind)
    if keeper.engine_refusals:
        log.warning("engine not started: %s", "; ".join(keeper.engine_refusals))
    elif not flag_ns.get("noEngine"):
        keeper.start_engine()
    stop = threading.Event()

    def _sig(*_: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    try:
        while not stop.is_set():
            stop.wait(0.5)
    finally:
        keeper.stop_engine()
        server.close()
    return 0
