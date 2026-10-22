"""The block clock: the hostility window is WALL-CLOCK time, and blocks
at a constant 2.5 s are only the fallback.

Yominet's block time varies (daily averages between roughly 2.2 and
2.7 s/block have been observed), so a window counted in blocks drifts by
several hours over 48 h — a class could expire before the attacker's
window really ended.

So the keeper records (wall-clock time, chain head) samples as it runs —
one per kill-scan pass, the `eth_blockNumber` that pass already makes —
and answers "which block was the chain at, `window_hours` ago?" by
interpolating them. `OwnerLedger._floor` and the `KillScanner` first-boot
window ask this clock; kill rows keep their `block` stamps, nothing
migrates, and with no samples every answer is byte-identical to the
arithmetic it replaces.

Persisted to `<dataDir>/blockclock.json` (atomic temp + rename, like
owners.json):

  {"schemaVersion": "0.1.0", "savedAt": "<ISO Z>",
   "samples": [[<float epoch sec>, <int block>], ...]}

Samples are strictly increasing in both time and block, spaced at least
SAMPLE_MIN_SEC apart, kept for RETAIN_SEC and capped at MAX_SAMPLES. The
measured rate is used only over a span of at least MIN_SPAN_SEC; below
it the fallback rate is used. `describe()` is for status only — never for
a decision.
"""

from __future__ import annotations

import bisect
import datetime as _dt
import json
import math
import os
from pathlib import Path
from typing import Any, Callable

from .kills import BLOCK_SECONDS, BLOCKS_PER_HOUR

BLOCKCLOCK_FILE = "blockclock.json"
BLOCKCLOCK_SCHEMA_VERSION = "0.1.0"

# A new sample only when it is at least this far after the last one (the
# window is then accurate to the sample spacing).
SAMPLE_MIN_SEC = 60.0
# Samples older than this are dropped on append (the largest window plus
# slack; nothing asks the clock further back than window + 1 day).
RETAIN_SEC = 8 * 86400.0
MAX_SAMPLES = 20000
# The measured rate is used only over a span of at least one hour.
MIN_SPAN_SEC = 3600.0


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BlockClock:
    """(time, head) samples and the two questions the window asks them:
    `block_at(ts)` and `floor(window_hours, head)`."""

    def __init__(self, data_dir: Path | str, clock: Callable[[], float], default_block_seconds: float = BLOCK_SECONDS):
        self.path = Path(data_dir) / BLOCKCLOCK_FILE
        self.clock = clock
        self.default_block_seconds = float(default_block_seconds)
        # (ts, block), strictly increasing in both
        self.samples: list[tuple[float, int]] = []
        self.restore: str = "missing"
        self._dirty = False

    # -- persistence ----------------------------------------------------------

    def load(self) -> str:
        if not self.path.exists():
            self.restore = "missing"
            return self.restore
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            rows = doc["samples"]
            out: list[tuple[float, int]] = []
            for r in rows:
                ts, blk = float(r[0]), int(r[1])
                if out and (ts <= out[-1][0] or blk <= out[-1][1]):
                    continue  # a document out of order is read as far as it is ordered
                out.append((ts, blk))
            self.samples = out
        except (OSError, ValueError, KeyError, TypeError, IndexError) as e:
            self.samples = []
            self.restore = f"unreadable: {type(e).__name__}"
            return self.restore
        self.restore = "restored"
        return self.restore

    def save(self) -> bool:
        """Atomic write when a sample was appended. Returns True when
        written; an OSError returns False (the clock is a measurement, never
        a reason to fail a pass)."""
        if not self._dirty:
            return False
        doc = {"schemaVersion": BLOCKCLOCK_SCHEMA_VERSION, "savedAt": _now_iso(), "samples": [[ts, blk] for ts, blk in self.samples]}
        tmp = self.path.with_name(BLOCKCLOCK_FILE + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(doc, indent=1, sort_keys=True))
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(self.path)
        except OSError:
            return False
        self._dirty = False
        return True

    # -- the sample stream ----------------------------------------------------

    def observe(self, block: int, ts: float | None = None) -> bool:
        """Record the chain head `block` seen at `ts` (default `clock()`).
        True only when the sample was appended: `block` an int above the
        last sample's block AND `ts` at least SAMPLE_MIN_SEC after the last
        sample's time (the first sample always appends)."""
        if isinstance(block, bool) or not isinstance(block, int) or block < 0:
            return False
        t = float(self.clock() if ts is None else ts)
        if self.samples:
            last_ts, last_block = self.samples[-1]
            if block <= last_block or t < last_ts + SAMPLE_MIN_SEC:
                return False
        self.samples.append((t, int(block)))
        self._prune()
        self._dirty = True
        return True

    def _prune(self) -> None:
        if not self.samples:
            return
        cutoff = self.samples[-1][0] - RETAIN_SEC
        if self.samples[0][0] < cutoff:
            self.samples = [s for s in self.samples if s[0] >= cutoff]
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]

    # -- the reads ------------------------------------------------------------

    def span_sec(self) -> float:
        return (self.samples[-1][0] - self.samples[0][0]) if len(self.samples) >= 2 else 0.0

    def measured(self) -> bool:
        return self.span_sec() >= MIN_SPAN_SEC

    def rate(self) -> float:
        """Seconds per block over the whole sample span; the fallback rate
        until the span is at least MIN_SPAN_SEC."""
        if not self.measured():
            return self.default_block_seconds
        blocks = self.samples[-1][1] - self.samples[0][1]
        if blocks <= 0:
            return self.default_block_seconds
        return self.span_sec() / float(blocks)

    def block_at(self, ts_target: float) -> int | None:
        """The block the chain was at at `ts_target` — interpolated between
        the bracketing samples, extrapolated at the measured rate outside
        the span. None with fewer than two samples."""
        if len(self.samples) < 2:
            return None
        t = float(ts_target)
        first_ts, first_block = self.samples[0]
        last_ts, last_block = self.samples[-1]
        rate = self.rate()
        if t <= first_ts:
            return max(0, int(math.floor(first_block - (first_ts - t) / rate)))
        if t >= last_ts:
            return max(0, int(math.floor(last_block + (t - last_ts) / rate)))
        i = bisect.bisect_right(self.samples, (t, float("inf")))
        t0, b0 = self.samples[i - 1]
        t1, b1 = self.samples[i]
        if t1 <= t0:
            return max(0, int(b0))
        return max(0, int(math.floor(b0 + (t - t0) / (t1 - t0) * (b1 - b0))))

    def floor(self, window_hours: int | float, head: int | None, now: float | None = None) -> int:
        """The block `window_hours` of WALL-CLOCK time ago: what every
        window read compares a kill's block against. With no samples it is
        the old arithmetic below `head` (and 0 with no head), so an empty
        clock answers exactly what the block arithmetic answers. Clamped
        to [0, head] when a head is known."""
        t = float(self.clock() if now is None else now)
        blk = self.block_at(t - float(window_hours) * 3600.0)
        if blk is None:
            if head is None:
                return 0
            blk = int(head) - int(window_hours) * BLOCKS_PER_HOUR
        if head is not None:
            blk = min(int(head), max(0, int(blk)))
        return int(blk)

    def describe(self) -> dict[str, Any]:
        """Status only (never a decision): how much the clock has measured."""
        return {"samples": len(self.samples), "spanSec": self.span_sec(), "blockSeconds": self.rate(), "measured": self.measured(), "restore": self.restore}
