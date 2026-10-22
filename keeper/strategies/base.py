"""The strategy interface. A strategy OBSERVES through the lens client,
ASSESSES into decisions (each with the intents it wants signed), and
keeps the book's PLAN — what it last asserted about the world — so the
core can tell an external change (SPEC P7) from coexistence.

Nothing here signs, logs or formats; the core owns the ledger, the
signer and every agent-visible string.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..books import Book
from ..ledger import decision_kamis
from ..lens import LensAnswer, LensClient
from ..protocol import Basis
from ..signer import Intent

HARVESTING = "HARVESTING"
RESTING = "RESTING"
DEAD = "DEAD"

# The decision kinds that move no plan entry — their landed records
# never changed a kami's planned harvest state, so the boot close of such
# a step has nothing to apply.
NO_PLAN_KINDS: tuple[str, ...] = ("feed", "collect", "revive", "craft")

# What a KamiView's `missing` carries when the kami had NO ROW in the
# party answer (`auto`). An absence is
# still an incomplete observation of that kami (unknown, never RESTING),
# but on `externalChangeTicks` consecutive otherwise-valid reads it is an
# external change of that kami (transferred, sacrificed, listed), not a
# lens fault — the core tells the two apart with `Observation.absent_kamis`.
MISSING_ROW = "row (absent from the party answer)"


@dataclass
class KamiView:
    index: int
    state: str
    hp_current: int
    hp_total: int
    node_index: int | None
    musu_accrued: int
    cooldown_sec: int
    level: int | None
    account_index: int | None
    # projected HP rate the lens reports (HP/h, negative while
    # harvesting); None when the answer did not carry it
    hp_rate_per_hr: float | None = None
    # SPEC P4: the fields this row LACKED. A non-empty tuple makes the
    # observation INCOMPLETE — an unknown, never a reading of the world (a
    # stalled lens can serve kami rows without a `node`; read as complete,
    # that would look like an external change).
    missing: tuple[str, ...] = ()

    @property
    def hp_pct(self) -> float:
        return 100.0 * self.hp_current / self.hp_total if self.hp_total > 0 else 0.0

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def absent(self) -> bool:
        """The kami had no row at all in the account's
        answer — distinct from a row lacking a field."""
        return self.missing == (MISSING_ROW,)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def kami_view(data: dict[str, Any]) -> KamiView:
    """From a lens `kami` answer (or a `party` / `node` occupant row).

    Validity rule (SPEC P4): a row is complete only when it
    carries `state`, `hp.current`, and — while HARVESTING — `node.index`.
    Anything else is recorded in `missing`; the values are still parsed
    (zeros / None) so nothing downstream raises, but the core gates the
    tick on `missing` before any decision or P7 evaluation.

    Lens 1.0.0: a LIST row the mirror cannot project completely keeps
    its identity and `state` and carries `incomplete: true` with NO vitals
    — such a row is an UNKNOWN, never a reading, so `incomplete` is the
    first entry of `missing` (the usual checks still run and add
    `hp.current` and the rest). The core's existing `field_incomplete()`
    path then gates the tick as `lens-incomplete`, exactly as it does for
    a row that lacked a field on 0.6.3."""
    hp = data.get("hp") if isinstance(data.get("hp"), dict) else {}
    node = data.get("node") if isinstance(data.get("node"), dict) else {}
    acct = data.get("account") or {}
    musu = data.get("musu") or {}
    state = str(data.get("state") or "")
    missing: list[str] = []
    if data.get("incomplete") is True:
        # lens 1.0.0: the daemon itself says this row is not a reading
        missing.append("incomplete")
    if not state:
        missing.append("state")
    if not _num(hp.get("current")):
        missing.append("hp.current")
    node_index = int(node["index"]) if _num(node.get("index")) else None
    if state == HARVESTING and node_index is None:
        missing.append("node.index")
    rate: float | None = None
    raw_rate = data.get("hpRatePerHr")
    if _num(raw_rate) or (isinstance(raw_rate, str) and raw_rate.strip()):
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            rate = None
    return KamiView(
        index=int(data["index"]),
        state=state,
        hp_current=int(hp.get("current")) if _num(hp.get("current")) else 0,
        hp_total=int(hp.get("total")) if _num(hp.get("total")) else 0,
        node_index=node_index,
        musu_accrued=int(musu.get("accrued", 0) or 0),
        cooldown_sec=int(data.get("cooldownSec", 0) or 0),
        level=int(data["level"]) if _num(data.get("level")) else None,
        account_index=int(acct["index"]) if _num(acct.get("index")) else None,
        hp_rate_per_hr=rate,
        missing=tuple(missing),
    )


def kami_view_from_chain(index: int, state: str, hp_current: int, hp_total: int, node_index: int | None, level: int | None = None) -> KamiView:
    """A KamiView built from a chain point read (keeper/chainhp.py)
    so the feed guard (`feed_withheld`, `record_feed`) sees the same shape
    it sees on a lens read. It is COMPLETE by construction (the chain
    answered state, the HP projection and the harvest's node) and carries
    no drain rate (the guard runs `stale=True` under the fallback, where
    only the cooldown clears it)."""
    return KamiView(index=int(index), state=str(state), hp_current=int(hp_current), hp_total=int(hp_total), node_index=node_index, musu_accrued=0, cooldown_sec=0, level=level, account_index=None, hp_rate_per_hr=None, missing=())


@dataclass
class NodeOccupant:
    """One row of a `node --with-vitals` answer (`auto`): a harvest on the
    node, whoever owns it. `vitals_missing` names what the row lacked; a
    FOREIGN row lacking vitals is never a gated tick (its threshold is
    simply unknown until previewed). A row of the PLAIN census (`node <i> --full`, no `--with-vitals`) carries
    `id, state, kami, account` and nothing else by construction — it is a
    plain occupant (`vitals_missing` empty, HP / cooldown 0, level None),
    never a row that lacked something."""

    harvest_id: str
    kami_id: str
    kami_index: int
    account_index: int | None
    hp_current: int
    hp_total: int
    hp_rate_per_hr: float | None
    cooldown_sec: int
    level: int | None
    musu_accrued: int
    vitals_missing: tuple[str, ...] = ()
    # the pairwise preview (`node <i> <attacker>`): served threshold, may be
    # negative; None when the answer carried no `liquidation` block
    threshold: int | None = None
    liquidation_reason: str | None = None
    # lens 1.0.0: the daemon flagged this row `incomplete: true` — it
    # carries identity and `state` and nothing else, so it is PRESENCE and
    # never a reading. A plain-census row of an incomplete kami still
    # counts as an occupant; the flag matters where vitals are read (the
    # preview: such a row never becomes a line — keeper/previews.py).
    incomplete: bool = False


def node_occupant(row: dict[str, Any], vitals_expected: bool = True) -> NodeOccupant | None:
    """Parse one `node` harvest row; None when it names no kami index.
    `vitals_expected` False (the plain census): the read asked for no
    vitals, so their absence is not recorded as missing. Lens 1.0.0:
    an `incomplete: true` row sets `incomplete` and records it in
    `vitals_missing` WHATEVER the read asked for."""
    kami = row.get("kami") if isinstance(row.get("kami"), dict) else {}
    if not _num(kami.get("index")):
        return None
    acct = row.get("account") if isinstance(row.get("account"), dict) else {}
    vit = row.get("vitals") if isinstance(row.get("vitals"), dict) else {}
    hp = vit.get("hp") if isinstance(vit.get("hp"), dict) else {}
    missing: list[str] = []
    if not vit:
        if vitals_expected:
            missing.append("vitals")
    else:
        if not _num(hp.get("current")):
            missing.append("hp.current")
        if not _num(hp.get("total")):
            missing.append("hp.total")
        if not _num(vit.get("level")):
            missing.append("level")
    rate: float | None = None
    raw_rate = vit.get("hpRatePerHr")
    if _num(raw_rate) or (isinstance(raw_rate, str) and raw_rate.strip()):
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            rate = None
    incomplete = row.get("incomplete") is True
    if incomplete:
        # whatever the read asked for: an incomplete row has no vitals by
        # construction (the 1.0.0 schema forbids them) and the keeper must
        # not read one as a line even under the plain census
        missing.append("incomplete")
    liq = row.get("liquidation") if isinstance(row.get("liquidation"), dict) else None
    thr = int(liq["threshold"]) if liq is not None and _num(liq.get("threshold")) else None
    return NodeOccupant(
        harvest_id=str(row.get("id") or ""),
        kami_id=str(kami.get("id") or ""),
        kami_index=int(kami["index"]),
        account_index=int(acct["index"]) if _num(acct.get("index")) else None,
        hp_current=int(hp.get("current")) if _num(hp.get("current")) else 0,
        hp_total=int(hp.get("total")) if _num(hp.get("total")) else 0,
        hp_rate_per_hr=rate,
        cooldown_sec=int(vit.get("cooldownSec", 0) or 0),
        level=int(vit["level"]) if _num(vit.get("level")) else None,
        musu_accrued=int(vit.get("musuAccrued", 0) or 0),
        vitals_missing=tuple(missing),
        threshold=thr,
        liquidation_reason=str(liq.get("reason")) if liq is not None and liq.get("reason") is not None else None,
        incomplete=incomplete,
    )


@dataclass
class Observation:
    basis: Basis
    kamis: dict[int, KamiView] = field(default_factory=dict)
    inventory: dict[int, int] | None = None  # item index -> balance; None = not read
    stamina: int | None = None
    answers: list[LensAnswer] = field(default_factory=list)
    # `auto`: every harvest on the book's node, by kami index
    occupants: dict[int, NodeOccupant] = field(default_factory=dict)
    # what the observation cost and noted (tick summary lines)
    notes: dict[str, Any] = field(default_factory=dict)
    # the plain census: the liquidation cooldown of the attackers
    # it was READ for this pass, by kami index — from the preview answer or
    # one `kami <index>` read; an attacker absent from it counts as cooldown
    # 0 (it binds). None = the vitals census: the occupant rows carry it.
    attacker_cooldowns: dict[int, int] | None = None

    def latest_basis(self) -> Basis:
        """The basis of the answers this observation rests on: the lowest
        reconciledThrough and the lowest appliedThrough (the weakest
        guarantee of either mark over the answers that carried one — 1.0.0;
        None when none did, which is every 0.6.3 answer) and the highest
        block."""
        blocks = [a.block for a in self.answers if a.block is not None]
        rts = [a.reconciled_through for a in self.answers if a.reconciled_through is not None]
        ats = [a.applied_through for a in self.answers if a.applied_through is not None]
        return Basis(lensBlock=max(blocks) if blocks else None, lensReconciledThrough=min(rts) if rts else None, lensAppliedThrough=min(ats) if ats else None)

    def incomplete(self) -> list[str]:
        """SPEC P4: one line per kami row that lacked a field the
        decision needs; empty when every row is complete. An incomplete
        observation gates the tick (`lens-incomplete`) — it is never
        compared against the plan."""
        return [f"kami {k}: missing {', '.join(v.missing)}" for k, v in sorted(self.kamis.items()) if v.missing]

    def absent_kamis(self) -> list[int]:
        """The owned kamis with NO ROW in the answer."""
        return [k for k, v in sorted(self.kamis.items()) if v.absent]

    def field_incomplete(self) -> list[str]:
        """The `incomplete()` lines that are NOT plain absences: a row the
        lens served but without a field the decision needs. Non-empty =
        the read is invalid (gated) whatever else it says."""
        return [f"kami {k}: missing {', '.join(v.missing)}" for k, v in sorted(self.kamis.items()) if v.missing and not v.absent]


@dataclass
class Decision:
    kind: str
    parameters: dict[str, Any]
    intents: list[Intent] = field(default_factory=list)
    # plan updates once the intents are submitted: {kami: {"state", "node"}}
    plan_after: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class StrategyContext:
    """Keeper-level services bound to every strategy: the
    threshold preview cache and the owner ledger (shared by every book on
    a node), the clock, the profile, and two pass-scoped tables the core
    owns — `node_occupants` (node -> {kami entity id: OccupantRef} from
    the last node read, what the kill scanner resolves DEAD writes
    against) and `scan` (the last kill-scan outcome: flags, counts).
    `census` — the node census cache (keeper/census.py), per node, shared
    by every book on it, never persisted. `watchlist` — the world killer
    list the operator's tooling writes (keeper/watchlist.py, read only) —
    and `watch`, the thresholds its kamis serve per node, also shared by
    every book on the node. `standing_owners` — the keeper's `standingOwners`, owners
    whose attackers bind as if killed-here, without a kill and without a
    window."""

    previews: Any = None
    owners: Any = None
    census: Any = None
    watchlist: Any = None
    watch: Any = None
    standing_owners: frozenset[int] = frozenset()
    clock: Any = None
    profile: str = "lab"
    node_occupants: dict[int, dict[int, Any]] = field(default_factory=dict)
    scan: dict[str, Any] = field(default_factory=dict)


class Strategy:
    type_name: str = ""
    implemented: bool = True

    def __init__(self, book: Book):
        self.book = book
        # keeper-level services a strategy may use (preview
        # cache, owner ledger, clock); bound by the core in `attach`
        self.context: Any = None

    def attach(self, context: Any) -> None:
        """Bind the keeper-level services. Default: keep them."""
        self.context = context

    # -- perception ----------------------------------------------------------
    def observe(self, lens: LensClient) -> Observation:
        raise NotImplementedError

    def refuse_start(self, obs: Observation) -> str | None:
        """A reason the START must be refused on this valid adoption read
        (an `auto` kami HARVESTING on another node), or None. The core
        answers BAD_ARGS with it and stores nothing."""
        return None

    def status_extra(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        """SPEC P3: per-type status counts served under the
        book's `strategy` key; None for a type with none."""
        return None

    # -- decision ------------------------------------------------------------
    def assess(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        raise NotImplementedError

    def assess_under_stale(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """SPEC P4 (the stale-lens allowance): what a book may still do
        under `lens-stale:<n>` alone. Default: nothing. `harvestAndFeed`
        returns a cooldown-guarded feed and never a start / stop /
        collect."""
        return []

    def assess_under_chain(self, reading: Any, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """SPEC P4 (the chain fallback): what a book may do from a
        CHAIN point read of its own kamis while the lens is out
        (`lens-unavailable` / `lens-not-live` / `lens-degraded:*` for at
        least `chainFallbackAfterSec`). Default: nothing. A feed book
        returns cooldown-guarded FEEDS (`chainFeedCooldownSec`) for kamis
        the chain says are HARVESTING on the book's node below the floor,
        and never a start / stop / collect / revive. A REST
        `auto` book returns the floor rests — a `harvest stop` for each of
        its kamis the chain says is HARVESTING on the book's node at or
        below the rest floor, one attempt per kami per `chainFeedCooldownSec`."""
        return []

    def chain_fallback_feeds(self) -> bool:
        """True when this book takes the chain-backed feed branch at all:
        a feeding book with `chainFallback` on."""
        return False

    def chain_fallback_rests(self) -> bool:
        """True when this book takes the chain-backed REST branch: an
        `auto` REST book with `chainFallback` on."""
        return False

    def expected_ticks_between_acts(self) -> int | None:
        return None

    def watches_node(self) -> bool:
        """True when this book takes the NODE-THREAT path — the
        node census, the preview pass, the attacker classes and the kill
        scan (`auto`, and a `harvestAndFeed` book in ladder mode). The core
        gates the boot / start seed, the scan flags on the tick, the tick
        row's `killScan` note and the kill-scan book set on this, not on the
        strategy type."""
        return False

    # -- the plan (SPEC P7) --------------------------------------------------
    def owned_kamis(self) -> list[int]:
        return []

    def revive_items(self) -> list[int]:
        return []

    def external_change(self, obs: Observation) -> str | None:
        """A contradiction between the plan and the observed HARVEST STATE
        or NODE of a book kami: a description, or None. HP, level, skills
        and item use are coexistence and never trigger."""
        for k in self.owned_kamis():
            planned = self.book.plan.get(str(k))
            view = obs.kamis.get(k)
            if not planned or view is None or view.state == DEAD:
                continue
            if view.absent:
                return absent_contradiction(k)
            p_state, p_node = planned.get("state"), planned.get("node")
            if p_state == HARVESTING and (view.state != HARVESTING or view.node_index != p_node):
                return f"kami {k}: planned {HARVESTING} on node {p_node}, observed {view.state} on node {view.node_index}"
            if p_state == RESTING and view.state == HARVESTING:
                return f"kami {k}: planned {RESTING}, observed {HARVESTING} on node {view.node_index}"
        return None

    def world_matches_plan(self, obs: Observation) -> bool:
        """SPEC P7 fleet re-adopt precondition: every owned kami is on the
        book's node in a state the book can own (HARVESTING there, or
        RESTING); a kami harvesting elsewhere, or dead, is a contradiction."""
        node = self.book.config.get("nodeIndex")
        for k in self.owned_kamis():
            view = obs.kamis.get(k)
            if view is None or view.state == DEAD or view.absent:
                return False
            if view.state == HARVESTING and view.node_index != node:
                return False
            if view.state not in (HARVESTING, RESTING):
                return False
        return True

    def adopt(self, obs: Observation) -> None:
        """Set the plan to what is observed (start-adoption and readopt)."""
        for k in self.owned_kamis():
            view = obs.kamis.get(k)
            if view is None:
                continue
            if view.state == HARVESTING:
                self.book.plan[str(k)] = {"state": HARVESTING, "node": view.node_index}
            elif view.state == RESTING:
                self.book.plan[str(k)] = {"state": RESTING, "node": None}

    def newly_dead(self, obs: Observation, now_sec: float | None = None) -> list[int]:
        """The book kamis the world reads DEAD that are owed a revive OFFER
        this tick: a NEW death (no `dead` flag in the plan), a legacy plan
        entry (`dead` with no `reviveRetryAt` key —
        offered once more, and the offer re-marks it with the key so this
        fires once per legacy entry), or an entry whose `reviveRetryAt` has
        come. A refused, reverted or lost revive is therefore never final:
        an item that extends a dead kami's cooldown makes the pre-send check
        refuse the revive ("kami on cooldown"), and without a retry the kami
        would stay DEAD in the world and in the plan."""
        out = []
        for k in self.owned_kamis():
            view = obs.kamis.get(k)
            if view is None or view.state != DEAD:
                continue
            entry = self.book.plan.get(str(k)) or {}
            if not entry.get("dead"):
                out.append(k)
            elif "reviveRetryAt" not in entry:
                out.append(k)
            else:
                at = entry.get("reviveRetryAt")
                if _num(at) and now_sec is not None and float(now_sec) >= float(at):
                    out.append(k)
        return out

    def is_revive_retry(self, kami: int) -> bool:
        """The plan already holds this kami DEAD, so the offer the
        core is about to make is a RE-offer, not a new death."""
        return bool((self.book.plan.get(str(kami)) or {}).get("dead"))

    def mark_dead(self, kami: int, retry_at: float | None = None) -> None:
        """`reviveRetryAt` is ALWAYS written — None means "never
        offer again by the timer" (revives disabled)."""
        self.book.plan[str(kami)] = {"state": DEAD, "node": None, "dead": True, "reviveRetryAt": retry_at}

    def apply_plan(self, decision: Decision) -> None:
        for k, v in decision.plan_after.items():
            self.book.plan[str(k)] = dict(v)

    def plan_state_for(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
        """The plan entry a LANDED decision of this kind writes
        per member — the one truth `assess` hands `plan_after` — or None
        when this strategy does not state one for that kind (the core then
        leaves the plan alone and says so). A start plans the node the
        DECISION recorded, never one inferred."""
        if kind == "start":
            node = parameters.get("node", self.book.config.get("nodeIndex"))
            if not isinstance(node, int) or isinstance(node, bool):
                return None
            return {"state": HARVESTING, "node": int(node)}
        if kind in ("rest", "failsafe-stop"):
            return {"state": RESTING, "node": None}
        return None

    def decision_from_ledger(self, kind: str, parameters: dict[str, Any], kamis: list[int] | None = None) -> Decision | None:
        """The Decision a ledger `decision` row stands for, with
        the `plan_after` that decision would have applied — for the boot
        close of a step the chain took while the daemon was down
        (`core._close_open_tx`). The intents are NOT rebuilt: nothing is
        signed from here, only the plan effect of a step already on chain.
        `kamis` limits the effect to the members the closed row named (a
        single re-offered after a batch). None when this strategy cannot
        state the effect of that kind; a kind with no plan effect
        (`NO_PLAN_KINDS`) answers a Decision with an empty `plan_after`."""
        if kind in NO_PLAN_KINDS:
            return Decision(kind, dict(parameters))
        state = self.plan_state_for(kind, parameters)
        members = decision_kamis(parameters) if kamis is None else [int(k) for k in kamis]
        if state is None or not members:
            return None
        return Decision(kind, dict(parameters), [], {str(k): dict(state) for k in members})

    def on_records(self, decisions: list[Decision], records: list[Any], now_sec: float, runtime: dict[str, Any]) -> None:
        """What the signer reported per intent, in decision order:
        a strategy that needs the chain's verdict (craft: park an exhausted
        recipe on a pre-check revert) reads it here. Default: nothing."""
        return None


def absent_contradiction(kami: int) -> str:
    """The external-change reason for a kami with no row in
    the account's party answer on `externalChangeTicks` consecutive
    otherwise-valid reads."""
    return f"kami {int(kami)}: not in the account's party"


def ticks_for(seconds: float, tick_seconds: int) -> int:
    return max(1, int(math.ceil(seconds / max(1, tick_seconds))))


# ---- feed idempotence (SPEC P4) -------------------------------------------
#
# Why: until the lens applies a feed's HP sync, the next tick still reads
# HP < floor and would feed the same kami again. After a feed LANDS the keeper records
# what it fed against and does not feed that kami again until the lens
# has visibly applied it (HP rose past the pre-feed read, to at least the
# expected post-feed HP less the drain since) or `feedCooldownSec` has
# elapsed. Shared here so `auto` uses the same guard.

# Heal in HP per food item (the item's HP effect in the game's item
# registry): 11301 Maple-Flavor Ghost Gum HP+25,
# 11302 Cheeseburger HP+50, 11303 Pom-Pom Fruit Candy HP+50, 11304 Gakki
# Cookie Sticks HP+100, 11311 Resin HP+35, 11312 Honeydew Scale HP+75,
# 11313 Golden Apple HP+150, 11314 Blue Pansy HP+25. An item absent here uses the +1 rule (`expectedHpAfter
# = lastFeedHp + 1`), which makes the "visibly applied" test "HP rose".
FOOD_HEAL_HP: dict[int, int] = {
    11301: 25,
    11302: 50,
    11303: 50,
    11304: 100,
    11311: 35,
    11312: 75,
    11313: 150,
    11314: 25,
}


def food_heal(item: int) -> int | None:
    return FOOD_HEAL_HP.get(int(item))


def _guards(runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return runtime.setdefault("feedGuard", {})


def record_feed(runtime: dict[str, Any], kami: int, now_sec: float, hp_before: int, hp_total: int, item: int) -> dict[str, Any]:
    """Called once a feed's record LANDED (success / unconfirmed / dry_run
    with a passing pre-check): the read the decision used and the HP the
    lens should show once it has applied the feed."""
    heal = food_heal(item)
    expected = hp_before + (heal if heal is not None else 1)
    if hp_total > 0:
        expected = min(expected, hp_total)
    g = {"lastFeedAt": float(now_sec), "lastFeedHp": int(hp_before), "expectedHpAfter": int(expected), "item": int(item), "heal": heal}
    _guards(runtime)[str(kami)] = g
    runtime["lastFeedAt"] = float(now_sec)  # interval mode reads this
    return g


def drain_allowance(hp_rate_per_hr: float | None, elapsed_sec: float) -> int:
    """HP the kami may legitimately have lost since the feed: the lens's
    projected rate over the elapsed time, plus one for rounding."""
    rate = abs(hp_rate_per_hr) if hp_rate_per_hr is not None else 0.0
    return 1 + int(math.ceil(rate * max(0.0, elapsed_sec) / 3600.0))


def feed_withheld(runtime: dict[str, Any], kami: int, view: KamiView, now_sec: float, cooldown_sec: int, stale: bool = False) -> dict[str, Any] | None:
    """None when a feed may be signed; else the guard detail explaining why
    it is withheld (the tick summary carries it). Under `stale` the lens
    HP read cannot show the feed applied, so only the cooldown clears."""
    g = _guards(runtime).get(str(kami))
    if not g:
        return None
    elapsed = now_sec - float(g["lastFeedAt"])
    if elapsed >= cooldown_sec:
        return None
    if not stale:
        allowance = drain_allowance(view.hp_rate_per_hr, elapsed)
        applied = view.hp_current > int(g["lastFeedHp"]) and view.hp_current >= int(g["expectedHpAfter"]) - allowance
        if applied:
            return None
    return {
        "kami": int(kami), "hp": view.hp_current, "lastFeedHp": g["lastFeedHp"], "expectedHpAfter": g["expectedHpAfter"],
        "sinceFeedSec": int(elapsed), "feedCooldownSec": int(cooldown_sec), "reason": "feed not yet visible in the lens HP read" if not stale else "lens stale; cooldown not elapsed",
    }
