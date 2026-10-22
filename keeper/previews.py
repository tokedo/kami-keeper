"""The threshold preview cache.

THRESHOLDS COME FROM THE LENS PREVIEW (the CLAUDE.md rule:
`node <i> <attacker> --with-vitals --full`, one attacker per call); the
keeper computes only the margin between refreshes, the time-to-cross and
the owner class. Nothing here computes a threshold from stats.

One cache per NODE AND ACCOUNT: `(nodeIndex, accountIndex)` ->
`{attackerKamiIndex: {fetchedAt, block, attackerLevel, attackerAccount,
rows: {ourKamiIndex: {threshold (served, may be negative), level, reason,
hpTotal (the occupant's served max HP)}}}}`. Rows are kept for every
occupant of the book's OWN account, so the books of ONE account on a node
share their scope (several books of one account on one node read one
entry per attacker) and the books of DIFFERENT accounts never see each
other's rows, entries, presence or due lists. Two things stay NODE facts,
shared by every book on the node whatever its account: the per-pass fetch
cap (`pass_fetches[node]`, `previewsPerTick`) and the cooldowns the preview
answers of this pass served (`pass_cooldowns[node]`). Persisted as
`previews/<node>-<account>.json`; the legacy `previews/<node>.json` is
never read and never written.
Why per account: a node-scoped cache holds only the rows of the account
whose book fetched them. A second account's book arriving on the same
node finds entries with no row for its own kamis, prices them at a
stand-in line until the entries age (the `no_rows` rule reads the other
account's rows as "rows"), and the two books then overwrite each other's
entries on every fetch, each pricing at the other's line. Keying the
cache by (node, account) removes the interference.

Fetch when: the attacker is first seen (arrival); its level in the node
vitals differs from `attackerLevel`; one of our kamis' level differs
from its row's; the entry is older than `previewRefreshSec` while any
clamped threshold is > 0, older than REFRESH_NO_THRESHOLD_SEC (86400)
when all are <= 0. At most `previewsPerTick` fetches per tick, arrivals
queued first; the rest are `previewsPending`.

Refresh order: when the hourly refresh demand exceeds what
`previewsPerTick` allows (e.g. hundreds of positive lines against a few
hundred fetches an hour), the refresh queue never drains, and a per-pass
cap that took the aged entries in attacker-index order could leave the
entry that binds waiting for hours. Level-change and aged entries are
therefore offered highest line first, oldest first among equals; arrivals
and no-rows entries keep their place at the front.

The served threshold can be negative (`THRESHOLD_ZERO` rows carry e.g.
-18); the keeper uses `max(0, threshold)` — a non-positive threshold
means this attacker cannot liquidate this kami at any HP.

Absent but remembered: when an attacker leaves the node its entry is
kept for the window if its owner OR the kami itself (whoever owns it
now) is HOSTILE at departure, else dropped. A remembered
attacker binds the margin like a present one, with cooldown unknown (0).

Persisted to `<dataDir>/previews/<nodeIndex>-<accountIndex>.json` (atomic) and
restored on boot so a restart is not a refetch storm; an entry older
than its refresh interval is simply due.

`previewPolicy`: WHO is offered to `due()` is the caller's choice — every
present foreign occupant (`all`) or only the attackers whose owner or
kami is killed-here inside the window plus a remembered one that came
back (`killed-here`: with `passiveOwnersBind` false nobody else can bind,
so nobody else is fetched, refreshed or counted pending). The
cache itself does not change: an entry whose attacker is no longer
offered is kept, not refreshed, and dropped when the attacker leaves, as
before. Under the PLAIN census the node rows carry no cooldown:
`reconcile_presence(refresh_cooldown=False)` leaves the entry's cooldown
alone, and `pass_cooldowns` holds the cooldown the preview answers of
THIS pass served (`attacker.cooldownSec`).
Valid rows: a served row is a LINE only when it carries vitals —
`valid_row` below. A row with no vitals, or with a threshold <= 0
under an attacker-side reason (`ATTACKER_*`) while the ATTACKER's own row
carries no vitals either, never replaces a line and never lowers one: the
old row is CARRIED (marked `carriedFrom`), a kami with no old row gets no
row at all, and the entry is due again after `previewRetrySec` (300)
instead of ageing out at `previewRefreshSec` or 86400. Why: a degraded
lens answer can serve every row without vitals and with an attacker-side
reason at threshold 0; replacing the entry with those zeros would freeze
it at `max_clamped() == 0` until it aged and price every kami at no line.
The attacker-side clause is narrow: with the attacker's own vitals
present, a threshold <= 0 under `ATTACKER_COOLDOWN` / `ATTACKER_STARVING`
is the lens's NORMAL answer for a weak attacker read on its cooldown (the
same pairs serve the same numbers under `THRESHOLD_ZERO` once it is off
cooldown) — it is stored as served; only the vitals-less shape of the
degraded answer is refused.
Lens 1.0.0: a served row the daemon flagged `incomplete: true` is never a
line (it carries identity and `state` only) — it takes the valid-row
path, so the old row is CARRIED, the kami gets no new line, the entry
reads `degraded_read` and is due again at `previewRetrySec`; the same
holds for an own row with no `liquidation` block at all. A preview read
also carries `--targets <our present kamis + the attacker>` where the
daemon knows the option (the caller builds the list), which selects
before projection and costs ~1 KB instead of ~1 MB on a crowded node; on
0.6.3 the option is dropped by the client and the full answer comes back
as before. `meta.incompleteRows` is read nowhere here: the entry's
`invalid_rows` already says what was not a line.

Legacy rows: the denominator of a row without `hpTotal` is the BOOK's own
max HP for that kami — `max_ratio` / `stand_in` take `hp_totals` — so a
stand-in is scaled whenever ANY counted row's max HP is known by either
source, and the absolute `max_clamped()` stands in only when none is.
Why: an absent, remembered entry is never refreshed (`due()` iterates
PRESENT occupants), so legacy rows without `hpTotal` would otherwise bind
at the absolute maximum indefinitely and hold the book's low-HP kamis
for ever.

The measured lift. A kill of one of the book's kamis on the
book's node is a MEASUREMENT of the killer's line. When the victim's HP
at the kill is ABOVE the served threshold of the (killer, victim) pair,
the served line was too low by at least that gap, so every line of the
killer's OWNER on that node — the killer and the owner's other
attackers, present or remembered — is
lifted by that gap as a RATIO of the victim's max HP, plus one HP,
rounded up to 0.1 % (`lift_ratio`, `lift_from`). The lift is a property
of the preview entry: it survives the entry's refresh (a refresh taken
after the bonus has ended serves the earlier number again), it is
persisted with the entry, it leaves when
the entry is forgotten, and it only ever grows — a second kill above the
LIFTED line lifts again, measured against the SERVED threshold. This is
still "thresholds come from the lens preview": the line is the lens's
number plus a gap the CHAIN measured through its own kill, and nothing
here computes a threshold from stats. A timed bonus is visible on chain
and in the lens while it lasts; an entry fetched before it serves the
earlier number until its next refresh, so a kill above the served line
measures the gap, and that gap is the lift. Hence both halves of the
rule: a lift HOLDS for `LIFT_HOLD_SEC` and then expires (`lift_until`,
`expire_lifts`) — a renewed kill extends it, and within the hold it is
the floor under the lines — and a kill by an owner flags that owner's
PRESENT entries for an immediate refetch (`refetch`, first in `due()`),
which reads the EXACT lines while the bonus lasts.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import SCHEMA_VERSION
from .lens import LensAnswer, LensClient
from .strategies.base import NodeOccupant, node_occupant

PREVIEWS_DIR = "previews"
REFRESH_NO_THRESHOLD_SEC = 86400
# a measured gap above a quarter of a kami's max HP is not a timed
# bonus but a wrong measurement — the lift is capped here and
# the cap is reported on the entry (`lift_from["capped"]`).
LIFT_CAP = 0.25
# how long a measured lift HOLDS: 6 h, and a renewed kill extends it;
# past it the entry is back on the number the lens serves.
LIFT_HOLD_SEC = 21600


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def clamp(threshold: int | None) -> int | None:
    """max(0, served threshold); None stays None (unknown)."""
    return None if threshold is None else max(0, int(threshold))


def valid_row(occ: NodeOccupant, attacker_has_vitals: bool) -> str | None:
    """None when this served row is a valid LINE,
    else why it is not: a row is a line only when it carries vitals, and a
    non-positive threshold under an attacker-side reason (`ATTACKER_*`) is a
    line as served whenever the ATTACKER's own row carried vitals too (a
    weak attacker read on its cooldown), refused only when it did not (the
    degraded answer drops the attacker's vitals with everyone else's).

    Lens 1.0.0: an `incomplete: true` row answers first (the belt —
    `fetch` names it too). Such a row carries no vitals and no
    `liquidation` block by construction: it is an unknown about that kami,
    never a line."""
    if occ.incomplete:
        return "incomplete row"
    if occ.vitals_missing:
        return "no vitals: " + ",".join(occ.vitals_missing)
    if not attacker_has_vitals and occ.threshold is not None and occ.threshold <= 0 and str(occ.liquidation_reason or "").upper().startswith("ATTACKER"):
        return f"attacker-side {occ.liquidation_reason} with threshold {occ.threshold}; attacker row without vitals"
    return None


@dataclass
class PreviewEntry:
    node: int
    attacker: int
    fetched_at: float
    block: int | None
    attacker_level: int | None
    attacker_account: int | None
    rows: dict[int, dict[str, Any]] = field(default_factory=dict)  # our kami -> {threshold, level}
    cooldown_sec: int = 0  # the attacker's cooldown at the last node read (0 while remembered)
    absent_since: float | None = None  # set when the attacker left the node and its owner was HOSTILE
    # our kami -> why its served row was not a line this fetch (the
    # entry is then due again after `previewRetrySec`, never at 86400)
    invalid_rows: dict[int, str] = field(default_factory=dict)
    # the measured lift — a share of a victim's max HP (0.025 =
    # 2.5 %) added to every served line of this entry, and the
    # measurement that set it ({victim, hpAtKill, served, gapHp, block,
    # txHash, at, attacker}, plus `viaAttacker` when the lift came from
    # another attacker of the same owner and `capped` at `LIFT_CAP`).
    # `lift_until` is the keeper clock the lift expires at (None = none).
    lift_ratio: float = 0.0
    lift_from: dict[str, Any] = field(default_factory=dict)
    lift_until: float | None = None
    # this owner killed — re-read its lines at the first chance
    # (`due()` offers a flagged PRESENT entry first; the fetch clears it)
    refetch: bool = False

    def lift_hp(self, kami: int) -> int:
        """The measured lift in HP for one row: `ceil(lift_ratio x the
        row's own hpTotal)`. A legacy row carries no `hpTotal`,
        so it is lifted by 0 and that is all — no other denominator stands
        in here (`max_ratio` / `stand_in` keep their legacy-row fallbacks)."""
        row = self.rows.get(int(kami))
        if not row or self.lift_ratio <= 0:
            return 0
        own = row.get("hpTotal")
        if not own or int(own) <= 0:
            return 0
        return int(math.ceil(self.lift_ratio * int(own)))

    def clamped(self, kami: int) -> int | None:
        """The line this entry holds against one kami: the SERVED
        threshold clamped to 0 first, then lifted by `lift_hp` (an
        attacker whose served line against a kami is 0 still has a line of
        0 + the lift: that is what the measurement says). None when there
        is no row."""
        row = self.rows.get(int(kami))
        if not row:
            return None
        base = clamp(row.get("threshold"))
        return None if base is None else base + self.lift_hp(kami)

    def lift(self, ratio: float, source: dict[str, Any]) -> bool:
        """Raise the measured lift to `ratio` (a share of a victim's
        max HP). False and no change when it is not higher than the lift
        already held — a lift only ever grows. The hold restarts from
        `source["at"]` on every raise."""
        if float(ratio) <= float(self.lift_ratio):
            return False
        self.lift_ratio = min(float(ratio), LIFT_CAP)
        self.lift_from = dict(source)
        if self.lift_ratio < float(ratio):
            self.lift_from["capped"] = True
        at = source.get("at")
        self.lift_until = float(at) + LIFT_HOLD_SEC if isinstance(at, (int, float)) and not isinstance(at, bool) else None
        return True

    def max_clamped(self, kamis: list[int] | None = None) -> int:
        ks = [int(k) for k in kamis] if kamis is not None else list(self.rows)
        vals = [self.clamped(k) for k in ks]
        return max([v for v in vals if v is not None] or [0])

    def _denominator(self, kami: int, hp_totals: dict[int, int] | None = None) -> int:
        """The max HP a row's threshold is a share of — the row's own
        served `hpTotal`, else the BOOK's own figure for that kami
        (`hp_totals`, the party read) when the row is a legacy one and
        carries none. 0 = the row does not count toward the ratio (it does
        not exist, or neither source knows its max HP)."""
        row = self.rows.get(int(kami))
        if not row:
            return 0
        own = row.get("hpTotal")
        if own and int(own) > 0:
            return int(own)
        alt = hp_totals.get(int(kami)) if hp_totals else None
        return int(alt) if alt and int(alt) > 0 else 0

    def max_ratio(self, kamis: list[int] | None = None, hp_totals: dict[int, int] | None = None) -> float:
        """The highest served threshold as a share of THAT occupant's max HP,
        over the rows whose max HP is known (restricted to `kamis` when
        given); 0.0 when no row's is. `hp_totals` is
        `{kami: max HP}` for the book's OWN kamis: the denominator of a
        legacy row (no `hpTotal`) whose kami the book
        holds. The measured `lift_ratio` is added to every counted
        row's ratio, so a stand-in scales off the LIFTED line."""
        ks = [int(k) for k in kamis] if kamis is not None else list(self.rows)
        best = 0.0
        for k in ks:
            denom = self._denominator(k, hp_totals)
            if denom <= 0:
                continue
            thr = clamp(self.rows[int(k)].get("threshold"))
            if thr is not None:
                best = max(best, thr / denom + float(self.lift_ratio))
        return best

    def stand_in(self, hp_total: int, kamis: list[int] | None = None, hp_totals: dict[int, int] | None = None) -> tuple[int, bool]:
        """(the threshold that stands in for a kami of `hp_total` max HP that
        has no row of its own, scaled: bool). Scaled = the highest served
        RATIO over the rows times `hp_total`, ceil'd, whenever ANY counted
        row's max HP is known — from the row itself or from `hp_totals`;
        otherwise (no row's max HP is known by either source) the
        absolute `max_clamped(kamis)`."""
        ks = [int(k) for k in kamis] if kamis is not None else list(self.rows)
        if any(self._denominator(k, hp_totals) > 0 for k in ks):
            return int(math.ceil(self.max_ratio(kamis, hp_totals) * int(hp_total))), True
        return self.max_clamped(kamis), False

    @property
    def remembered(self) -> bool:
        return self.absent_since is not None

    @property
    def degraded_read(self) -> bool:
        """This fetch served at least one row that was not a line."""
        return bool(self.invalid_rows)

    def to_dict(self) -> dict[str, Any]:
        return {"node": self.node, "attacker": self.attacker, "fetchedAt": self.fetched_at, "block": self.block, "attackerLevel": self.attacker_level, "attackerAccount": self.attacker_account, "rows": {str(k): v for k, v in self.rows.items()}, "cooldownSec": self.cooldown_sec, "absentSince": self.absent_since, "invalidRows": {str(k): v for k, v in self.invalid_rows.items()}, "liftRatio": self.lift_ratio, "liftFrom": self.lift_from, "liftUntil": self.lift_until, "refetch": self.refetch}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PreviewEntry":
        """Only known keys are read, so a file written by an older build
        (no `liftRatio` / `liftFrom` / `liftUntil` / `refetch`) loads with
        no lift and no flag, and a file written by this one loads under
        that build unchanged (the previews `schemaVersion` does not
        move — the four keys are additive)."""
        return cls(int(d["node"]), int(d["attacker"]), float(d.get("fetchedAt") or 0), d.get("block"), d.get("attackerLevel"), d.get("attackerAccount"), {int(k): dict(v) for k, v in (d.get("rows") or {}).items()}, int(d.get("cooldownSec") or 0), d.get("absentSince"), {int(k): str(v) for k, v in (d.get("invalidRows") or {}).items()}, float(d.get("liftRatio") or 0.0), dict(d.get("liftFrom") or {}), d.get("liftUntil"), bool(d.get("refetch") or False))


class PreviewCache:
    def __init__(self, data_dir: Path | str, clock: Callable[[], float]):
        self.dir = Path(data_dir) / PREVIEWS_DIR
        self.clock = clock
        # the scope is (nodeIndex, accountIndex) — the books of one
        # account on a node share it, the books of different accounts do not
        self._scopes: dict[tuple[int, int], dict[int, PreviewEntry]] = {}
        self._loaded: set[tuple[int, int]] = set()
        self._dirty: set[tuple[int, int]] = set()
        self.restore: dict[str, str] = {}  # "<node>-<account>" -> restore state
        # fetches made this tick PASS per node: the `previewsPerTick` cap
        # is per pass and per node, shared by every book on the node (three
        # books on one node are one cap, not three) — reset by the core
        self.pass_fetches: dict[int, int] = {}
        # node -> {attacker: cooldownSec} as served by the preview
        # answers of this pass (`attacker.cooldownSec`); what a book under
        # the plain census reads an attacker's cooldown from before it
        # spends a `kami <index>` read on it
        self.pass_cooldowns: dict[int, dict[int, int]] = {}

    def begin_pass(self) -> None:
        self.pass_fetches = {}
        self.pass_cooldowns = {}

    # -- persistence ----------------------------------------------------------

    def path(self, node: int, account: int) -> Path:
        return self.dir / f"{int(node)}-{int(account)}.json"

    def entries(self, node: int, account: int) -> dict[int, PreviewEntry]:
        scope = (int(node), int(account))
        if scope not in self._loaded:
            self._loaded.add(scope)
            self._scopes.setdefault(scope, {})
            p = self.path(*scope)
            key = f"{scope[0]}-{scope[1]}"
            if not p.exists():
                self.restore[key] = "missing"
            else:
                try:
                    doc = json.loads(p.read_text(encoding="utf-8"))
                    for a, e in (doc.get("entries") or {}).items():
                        ent = PreviewEntry.from_dict(e)
                        self._scopes[scope][int(a)] = ent
                    self.restore[key] = "restored"
                except (OSError, ValueError, KeyError, TypeError) as e:
                    self.restore[key] = f"unreadable: {type(e).__name__}"
        return self._scopes[scope]

    def save(self, node: int, account: int) -> bool:
        scope = (int(node), int(account))
        if scope not in self._dirty:
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        doc = {"schemaVersion": SCHEMA_VERSION, "node": scope[0], "account": scope[1], "savedAt": _now_iso(), "entries": {str(a): e.to_dict() for a, e in self._scopes.get(scope, {}).items()}}
        tmp = self.dir / f"{scope[0]}-{scope[1]}.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=1, sort_keys=True))
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path(*scope))
        self._dirty.discard(scope)
        return True

    # -- what is due ------------------------------------------------------------

    def due(self, node: int, account: int, foreign: dict[int, NodeOccupant], owned_levels: dict[int, int | None], refresh_sec: int, now: float, own_on_node: bool = False, retry_sec: int = 300) -> list[tuple[int, str]]:
        """[(attacker, reason)] in fetch order: the PRESENT entries flagged
        for a refetch (their owner killed one of ours on this node),
        then arrivals, then the entries
        with no rows, then level changes, then age — level changes and aged
        entries ordered by binding weight: the highest clamped
        threshold over the entry's rows first, the oldest fetch first among
        equals, attacker index among equals still; arrivals and no-rows
        entries stay in attacker-index order. `foreign` = the PRESENT
        foreign occupants by kami index (listed owners already excluded by
        the caller). `own_on_node`: a kami of the account is on the
        node now, so a preview fetched while none was — its `rows` are
        empty, and an empty entry binds nothing and holds every start — is
        due again; with it false such an entry ages as any other.
        `retry_sec` (`previewRetrySec`): an entry whose last fetch
        served a row that was not a line is due again this many seconds
        after it, never at `refresh_sec` or 86400."""
        ents = self.entries(node, account)
        arrivals: list[tuple[int, str]] = []
        refetch: list[tuple[int, str]] = []
        no_rows: list[tuple[int, str]] = []
        levels: list[tuple[int, str]] = []
        aged: list[tuple[int, str]] = []
        for a, occ in sorted(foreign.items()):
            e = ents.get(a)
            if e is None:
                arrivals.append((a, "arrival"))
                continue
            if e.refetch and not e.remembered:
                # this owner killed one of ours on this node — its
                # lines are re-read before anything else, because inside
                # an item's buff window the lens most likely serves the
                # right ones and the measured lift is only the floor
                refetch.append((a, "kill by this owner: refetch"))
                continue
            if own_on_node and not e.remembered and not e.rows:
                no_rows.append((a, "no rows for the account; a kami is on the node now"))
                continue
            if occ.level is not None and e.attacker_level is not None and occ.level != e.attacker_level:
                levels.append((a, f"attacker level {e.attacker_level} -> {occ.level}"))
                continue
            moved = next((k for k, lvl in owned_levels.items() if k in e.rows and lvl is not None and e.rows[k].get("level") is not None and int(e.rows[k]["level"]) != int(lvl)), None)
            if moved is not None:
                levels.append((a, f"kami {moved} level changed"))
                continue
            if e.degraded_read:
                if now - e.fetched_at >= int(retry_sec):
                    aged.append((a, f"degraded read: {len(e.invalid_rows)} invalid rows; retry"))
                continue
            limit = int(refresh_sec) if e.max_clamped() > 0 else REFRESH_NO_THRESHOLD_SEC
            if now - e.fetched_at >= limit:
                aged.append((a, f"older than {limit} s"))
        # when the demand exceeds `previewsPerTick` the cap must take the
        # entry that BINDS first — highest clamped line, oldest fetch among
        # equals, attacker index among equals still (a deterministic order).
        # `arrivals` (no entry) and `no_rows` (no weight) keep index order.
        def weight(item: tuple[int, str]) -> tuple[int, float, int]:
            a = item[0]
            e = ents[a]
            return (-e.max_clamped(), e.fetched_at, a)

        levels.sort(key=weight)
        aged.sort(key=weight)
        return refetch + arrivals + no_rows + levels + aged

    def next_due_at(self, node: int, account: int, refresh_sec: int, only: set[int] | None = None, retry_sec: int = 300) -> float | None:
        """`only`: the attackers the book's `previewPolicy` offers — an
        entry outside it is never refreshed, so it names no due time.
        `retry_sec`: a degraded read is due again that soon. An
        entry flagged for a refetch is due NOW (0.0, the minimum)."""
        ents = self.entries(node, account)
        times = [0.0 if e.refetch else e.fetched_at + (int(retry_sec) if e.degraded_read else (int(refresh_sec) if e.max_clamped() > 0 else REFRESH_NO_THRESHOLD_SEC)) for a, e in ents.items() if not e.remembered and (only is None or a in only)]
        return min(times) if times else None

    # -- fetch ------------------------------------------------------------------

    def fetch(self, lens: LensClient, node: int, attacker: int, occupant: NodeOccupant | None, own_account: int, now: float, targets: list[int] | None = None) -> tuple[PreviewEntry, bool, LensAnswer]:
        """One `node <node> <attacker> --with-vitals --full` read; the entry
        is replaced. Returns (entry, changed: a clamped threshold moved
        against a kami already in the entry, answer).

        `targets` (lens 1.0.0): the occupant kamis to select BEFORE
        projection — ~1 KB instead of ~1 MB on a crowded node. The lens
        client drops the option on a 0.6.3 daemon, so the full answer comes
        back there and the rows below are the same either way. The list
        MUST already contain the ATTACKER (its own row decides whether an
        attacker-side threshold is a line): that is the caller's
        duty — keeper/strategies/threat.py builds it — and nothing here
        asserts it."""
        ans = lens.node(int(node), attacker=int(attacker), with_vitals=True, full=True, targets=targets)
        data = ans.data if isinstance(ans.data, dict) else {}
        ents = self.entries(node, own_account)
        old = ents.get(int(attacker))
        rows: dict[int, dict[str, Any]] = {}
        invalid: dict[int, str] = {}
        att_level = occupant.level if occupant is not None else None
        att_account = occupant.account_index if occupant is not None else None
        # the ATTACKER's own row first — its vitals decide whether a
        # non-positive threshold under an `ATTACKER_*` reason is a line (the
        # weak-attacker-on-cooldown answer) or the degraded answer's shape. No row for
        # the attacker at all counts as no vitals.
        att_occ: NodeOccupant | None = None
        for raw in data.get("harvests") or []:
            if not isinstance(raw, dict):
                continue
            occ = node_occupant(raw)
            if occ is None or occ.kami_index != int(attacker):
                continue
            if att_occ is None:
                att_occ = occ
            if att_level is None:
                att_level = occ.level
            if att_account is None:
                att_account = occ.account_index
        attacker_has_vitals = att_occ is not None and not att_occ.vitals_missing
        for raw in data.get("harvests") or []:
            if not isinstance(raw, dict):
                continue
            occ = node_occupant(raw)
            if occ is None:
                continue
            if occ.kami_index == int(attacker):
                continue
            if occ.account_index != int(own_account):
                continue
            # lens 1.0.0: an `incomplete` row of OURS is an unknown, and a
            # row with no `liquidation` block states no line either — both
            # go the invalid-row way (carried, counted, retried at
            # `previewRetrySec`) instead of being dropped in silence.
            why = "incomplete row" if occ.incomplete else ("no liquidation block" if occ.threshold is None else valid_row(occ, attacker_has_vitals))
            if why is not None:
                # not a line — carry the old row, never replace or lower
                invalid[occ.kami_index] = why
                if old is not None and occ.kami_index in old.rows:
                    rows[occ.kami_index] = dict(old.rows[occ.kami_index], carriedFrom=old.fetched_at)
                continue
            rows[occ.kami_index] = {"threshold": int(occ.threshold), "level": occ.level, "reason": occ.liquidation_reason}
            if occ.hp_total > 0:
                rows[occ.kami_index]["hpTotal"] = int(occ.hp_total)
        att = data.get("attacker") if isinstance(data.get("attacker"), dict) else {}
        cooldown = int(att.get("cooldownSec") or 0) if att else (occupant.cooldown_sec if occupant is not None else 0)
        served = att.get("cooldownSec") if att else None
        if isinstance(served, (int, float)) and not isinstance(served, bool):
            self.pass_cooldowns.setdefault(int(node), {})[int(attacker)] = int(served)
        changed = False
        if old is not None:
            for k, r in rows.items():
                if "carriedFrom" in r:
                    continue
                if k in old.rows and clamp(old.rows[k].get("threshold")) != clamp(r.get("threshold")):
                    changed = True
                    break
        # the replacement entry CARRIES the measured lift and its
        # hold — a refresh taken after the bonus has ended serves the
        # earlier number again, so it must not drop what the chain
        # measured — and clears the `refetch`
        # flag, which this fetch has just answered. `changed` above stays
        # on the SERVED thresholds (the raw rows).
        entry = PreviewEntry(int(node), int(attacker), float(now), ans.block, att_level, att_account, rows, cooldown, None, invalid_rows=invalid, lift_ratio=(old.lift_ratio if old is not None else 0.0), lift_from=(dict(old.lift_from) if old is not None else {}), lift_until=(old.lift_until if old is not None else None))
        ents[int(attacker)] = entry
        self._dirty.add((int(node), int(own_account)))
        self.pass_fetches[int(node)] = self.pass_fetches.get(int(node), 0) + 1
        return entry, changed, ans

    # -- presence -----------------------------------------------------------------

    def reconcile_presence(self, node: int, account: int, present: dict[int, NodeOccupant], hostile_owners: set[int], now: float, window_sec: float, hostile_kamis: set[int] | None = None, refresh_cooldown: bool = True) -> dict[str, list[int]]:
        """After a node read: a present attacker is present (cooldown
        refreshed, `absent_since` cleared); an absent one is remembered
        for `window_sec` when its owner is in `hostile_owners` OR the kami
        itself is in `hostile_kamis` (a kami that killed is remembered
        under a clean owner too), else dropped; a remembered one past the
        window is dropped. `refresh_cooldown` False (the plain census):
        the rows carry no cooldown, so the entry's is left as fetched."""
        hostile_kamis = hostile_kamis or set()
        scope = (int(node), int(account))
        ents = self.entries(*scope)
        out: dict[str, list[int]] = {"remembered": [], "dropped": [], "expired": []}
        for a in list(ents):
            e = ents[a]
            occ = present.get(a)
            if occ is not None:
                if not refresh_cooldown:
                    if e.absent_since is not None:
                        e.absent_since = None
                        self._dirty.add(scope)
                elif e.absent_since is not None or e.cooldown_sec != occ.cooldown_sec:
                    e.absent_since = None
                    e.cooldown_sec = int(occ.cooldown_sec)
                    self._dirty.add(scope)
                continue
            if e.absent_since is None:
                if (e.attacker_account is not None and int(e.attacker_account) in hostile_owners) or int(a) in hostile_kamis:
                    e.absent_since = float(now)
                    e.cooldown_sec = 0
                    out["remembered"].append(a)
                    self._dirty.add(scope)
                else:
                    del ents[a]
                    out["dropped"].append(a)
                    self._dirty.add(scope)
            elif now - float(e.absent_since) >= window_sec:
                del ents[a]
                out["expired"].append(a)
                self._dirty.add(scope)
        return out

    # -- the measured lift --------------------------------------------------

    def lift_owner(self, node: int, account: int, owner: int, ratio: float, source: dict[str, Any]) -> list[int]:
        """A kill measured the line of `owner`'s attacker `source["attacker"]`
        too low by a share `ratio` of the victim's max HP: lift every entry
        of that OWNER in the scope — present AND remembered — and flag its
        PRESENT entries for a refetch.
        Returns the sorted attacker indices whose lift actually ROSE; an
        owner with no entry here returns [] (there is no line to lift yet:
        the arrival path previews it this pass and the measurement is not
        applied to that fresh entry)."""
        ents = self.entries(node, account)
        killer = source.get("attacker")
        rose: list[int] = []
        touched = False
        for a, e in ents.items():
            if e.attacker_account is None or int(e.attacker_account) != int(owner):
                continue
            src = dict(source) if killer is not None and int(a) == int(killer) else dict(source, viaAttacker=killer)
            if e.lift(float(ratio), src):
                rose.append(int(a))
                touched = True
            if not e.remembered and not e.refetch:
                e.refetch = True
                touched = True
        if touched:
            self._dirty.add((int(node), int(account)))
        return sorted(rose)

    def mark_refetch(self, node: int, account: int, owner: int) -> list[int]:
        """A kill by `owner` is reason enough to re-read its lines,
        whatever the measurement said. Flags every PRESENT entry of that
        owner in the scope and returns them sorted (an already flagged one
        included — the flag is the state, not the event)."""
        ents = self.entries(node, account)
        out: list[int] = []
        touched = False
        for a, e in ents.items():
            if e.attacker_account is None or int(e.attacker_account) != int(owner) or e.remembered:
                continue
            out.append(int(a))
            if not e.refetch:
                e.refetch = True
                touched = True
        if touched:
            self._dirty.add((int(node), int(account)))
        return sorted(out)

    def expire_lifts(self, node: int, account: int, now: float) -> list[int]:
        """Drop every measured lift whose hold has passed — a lift that is
        not renewed by a new kill goes and the entry is back on the number
        the lens serves. Returns
        the sorted attacker indices expired."""
        ents = self.entries(node, account)
        out: list[int] = []
        for a, e in ents.items():
            if e.lift_until is not None and float(e.lift_until) <= float(now):
                e.lift_ratio = 0.0
                e.lift_from = {}
                e.lift_until = None
                out.append(int(a))
        if out:
            self._dirty.add((int(node), int(account)))
        return sorted(out)

    def forget(self, node: int, account: int, attacker: int) -> None:
        scope = (int(node), int(account))
        if self.entries(*scope).pop(int(attacker), None) is not None:
            self._dirty.add(scope)
