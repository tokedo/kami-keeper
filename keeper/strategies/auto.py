"""`auto` — a kami set on one node (SPEC P2).

Per tick (observation -> action, no policy language):
  observe   party <accountIndex> --full   -> every owned kami (a kami
            absent from the answer is an INCOMPLETE observation of that
            kami: unknown, never RESTING; absent on
            externalChangeTicks consecutive otherwise-valid reads = an
            external change of that kami, the core pauses the book)
            node <nodeIndex> --with-vitals --full -> every occupant;
            FOREIGN = account index not in friends and not the book's;
            rows of the book's own account outside kamiIndices are never
            foreign and never previewed
            inventory <accountIndex> when regenMethod FEED or reviveItems
            previews due (keeper/previews.py; <= previewsPerTick per tick)
            the read policy (keeper/census.py; a stored config
            without the keys keeps the three lines above as they are):
              previewPolicy killed-here -> only attackers whose owner or
                kami is killed-here inside the window (plus a remembered
                one that came back) are fetched, refreshed or counted
                pending; an attacker never read and never killed-here has
                threshold 0 by construction: never `unknown`, never binds
              nodeCensus plain -> node <nodeIndex> --full (no vitals);
                cooldown and level of the attackers that can bind from the
                preview answer of the pass or one `kami <index>` read each
                (<= attackerVitalsPerTick per pass and node; none = binds)
              nodeCensusEverySec > tickSeconds -> the census of an earlier
                pass is reused (`censusAgeSec`); a kill row on the node
                forces a read; own kamis are `party --full` every pass
  bound(k)  over attackers X on / remembered for the node whose owner is
            not listed, skipping X while X.cooldownSec > cooldownHorizonSec
            (and every threshold-no-kill X when passiveOwnersBind is false):
            (the class is per ATTACKER X: X is killed-here when its
            owner killed on this node / killed ours anywhere inside the
            window, OR the kami X itself did, whoever owns it now)
              killed-here      -> max(0, thr(X,k)) + safetyMarginPct % x hpTotal
              standing         -> max(0, thr(X,k)) + safetyMarginPct % x hpTotal (the owner is on the keeper's standingOwners; no kill needed, no window)
              threshold-no-kill-> max(0, thr(X,k)) + passiveMarginPct % x hpTotal
              no-threshold     -> 0
            thr from the lens preview only; an attacker never previewed
            makes the kami's bound UNKNOWN: the book neither rests nor
            deploys it on the bound this tick (`held`), the floor still
            rests it
  restFloor restFloorPct % x hpTotal — `harvest stop` reverts on a kami at
            HP 0 ("feed first", harness 3.4.0), so a REST book stops first
  HARVESTING on nodeIndex, hp <= max(bound, restFloor):
            REST -> stop, batched per book <= 12 (executeBatchedAllowFailure)
            with stopJoinWindowMin > 0 such a stop also takes
            the book's harvesting kamis whose projected minutes to their
            OWN limit — (hp - max(bound, restFloor)) / this tick's drain
            rate — are within the window, nearest first, until the pack
            holds stopPackMax (`pulled` + `projectedMinutes` on the detail
            row, the tick note `stopPulled`, the count `kamis.pulled`); a
            joiner is only ever stopped EARLIER than its limit, an anchor
            is never held for company, and a joiner of a pack that did not
            land is not re-offered as a single
            FEED -> use-item foodItem per kami (feed-idempotence guard); no food -> stop
            autoCollect and musuAccrued >= bountyCollectThreshold -> collect
            (pooled per account by the core; a mid-harvest collect resets
            the harvest's intensity clock)
  RESTING   hp >= resumeHpPct % x hpTotal and hp > bound + drain x 1 h
            -> READY: start, batched per book <= 10 (`BATCH_CAPS`;
            executeBatched); else
            held. With startBatchWaitSec > 0 a ready kami waits
            (`startQueue`: kami -> first read ready, persisted) until
            startBatchMin of the book are ready or the oldest has waited
            startBatchWaitSec, then every ready kami goes out at once; a
            kami no longer ready (started, bound above its HP, backoff,
            held) leaves the queue; the wait is the tick note
            `startWaiting` and the status count `kamis.waiting`
            with startJoinWindowMin > 0 a tick that starts at
            least one kami anyway also starts the book's RESTING kamis
            whose projected minutes to resumeHpPct — (ceil(resumeHpPct x
            hpTotal / 100) - hp) / this tick's regen rate — are within the
            window, nearest first, into the FREE places of the last start
            chunk only (`startJoined` + `projectedMinutes` + `regenPerHr`
            on the detail row, the tick note `startJoined`, the count
            `kamis.joined`); a joiner is only ever started EARLIER than
            its readiness, nothing is ever held longer, a joiner never
            adds a transaction, and a joiner of a start that did not land
            is not re-offered as a single
  refused   a kami the chain refuses to start / stop waits startBackoffSec
            (`backoff`, a `deviation` row), not retried every tick; a
            refusal that is not a verdict (pre-check ok, the node refused
            the broadcast with a non-revert text) waits
            min(startBackoffSec, 60 s) instead and the deviation row reads
            `transient: true`
  cooldown  a kami the lens reads with cooldownSec > 0 is neither
            started (it leaves the start queue, so its wait starts over when
            the cooldown ends) nor, at or below its limit on a REST book,
            stopped this tick, and is never a stop joiner — the chain
            refuses both pre-send (`kamis.cooldown`, `cooldownDeferred[]`);
            a FEED book's feed decision is NOT deferred. A `kami on
            cooldown` refusal that reaches the chain all the same waits
            min(startBackoffSec, COOLDOWN_REFUSAL_BACKOFF_SEC), not an hour
  DEAD      the core's death block (row, revive from inventory)
  elsewhere the core's P7 (external change) — the set rule in base.py
  stale     FEED books feed (the stale-lens guards); REST books do nothing; never
            a rest, start or collect on a stale HP read
  outage    a FEED book with chainFallback feeds from a chain
            point read of its own kamis at or below restFloorPct x hpTotal
            — never the bound, never a rest / start / collect / revive;
            a REST book with chainFallback stops every kami of
            the same reading at or below the floor, one attempt per kami
            per chainFeedCooldownSec, and signs nothing else
  parking   after `park <book>`: no start, feed or collect;
            `harvest stop` for every kami HARVESTING on the node, in packs
            of PARK_PACK_MAX and at most PARK_PACKS_PER_TICK packs a tick,
            on VALID reads only (the core gates — never the stale or the
            chain branch); revives as usual; the book stops ITSELF once a
            valid read past its last act shows none HARVESTING on the node,
            none DEAD and nothing pending (`assess_parking`)

A batched stop uses `executeBatchedAllowFailure`, so the tx lands even
when a member reverts: the plan marks each member RESTING `pending`,
and the next valid read settles it — RESTING confirms, HARVESTING on
the node means the stop did not apply to that member (`restNotApplied`,
re-decided alone, backoff on refusal). Neither reading is an external
change (`external_change` / `world_matches_plan` below).

The node census, the preview pass, the attacker classes and bounds:
keeper/strategies/threat.py (the `NodeThreat` mixin, kept in its own
module so that `harvestAndFeed` shares the same threat model).
"""

from __future__ import annotations

import math
from typing import Any

from ..lens import LensClient, LensQueryError
from ..owner_class import OwnerClass, render, worst
from ..signer import BATCH_CAPS, Intent
from .base import DEAD, HARVESTING, MISSING_ROW, RESTING, Decision, KamiView, Observation, Strategy, absent_contradiction, feed_withheld, food_heal, kami_view, kami_view_from_chain, record_feed
from .threat import PREVIEW_ALL, PREVIEW_KILLED_HERE, NodeThreat  # noqa: F401  (PREVIEW_ALL: the import path `keeper.strategies.auto.PREVIEW_ALL` stays live)


# The wait after a `kami on cooldown` refusal — the lens normally
# makes the refusal impossible, so this only covers a lens / chain
# disagreement, and 5 min is one re-offer, not an hour.
COOLDOWN_REFUSAL_BACKOFF_SEC = 300

# The park PACK. `harvest stop` takes up to 12 kamis in one transaction
# (the whitelist cap), and the pack is 10; 5 packs a tick is ONE burst of 5 consecutive nonces on the
# account's single operator lane — "sequential" — so 50 kamis leave a tick
# and a 300-kami book parks in ~6 ticks.
PARK_PACK_MAX = 10
PARK_PACKS_PER_TICK = 5

# The wait after a refusal that is NOT the chain's verdict on the
# kami — the pre-check passed and the node refused the BROADCAST with a
# non-revert text (a readiness race, a missing batch answer, a transport
# failure, a chunk not offered). One or two ticks, never an hour: the
# re-offer's own pre-check is the guard against a real revert.
TRANSIENT_REFUSAL_BACKOFF_SEC = 60


def refusal_is_verdict(rec: Any, reason: str) -> bool:
    """True when the refusal is the chain's verdict on the kami:
    the pre-check reverted, the tx landed and reverted, or
    the node's text names a revert. Anything else — the pre-check
    passed and the node refused the broadcast for a reason of its
    own — is transient."""
    if getattr(rec, "precheck", None) == "reverted" or rec.status == "reverted":
        return True
    t = reason.lower()
    return "revert" in t


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def margin_needed(hp_rate_per_hr: float | None, tick_seconds: int, lens_lag_blocks: int, block_seconds: float = 2.5) -> float:
    """The HP a margin must cover, made checkable = drain over one tick + drain over the lens lag at the
    read + one block for the attacker's own send (the lens states its
    projection error is unbounded; this is the keeper's stated part)."""
    rate = abs(float(hp_rate_per_hr or 0.0))
    seconds = float(tick_seconds) + float(lens_lag_blocks) * block_seconds + block_seconds
    return rate * seconds / 3600.0


class Auto(NodeThreat, Strategy):
    type_name = "auto"
    implemented = True

    def __init__(self, book: Any) -> None:
        super().__init__(book)
        # the PREVIOUS pass's complete views, by kami — what the
        # kill scan reads the victim's HP from (this pass reads it DEAD,
        # and the scan runs before any book observes). Per instance:
        # `learn_kill` is a book-level measurement.
        self._last_views: dict[int, KamiView] = {}
        self._last_views_at: float = 0.0

    # -- config ------------------------------------------------------------

    @property
    def node(self) -> int:
        return int(self.book.config["nodeIndex"])

    @property
    def account_index(self) -> int:
        return int(self.book.config["accountIndex"])

    def owned_kamis(self) -> list[int]:
        return [int(k) for k in self.book.config.get("kamiIndices", [])]

    def revive_items(self) -> list[int]:
        return [int(i) for i in self.book.config.get("reviveItems", [])] if self.book.config.get("reviveOnDeath", True) else []

    def _cfg(self, key: str, default: Any) -> Any:
        v = self.book.config.get(key)
        return default if v is None else v

    def watches_node(self) -> bool:
        return True

    def expected_ticks_between_acts(self) -> int | None:
        return None  # an auto book may legitimately be quiet for hours

    # -- perception --------------------------------------------------------

    def observe(self, lens: LensClient) -> Observation:
        ctx = self.context
        now = float(ctx.clock()) if ctx is not None and ctx.clock is not None else 0.0
        counters = lens.counters() if hasattr(lens, "counters") else {"calls": 0, "bytes": 0, "servedFromPass": 0}
        party = lens.party(self.account_index, full=True)
        rows: dict[int, dict[str, Any]] = {}
        for r in (party.data or {}).get("kamis", []) if isinstance(party.data, dict) else []:
            if isinstance(r, dict) and _num(r.get("index")):
                rows[int(r["index"])] = r
        kamis: dict[int, KamiView] = {}
        for k in self.owned_kamis():
            if k in rows:
                kamis[k] = kami_view(rows[k])
            else:
                kamis[k] = KamiView(index=k, state="", hp_current=0, hp_total=0, node_index=None, musu_accrued=0, cooldown_sec=0, level=None, account_index=None, missing=(MISSING_ROW,))
        census, node_ans = self._census(lens, now)
        occupants = census.occupants
        obs = Observation(basis=party.basis(), kamis=kamis, answers=[party] + ([node_ans] if node_ans is not None else []), occupants=occupants)
        if self._cfg("regenMethod", "REST") == "FEED" or self.revive_items():
            try:
                inv = lens.inventory(self.account_index)
            except LensQueryError:
                inv = None
            if inv is not None:
                obs.answers.append(inv)
                obs.inventory = {int(r["item"]["index"]): int(r["balance"]) for r in (inv.data or {}).get("items", []) if isinstance(r, dict) and isinstance(r.get("item"), dict)}
        notes: dict[str, Any] = {}
        self._observe_attackers(lens, obs, census, node_ans, party, kamis, now, notes)
        after = lens.counters() if hasattr(lens, "counters") else counters
        notes["lensCalls"] = int(after["calls"] - counters["calls"])
        notes["lensCallsServedFromPass"] = int(after["servedFromPass"] - counters["servedFromPass"])
        notes["lensBytes"] = int(after["bytes"] - counters["bytes"])
        obs.notes = notes
        obs.basis = obs.latest_basis()
        # keep this pass's complete views for the NEXT pass's kill
        # scan — the HP a victim had is the last reading before it died
        self._last_views = {k: v for k, v in kamis.items() if not v.missing}
        self._last_views_at = now
        return obs

    def refuse_start(self, obs: Observation) -> str | None:
        for k in self.owned_kamis():
            v = obs.kamis.get(k)
            if v is not None and v.state == HARVESTING and v.node_index is not None and v.node_index != self.node:
                return f"kami {k} is HARVESTING on node {v.node_index}, not the book's node {self.node}; a plan the world already contradicts is not asserted"
        return None

    # -- the measured line ---------------------------------------------------

    def learn_kill(self, kill: dict[str, Any], now: float) -> dict[str, Any]:
        """What a kill of one of this book's kamis on this
        book's node MEASURED about the killer's line. Called by the core's
        kill scan (before any book observes) for a kill whose
        `victimAccount` is this book's account.

        The victim's HP comes from the PREVIOUS pass's view only — one
        source, always in hand (this pass reads the kami DEAD) — and the
        +1 HP plus the rounding up to 0.1 % cover the <= 2 min of drain
        between that reading and the kill. The served threshold is the
        lens's own number for the pair, never the lifted one. When the HP
        at the kill was ABOVE it, every line of the killer's OWNER on this
        node is lifted by the gap as a share of the victim's max HP
        (`PreviewCache.lift_owner`). Returns the `learned` dict the kill
        row carries."""
        from ..previews import clamp  # local: keeper.previews imports strategies.base

        owner = kill.get("attackerAccount")
        if owner is None:
            return {"skipped": "unattributed"}
        ctx = self.context
        previews = ctx.previews if ctx is not None else None
        victim = kill.get("victimKami")
        view = self._last_views.get(int(victim)) if victim is not None else None
        if view is None or view.state != HARVESTING or view.node_index != self.node or int(view.hp_total) <= 0:
            return {"skipped": "no view of the victim from the previous pass"}
        attacker = kill.get("attackerKami")
        entry = previews.entries(self.node, self.account_index).get(int(attacker)) if previews is not None and attacker is not None else None
        row = entry.rows.get(int(victim)) if entry is not None else None
        served = clamp(row.get("threshold")) if row else None
        if served is None:
            return {"skipped": "no served line for the pair"}
        hp_at_kill = int(view.hp_current)
        gap = hp_at_kill - int(served)
        if gap <= 0:
            # the line explains the kill: the rest was late, nothing is
            # measured. A kill by this owner is still reason to re-read
            # its lines (a bonus may be on them already).
            previews.mark_refetch(self.node, self.account_index, int(owner))
            return {"hpAtKill": hp_at_kill, "served": int(served), "gapHp": gap, "lift": None, "note": "at or below the served line: a late rest"}
        ratio = math.ceil((gap + 1) / int(view.hp_total) * 1000) / 1000
        source = {"victim": int(victim), "hpAtKill": hp_at_kill, "served": int(served), "gapHp": gap, "block": kill.get("block"), "txHash": kill.get("txHash"), "at": float(now), "attacker": int(attacker)}
        lifted = previews.lift_owner(self.node, self.account_index, int(owner), ratio, source)
        out: dict[str, Any] = {"hpAtKill": hp_at_kill, "served": int(served), "gapHp": gap, "liftPct": round(ratio * 100, 1), "lifted": lifted, "owner": int(owner)}
        if entry.lift_from.get("capped"):
            out["capped"] = True
        return out

    # -- the plan (pending rests; SPEC P7) --------------------------------------

    def _plan(self, k: int) -> dict[str, Any]:
        return self.book.plan.get(str(k)) or {}

    def external_change(self, obs: Observation) -> str | None:
        for k in self.owned_kamis():
            planned = self._plan(k)
            view = obs.kamis.get(k)
            if not planned or view is None or view.state == DEAD:
                continue
            if view.absent:
                return absent_contradiction(k)
            if planned.get("pending"):
                # a member of an AllowFailure stop: RESTING confirms, HARVESTING
                # on the book's node means the stop did not apply — neither
                # is an external change; harvesting ELSEWHERE still is
                if view.state == HARVESTING and view.node_index != self.node:
                    return f"kami {k}: planned {RESTING}, observed {HARVESTING} on node {view.node_index}"
                continue
            p_state, p_node = planned.get("state"), planned.get("node")
            if p_state == HARVESTING and (view.state != HARVESTING or view.node_index != p_node):
                return f"kami {k}: planned {HARVESTING} on node {p_node}, observed {view.state} on node {view.node_index}"
            if p_state == RESTING and view.state == HARVESTING:
                return f"kami {k}: planned {RESTING}, observed {HARVESTING} on node {view.node_index}"
        return None

    def _settle_pending(self, obs: Observation) -> list[int]:
        """Resolve `pending` plan entries on a valid read; returns the kamis
        whose stop did not apply (still HARVESTING on the node)."""
        not_applied: list[int] = []
        for k in self.owned_kamis():
            planned = self._plan(k)
            view = obs.kamis.get(k)
            if not planned.get("pending") or view is None or view.missing:
                continue
            if view.state == RESTING:
                self.book.plan[str(k)] = {"state": RESTING, "node": None}
            elif view.state == HARVESTING and view.node_index == self.node:
                self.book.plan[str(k)] = {"state": HARVESTING, "node": self.node}
                not_applied.append(k)
        return not_applied

    # -- decisions ------------------------------------------------------------

    def _cooldown(self) -> int:
        return int(self._cfg("feedCooldownSec", 300))

    def _stale_cooldown(self) -> int:
        return int(self._cfg("staleFeedCooldownSec", 1800))

    def _feed_decision(self, view: KamiView, have: int | None, limit: dict[str, Any], under_stale: bool) -> Decision:
        food = int(self.book.config["foodItem"])
        params: dict[str, Any] = {"kami": view.index, "hp": view.hp_current, "hpMax": view.hp_total, "item": food, "heal": food_heal(food), "inventoryBalance": have, "bound": limit["bound"], "restFloor": limit["floor"], "boundBy": limit["boundBy"]}
        if under_stale:
            params["underStale"] = True
        return Decision("feed", params, [Intent("system.kami.use.item", self.book.account, view.index, {"itemIndex": food, "source": "inventory"}, self.book.id)])

    def _rest_plan_state(self) -> dict[str, Any]:
        """The plan entry a LANDED stop writes per member: RESTING, and
        `pending` until a valid read settles it — a member of an
        AllowFailure batch can fail inside a receipt that succeeded, so
        RESTING is not claimed against the world until the lens confirms
        it (`_settle_pending`). The one place this entry is written, read
        by `assess` and by the boot close alike."""
        return {"state": RESTING, "node": None, "pending": True}

    def plan_state_for(self, kind: str, parameters: dict[str, Any]) -> dict[str, Any] | None:
        """`rest` is this type's own stop — RESTING `pending`."""
        if kind == "rest":
            return self._rest_plan_state()
        return super().plan_state_for(kind, parameters)

    def _batch(self, kind: str, system: str, members: list[dict[str, Any]], cap: int, extra: dict[str, Any], plan_state: dict[str, Any]) -> list[Decision]:
        out: list[Decision] = []
        for i in range(0, len(members), cap):
            chunk = members[i:i + cap]
            kamis = [int(m["kami"]) for m in chunk]
            params: dict[str, Any] = {"kamis": kamis, "batchSize": len(kamis), "detail": chunk, **extra}
            if len(kamis) == 1:
                intent = Intent(system, self.book.account, kamis[0], {"count": 1, **({"nodeIndex": self.node} if system == "system.harvest.start" else {})}, self.book.id)
            else:
                intent = Intent(system, self.book.account, None, {"kamiIndices": kamis, "count": len(kamis), **({"nodeIndex": self.node} if system == "system.harvest.start" else {})}, self.book.id)
            out.append(Decision(kind, params, [intent], plan_after={str(k): dict(plan_state) for k in kamis}))
        return out

    def split_batch(self, d: Decision) -> list[Decision]:
        """A batched start / rest as one single decision per member (the
        core's fallback after a batch that did not land); a feed or collect is never batched and returns itself."""
        if d.kind not in ("start", "rest") or len(d.parameters.get("kamis") or []) <= 1:
            return [d]
        system = d.intents[0].system
        by_kami = {int(m["kami"]): m for m in d.parameters.get("detail") or []}
        out: list[Decision] = []
        for k in d.parameters["kamis"]:
            m = by_kami.get(int(k), {"kami": int(k)})
            # a JOINER of a pack that did not land is not re-offered as
            # a single — it was not due (it is inside stopJoinWindowMin of its
            # own limit, not at it), so it simply joins the next pack. Only the
            # anchors are re-offered, and only an anchor can back off.
            # the same for a start JOINER — it was not ready (it is
            # inside startJoinWindowMin of resumeHpPct, not at it), so it
            # simply joins the next start that leaves.
            if m.get("pulled") or m.get("startJoined"):
                continue
            extra = {kk: v for kk, v in d.parameters.items() if kk not in ("kamis", "batchSize", "detail")}
            out.extend(self._batch(d.kind, system, [m], 1, extra, d.plan_after.get(str(k)) or {}))
        return out

    def assess(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        cfg = self.book.config
        regen = str(self._cfg("regenMethod", "REST"))
        runtime.pop("withheld", None)
        not_applied = self._settle_pending(obs)
        views = {k: v for k, v in obs.kamis.items() if not v.missing and v.state != DEAD}
        limits, classes, attackers, skipped = self.bounds(obs, views)
        backoff: dict[str, dict[str, Any]] = runtime.setdefault("backoff", {})
        for k in list(backoff):
            if float(backoff[k].get("until", 0)) <= now_sec:
                del backoff[k]
        drain: dict[str, float] = runtime.setdefault("drainRate", {})
        counts = {"harvesting": 0, "resting": 0, "dead": 0, "held": 0, "backoff": 0, "elsewhere": 0, "waiting": 0, "pulled": 0, "joined": 0, "cooldown": 0}
        held: list[dict[str, Any]] = []
        # own kamis the lens reads on the chain's action cooldown —
        # the start / stop the chain would refuse pre-send is deferred, not
        # offered and backed off for an hour
        cooldown_rows: list[dict[str, Any]] = []
        rests: list[dict[str, Any]] = []
        starts: list[dict[str, Any]] = []
        feeds: list[Decision] = []
        collects: list[Decision] = []
        withheld: list[dict[str, Any]] = []
        margins: list[int] = []
        bound_kamis = 0
        # nothing of the account is on the node and every unknown attacker
        # is an entry with no rows — a refetch answers the same nothing, so
        # ONE kami of the book goes out to give the refetch an occupant to
        # answer for (the others stay held)
        own_on_node = bool(obs.notes.get("ownOnNode"))
        probe: int | None = None
        food = int(cfg["foodItem"]) if regen == "FEED" and cfg.get("foodItem") is not None else None
        have = None if obs.inventory is None or food is None else obs.inventory.get(food, 0)
        max_drain = max([abs(v.hp_rate_per_hr or 0.0) for v in views.values() if v.state == HARVESTING and v.node_index == self.node] or [0.0])
        for k in self.owned_kamis():
            view = obs.kamis.get(k)
            if view is None or view.missing:
                continue
            if view.state == DEAD:
                counts["dead"] += 1
                continue
            lim = limits[k]
            # the runtime drain rate is recorded BEFORE the row is built,
            # so a `rest` row states this tick's reading and not the last one.
            if view.state == HARVESTING and view.node_index == self.node and view.hp_rate_per_hr is not None:
                drain[str(k)] = abs(float(view.hp_rate_per_hr))
            # `drainPerHr` on the row: the same source the `held` and
            # `starts` rows use — the runtime `drainRate` for this kami, `None`
            # when the keeper has never read one.
            row = {"kami": k, "hp": view.hp_current, "hpTotal": view.hp_total, "bound": lim["bound"], "restFloor": lim["floor"], "boundBy": lim["boundBy"], "drainPerHr": drain.get(str(k))}
            if lim["bound"] > 0:
                bound_kamis += 1
            if view.state == HARVESTING and view.node_index == self.node:
                counts["harvesting"] += 1
                # an unread attacker never LOWERS a bound that is already
                # known — the kami rests at the LARGER of the known bound and
                # the floor, always. `held` still says an attacker is unread
                # (it holds STARTS, not rests), and `minMarginHp` still means
                # "the full bound is known", so margins are appended only when
                # nothing is unknown.
                limit = max(lim["bound"], lim["floor"])
                if lim["unknown"]:
                    counts["held"] += 1
                    held.append({"kami": k, "reason": "bound unknown: attacker not yet previewed", "attackers": lim["unknown"][:5], "bound": lim["bound"], "boundBy": lim["boundBy"]})
                else:
                    margins.append(view.hp_current - lim["bound"])
                if view.hp_current <= limit:
                    if str(k) in backoff and backoff[str(k)].get("kind") == "rest":
                        counts["backoff"] += 1
                    elif regen == "FEED" and (have is None or have > 0):
                        w = feed_withheld(runtime, k, view, now_sec, self._cooldown())
                        if w is None:
                            feeds.append(self._feed_decision(view, have, lim, under_stale=False))
                        else:
                            withheld.append(w)
                    else:
                        if view.cooldown_sec > 0:
                            # the chain refuses `harvest stop` on a kami
                            # inside its action cooldown; the stop is deferred
                            # instead of taking the 3600-s rest backoff
                            counts["cooldown"] += 1
                            cooldown_rows.append({"kami": k, "action": "rest", "cooldownSec": view.cooldown_sec, "hp": view.hp_current, "limit": limit})
                        else:
                            if regen == "FEED":
                                row = dict(row, failsafe="no food in inventory", item=food)
                            rests.append(row)
                if bool(self._cfg("autoCollect", True)) and view.musu_accrued >= int(self._cfg("bountyCollectThreshold", 10000)):
                    collects.append(Decision("collect", {"kami": k, "musuAccrued": view.musu_accrued, "bountyCollectThreshold": int(self._cfg("bountyCollectThreshold", 10000))}, [Intent("system.harvest.collect", self.book.account, k, {"count": 1}, self.book.id)]))
            elif view.state == RESTING:
                counts["resting"] += 1
                if str(k) in backoff:
                    counts["backoff"] += 1
                    continue
                if view.cooldown_sec > 0:
                    # the chain refuses `harvest start` on a kami inside a
                    # cooldown (a transfer's 1-h purchase cooldown, its own
                    # last act). It is not in `starts`, so `_hold_for_batch`
                    # drops it from the start queue: `readySinceSec` starts over
                    # when the cooldown ends. Deliberate.
                    counts["cooldown"] += 1
                    cooldown_rows.append({"kami": k, "action": "start", "cooldownSec": view.cooldown_sec})
                    continue
                resume = int(self._cfg("resumeHpPct", 80))
                if view.hp_total <= 0 or view.hp_current * 100 < resume * view.hp_total:
                    continue
                if lim["unknown"]:
                    if probe is not None or starts or own_on_node or any(a not in lim["unknownNoRows"] for a in lim["unknown"]):
                        counts["held"] += 1
                        held.append({"kami": k, "reason": "bound unknown: attacker not yet previewed", "attackers": lim["unknown"][:5]})
                        continue
                    probe = k  # the one probe start of this tick: next tick it is an occupant and the entry is due again
                rate = float(drain.get(str(k), max_drain))
                need = lim["bound"] + rate
                if view.hp_current <= need:
                    counts["held"] += 1
                    held.append({"kami": k, "reason": "bound above resume HP", "hp": view.hp_current, "bound": lim["bound"], "drainPerHr": rate})
                    continue
                starts.append(dict(row, resumeHpPct=resume, drainPerHr=rate, **({"probe": True} if k == probe else {})))
            elif view.state == HARVESTING:
                counts["elsewhere"] += 1
        # the stop pack. A tick that rests at least one kami takes
        # the nearest harvesting kamis with it (never LATER than their own
        # limit); with stopJoinWindowMin 0 this is exactly what it was.
        rests, pulled = self._pack_stops(rests, views, limits, drain, backoff)
        counts["pulled"] = len(pulled)
        # hold a ready kami for company (startBatchWaitSec). The queue holds
        # every kami ready THIS tick, keyed to the clock at which it was
        # first read ready (a failed start keeps its place; a kami no longer
        # ready leaves); one batched start goes out once startBatchMin are
        # ready or the oldest has waited startBatchWaitSec.
        starts, waiting = self._hold_for_batch(starts, now_sec, runtime)
        counts["waiting"] = len(waiting)
        # the start join. A tick that starts at least one kami
        # anyway takes the nearest nearly-full resting kamis with it, into
        # the FREE places of its last chunk only (never a transaction more);
        # with startJoinWindowMin 0 this is exactly what it was.
        starts, joined = self._join_starts(starts, views, limits, drain, max_drain, backoff, probe)
        counts["joined"] = len(joined)
        decisions: list[Decision] = []
        decisions += feeds
        decisions += self._batch("rest", "system.harvest.stop", rests, int(BATCH_CAPS["system.harvest.stop"]), {"regenMethod": regen}, self._rest_plan_state())
        decisions += self._batch("start", "system.harvest.start", starts, int(BATCH_CAPS["system.harvest.start"]), {"node": self.node}, {"state": HARVESTING, "node": self.node})
        decisions += collects
        if withheld:
            runtime["withheld"] = withheld if len(withheld) > 1 else withheld[0]
        owners_count: dict[str, int] = {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 0, "standing": 0}
        listed = self.listed_accounts()
        listed_present = {o.account_index for o in obs.occupants.values() if o.account_index is not None and int(o.account_index) in listed and int(o.account_index) != self.account_index}
        owners_count["listed"] = len(listed_present)
        # the summary keeps counting OWNERS: an owner whose attackers
        # differ in class (a clean owner holding a kami that killed) counts
        # once, under the worst of them; the kami dimension is `hostileKamis`
        by_owner: dict[Any, list[OwnerClass]] = {}
        previewed: set[Any] = set()
        for a in attackers:
            by_owner.setdefault(a["owner"], []).append(a["class"])
            if a["entry"] is not None:
                previewed.add(a["owner"])
        # under `previewPolicy` killed-here an owner with no kill inside
        # the window is never read. `unpreviewed` COUNTS those owners — it is
        # not a class (P3a: the closed set of rendered names is unchanged):
        # nothing is known of their thresholds and nothing needs to be
        lazy = self.preview_policy == PREVIEW_KILLED_HERE
        if lazy:
            owners_count["unpreviewed"] = 0
        for owner, clss in by_owner.items():
            top = worst(clss)
            if lazy and top is OwnerClass.HARMLESS and owner not in previewed:
                owners_count["unpreviewed"] += 1
                continue
            name = render(top, self.window_hours)
            key = "killed-here" if name.startswith("killed-here-") else name
            owners_count[key] = owners_count.get(key, 0) + 1
        # bound kami-hours by class: the book's kamis
        # with a non-zero bound, keyed `<rendered class>` or, for a
        # killed-here attacker, `<rendered class>/<hostileBy>`
        bound_by_class: dict[str, int] = {}
        for k, lim in limits.items():
            bb = lim.get("boundBy")
            if lim["bound"] > 0 and bb:
                key = str(bb["ownerClass"]) + (f"/{bb['hostileBy']}" if bb.get("hostileBy") else "")
                bound_by_class[key] = bound_by_class.get(key, 0) + 1
        summary: dict[str, Any] = {
            "kamis": counts,
            "owners": owners_count,
            "minMarginHp": min(margins) if margins else None,
            "boundKamis": bound_kamis,
            "boundKamisBy": bound_by_class,
            "hostileKamis": sum(1 for a in attackers if a["present"] and a.get("hostileBy") in ("kami", "both")),
            "cooldownSkipped": skipped,
            "attackers": len(attackers),
            "remembered": sorted(a["kami"] for a in attackers if not a["present"]),
        }
        # the measured lift, only when one is in force — a present
        # attacker whose entry carries one, and the largest of them
        lifted_now = [a["entry"].lift_ratio for a in attackers if a["present"] and a["entry"] is not None and a["entry"].lift_ratio > 0]
        if lifted_now:
            summary["liftedAttackers"] = len(lifted_now)
            summary["liftMaxPct"] = round(max(lifted_now) * 100, 1)
        if held:
            summary["held"] = held[:10]
        if cooldown_rows:
            summary["cooldownDeferred"] = cooldown_rows[:10]
        probe_start = next((int(m["kami"]) for m in starts if m.get("probe")), None)
        if probe_start is not None:
            summary["probeStart"] = probe_start
        if waiting:
            summary["startWaiting"] = waiting
        if pulled:
            summary["stopPulled"] = pulled
        if joined:
            summary["startJoined"] = joined
        if not_applied:
            summary["restNotApplied"] = not_applied
        if backoff:
            summary["backoffKamis"] = sorted(int(k) for k in backoff)
        summary.update(obs.notes)
        runtime["summaryExtra"] = summary
        # under the killed-here policy only those attackers are ever refreshed
        # (a STANDING attacker is fetched and refreshed on the same path,
        # so `nextPreviewDueAt` must count it — the set is the refresh set)
        runtime["autoStatus"] = self._status_counts(summary, {int(a["kami"]) for a in attackers if a["present"] and a["class"] in (OwnerClass.HOSTILE, OwnerClass.STANDING)} if lazy else None)
        return decisions

    def assess_parking(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """SPEC P8: the assessment of a PARKING book. No start, no
        feed, no collect — only `harvest stop` for the book's kamis the read
        shows HARVESTING on its node, in packs of at most PARK_PACK_MAX, at
        most PARK_PACKS_PER_TICK packs this tick (one burst, consecutive
        nonces on the account's one lane). A kami on the chain's action
        cooldown is deferred (`cooldownDeferred`, as on a normal tick) and
        one in rest backoff waits it out, exactly as a rest does; the core's death
        block still offers revives. The book's COMPLETION is marked here —
        `runtime["parkComplete"]` on the first read (past the book's last
        landed act, as every read is) that shows none of its kamis
        HARVESTING on the node, none DEAD and no plan entry pending — and
        the core stops the book on it. No bound is read: nothing here rests
        on a threshold, so a parking tick asks the lens for no preview it
        would not have asked for anyway."""
        regen = str(self._cfg("regenMethod", "REST"))
        runtime.pop("withheld", None)
        not_applied = self._settle_pending(obs)
        views = {k: v for k, v in obs.kamis.items() if not v.missing and v.state != DEAD}
        backoff: dict[str, dict[str, Any]] = runtime.setdefault("backoff", {})
        for k in list(backoff):
            if float(backoff[k].get("until", 0)) <= now_sec:
                del backoff[k]
        # the P3 key set, unchanged — a parking book simply never moves
        # `held`, `waiting`, `pulled` or `joined`
        counts = {"harvesting": 0, "resting": 0, "dead": 0, "held": 0, "backoff": 0, "elsewhere": 0, "waiting": 0, "pulled": 0, "joined": 0, "cooldown": 0}
        rows: list[dict[str, Any]] = []
        cooldown_rows: list[dict[str, Any]] = []
        pending = 0
        for k in self.owned_kamis():
            served = obs.kamis.get(k)
            if served is None or served.missing:
                continue  # an unknown, never an observation (P4)
            if served.state == DEAD:
                counts["dead"] += 1
                continue  # the core's death block offers the revive
            view = views[k]
            if view.state == HARVESTING and view.node_index == self.node:
                counts["harvesting"] += 1
                if self._plan(k).get("pending"):
                    # `_settle_pending` above clears a pending entry on a
                    # complete row, so this cannot be reached; counted
                    # defensively, because a pending stop must never be
                    # read as a finished park
                    pending += 1
                    continue
                if str(k) in backoff and backoff[str(k)].get("kind") == "rest":
                    counts["backoff"] += 1
                    continue
                if view.cooldown_sec > 0:
                    # the chain refuses `harvest stop` inside the action
                    # cooldown; the stop is deferred to a later tick, and the
                    # park does not complete while it is owed
                    counts["cooldown"] += 1
                    cooldown_rows.append({"kami": k, "action": "rest", "cooldownSec": view.cooldown_sec, "hp": view.hp_current})
                    continue
                rows.append({"kami": k, "hp": view.hp_current, "hpTotal": view.hp_total, "parked": True})
            elif view.state == RESTING:
                counts["resting"] += 1  # never a start while parking
            elif view.state == HARVESTING:
                counts["elsewhere"] += 1  # P7's business; never stopped
        rows = rows[:PARK_PACK_MAX * PARK_PACKS_PER_TICK]
        decisions = self._batch("rest", "system.harvest.stop", rows, PARK_PACK_MAX, {"regenMethod": regen, "parking": True}, self._rest_plan_state())
        summary: dict[str, Any] = {
            "kamis": counts,
            "parking": True,
            "parkOffered": len(rows),
            "parkPacks": len(decisions),
            "parkRemaining": counts["harvesting"] - len(rows),
        }
        if cooldown_rows:
            summary["cooldownDeferred"] = cooldown_rows[:10]
        if not_applied:
            summary["restNotApplied"] = not_applied
        if backoff:
            summary["backoffKamis"] = sorted(int(k) for k in backoff)
        summary.update(obs.notes)
        runtime["summaryExtra"] = summary
        runtime["autoStatus"] = self._status_counts(summary)
        if counts["harvesting"] == 0 and counts["dead"] == 0 and pending == 0:
            runtime["parkComplete"] = {
                "kamis": len(self.owned_kamis()),
                "resting": counts["resting"],
                "dead": 0,
                "elsewhere": counts["elsewhere"],
                "lensBlock": obs.basis.lensBlock,
                "lensAppliedThrough": obs.basis.lensAppliedThrough,
            }
            return []
        return decisions

    def _pack_stops(self, rests: list[dict[str, Any]], views: dict[int, KamiView], limits: dict[int, dict[str, Any]], drain: dict[str, float], backoff: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(the kamis to rest now, the JOINERS pulled into the
        pack). On a tick with at least one ANCHOR (a kami at or below its own
        limit — the rest rule, unchanged), the harvesting kamis of the book
        whose PROJECTED minutes to their own limit are within
        `stopJoinWindowMin` ride the same stop, nearest first, until the pack
        holds `stopPackMax`. One-directional: a joiner is only ever stopped
        EARLIER than its limit, an anchor is never held back, and with the
        window at 0 (the default) nothing is projected."""
        window = float(self._cfg("stopJoinWindowMin", 0) or 0)
        if window <= 0 or not rests or str(self._cfg("regenMethod", "REST")) != "REST":
            return rests, []  # a FEED book rests nothing: the pack is a REST-book rule
        room = max(0, max(1, int(self._cfg("stopPackMax", 12))) - len(rests))
        if room <= 0:
            return rests, []  # the anchors alone fill the pack; they chunk at the stop cap as usual
        anchors = {int(m["kami"]) for m in rests}
        cands: list[tuple[float, int, dict[str, Any]]] = []
        for k in self.owned_kamis():
            view, lim = views.get(k), limits.get(k)
            if view is None or lim is None or k in anchors or lim["unknown"]:
                continue  # an UNKNOWN bound is an unknown distance: never a joiner
            if view.state != HARVESTING or view.node_index != self.node:
                continue
            if str(k) in backoff and backoff[str(k)].get("kind") == "rest":
                continue
            if view.cooldown_sec > 0:
                continue  # the chain would refuse this member's stop; never a joiner
            rate = drain.get(str(k))
            if not _num(rate) or float(rate) <= 0:
                continue  # no drain rate read this tick: nothing to project from
            rate = float(rate)
            minutes = (view.hp_current - max(lim["bound"], lim["floor"])) / rate * 60.0
            if minutes <= window:
                cands.append((minutes, k, {"kami": k, "hp": view.hp_current, "hpTotal": view.hp_total, "bound": lim["bound"], "restFloor": lim["floor"], "boundBy": lim["boundBy"], "drainPerHr": rate, "pulled": True, "projectedMinutes": round(minutes, 1)}))
        cands.sort(key=lambda c: (c[0], c[1]))
        joiners = [row for _m, _k, row in cands[:room]]
        return rests + joiners, [{"kami": int(row["kami"]), "projectedMinutes": row["projectedMinutes"]} for row in joiners]

    def _join_starts(self, starts: list[dict[str, Any]], views: dict[int, KamiView], limits: dict[int, dict[str, Any]], drain: dict[str, float], max_drain: float, backoff: dict[str, dict[str, Any]], probe: int | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(the kamis to start now, the JOINERS added). On a tick
        that STARTS at least one kami anyway, the book's RESTING kamis whose
        PROJECTED minutes to `resumeHpPct` at THIS tick's regen rate are
        within `startJoinWindowMin` ride the same transaction, nearest first,
        filling only the FREE places of its last chunk — so a joiner never
        adds a transaction and never exceeds the start cap. Every other start
        gate is unchanged. One-directional: a joiner is only ever started
        EARLIER than its own readiness, nothing is ever held longer, and with
        the window at 0 (the default) nothing is projected."""
        window = float(self._cfg("startJoinWindowMin", 0) or 0)
        if window <= 0 or not starts:
            return starts, []
        cap = int(BATCH_CAPS["system.harvest.start"])
        room = (-len(starts)) % cap
        if room <= 0:
            return starts, []  # the last chunk is full: a joiner would cost a transaction of its own
        resume = int(self._cfg("resumeHpPct", 80))
        going = {int(m["kami"]) for m in starts}
        cands: list[tuple[float, int, dict[str, Any]]] = []
        for k in self.owned_kamis():
            view, lim = views.get(k), limits.get(k)
            if view is None or lim is None or k in going or k == probe or lim["unknown"]:
                continue  # an UNKNOWN bound is an unknown limit: never a joiner
            if view.state != RESTING or str(k) in backoff or view.cooldown_sec > 0 or view.hp_total <= 0:
                continue  # the chain would refuse this kami's start
            if view.hp_current * 100 >= resume * view.hp_total:
                continue  # READY already: the start hold's business, never a joiner
            rate = float(drain.get(str(k), max_drain))
            if view.hp_current <= lim["bound"] + rate:
                continue  # the ready rule's `need`: the bound plus an hour of drain
            regen = view.hp_rate_per_hr
            if not _num(regen) or float(regen) <= 0:
                continue  # no regen rate read this tick: nothing to project from
            target = math.ceil(resume * view.hp_total / 100.0)
            minutes = (target - view.hp_current) / float(regen) * 60.0
            if minutes <= window:
                cands.append((minutes, k, {"kami": k, "hp": view.hp_current, "hpTotal": view.hp_total, "bound": lim["bound"], "restFloor": lim["floor"], "boundBy": lim["boundBy"], "drainPerHr": rate, "resumeHpPct": resume, "readySinceSec": 0, "startJoined": True, "projectedMinutes": round(minutes, 1), "regenPerHr": float(regen)}))
        cands.sort(key=lambda c: (c[0], c[1]))
        joiners = [row for _m, _k, row in cands[:room]]
        return starts + joiners, [{"kami": int(row["kami"]), "projectedMinutes": row["projectedMinutes"]} for row in joiners]

    def _hold_for_batch(self, starts: list[dict[str, Any]], now_sec: float, runtime: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(the starts to sign now, the ready kamis held back as
        `startWaiting` rows). With startBatchWaitSec 0 nothing is held and
        no queue is kept."""
        wait = int(self._cfg("startBatchWaitSec", 0))
        if wait <= 0:
            runtime.pop("startQueue", None)
            return [dict(m, readySinceSec=0) for m in starts], []
        need = max(1, int(self._cfg("startBatchMin", 4)))
        queue: dict[str, float] = runtime.setdefault("startQueue", {})
        ready = {int(m["kami"]) for m in starts}
        for key in list(queue):
            if int(key) not in ready:
                del queue[key]
        for k in ready:
            queue.setdefault(str(k), float(now_sec))
        if not starts:
            return [], []
        since = {int(m["kami"]): int(max(0.0, now_sec - float(queue[str(m["kami"])]))) for m in starts}
        oldest = max(since.values())
        if len(ready) >= need or oldest >= wait:
            return [dict(m, readySinceSec=since[int(m["kami"])]) for m in starts], []
        return [], [{"kami": int(m["kami"]), "readySinceSec": since[int(m["kami"])]} for m in starts]

    def _status_counts(self, summary: dict[str, Any], offered: set[int] | None = None) -> dict[str, Any]:
        ctx = self.context
        nxt = None
        if ctx is not None and ctx.previews is not None:
            nxt = ctx.previews.next_due_at(self.node, self.account_index, int(self._cfg("previewRefreshSec", 3600)), offered, retry_sec=int(self._cfg("previewRetrySec", 300)))
        return {"kamis": dict(summary.get("kamis") or {}), "owners": dict(summary.get("owners") or {}), "minMarginHp": summary.get("minMarginHp"), "boundKamis": summary.get("boundKamis"), "previewsPending": summary.get("previewsPending"), "nextPreviewDueAt": nxt}

    def status_extra(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        st = runtime.get("autoStatus")
        return dict(st) if isinstance(st, dict) else {"kamis": None, "owners": None, "minMarginHp": None, "boundKamis": None, "previewsPending": None, "nextPreviewDueAt": None}

    def assess_under_stale(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """The stale-lens branch of a FEED book: cooldown-guarded feeds on complete rows
        HARVESTING on the node at or below max(bound, floor); a REST book
        does nothing. Never a rest, start or collect on a stale HP read."""
        runtime.pop("withheld", None)
        if str(self._cfg("regenMethod", "REST")) != "FEED" or self.book.config.get("foodItem") is None:
            runtime["summaryExtra"] = {"underStaleIdle": "REST book: no stop, start or collect on a stale read", **obs.notes}
            return []
        food = int(self.book.config["foodItem"])
        have = None if obs.inventory is None else obs.inventory.get(food, 0)
        if have is not None and have <= 0:
            return []
        views = {k: v for k, v in obs.kamis.items() if not v.missing and v.state == HARVESTING and v.node_index == self.node}
        limits, _c, _a, _s = self.bounds(obs, views)
        out: list[Decision] = []
        withheld: list[dict[str, Any]] = []
        for k, view in views.items():
            lim = limits[k]
            limit = max(lim["bound"], lim["floor"]) if not lim["unknown"] else lim["floor"]
            if view.hp_current > limit:
                continue
            w = feed_withheld(runtime, k, view, now_sec, self._stale_cooldown(), stale=True)
            if w is None:
                out.append(self._feed_decision(view, have, lim, under_stale=True))
            else:
                withheld.append(w)
        if withheld:
            runtime["withheld"] = withheld if len(withheld) > 1 else withheld[0]
        runtime["summaryExtra"] = dict(obs.notes)
        return out

    # -- the chain-backed fallback ---------------------------------------------

    def chain_fallback_feeds(self) -> bool:
        return bool(self._cfg("chainFallback", True)) and str(self._cfg("regenMethod", "REST")) == "FEED" and self.book.config.get("foodItem") is not None

    def chain_fallback_rests(self) -> bool:
        return bool(self._cfg("chainFallback", True)) and str(self._cfg("regenMethod", "REST")) == "REST"

    def _chain_cooldown(self) -> int:
        return int(self._cfg("chainFeedCooldownSec", 1800))

    def assess_under_chain(self, reading: Any, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """Under a lens outage, from the chain point read: for a FEED book,
        a `chainFeedCooldownSec`-guarded feed per owned kami the chain says
        is HARVESTING on the book's node at or below restFloorPct x hpTotal
        (the floor — never the bound: no preview, no attacker table, no
        owner class is read here). A REST book signs a `harvest
        stop` for each of its kamis the chain shows the same way, ONE
        attempt per kami per `chainFeedCooldownSec`. Never a start, collect or revive; a
        chain state DEAD is a tick note."""
        runtime.pop("withheld", None)
        notes: dict[str, Any] = {"chainReadKamis": len(reading.kamis)}
        unread = sorted(k for k, v in reading.kamis.items() if not v.read)
        dead = sorted(k for k, v in reading.kamis.items() if v.read and v.state == DEAD)
        if unread:
            notes["chainUnread"] = unread[:20]
        if dead:
            notes["chainDead"] = dead[:20]
        if not self.chain_fallback_feeds():
            if not self.chain_fallback_rests():
                # the core never takes the branch for such a book; kept as
                # the guard it is
                runtime["summaryExtra"] = notes
                return []
            # the REST branch. A `harvest stop` for every owned
            # kami the chain says is HARVESTING on the book's node at or
            # below the rest FLOOR — never the bound (nothing that needs a
            # preview is read here). One attempt per kami per
            # `chainFeedCooldownSec` (the same "one chain-backed act per kami
            # per cooldown" as a fallback feed): a stop
            # that landed shows RESTING on the next read and is not offered
            # again; one that did not land is retried after the cooldown —
            # never every tick, and never "never". The guard is a clock per
            # kami, persisted, so a keeper restart mid-outage keeps it.
            cooldown = self._chain_cooldown()
            prior = runtime.get("chainRests")
            done: dict[str, float] = {str(k): float(v) for k, v in prior.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and now_sec - float(v) < cooldown} if isinstance(prior, dict) else {}
            deviations: list[dict[str, Any]] = runtime.setdefault("deviations", [])
            floor_pct = int(self._cfg("restFloorPct", 10))
            members: list[dict[str, Any]] = []
            hps: dict[str, int] = {}
            for k in self.owned_kamis():
                ck = reading.kamis.get(k)
                if ck is None or not ck.read or not ck.harvesting_on(self.node):
                    continue
                hp = int(reading.projected.get(k, ck.hp_sync))
                hps[str(k)] = hp
                floor = int(math.ceil(floor_pct * ck.hp_max / 100.0))
                if hp > floor or str(k) in done:
                    continue
                members.append({"kami": k, "hp": hp, "hpTotal": ck.hp_max, "bound": 0, "restFloor": floor, "boundBy": None, "underChain": True, "chainBlock": reading.block, "hpProjected": hp})
                done[str(k)] = float(now_sec)
                deviations.append({"kami": k, "action": "rest", "reason": "lens outage: rested from the chain reading at the rest floor", "hpProjected": hp, "restFloor": floor, "chainBlock": reading.block})
            runtime["chainRests"] = done
            notes["chainHp"] = hps
            notes["chainRested"] = [int(m["kami"]) for m in members]
            runtime["summaryExtra"] = notes
            return self._batch("rest", "system.harvest.stop", members, int(BATCH_CAPS["system.harvest.stop"]), {"regenMethod": "REST", "underChain": True}, self._rest_plan_state())
        floor_pct = int(self._cfg("restFloorPct", 10))
        out: list[Decision] = []
        withheld: list[dict[str, Any]] = []
        hps: dict[str, int] = {}
        for k in self.owned_kamis():
            ck = reading.kamis.get(k)
            if ck is None or not ck.read or not ck.harvesting_on(self.node):
                continue
            hp = int(reading.projected.get(k, ck.hp_sync))
            hps[str(k)] = hp
            floor = int(math.ceil(floor_pct * ck.hp_max / 100.0))
            if hp > floor:
                continue
            view = kami_view_from_chain(k, ck.state, hp, ck.hp_max, ck.node_index, ck.level)
            w = feed_withheld(runtime, k, view, now_sec, self._chain_cooldown(), stale=True)
            if w is not None:
                w["reason"] = "chain fallback; cooldown not elapsed"
                withheld.append(w)
                continue
            d = self._feed_decision(view, None, {"bound": 0, "floor": floor, "boundBy": None}, under_stale=False)
            d.parameters["underChain"] = True
            d.parameters["chainBlock"] = reading.block
            d.parameters["hpProjected"] = hp
            out.append(d)
        notes["chainHp"] = hps
        if withheld:
            runtime["withheld"] = withheld if len(withheld) > 1 else withheld[0]
        runtime["summaryExtra"] = notes
        return out

    def on_records(self, decisions: list[Decision], records: list[Any], now_sec: float, runtime: dict[str, Any]) -> None:
        backoff: dict[str, dict[str, Any]] = runtime.setdefault("backoff", {})
        deviations: list[dict[str, Any]] = runtime.setdefault("deviations", [])
        wait = int(self._cfg("startBackoffSec", 3600))
        pos = 0
        for d in decisions:
            mine = records[pos:pos + len(d.intents)] if d.intents else []
            pos += len(d.intents)
            if not mine:
                continue
            rec = mine[0]
            refused = getattr(rec, "precheck", None) == "reverted" or rec.status in ("not_sent", "reverted")
            # a step lost to a nonce race (`reason: nonce race: …`,
            # SPEC P6) was never a verdict on the kami — it is re-decided
            # next tick, never backed off
            nonce_race = rec.status == "not_sent" and str(getattr(rec, "reason", "") or "").startswith("nonce race")
            if d.kind == "feed":
                if not refused and rec.status in ("success", "unconfirmed", "dry_run"):
                    record_feed(runtime, int(d.parameters["kami"]), now_sec, int(d.parameters["hp"]), int(d.parameters.get("hpMax") or 0), int(d.parameters["item"]))
            elif d.kind in ("start", "rest") and refused and not nonce_race and len(d.parameters.get("kamis") or []) == 1:
                # a SINGLE the chain refused (a batch is re-offered as singles
                # by the core first): the kami waits out startBackoffSec
                k = int(d.parameters["kamis"][0])
                reason = str(getattr(rec, "reason", "") or "reverted on chain")[:160]
                transient = False
                if "on cooldown" in reason.lower():
                    # a `kami on cooldown` refusal is not a verdict on the
                    # kami — it clears by itself, so the wait is one re-offer,
                    # not an hour (checked FIRST: the text names a revert)
                    this_wait = min(wait, COOLDOWN_REFUSAL_BACKOFF_SEC)
                elif not refusal_is_verdict(rec, reason):
                    # the pre-check passed and the node refused the
                    # BROADCAST for a reason of its own — nothing about the
                    # kami, so it is re-offered on one of the next ticks
                    this_wait = min(wait, TRANSIENT_REFUSAL_BACKOFF_SEC)
                    transient = True
                else:
                    this_wait = wait
                backoff[str(k)] = {"until": float(now_sec + this_wait), "kind": d.kind, "reason": reason, "since": float(now_sec)}
                row = {"kami": k, "action": d.kind, "reason": reason, "backoffSec": this_wait, "until": float(now_sec + this_wait)}
                if transient:
                    row["transient"] = True
                deviations.append(row)
