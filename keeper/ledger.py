"""SPEC P5 — the append-only per-book ledger.

`<dataDir>/books/<id>/ledger.jsonl`, one JSON object per line, every row
versioned with `schemaVersion` and carrying the perception basis it acted
on (`lensBlock`, `lensReconciledThrough`, `lensAppliedThrough` — the
1.0.0 applied mark, always null against a 0.6.3 daemon), any
`degraded[]`, and `dryRun`.
Rows are validated against keeper/schemas/ledger-row.json before they are
written, and every string value passes the scrub. The ledger is the
attribution primitive: every keeper-signed tx is a `tx` row joinable by
hash.

A live step is TWO `tx` rows — `stage: broadcast` written
before any receipt is awaited (`unconfirmed`, hash, nonce), and `stage:
receipt` written when the receipt read answered (same hash, the final
status, block / gas / MUSU where they apply; everything the broadcast row
stated is repeated). A `not_sent` record is ONE row at the broadcast
stage. A dry-run row carries no `stage`. `open_tx()` is the broadcast
rows whose hash has no FINAL receipt row yet; `tx_hashes()` stays a set,
so a hash counts once whatever its row count.

A `receipt` row whose status is `unconfirmed` (the receipt
read did not answer) is NOT final — the step stays OPEN, is probed once
per tick, and the receipt the chain finally answers becomes its final
row. So a hash can carry three rows: `broadcast unconfirmed`, `receipt
unconfirmed`, then the closing `receipt` row.

`decision_of_tx()` joins a `tx` row back to the `decision`
row it came from, so a step the chain took while the daemon was down can
have its plan effect applied at boot (`core._close_open_tx`). The join is
this ledger's own order plus `SYSTEM_OF_DECISION`: no row names the
decision it belongs to (the row schema deliberately carries no such key).
"""

from __future__ import annotations

import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Any, Iterator

import jsonschema

from . import SCHEMA_VERSION
from .scrub import assert_no_key_shaped, forbidden_in, ScrubError

ROW_KINDS: tuple[str, ...] = ("tick", "decision", "tx", "death", "lifecycle", "kill", "deviation")
# `parking` — the `park` verb was asked of a running auto book
# (reason `agent: park`, with `kamis` and `node`); the book stays running
# and stops ITSELF with a `stopped_by` row (reason `park complete`,
# carrying `parked`) once the park is done.
LIFECYCLE_EVENTS: tuple[str, ...] = ("started", "stopped_by", "paused", "resumed", "external-change", "readopted", "parking", "failed")
# `revive-retry` (the kami is still DEAD and the revive is
# offered again) and the free-text `revive-refused: <reason>` (an offer
# that did not land), which the row schema matches by prefix
DEATH_OUTCOMES: tuple[str, ...] = ("observed", "revived", "revive-retry", "revive-skipped: no-item", "revive-skipped: disabled")
DEATH_OUTCOME_PREFIXES: tuple[str, ...] = ("revive-refused: ",)

# the system each decision kind signs — the join key between a
# `decision` row and the `tx` rows it produced. A `tx` row does not name
# its decision, so the join is the ledger's own order plus this map; a
# kind that is not here is never joined (the boot plan advance then
# leaves the plan alone and says so in the row).
SYSTEM_OF_DECISION: dict[str, str] = {
    "start": "system.harvest.start",
    "rest": "system.harvest.stop",
    "failsafe-stop": "system.harvest.stop",
    "collect": "system.harvest.collect",
    "feed": "system.kami.use.item",
    "revive": "system.kami.use.item",
    "craft": "system.craft",
}

_SCHEMA_PATH = Path(__file__).with_name("schemas") / "ledger-row.json"
_ROW_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_VALIDATOR = jsonschema.Draft202012Validator(_ROW_SCHEMA)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ints(v: Any) -> list[int]:
    out = []
    for x in v if isinstance(v, list) else [v]:
        if isinstance(x, int) and not isinstance(x, bool):
            out.append(int(x))
    return out


def tx_row_kamis(tx: dict[str, Any]) -> list[int] | None:
    """The members a `tx` row NAMES — its own `kamis` (a pooled stop
    row), else its `kami` (a single step). None when the row names none: a
    BATCHED burst row carries `count`, not the indices, which is why the
    boot plan advance has to read the decision row at all."""
    if isinstance(tx.get("kamis"), list):
        return _ints(tx["kamis"])
    k = tx.get("kami")
    return _ints(k) if isinstance(k, int) and not isinstance(k, bool) else None


def decision_kamis(parameters: dict[str, Any]) -> list[int]:
    """The members a `decision` row's parameters name — `kamis` (a
    batch) else `kami` (a single); empty when it names none."""
    if isinstance(parameters.get("kamis"), list):
        return _ints(parameters["kamis"])
    return _ints(parameters.get("kami"))


def _basis_of(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("lensBlock"), row.get("lensReconciledThrough"), row.get("lensAppliedThrough"))


def _first_decision(rows: list[dict[str, Any]], want: list[int] | None, count: Any) -> dict[str, Any] | None:
    """The first of `rows` (candidates in ledger order) that answers the tx
    row: the same member set, else a set that CONTAINS it (a single
    re-offered after a batch that did not land), else — when the row names
    no member — the same member count."""
    sets = [(r, set(decision_kamis((r.get("decision") or {}).get("parameters") or {}))) for r in rows]
    if want is not None:
        w = set(want)
        for r, ks in sets:
            if ks == w:
                return r
        for r, ks in sets:
            if ks and ks > w:
                return r
        return None
    if isinstance(count, int) and not isinstance(count, bool):
        for r, ks in sets:
            if len(ks) == count:
                return r
    return None


def validate_row(row: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError or ScrubError."""
    _VALIDATOR.validate(row)
    hits = forbidden_in(row)
    if hits:
        raise ScrubError(f"ledger row carries forbidden language at {hits[0][0]}: {hits[0][1]!r}")
    assert_no_key_shaped(row, "ledger row")


class Ledger:
    def __init__(self, book_dir: Path, book_id: str, account: str):
        self.dir = Path(book_dir)
        self.path = self.dir / "ledger.jsonl"
        self.book_id = book_id
        self.account = account
        self._lock = threading.Lock()

    # ---- writing ---------------------------------------------------------

    def _base(self, kind: str, basis: Any, degraded: list[str] | None, dry_run: bool) -> dict[str, Any]:
        return {
            "schemaVersion": SCHEMA_VERSION,
            "kind": kind,
            "at": _now_iso(),
            "bookId": self.book_id,
            "account": self.account,
            "lensBlock": getattr(basis, "lensBlock", None),
            "lensReconciledThrough": getattr(basis, "lensReconciledThrough", None),
            "lensAppliedThrough": getattr(basis, "lensAppliedThrough", None),
            "degraded": list(degraded or []),
            "dryRun": bool(dry_run),
        }

    def append(self, row: dict[str, Any]) -> dict[str, Any]:
        validate_row(row)
        line = json.dumps(row, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return row

    def tick(self, basis: Any, summary: dict[str, Any], degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        row = self._base("tick", basis, degraded, dry_run)
        row["summary"] = summary
        return self.append(row)

    def decision(self, basis: Any, kind: str, parameters: dict[str, Any], degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        row = self._base("decision", basis, degraded, dry_run)
        row["decision"] = {"kind": kind, "parameters": parameters}
        return self.append(row)

    def tx(self, basis: Any, tx: dict[str, Any], degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        row = self._base("tx", basis, degraded, dry_run)
        full = {
            "txHash": None, "nonce": None, "block": None, "status": "dry_run" if dry_run else "unconfirmed",
            "gasUsed": None, "effectiveGasPrice": None, "account": self.account, "kami": None,
            "bookId": self.book_id, "cycleOpenedBy": "keeper", "musuCollected": None, "system": "",
        }
        full.update(tx)
        row["tx"] = full
        return self.append(row)

    def death(self, basis: Any, kami: int, dead_block: int | None, outcome: str, tx_hash: str | None = None, degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        row = self._base("death", basis, degraded, dry_run)
        row["death"] = {"kami": int(kami), "deadBlock": dead_block, "outcome": outcome, "txHash": tx_hash}
        return self.append(row)

    def lifecycle(self, basis: Any, event: str, reason: str | None = None, degraded: list[str] | None = None, dry_run: bool = False, **extra: Any) -> dict[str, Any]:
        row = self._base("lifecycle", basis, degraded, dry_run)
        row["event"] = event
        row["reason"] = reason
        row.update(extra)
        return self.append(row)

    def kill(self, basis: Any, kill: dict[str, Any], degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        """SPEC P5: a liquidate (or a KAMI_CAST) on the book's
        node, from the chain kill scan — `event`, `victimKami`,
        `victimAccount`, `attackerKami`, `attackerAccount` (null when
        unattributed), `block`, `txHash`, `ownerClassAfter` (rendered,
        P3a; null when unattributed), `learned` (null for another
        account's kami): the measurement of the killer's line —
        `hpAtKill`, `served`, `gapHp`, `liftPct`, `lifted[]`, `owner`, or
        `skipped`."""
        row = self._base("kill", basis, degraded, dry_run)
        full = {"event": "liquidate", "victimKami": None, "victimAccount": None, "attackerKami": None, "attackerAccount": None, "block": None, "txHash": None, "ownerClassAfter": None, "learned": None}
        full.update(kill)
        row["kill"] = full
        return self.append(row)

    def deviation(self, basis: Any, detail: dict[str, Any], degraded: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        """SPEC P5: an action the chain refused and what the book
        does about it (`kami`, `action`, `reason`, `backoffSec`, `until`)."""
        row = self._base("deviation", basis, degraded, dry_run)
        row["deviation"] = dict(detail)
        return self.append(row)

    # ---- reading ---------------------------------------------------------

    def rows(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        return self._iter()

    def _iter(self) -> Iterator[dict[str, Any]]:
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield json.loads(line)

    def since(self, block: int | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        """Rows whose lensBlock is >= block (rows with a null basis are
        included when block is None only), optionally filtered by kind."""
        out = []
        for r in self.rows():
            if kind is not None and r.get("kind") != kind:
                continue
            if block is not None:
                lb = r.get("lensBlock")
                if lb is None or lb < block:
                    continue
            out.append(r)
        return out

    def tx_hashes(self) -> set[str]:
        return {r["tx"]["txHash"] for r in self.rows() if r.get("kind") == "tx" and r["tx"].get("txHash")}

    def receipt_row_hashes(self) -> set[str]:
        """The hashes that already carry a `receipt` row, of any
        status. A hash with one reached the act path's receipt stage, which
        is where that path applies the plan of a landed decision
        (`_landed` counts `unconfirmed` as landed), so the boot close of
        such a row must NOT apply it a second time."""
        return {r["tx"]["txHash"] for r in self.rows() if r.get("kind") == "tx" and (r.get("tx") or {}).get("stage") == "receipt" and (r.get("tx") or {}).get("txHash")}

    def decision_of_tx(self) -> dict[str, dict[str, Any]]:
        """Tx hash -> the `decision` row the FIRST `broadcast`
        row of that hash belongs to, from one forward pass over this
        ledger. Hashes with no answer are absent.

        No row names its decision, so the join is three facts the rows do
        carry: the SYSTEM the kind signs (`SYSTEM_OF_DECISION`), the
        perception BASIS (a book writes its decision rows and the tx rows
        of that tick with the same basis, so a candidate must match all
        three basis fields), and the ledger's own ORDER — a decision is
        claimed by the first broadcast row that answers it, so two chunks
        of the same shape in one tick take their own rows. The members
        match exactly where the tx row names them (`kamis` / `kami`), by
        count where it does not (a batched burst row carries `count`). A
        single re-offered after a batch that did not land has no decision
        row of its own: it matches the decision it was split from, which
        stays claimed by the batch row it followed."""
        pending: dict[tuple[str, tuple[Any, Any, Any]], list[dict[str, Any]]] = {}
        seen: dict[tuple[str, tuple[Any, Any, Any]], list[dict[str, Any]]] = {}
        claims: dict[str, dict[str, Any]] = {}
        for r in self.rows():
            kind = r.get("kind")
            if kind == "decision":
                d = r.get("decision") or {}
                system = SYSTEM_OF_DECISION.get(str(d.get("kind")))
                if system:
                    key = (system, _basis_of(r))
                    pending.setdefault(key, []).append(r)
                    seen.setdefault(key, []).append(r)
                continue
            if kind != "tx":
                continue
            t = r.get("tx") or {}
            h = t.get("txHash")
            if t.get("stage") != "broadcast" or not h or h in claims:
                continue
            key = (str(t.get("system") or ""), _basis_of(r))
            want, count = tx_row_kamis(t), t.get("count")
            for pool, consume in ((pending.get(key) or [], True), (seen.get(key) or [], False)):
                hit = _first_decision(pool, want, count)
                if hit is not None:
                    claims[h] = hit
                    if consume:
                        pool.remove(hit)
                    break
        return claims

    def open_tx(self) -> dict[str, dict[str, Any]]:
        """The `broadcast` rows whose hash has no FINAL `receipt`
        row in this ledger, by hash (the first broadcast row of a hash).
        A `receipt` row whose status is `unconfirmed` does NOT
        close the hash — the step is still open (the receipt read did not
        answer; the chain may well have taken it), so it is probed like a
        broadcast row with no receipt row at all. Only a `receipt` row with
        another status (`success`, `reverted`, `not_sent`) closes it. Rows
        without a `stage` (dry run, or written before the two-stage rows) are never open, and a
        `not_sent` broadcast row (no hash) is never open."""
        opened: dict[str, dict[str, Any]] = {}
        closed: set[str] = set()
        for r in self.rows():
            if r.get("kind") != "tx":
                continue
            t = r.get("tx") or {}
            h = t.get("txHash")
            if not h:
                continue
            stage = t.get("stage")
            if stage == "broadcast":
                opened.setdefault(h, r)
            elif stage == "receipt" and t.get("status") != "unconfirmed":
                closed.add(h)
        return {h: r for h, r in opened.items() if h not in closed}
