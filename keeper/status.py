"""SPEC P4 — never-silent degradation: the named flags, and the book
status shape (SPEC P3).

Flags (closed set of shapes):
  lens-unavailable, lens-not-live, lens-stale:<blocks>,
  lens-degraded:<flag> (the lens's own `status.degraded` carried
  `stream-stalled…` or `unhealed-ranges…`: the mirror is KNOWN
  incomplete), lens-incomplete (a kami row of the answer lacked `state`,
  `hp.current`, or `node.index` while HARVESTING — a missing
  field is an unknown, never an observation of the world),
  rpc-unavailable, ws-logs-gap:<N>, signer-nonce-stuck,
  kamiden-unavailable (enrichment only; never gates a decision).

Lens 1.0.0 adds one `status.degraded` entry — `reconcile-stalled:<sec>` —
and it is deliberately NOT a gating prefix below: it says the periodic
chain RE-CHECK has not advanced, not that the mirror is incomplete. The
reads are applied and complete; the proof behind them is late. A book acts.
`stream-stalled` and `unhealed-ranges` keep gating.
"""

from __future__ import annotations

import re
from typing import Any

FLAG_SHAPES: tuple[re.Pattern[str], ...] = (
    re.compile(r"^lens-unavailable$"),
    re.compile(r"^lens-not-live$"),
    re.compile(r"^lens-stale:\d+$"),
    re.compile(r"^lens-degraded:[A-Za-z0-9._-]+$"),
    re.compile(r"^lens-incomplete$"),
    re.compile(r"^rpc-unavailable$"),
    re.compile(r"^ws-logs-gap:\d+$"),
    re.compile(r"^signer-nonce-stuck$"),
    re.compile(r"^kamiden-unavailable$"),
)

# Flags that GATE a decision (fail-closed). kamiden-unavailable never does.
GATING_PREFIXES: tuple[str, ...] = ("lens-unavailable", "lens-not-live", "lens-stale:", "lens-degraded:", "lens-incomplete", "rpc-unavailable", "signer-nonce-stuck")

# The lens `status.degraded` entries that make every read of the tick an
# unknown (a stalled daemon reports `stream-stalled:<sec>` with
# `gapsDeferred` rising and can serve kami rows without a node). Matched
# by prefix.
#
# 1.0.0's `reconcile-stalled:<sec>` is NOT here, and that is a decision:
# `reconciledThrough` has not advanced for more than two reconcile
# intervals while there was work to do, which means the chain proof is
# behind — the mirror is not KNOWN incomplete (that is what
# `unhealed-ranges:<N>` says) and `appliedThrough` keeps advancing, so
# every read is still a reading. Gating on it would stop every book for
# the length of a slow RPC.
LENS_DEGRADED_GATING_PREFIXES: tuple[str, ...] = ("stream-stalled", "unhealed-ranges")


def is_valid_flag(flag: str) -> bool:
    return any(p.match(flag) for p in FLAG_SHAPES)


def lens_unavailable() -> str:
    return "lens-unavailable"


def lens_not_live() -> str:
    return "lens-not-live"


def lens_stale(blocks: int) -> str:
    return f"lens-stale:{int(blocks)}"


def lens_degraded(flag: str) -> str:
    """`lens-degraded:<name>` — the lens flag's NAME only (`stream-stalled`,
    `unhealed-ranges`), never its value, so a continuing stall reads as
    one unchanged flag and does not write a tick row every tick. The lens's full text rides the tick summary
    (`lensDegraded`)."""
    name = str(flag).split(":", 1)[0]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "-", name) or "unknown"
    return f"lens-degraded:{safe_name}"


def lens_incomplete() -> str:
    return "lens-incomplete"


def rpc_unavailable() -> str:
    return "rpc-unavailable"


def ws_logs_gap(n: int) -> str:
    return f"ws-logs-gap:{int(n)}"


def signer_nonce_stuck() -> str:
    return "signer-nonce-stuck"


def kamiden_unavailable() -> str:
    return "kamiden-unavailable"


def gates_action(flags: list[str]) -> bool:
    """True when any flag means the book must not act this tick."""
    return any(f.startswith(p) for f in flags for p in GATING_PREFIXES)


def stale_only(flags: list[str]) -> bool:
    """SPEC P4, the stale-only state: the gate is `lens-stale:<n>`
    and nothing else that gates — the daemon is LIVE, reachable, reports
    no stall and no unhealed range; only its verified lower bound lags.
    That is the one degraded state under which a harvestAndFeed book may
    still feed."""
    gating = [f for f in flags if any(f.startswith(p) for p in GATING_PREFIXES)]
    return bool(gating) and all(f.startswith("lens-stale:") for f in gating)


# SPEC P4, the lens OUTAGE flags — the
# daemon is unreachable, not LIVE, or reports its mirror KNOWN incomplete.
# Under these (and only these, `lens-stale:<n>` alongside allowed since a
# not-LIVE daemon's reconciledThrough lags too) a feed book may take the
# chain-backed HP fallback once the outage has lasted chainFallbackAfterSec.
# `lens-incomplete` (a row of an ANSWER lacked a field) is not an outage:
# the lens answered, so it is excluded.
OUTAGE_PREFIXES: tuple[str, ...] = ("lens-unavailable", "lens-not-live", "lens-degraded:")


def outage_only(flags: list[str]) -> bool:
    """True when at least one gating flag is a lens outage and every gating
    flag is an outage flag or `lens-stale:<n>` — nothing else gates (no
    `rpc-unavailable`, no `lens-incomplete`, no `signer-nonce-stuck`)."""
    gating = [f for f in flags if any(f.startswith(p) for p in GATING_PREFIXES)]
    outage = [f for f in gating if any(f.startswith(p) for p in OUTAGE_PREFIXES)]
    return bool(outage) and all(f.startswith("lens-stale:") or f in outage for f in gating)


# SPEC P3: the per-book status keys, always all present.
BOOK_STATUS_KEYS: tuple[str, ...] = (
    "id",
    "account",
    "strategyType",
    "state",
    "reason",
    "createdAt",
    "lastTickAt",
    "lastActAt",
    "lastActBlock",
    "lastTxHash",
    "nextTickAt",
    "lastError",
    "ticksSinceAct",
    "expectedTicksBetweenActs",
    "degraded",
    "dryRun",
    "pausedReason",
    "pausedAt",
    "parking",
    "strategy",
    "openTx",
)


def book_status(book: Any, runtime: dict[str, Any], dry_run: bool, strategy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derived at answer time from the book record + the engine's runtime
    counters (never a cache). `RUNNING != executing` is visible: a running
    book with ticksSinceAct above expectedTicksBetweenActs reads as such.
    `strategy` (SPEC P3) is the type's own counts — the `auto`
    kami / owner-class counts and `nextPreviewDueAt` — or null. `openTx`
    counts the book's broadcast rows still without a receipt row."""
    return {
        "id": book.id,
        "account": book.account,
        "strategyType": book.strategyType,
        "state": book.state,
        "reason": book.reason,
        "createdAt": book.createdAt,
        "lastTickAt": runtime.get("lastTickAt"),
        "lastActAt": runtime.get("lastActAt"),
        # SPEC P3: the receipt block of the book's last landed tx —
        # the bar a lens read must reach before the book decides anything
        "lastActBlock": runtime.get("lastActBlock"),
        "lastTxHash": runtime.get("lastTxHash"),
        "nextTickAt": runtime.get("nextTickAt"),
        "lastError": runtime.get("lastError"),
        "ticksSinceAct": int(runtime.get("ticksSinceAct", 0)),
        "expectedTicksBetweenActs": runtime.get("expectedTicksBetweenActs"),
        "degraded": list(runtime.get("degraded", [])),
        "dryRun": bool(dry_run),
        # SPEC P3: why and when a paused book paused —
        # `agent` | `external-change` | `kami-dead`; null unless paused
        "pausedReason": getattr(book, "pausedReason", None) if book.state == "paused" else None,
        "pausedAt": getattr(book, "pausedAt", None) if book.state == "paused" else None,
        # SPEC P8: the parking mark — {"since", "by"} — of an auto
        # book asked to park; null unless parking, and KEPT on the stopped
        # book (the book stops itself once the park completed, and the mark
        # is the history of it)
        "parking": getattr(book, "parking", None),
        "strategy": dict(strategy) if strategy else None,
        # SPEC P3: broadcast rows without a receipt row yet —
        # 0 normally; a restart inside a receipt wait leaves them open
        # until the chain answers for them
        "openTx": int(runtime.get("openTx", 0) or 0),
    }
