"""The world killer watchlist and the thresholds it serves.

The measured dip cap: with `dipCapMode` measured, a ladder kami's dip cap
is the highest threshold served against THAT kami by a kami that
liquidated inside `dipWindowDays`, instead of a flat share of max HP.

`<dataDir>/watchlist.json` is written by the operator's own tooling (from
any kill history it holds) and never by the keeper:

  {"updatedAt": "<ISO Z>",
   "killers": [{"kami": <int>, "account": <int|null>,
                "lastKillAt": "<ISO Z>"}]}

`Watchlist` reads that file (re-read only when its mtime moved; a missing
or unreadable file is an empty list and `updated_at` None, never an
exception) and answers who is ACTIVE for a book: inside the window, and
not one of the book's listed accounts.

`WatchCache` holds what one `node <i> <killer> --with-vitals --full` read
answers against the account's occupants, per node and killer, in its own
store `<dataDir>/watch/<nodeIndex>.json`. It is deliberately NOT the
preview cache: preview entries are presence-managed (an attacker absent
from the node is dropped, or remembered), and a watchlist killer is
normally not on the node at all — the lens serves its thresholds against
the occupants all the same, which is what makes the measurement possible.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any, Callable

from . import SCHEMA_VERSION
from .lens import LensAnswer, LensClient, LensQueryError
from .strategies.base import node_occupant

WATCHLIST_FILE = "watchlist.json"
WATCH_DIR = "watch"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _epoch(value: Any) -> float | None:
    """An ISO-8601 Z stamp as epoch seconds; None when it is not one (a
    missing or malformed field is unknown, never an exception)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        t = _dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.timestamp()


class Watchlist:
    """The file, read only. `load()` on every tick pass; the file's mtime
    is the only thing that costs a re-read."""

    def __init__(self, data_dir: Path | str, clock: Callable[[], float]):
        self.path = Path(data_dir) / WATCHLIST_FILE
        self.clock = clock
        self.killers: list[dict[str, Any]] = []
        self.updated_at: float | None = None  # epoch of the file's updatedAt
        self._mtime: float | None = None

    def load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self.killers, self.updated_at, self._mtime = [], None, None
            return
        if mtime == self._mtime:
            return
        self._mtime = mtime
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            rows = (doc.get("killers") or []) if isinstance(doc, dict) else []
            self.killers = [r for r in rows if isinstance(r, dict) and _num(r.get("kami"))]
            self.updated_at = _epoch(doc.get("updatedAt")) if isinstance(doc, dict) else None
        except (OSError, ValueError, TypeError, AttributeError):
            self.killers, self.updated_at = [], None

    def active(self, window_days: int, listed: set[int], now: float) -> list[int]:
        """The kami indices that count toward a measured cap: last kill
        inside the window, owner not listed (an entry without an account is
        kept — it still killed)."""
        window = float(window_days) * 86400.0
        out: set[int] = set()
        for r in self.killers:
            acct = r.get("account")
            if _num(acct) and int(acct) in listed:
                continue
            at = _epoch(r.get("lastKillAt"))
            if at is None or now - at > window:
                continue
            out.add(int(r["kami"]))
        return sorted(out)

    def fresh(self, max_age_sec: float, now: float) -> bool:
        """The list is young enough to measure against; a missing file is
        never fresh."""
        return self.updated_at is not None and now - self.updated_at <= float(max_age_sec)


class WatchCache:
    """`entries(node)[killer] = {"fetchedAt": float, "rows": {kami:
    threshold}}` — the served thresholds (negative as served, clamped at
    read), persisted per node and restored lazily."""

    def __init__(self, data_dir: Path | str, clock: Callable[[], float]):
        self.dir = Path(data_dir) / WATCH_DIR
        self.clock = clock
        self._nodes: dict[int, dict[int, dict[str, Any]]] = {}
        self._loaded: set[int] = set()
        self._dirty: set[int] = set()
        # fetches made this tick PASS per node: the `watchPreviewsPerTick`
        # cap is per pass and per node, shared by every book on the node
        self.pass_fetches: dict[int, int] = {}

    def begin_pass(self) -> None:
        self.pass_fetches = {}

    # -- persistence ----------------------------------------------------------

    def path(self, node: int) -> Path:
        return self.dir / f"{int(node)}.json"

    def entries(self, node: int) -> dict[int, dict[str, Any]]:
        node = int(node)
        if node not in self._loaded:
            self._loaded.add(node)
            self._nodes.setdefault(node, {})
            p = self.path(node)
            if p.exists():
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    for k, e in (doc.get("entries") or {}).items():
                        self._nodes[node][int(k)] = {"fetchedAt": float(e.get("fetchedAt") or 0), "rows": {int(r): int(v) for r, v in (e.get("rows") or {}).items()}}
                except (OSError, ValueError, KeyError, TypeError):
                    self._nodes[node] = {}
        return self._nodes[node]

    def save(self, node: int) -> bool:
        node = int(node)
        if node not in self._dirty:
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        doc = {"schemaVersion": SCHEMA_VERSION, "node": node, "savedAt": _now_iso(), "entries": {str(k): {"fetchedAt": e["fetchedAt"], "rows": {str(r): v for r, v in e["rows"].items()}} for k, e in self._nodes.get(node, {}).items()}}
        tmp = self.dir / f"{node}.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path(node))
        self._dirty.discard(node)
        return True

    # -- what is due ------------------------------------------------------------

    def due(self, node: int, killers: list[int], refresh_sec: int, now: float) -> list[int]:
        """The killers to read for this node, never-fetched first, then the
        oldest entry past `refresh_sec`."""
        ents = self.entries(node)
        never = [int(k) for k in sorted(killers) if int(k) not in ents]
        aged = [int(k) for k in killers if int(k) in ents and now - float(ents[int(k)]["fetchedAt"]) >= int(refresh_sec)]
        aged.sort(key=lambda k: (float(ents[k]["fetchedAt"]), k))
        return never + aged

    # -- fetch ------------------------------------------------------------------

    def fetch(self, lens: LensClient, node: int, killer: int, own_account: int, now: float) -> LensAnswer | None:
        """One `node <node> <killer> --with-vitals --full` read; the entry is
        replaced. None when the lens refused it — nothing is stored and the
        tick goes on (the killer stays unread, so the flat cap stands)."""
        try:
            ans = lens.node(int(node), attacker=int(killer), with_vitals=True, full=True)
        except LensQueryError:
            return None
        data = ans.data if isinstance(ans.data, dict) else {}
        rows: dict[int, int] = {}
        for raw in data.get("harvests") or []:
            if not isinstance(raw, dict):
                continue
            occ = node_occupant(raw)
            if occ is None or occ.kami_index == int(killer):
                continue
            if occ.account_index != int(own_account) or occ.threshold is None:
                continue
            rows[occ.kami_index] = int(occ.threshold)
        self.entries(node)[int(killer)] = {"fetchedAt": float(now), "rows": rows}
        self._dirty.add(int(node))
        self.pass_fetches[int(node)] = self.pass_fetches.get(int(node), 0) + 1
        return ans

    def drop_missing(self, node: int, killers: list[int]) -> list[int]:
        """Forget the killers this book no longer counts as active."""
        ents = self.entries(node)
        keep = {int(k) for k in killers}
        gone = [k for k in sorted(ents) if k not in keep]
        for k in gone:
            del ents[k]
            self._dirty.add(int(node))
        return gone
