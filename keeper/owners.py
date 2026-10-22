"""The owner ledger: kills (and casts) per node per owner account, what
the owner-class policy (keeper/owner_class.py) reads. Forgiveness, one
window, attacker memory by kami.

Per node, per owner account index: `kills[] {block, txHash, victimKami,
victimAccount, attackerKami}`; per owner `killedAccounts {victimAccount:
lastBlock}` — the block of the owner's LAST kill (or cast) on a kami of
that account, on any node. It counts only inside the window
(`killed_ours_in_window`): there is no permanent class (an older file
stored the FIRST block, see the migration below). It is seeded per
account from the lens `battles` of the book's kamis (`seed_killed`: adds
or RAISES a block, never removes; `seeded` marks the kamis read so a
restart does not re-read). A cast on one of our kamis (KAMI_CAST) is
recorded the same way under `casts[]` and counts as a kill for the class.

Per attacker KAMI: `hostileKamis {attackerKami: {lastBlock,
node, victimAccount, ownerAtKill, nodes {node: lastBlock}, victimAccounts
{account: lastBlock}}}` — written by the same `record_kill` /
`record_cast` / `seed_killed` call for EVERY attributed kill the scanner
sees, whoever the victim is. The first four fields describe the kami's
last kill; `nodes` / `victimAccounts` keep the last block per node and per
victim account, because the read (`kami_hostile_in_window`: killed ANY
victim on this node, or killed OURS on any node, inside the window) cannot
be answered from the last row alone once a kami kills twice. A KAMI_CAST
event names the caster's ACCOUNT, never a kami, so a cast writes a kami
row only when the cast dict carries `casterKami` (the scanner serves
none).

An UNATTRIBUTED kill (`attackerAccount` null: unknown selector, killer
not resolvable) is recorded — it is a row and a tick note in the books —
and changes no owner's class and writes no kami row. Such a row is
re-read from the
chain on a later pass and, when the transaction resolves, PROMOTED into
`kills` with its own `block` / `blockTs`, so the window it opens starts at
the kill and not at the re-attribution.

Persisted to `<dataDir>/owners.json` (atomic temp + rename, like
runtime.json) with `lastScannedBlock`; kills, casts, `killedAccounts` and
`hostileKamis` rows older than the largest `hostileWindowHours` among
running books plus one day are pruned. The window is WALL-CLOCK time, measured through
the block clock (keeper/blockclock.py) — blocks at 2.5 s are only the
fallback, used until the clock has samples.

A row is DATED by its block's own header
timestamp — the chain's fact, not the clock's model — and every window
read compares wall-clock time when the stamp is there. The block floor
stays the fallback for a row without one (recorded by an older build, or
a header read that failed). The added keys are ADDITIVE, so
`schemaVersion` does NOT move: every kill / cast row gains `blockTs`
(int | null), `killedAccountsTs {owner: {victimAccount: lastTs}}` sits
beside `killedAccounts`, and a `hostileKamis` row gains `lastTs`,
`nodesTs {node: ts}` and `victimAccountsTs {victimAccount: ts}` beside
`lastBlock` / `nodes` / `victimAccounts`. A ts entry ALWAYS describes the
row that holds the block entry beside it: the row that raises
a block entry writes its own stamp there, and an UNDATED later kill CLEARS
it, so that entry falls back to the block floor instead of being judged by
an older row's date. An older build after a rollback
keeps LOADING the file (`load()` refuses only a NEWER version, which
would empty the hostile memory) and simply drops the keys it does not
know at its next save.

The document carries its own
`schemaVersion` (OWNERS_SCHEMA_VERSION): an older file loads — its
`killedAccounts` blocks are FIRST-kill blocks, kept as they are (they are
at or below the truth, so they can only forgive earlier, never bind
longer) and raised by new rows — and a NEWER file is refused: the ledger
starts empty and the first save keeps the refused file aside.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import time
from pathlib import Path
from typing import Any

from .blockclock import BlockClock
from .kills import BLOCKS_PER_HOUR

OWNERS_FILE = "owners.json"

# The owners document's own version. "0.1.0" = the package stamp the first
# owners format carries: `killedAccounts` held FIRST-kill blocks and there
# was no `hostileKamis`. "0.2.0": LAST-kill blocks + `hostileKamis`.
OWNERS_SCHEMA_VERSION = "0.2.0"


def _version_tuple(v: Any) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OwnerLedger:
    def __init__(self, data_dir: Path | str, block_clock: BlockClock | None = None):
        self.path = Path(data_dir) / OWNERS_FILE
        # the wall-clock window. None = the block arithmetic at the fallback rate.
        self.block_clock = block_clock
        self.last_scanned_block: int | None = None
        # node -> owner account index (str key in JSON) -> [kill]
        self.kills: dict[int, dict[int, list[dict[str, Any]]]] = {}
        self.casts: dict[int, dict[int, list[dict[str, Any]]]] = {}
        # node -> [unattributed kills]
        self.unattributed: dict[int, list[dict[str, Any]]] = {}
        # owner -> {victimAccount: lastBlock} (the LAST kill; a 0.1.0 file
        # holds first-kill blocks, kept as they are)
        self.killed_accounts: dict[int, dict[int, int]] = {}
        # the same pairs by the kill's BLOCK TIMESTAMP — owner ->
        # {victimAccount: lastTs}; a pair recorded without a stamp has no entry
        # here and is judged by the block floor
        self.killed_accounts_ts: dict[int, dict[int, int]] = {}
        # attacker kami -> {lastBlock, node, victimAccount, ownerAtKill,
        # nodes {node: lastBlock}, victimAccounts {account: lastBlock}}
        # and lastTs, nodesTs {node: ts}, victimAccountsTs {account: ts}
        self.hostile_kamis: dict[int, dict[str, Any]] = {}
        self.loaded_version: str | None = None
        self._refused_version: str | None = None
        # the book's account index -> what the lens `battles`
        # seed established, so a restart never re-reads it
        self.seeded: dict[int, dict[str, Any]] = {}
        self.restore: str = "missing"
        self._dirty = False

    # -- persistence ----------------------------------------------------------

    def load(self) -> str:
        if not self.path.exists():
            self.restore = "missing"
            return self.restore
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.restore = f"unreadable: {type(e).__name__}"
            return self.restore
        if not isinstance(doc, dict) or not isinstance(doc.get("owners"), dict):
            self.restore = "unreadable: not an owners document"
            return self.restore
        version = doc.get("schemaVersion")
        vt, mine = _version_tuple(version), _version_tuple(OWNERS_SCHEMA_VERSION)
        if vt is not None and mine is not None and vt > mine:
            # a NEWER document is refused, never half-read: the ledger starts
            # empty and the first save moves the refused file aside
            self._refused_version = str(version)
            self.restore = f"refused: schemaVersion {version} is newer than {OWNERS_SCHEMA_VERSION}"
            return self.restore
        self.loaded_version = str(version) if version is not None else None
        o = doc["owners"]
        lsb = o.get("lastScannedBlock")
        self.last_scanned_block = int(lsb) if isinstance(lsb, int) and not isinstance(lsb, bool) else None
        self.kills = {int(n): {int(a): list(v) for a, v in (per or {}).items()} for n, per in (o.get("kills") or {}).items()}
        self.casts = {int(n): {int(a): list(v) for a, v in (per or {}).items()} for n, per in (o.get("casts") or {}).items()}
        self.unattributed = {int(n): list(v) for n, v in (o.get("unattributed") or {}).items()}
        self.killed_accounts = {int(a): {int(b): int(blk) for b, blk in (per or {}).items()} for a, per in (o.get("killedAccounts") or {}).items()}
        self.killed_accounts_ts = {int(a): {int(b): int(ts) for b, ts in (per or {}).items()} for a, per in (o.get("killedAccountsTs") or {}).items()}  # absent in an older file
        self.hostile_kamis = {int(k): self._kami_row_from(v) for k, v in (o.get("hostileKamis") or {}).items() if isinstance(v, dict)}
        self.seeded = {int(a): dict(v) for a, v in (o.get("seeded") or {}).items() if isinstance(v, dict)}
        self.restore = "restored"
        if vt is None or mine is None or vt < mine:
            # migration: an older file's `killedAccounts` blocks are
            # FIRST-kill blocks. Kept as they are — at or below the true last
            # kill, so they can only forgive EARLIER, never bind longer —
            # and raised by new rows; the file is rewritten at this version.
            self.restore = f"restored (migrated from schemaVersion {version})"
            self._dirty = True
        return self.restore

    @staticmethod
    def _kami_row_from(v: dict[str, Any]) -> dict[str, Any]:
        def _int(x: Any) -> int | None:
            return int(x) if isinstance(x, int) and not isinstance(x, bool) else None

        return {
            "lastBlock": int(v.get("lastBlock") or 0),
            "node": _int(v.get("node")),
            "victimAccount": _int(v.get("victimAccount")),
            "ownerAtKill": _int(v.get("ownerAtKill")),
            "nodes": {int(n): int(b) for n, b in (v.get("nodes") or {}).items()},
            "victimAccounts": {int(a): int(b) for a, b in (v.get("victimAccounts") or {}).items()},
            # absent in an older document — the row is then judged by its
            # blocks
            "lastTs": _int(v.get("lastTs")),
            "nodesTs": {int(n): int(t) for n, t in (v.get("nodesTs") or {}).items()},
            "victimAccountsTs": {int(a): int(t) for a, t in (v.get("victimAccountsTs") or {}).items()},
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "lastScannedBlock": self.last_scanned_block,
            "kills": {str(n): {str(a): v for a, v in per.items()} for n, per in self.kills.items()},
            "casts": {str(n): {str(a): v for a, v in per.items()} for n, per in self.casts.items()},
            "unattributed": {str(n): v for n, v in self.unattributed.items()},
            "killedAccounts": {str(a): {str(b): blk for b, blk in per.items()} for a, per in self.killed_accounts.items()},
            "killedAccountsTs": {str(a): {str(b): ts for b, ts in per.items()} for a, per in self.killed_accounts_ts.items()},
            "hostileKamis": {str(k): {"lastBlock": r["lastBlock"], "node": r["node"], "victimAccount": r["victimAccount"], "ownerAtKill": r["ownerAtKill"], "nodes": {str(n): b for n, b in r["nodes"].items()}, "victimAccounts": {str(a): b for a, b in r["victimAccounts"].items()}, "lastTs": r.get("lastTs"), "nodesTs": {str(n): t for n, t in (r.get("nodesTs") or {}).items()}, "victimAccountsTs": {str(a): t for a, t in (r.get("victimAccountsTs") or {}).items()}} for k, r in self.hostile_kamis.items()},
            "seeded": {str(a): dict(v) for a, v in self.seeded.items()},
        }

    def save(self) -> bool:
        """Atomic write when something changed. Returns True when written."""
        if not self._dirty:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._refused_version is not None and self.path.exists():
            # never write over a document this build refused to read
            self.path.replace(self.path.with_name(f"{OWNERS_FILE}.v{self._refused_version}.refused"))
            self._refused_version = None
        doc = {"schemaVersion": OWNERS_SCHEMA_VERSION, "savedAt": _now_iso(), "owners": self.snapshot()}
        tmp = self.path.with_name(OWNERS_FILE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        self._dirty = False
        return True

    # -- writes ---------------------------------------------------------------

    def set_last_scanned(self, block: int | None) -> None:
        if block is not None and block != self.last_scanned_block:
            self.last_scanned_block = int(block)
            self._dirty = True

    def record_kill(self, kill: dict[str, Any]) -> bool:
        """Record one scanner kill dict; False when already known (same tx
        + log index)."""
        node = int(kill["node"])
        attacker = kill.get("attackerAccount")
        row = {k: kill.get(k) for k in ("block", "txHash", "logIndex", "victimKami", "victimAccount", "attackerKami", "attackerAccount", "attribution", "blockTs")}
        if attacker is None:
            bucket = self.unattributed.setdefault(node, [])
        else:
            bucket = self.kills.setdefault(node, {}).setdefault(int(attacker), [])
        if any(r.get("txHash") == row["txHash"] and r.get("logIndex") == row["logIndex"] for r in bucket):
            return False
        bucket.append(row)
        block = int(row["block"] or 0)
        ts = row["blockTs"]  # the chain's own date for this row, None when unread
        victim_account = int(kill["victimAccount"]) if kill.get("victimAccount") is not None else None
        if attacker is not None and victim_account is not None:
            self._raise_killed(int(attacker), victim_account, block, ts)
        if attacker is not None and kill.get("attackerKami") is not None:
            self._raise_kami(int(kill["attackerKami"]), block, node, victim_account, int(attacker), ts)
        self._dirty = True
        return True

    # -- re-attribution of a stored unattributed row --------------------------

    def unattributed_rows(self) -> list[tuple[int, dict[str, Any]]]:
        """Every stored unattributed kill row as (node, row), oldest
        block first. Pure read: never marks the ledger dirty."""
        out: list[tuple[int, dict[str, Any]]] = []
        for node, rows in self.unattributed.items():
            out.extend((int(node), r) for r in rows)
        return sorted(out, key=lambda nr: (int(nr[1].get("block") or 0), int(nr[1].get("logIndex") or 0)))

    def promote_unattributed(self, node: int, tx_hash: str, log_index: int | None, attacker_kami: int | None, attacker_account: int, attribution: str) -> dict[str, Any] | None:
        """Move the unattributed row (`tx_hash`, `log_index`) of `node`
        into `kills[node][attacker_account]` with the attacker filled in, and
        raise `killedAccounts` / `hostileKamis` from the row's OWN `block` and
        `blockTs` — never from now. Returns the promoted row, or None when
        this node holds no such unattributed row, or when the attacker's
        bucket already holds this exact log (then the unattributed row is
        still removed: the event is attributed)."""
        rows = self.unattributed.get(int(node), [])
        stored = next((r for r in rows if r.get("txHash") == tx_hash and r.get("logIndex") == log_index), None)
        if stored is None:
            return None
        rows.remove(stored)
        if not rows:
            del self.unattributed[int(node)]
        row = {k: stored.get(k) for k in ("block", "txHash", "logIndex", "victimKami", "victimAccount", "attackerKami", "attackerAccount", "attribution", "blockTs")}
        row["attackerKami"], row["attackerAccount"], row["attribution"] = attacker_kami, int(attacker_account), attribution
        bucket = self.kills.setdefault(int(node), {}).setdefault(int(attacker_account), [])
        if any(r.get("txHash") == row["txHash"] and r.get("logIndex") == row["logIndex"] for r in bucket):
            # the attacker's bucket already holds this exact log: nothing is
            # appended and no block is raised, but the unattributed row is
            # gone for good — the event IS attributed
            self._dirty = True
            return None
        bucket.append(row)
        block = int(row["block"] or 0)
        ts = row["blockTs"]  # the chain's own date for this row, None when unread
        victim_account = int(stored["victimAccount"]) if stored.get("victimAccount") is not None else None
        if victim_account is not None:
            self._raise_killed(int(attacker_account), victim_account, block, ts)
        if attacker_kami is not None:
            self._raise_kami(int(attacker_kami), block, int(node), victim_account, int(attacker_account), ts)
        self._dirty = True
        return row

    def record_cast(self, cast: dict[str, Any]) -> bool:
        node = int(cast["node"])
        caster = cast.get("casterAccount")
        if caster is None:
            return False
        row = {k: cast.get(k) for k in ("block", "txHash", "logIndex", "targetKami", "targetAccount", "casterAccount", "item", "blockTs")}
        bucket = self.casts.setdefault(node, {}).setdefault(int(caster), [])
        if any(r.get("txHash") == row["txHash"] and r.get("logIndex") == row["logIndex"] for r in bucket):
            return False
        bucket.append(row)
        block = int(row["block"] or 0)
        ts = row["blockTs"]
        target_account = int(cast["targetAccount"]) if cast.get("targetAccount") is not None else None
        if target_account is not None:
            self._raise_killed(int(caster), target_account, block, ts)
        if cast.get("casterKami") is not None:
            # a KAMI_CAST event names the caster's ACCOUNT only; the kami row
            # is written when a caller can name the kami (none does)
            self._raise_kami(int(cast["casterKami"]), block, node, target_account, int(caster), ts)
        self._dirty = True
        return True

    def _raise_killed(self, owner: int, victim_account: int, block: int, ts: int | None = None) -> bool:
        """`killedAccounts[owner][victimAccount]` = the LAST block: a
        new row raises it, never lowers it. The ts entry
        ALWAYS describes the row that holds the block entry — a row that
        raises the block writes its own `ts` into `killedAccountsTs` or, when
        it has none, CLEARS that entry (the pair falls back to the block
        floor); a row that does not raise the block touches neither table,
        however late its stamp. Returns whether the BLOCK was raised."""
        per = self.killed_accounts.setdefault(int(owner), {})
        if int(victim_account) in per and int(block) <= per[int(victim_account)]:
            return False
        per[int(victim_account)] = int(block)
        if ts is not None:
            self.killed_accounts_ts.setdefault(int(owner), {})[int(victim_account)] = int(ts)
        else:
            per_ts = self.killed_accounts_ts.get(int(owner))
            if per_ts is not None:
                per_ts.pop(int(victim_account), None)
                if not per_ts:
                    del self.killed_accounts_ts[int(owner)]
        self._dirty = True
        return True

    def _raise_kami(self, kami: int, block: int, node: int | None, victim_account: int | None, owner: int | None, ts: int | None = None) -> None:
        """Every ts entry ALWAYS describes the row that holds
        the block entry beside it — `lastTs` follows `lastBlock`,
        `nodesTs[node]` follows `nodes[node]` and `victimAccountsTs[acct]`
        follows `victimAccounts[acct]`, each SET to this row's `ts` when this
        row's block takes that entry and CLEARED when the row that takes it has
        no stamp; a row whose block does not take it touches neither table."""
        row = self.hostile_kamis.setdefault(int(kami), {"lastBlock": -1, "node": None, "victimAccount": None, "ownerAtKill": None, "nodes": {}, "victimAccounts": {}, "lastTs": None, "nodesTs": {}, "victimAccountsTs": {}})

        def _stamp(ts_key: str, key: int) -> None:
            per = row.setdefault(ts_key, {})
            if ts is not None:
                per[int(key)] = int(ts)
            else:
                per.pop(int(key), None)

        if int(block) >= int(row["lastBlock"]):
            row.update({"lastBlock": int(block), "node": (int(node) if node is not None else None), "victimAccount": (int(victim_account) if victim_account is not None else None), "ownerAtKill": (int(owner) if owner is not None else None)})
            row["lastTs"] = int(ts) if ts is not None else None
        if node is not None and int(block) >= int(row["nodes"].get(int(node), -1)):
            row["nodes"][int(node)] = int(block)  # on a tie the incoming row wins, as `max` did
            _stamp("nodesTs", int(node))
        if victim_account is not None and int(block) >= int(row["victimAccounts"].get(int(victim_account), -1)):
            row["victimAccounts"][int(victim_account)] = int(block)
            _stamp("victimAccountsTs", int(victim_account))
        self._dirty = True

    def prune(self, keep_hours: int, head: int | None) -> int:
        """Drop kills / casts / `killedAccounts` / `hostileKamis` rows older
        than `keep_hours` (the largest window among running books plus one
        day) below `head` (nothing in the ledger is kept
        for ever). Returns the count dropped. A dated row is judged by
        wall-clock time; an undated one by the block floor — so with no head
        the dated rows are still judged and the undated ones are all kept."""
        if not (self.kills or self.casts or self.unattributed or self.killed_accounts or self.hostile_kamis):
            return 0
        dropped = 0
        for table in (self.kills, self.casts):
            for node in list(table):
                for owner in list(table[node]):
                    kept = [r for r in table[node][owner] if self._inside(r.get("block"), r.get("blockTs"), keep_hours, head)]
                    dropped += len(table[node][owner]) - len(kept)
                    if kept:
                        table[node][owner] = kept
                    else:
                        del table[node][owner]
                if not table[node]:
                    del table[node]
        for node in list(self.unattributed):
            kept = [r for r in self.unattributed[node] if self._inside(r.get("block"), r.get("blockTs"), keep_hours, head)]
            dropped += len(self.unattributed[node]) - len(kept)
            if kept:
                self.unattributed[node] = kept
            else:
                del self.unattributed[node]
        for owner in list(self.killed_accounts):
            per = self.killed_accounts[owner]
            per_ts = self.killed_accounts_ts.get(owner) or {}
            for acct in [a for a, blk in per.items() if not self._inside(blk, per_ts.get(a), keep_hours, head)]:
                del per[acct]
                per_ts.pop(acct, None)  # the ts entry goes with its block entry
                dropped += 1
            if not per:
                del self.killed_accounts[owner]
            if not per_ts:
                self.killed_accounts_ts.pop(owner, None)
        for kami in list(self.hostile_kamis):
            row = self.hostile_kamis[kami]
            if not self._inside(row["lastBlock"], row.get("lastTs"), keep_hours, head):
                del self.hostile_kamis[kami]
                dropped += 1
                continue
            for key, ts_key in (("nodes", "nodesTs"), ("victimAccounts", "victimAccountsTs")):
                per_key_ts = row.get(ts_key) or {}
                for k in [k for k, blk in row[key].items() if not self._inside(blk, per_key_ts.get(k), keep_hours, head)]:
                    del row[key][k]
                    per_key_ts.pop(k, None)
                    dropped += 1
        if dropped:
            self._dirty = True
        return dropped

    # -- the lens `battles` seed ----------------------------------------------

    def seed_killed(self, owner: int, our_account: int, block: int | None, attacker_kami: int | None = None, node: int | None = None, ts: int | None = None) -> bool:
        """Record that `owner` killed one of `our_account`'s kamis at
        `block` (a lens `battles` row with isDeath true): adds the pair or
        RAISES its block to the later one (the LAST kill is what the
        window is measured from), never removes, and `kills` (the node
        window) is never touched. With `attacker_kami` the killer's kami
        row is raised too. The caller seeds only deaths inside the window
        plus one day. Returns True when a new owner / account pair
        was added. `ts` — the battles row's own `timestamp`, when it
        carries one — dates the seeded entry exactly as a scanned row's block
        header does."""
        blk = int(block or 0)
        new_pair = int(our_account) not in self.killed_accounts.get(int(owner), {})
        self._raise_killed(int(owner), int(our_account), blk, ts)
        if attacker_kami is not None:
            self._raise_kami(int(attacker_kami), blk, node, int(our_account), int(owner), ts)
        return new_pair

    def seeded_kamis(self, our_account: int) -> set[int]:
        entry = self.seeded.get(int(our_account)) or {}
        return {int(k) for k in entry.get("kamis") or []}

    def unread_for_seed(self, our_account: int, kamis: list[int]) -> list[int]:
        """The book's kamis whose `battles` were never read for this
        account (a restart or a second book of the account re-reads none)."""
        done = self.seeded_kamis(our_account)
        return [int(k) for k in kamis if int(k) not in done]

    def is_seeded(self, our_account: int) -> bool:
        return int(our_account) in self.seeded

    def mark_seeded(self, our_account: int, kamis_read: list[int], info: dict[str, Any]) -> None:
        """Merge `kamis_read` into the account's seed marker (persisted)."""
        entry = self.seeded.setdefault(int(our_account), {"kamis": []})
        have = {int(k) for k in entry.get("kamis") or []}
        entry["kamis"] = sorted(have | {int(k) for k in kamis_read})
        for k, v in info.items():
            entry[k] = v
        self._dirty = True

    # -- reads (what the class policy asks) ------------------------------------

    def killed_here_in_window(self, owner: int, node: int, window_hours: int, head: int | None) -> bool:
        """A liquidate (or cast) by `owner` on `node` inside the window
        (wall-clock hours through the block clock, blocks below `head` without
        one); with no head every recorded kill counts. A dated row is
        judged by wall-clock time; an undated one by the block floor."""
        for table in (self.kills, self.casts):
            for r in table.get(int(node), {}).get(int(owner), []):
                if self._inside(r.get("block"), r.get("blockTs"), window_hours, head):
                    return True
        return False

    def _floor(self, window_hours: int, head: int | None) -> int:
        """The block the window starts at: the block clock's wall-clock
        answer when one is set, else `head` minus the window in blocks
        at the fallback rate."""
        if self.block_clock is not None:
            return self.block_clock.floor(window_hours, head)
        return (int(head) - int(window_hours) * BLOCKS_PER_HOUR) if head is not None else 0

    def _now(self) -> float:
        """Wall-clock now: the block clock's injected clock when one is
        set — the keeper's own, which the tests move — else `time.time()`."""
        if self.block_clock is not None:
            return float(self.block_clock.clock())
        return time.time()

    def _inside(self, block: int | None, ts: int | None, window_hours: int, head: int | None) -> bool:
        """THE window rule. A row DATED by its block's own header
        timestamp is judged by WALL-CLOCK time and the head is not consulted
        at all — the chain has the fact, the block clock is only a model. An
        undated row (recorded without a stamp, or a header read that failed)
        keeps the block rule: with no head every row counts, else its block
        against the clock's floor."""
        if ts is not None:
            return float(ts) >= self._now() - float(window_hours) * 3600.0
        if head is None:
            return True
        return int(block or 0) >= self._floor(window_hours, head)

    def killed_ours_in_window(self, owner: int, our_account: int, window_hours: int, head: int | None) -> bool:
        """`owner`'s LAST kill / cast on a kami of `our_account` (any node)
        is inside the window (forgiveness: there is no permanent class);
        with no head every recorded row counts. A
        dated row is judged by wall-clock time; an undated one by the block
        floor."""
        blk = self.killed_accounts.get(int(owner), {}).get(int(our_account))
        if blk is None:
            return False
        return self._inside(blk, self.killed_accounts_ts.get(int(owner), {}).get(int(our_account)), window_hours, head)

    def kami_hostile_in_window(self, kami: int, node: int, our_account: int, window_hours: int, head: int | None) -> bool:
        """The attacker KAMI itself, whoever owns it now:
        it killed ANY victim on `node`, or killed OURS on any node, inside
        the window; with no head every recorded row counts. A dated row
        is judged by wall-clock time; an undated one by the block floor — the
        per-key stamps (`nodesTs` / `victimAccountsTs`), never `lastTs`."""
        row = self.hostile_kamis.get(int(kami))
        if row is None:
            return False
        for blk, ts in ((row["nodes"].get(int(node)), (row.get("nodesTs") or {}).get(int(node))), (row["victimAccounts"].get(int(our_account)), (row.get("victimAccountsTs") or {}).get(int(our_account)))):
            if blk is not None and self._inside(blk, ts, window_hours, head):
                return True
        return False

    def known(self, tx_hash: str | None, log_index: int | None) -> bool:
        """Is this exact log already a row of the ledger — a kill (any
        node, any owner), an unattributed kill (any node) or a cast (any
        node, any caster)? The kill scan's overlap re-read asks before it
        reads the transaction, so a row re-read behind the cursor costs
        nothing but the `eth_getLogs` it rode in on. Pure read: it never
        marks the ledger dirty."""
        if tx_hash is None:
            return False
        for table in (self.kills, self.casts):
            for per_owner in table.values():
                for rows in per_owner.values():
                    for r in rows:
                        if r.get("txHash") == tx_hash and r.get("logIndex") == log_index:
                            return True
        for rows in self.unattributed.values():
            for r in rows:
                if r.get("txHash") == tx_hash and r.get("logIndex") == log_index:
                    return True
        return False

    def kills_on(self, node: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for owner, rows in self.kills.get(int(node), {}).items():
            out.extend(dict(r, attackerAccount=owner) for r in rows)
        out.extend(dict(r) for r in self.unattributed.get(int(node), []))
        return sorted(out, key=lambda r: (int(r.get("block") or 0), int(r.get("logIndex") or 0)))

    def hostile_owners(self, node: int, window_hours: int, head: int | None, our_account: int | None = None) -> set[int]:
        """Owners with a kill / cast on `node` inside the window and, with
        `our_account`, owners whose last kill of OURS (any node) is inside
        it — the one window (the preview cache's "remember while
        HOSTILE" input)."""
        owners = set(self.kills.get(int(node), {})) | set(self.casts.get(int(node), {}))
        out = {o for o in owners if self.killed_here_in_window(o, node, window_hours, head)}
        if our_account is not None:
            out |= {o for o in self.killed_accounts if self.killed_ours_in_window(o, our_account, window_hours, head)}
        return out

    def hostile_kamis_for(self, node: int, our_account: int, window_hours: int, head: int | None) -> set[int]:
        """Attacker kamis hostile in the window against this node / account
        (the preview cache's "remember the KAMI" input)."""
        return {k for k in self.hostile_kamis if self.kami_hostile_in_window(k, node, our_account, window_hours, head)}
