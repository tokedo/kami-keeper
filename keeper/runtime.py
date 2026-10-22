"""SPEC P5 / P8 — per-book runtime persistence.

`Keeper.runtime[book.id]` holds what a book has done between ticks:
the collect clock (`lastCollectAt`), the feed clock and the feed
idempotence guard (`lastFeedAt`, `feedGuard` with `lastFeedHp` /
`expectedHpAfter`), the craft counters (`crafted`, `parked`), the P7
counters (`contradictionTicks`, `contradiction`, `matchingSinceBlock`),
an `auto` book's `startQueue` (kami -> the clock at which it was first
read ready to start), a `harvestAndFeed` book's drawn collect time
(`nextCollectAt`, `collectIntervalSec`, `collectDraws`), and a ladder
book's rung (`ladder`).
Why persisted: held only in the process, every collect timer would reset
to the restart time, so a book restarted often would collect a full
interval after its LAST restart instead of after its start.

Now the persisted subset is written to `<dataDir>/books/<id>/runtime.json`
atomically (temp + rename) whenever it changes, and restored on boot
before the book's `resumed` row is written; that row states what was
restored (`runtime.restore`: `restored` | `missing` | `unreadable: <why>`,
and the restored `lastCollectAt`). A missing or unreadable file falls
back to the clocks starting at the next tick, and
says so in the row.

Not persisted, by design: the tick bookkeeping (`nextTickAt`,
`lastTickAt`, `ticksSinceAct`, `lastTickKey`, `ticksSinceTickRow`), the
last error / degraded flags (re-derived on the next tick), the
per-tick scratch (`withheld`, `lastSummary`). `lastActAt` / `lastTxHash`
are persisted so `status` after a restart still names the last act.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION

RUNTIME_FILE = "runtime.json"

PERSISTED_KEYS: tuple[str, ...] = (
    "lastCollectAt",
    # `harvestAndFeed`: the drawn collect time, the draw
    # it came from and the draw counter (the generator's seed is book id +
    # counter) — a restart never redraws and never resets the collect
    "nextCollectAt",
    "collectIntervalSec",
    "collectDraws",
    "lastFeedAt",
    "feedGuard",
    "crafted",
    "parked",
    "staminaMin",  # learned per-recipe stamina floor
    "contradictionTicks",
    "contradiction",
    "matchingSinceBlock",
    "lastActAt",
    "lastActBlock",  # the receipt block of the last landed tx — the freshness bar a read must reach (survives a restart)
    "lastTxHash",
    # `auto`: a kami the chain refused to start / stop waits
    # out `startBackoffSec`; the last drain rate seen per kami feeds the
    # hold rule for a resting kami
    "backoff",
    "drainRate",
    # `auto`: the start queue — per kami, the clock at which the
    # keeper first read it ready to start, so a restart does not reset a
    # `startBatchWaitSec` wait
    "startQueue",
    # `harvestAndFeed` ladder mode: the rung in force —
    # {item, heal, steppedAt} — so a restart keeps the step clock and the
    # stale / chain branches keep feeding the rung the node last justified
    "ladder",
    # `auto` REST: kami -> the clock of its last chain-backed
    # floor-rest attempt — one attempt per kami per `chainFeedCooldownSec`,
    # kept across a keeper restart
    "chainRests",
)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def runtime_path(book_dir: Path) -> Path:
    return Path(book_dir) / RUNTIME_FILE


def snapshot(rt: dict[str, Any]) -> dict[str, Any]:
    """The persisted subset of a runtime dict (keys present only)."""
    return {k: rt[k] for k in PERSISTED_KEYS if k in rt and rt[k] is not None}


def save_runtime(book_dir: Path, book_id: str, rt: dict[str, Any]) -> bool:
    """Write the snapshot when it differs from the last one written
    (tracked on the dict itself under `_persistedKey`); atomic temp +
    rename. Returns True when a file was written."""
    snap = snapshot(rt)
    key = json.dumps(snap, sort_keys=True, separators=(",", ":"), default=str)
    if rt.get("_persistedKey") == key:
        return False
    d = Path(book_dir)
    d.mkdir(parents=True, exist_ok=True)
    doc = {"schemaVersion": SCHEMA_VERSION, "bookId": book_id, "savedAt": _now_iso(), "runtime": snap}
    tmp = d / (RUNTIME_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc, indent=1, sort_keys=True, default=str))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(d / RUNTIME_FILE)
    rt["_persistedKey"] = key
    return True


def load_runtime(book_dir: Path, book_id: str) -> tuple[dict[str, Any] | None, str]:
    """(snapshot, status): status is `restored`, `missing`, or
    `unreadable: <ClassName or reason>`. Only PERSISTED_KEYS are taken
    from the file, whatever else it carries."""
    p = runtime_path(book_dir)
    if not p.exists():
        return None, "missing"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"unreadable: {type(e).__name__}"
    if not isinstance(doc, dict) or not isinstance(doc.get("runtime"), dict):
        return None, "unreadable: not a runtime document"
    if doc.get("bookId") not in (None, book_id):
        return None, f"unreadable: bookId mismatch ({doc.get('bookId')})"
    snap = {k: v for k, v in doc["runtime"].items() if k in PERSISTED_KEYS}
    return snap, "restored"


def restore_into(rt: dict[str, Any], snap: dict[str, Any]) -> None:
    """Apply a loaded snapshot onto a fresh runtime dict and mark it as
    the last persisted state (so the next save writes only on change)."""
    rt.update(snap)
    rt["_persistedKey"] = json.dumps(snapshot(rt), sort_keys=True, separators=(",", ":"), default=str)


def iso(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
