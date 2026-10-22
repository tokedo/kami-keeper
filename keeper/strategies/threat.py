"""`threat` — the node-threat machinery shared by the strategy types.

What it is: the node census, the preview cache pass, the attacker
classes and `bounds()` — everything a book needs to answer "how much HP
must this kami keep on this node so that no attacker can take it". It
is shared by `auto` and `harvestAndFeed`, so both strategy types use the
SAME threat model.

`NodeThreat` is a MIXIN with no state of its own. The host class must
provide:
  node           int property — the node index the book is on
  account_index  int property — the book's account
  owned_kamis()  -> list[int] — the kamis of the book
  _cfg(key, d)   the book's config read with a default
  book           the Book (config, id, account)
  context        the StrategyContext (clock, census, previews, owners,
                 node_occupants) or None

The measured lift: a kill of one of the book's kamis on the book's node above
the served line of the (killer, victim) pair LIFTS every line of the
killer's owner on that node by the measured gap (keeper/previews.py —
`bounds()` reads it through `entry.clamped` and says so in
`boundBy.liftPct`); a lift holds for `LIFT_HOLD_SEC` and the pass
expires what is past its hold before it fetches anything.
"""

from __future__ import annotations

import math
from typing import Any

from ..census import PLAIN, VITALS, NodeCensus, entity_id, parse_census
from ..kills import OccupantRef
from ..lens import LensAnswer, LensClient, LensQueryError
from ..owner_class import OwnerClass, classify, hostile_by, render
from .base import HARVESTING, KamiView, Observation

# `previewPolicy` values. The narrow one is named after the RENDERED
# class it previews (P3a) — the internal word is on the P9 scrub list and a
# config value is an agent-visible string.
PREVIEW_ALL = "all"
PREVIEW_KILLED_HERE = "killed-here"


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class NodeThreat:
    """The node census, the preview pass, the attacker classes and the
    bounds — mixed into a Strategy (see the module docstring for what the
    host class must provide). No `__init__`, no state of its own."""

    # -- config ------------------------------------------------------------

    def listed_accounts(self) -> set[int]:
        return {int(a) for a in self.book.config.get("friends") or []} | {self.account_index}

    @property
    def standing_owners(self) -> frozenset[int]:
        """The keeper's `standingOwners` — the operator-named owners
        whose attackers bind as if killed-here, without a kill and without
        a window. Keeper-level config, so it is the context's, never the
        book's; empty without a context."""
        ctx = self.context
        return frozenset() if ctx is None else frozenset(int(a) for a in (getattr(ctx, "standing_owners", None) or ()))

    @property
    def window_hours(self) -> int:
        return int(self._cfg("hostileWindowHours", 48))

    # the read policy. A stored config that predates these keys carries
    # none of the three and keeps the default reads — every present foreign
    # occupant previewed, the vitals census, read every pass — until the
    # book is re-declared (the conditional defaults apply at `start` only).
    @property
    def preview_policy(self) -> str:
        return str(self._cfg("previewPolicy", PREVIEW_ALL))

    @property
    def census_mode(self) -> str:
        return PLAIN if str(self._cfg("nodeCensus", VITALS)) == PLAIN else VITALS

    # -- perception --------------------------------------------------------

    def _observe_attackers(self, lens: LensClient, obs: Observation, census: NodeCensus, node_ans: LensAnswer | None, party: LensAnswer, kamis: dict[int, KamiView], now: float, notes: dict[str, Any]) -> None:
        """The attacker half of a tick's observation, cut out of
        `Auto.observe`: the foreign-occupant notes and the
        preview cache pass. Mutates `obs` (answers, `attacker_cooldowns`)
        and `notes` in place."""
        ctx = self.context
        occupants = census.occupants
        plain = census.mode == PLAIN
        listed = self.listed_accounts()
        foreign = {i: o for i, o in occupants.items() if o.account_index is None or int(o.account_index) not in listed}
        notes["foreignOccupants"] = len(foreign)
        if not plain:
            notes["occupantsWithoutVitals"] = sorted(i for i, o in foreign.items() if o.vitals_missing)
        # what the census cost THIS tick on the wire (0 when the pass
        # cache or an earlier pass served it) and how old the one in use is
        notes["nodeReadBytes"] = int(node_ans.size or 0) if node_ans is not None and not node_ans.served_from_pass else 0
        notes["censusAgeSec"] = 0 if node_ans is not None else int(max(0.0, now - census.read_at))
        notes["attackerVitalsCalls"] = 0
        # is anything of the ACCOUNT on the node? The preview serves
        # thresholds against occupants only, so a fetch made while nothing
        # of ours was there has no rows at all — it binds nothing and holds
        # every start until it is fetched again with one of ours present
        own_on_node = any(o.account_index is not None and int(o.account_index) == self.account_index for o in occupants.values())
        if not own_on_node and node_ans is None:
            # a reused census may predate a deploy of ours (the same reason
            # the kill scanner takes them from the party read below)
            own_on_node = any(not v.missing and v.state == HARVESTING and v.node_index == self.node for v in kamis.values())
        notes["ownOnNode"] = own_on_node
        if ctx is not None and ctx.previews is not None:
            previews = ctx.previews
            owned_levels = {k: v.level for k, v in kamis.items() if not v.missing}
            # a reused census carries the block of an earlier pass: the window
            # is measured below the freshest read of this one
            head = node_ans.block if node_ans is not None else party.block
            # one window — an absent attacker is remembered while its
            # owner (killed here, or killed ours anywhere) OR the kami
            # itself is hostile inside it
            hostile = ctx.owners.hostile_owners(self.node, self.window_hours, head, self.account_index) if ctx.owners is not None else set()
            # a standing owner's attackers are fetched and refreshed
            # under `previewPolicy killed-here`, are arrivals (front of the
            # queue, `unknown`, held from starting) until fetched, and are
            # remembered on departure for the window — the same three paths
            # a killed-here owner takes, through the same set
            hostile = set(hostile) | self.standing_owners
            hostile_kamis = ctx.owners.hostile_kamis_for(self.node, self.account_index, self.window_hours, head) if ctx.owners is not None else set()
            # the attackers that are killed-here inside the window, by
            # owner OR by kami — the same two sets `reconcile_presence` takes
            binding = sorted(i for i, o in foreign.items() if i in hostile_kamis or (o.account_index is not None and int(o.account_index) in hostile))
            offered = foreign
            if self.preview_policy == PREVIEW_KILLED_HERE:
                # lazy previews: with passiveOwnersBind false only a killed-here
                # attacker binds, so only those are fetched, refreshed or
                # counted pending — plus a remembered one that came back. The
                # tick after a kill row its owner's present attackers have no
                # entry: they are ARRIVALS (front of the queue) and `unknown`
                # (held at the floor) until fetched.
                ents = previews.entries(self.node, self.account_index)
                keep = set(binding) | {i for i in foreign if i in ents and ents[i].remembered}
                offered = {i: o for i, o in foreign.items() if i in keep}
            # a measured lift HOLDS for `LIFT_HOLD_SEC` and then
            # goes — an entry whose hold has passed is back on the number
            # the lens serves
            expired = previews.expire_lifts(self.node, self.account_index, now)
            if expired:
                notes["liftsExpired"] = expired
            due = previews.due(self.node, self.account_index, offered, owned_levels, int(self._cfg("previewRefreshSec", 3600)), now, own_on_node=own_on_node, retry_sec=int(self._cfg("previewRetrySec", 300)))
            cap = max(1, int(self._cfg("previewsPerTick", 10)))
            room = max(0, cap - int(previews.pass_fetches.get(self.node, 0)))  # the cap is per pass and node, shared by the books on it
            got = {"calls": 0, "changed": 0, "bytes": 0, "invalid": 0}
            # lens 1.0.0: the preview is only ever read for OUR lines,
            # so it selects our present kamis plus the attacker itself
            # (whose own row decides an attacker-side threshold)
            # BEFORE projection — ~1 KB instead of ~1 MB on a crowded node. The
            # census above is the full node read and is unchanged by this.
            # The client drops the option on 0.6.3, where the full answer
            # comes back exactly as before. Built once per observe.
            own_present = sorted(i for i, o in occupants.items() if o.account_index is not None and int(o.account_index) == self.account_index)

            def fetch(a: int) -> None:
                # 1-500 indices are allowed and the attacker takes one slot
                targets = own_present + [a] if len(own_present) < 500 else None
                try:
                    _entry, ch, ans = previews.fetch(lens, self.node, a, foreign.get(a), self.account_index, now, targets=targets)
                except LensQueryError as e:
                    # the attacker may have left since the census was read —
                    # of an earlier pass or of this one (a refused
                    # preview never refuses the tick). What is unknown stays
                    # unknown (held at the floor) and the census is read
                    # again next pass. Lens 1.0.0: an attacker the mirror
                    # cannot project completely refuses the pairing read
                    # with INCOMPLETE — the same handling, counted apart.
                    ctx.census.force(self.node)
                    notes.setdefault("previewsRefused", []).append(a)
                    if getattr(e, "code", None) == "INCOMPLETE":
                        notes.setdefault("previewsIncomplete", []).append(a)
                    return
                obs.answers.append(ans)
                got["calls"] += 1
                got["changed"] += 1 if ch else 0
                got["bytes"] += int(ans.size or 0)
                # rows this answer served that were not lines (carried, never applied)
                got["invalid"] += len(_entry.invalid_rows)

            for a, _reason in due[:room]:
                fetch(a)
            pending = [a for a, _r in due[room:]]
            if plain:
                # the plain census says nothing of cooldown or level: both are
                # read for the attackers that can bind, and a level that moved
                # is a refresh like any other (inside the same per-pass cap)
                for a in self._attacker_vitals(lens, obs, binding, notes):
                    if a in pending:
                        continue
                    if int(previews.pass_fetches.get(self.node, 0)) >= cap:
                        pending.append(a)
                        continue
                    fetch(a)
                    served = previews.pass_cooldowns.get(self.node, {}).get(a)
                    if served is not None and obs.attacker_cooldowns is not None:
                        obs.attacker_cooldowns[a] = int(served)
            fetched, changed, pbytes = got["calls"], got["changed"], got["bytes"]
            # presence is a NODE fact shared by every book on it: every
            # occupant of another account counts as present, whatever this
            # book's friends list says (a listing only skips the fetch)
            others = {i: o for i, o in occupants.items() if o.account_index is None or int(o.account_index) != self.account_index}
            presence = previews.reconcile_presence(self.node, self.account_index, others, hostile, now, self.window_hours * 3600.0, hostile_kamis, refresh_cooldown=not plain)
            previews.save(self.node, self.account_index)
            notes.update({"previewCalls": fetched, "previewBytes": pbytes, "previewChangedThreshold": changed, "previewInvalidRows": got["invalid"], "previewsPending": len(pending), "previewsPendingKamis": pending[:20]})
            for key in ("remembered", "dropped", "expired"):
                if presence.get(key):
                    notes["previews" + key.capitalize()] = presence[key]
            # the kill scanner resolves DEAD writes against these rows next pass
            if node_ans is None:
                # a reused census may predate a deploy of this book's own: a
                # kill of OURS must never be missed for the age of a census
                for k, v in kamis.items():
                    if not v.missing and v.state == HARVESTING and v.node_index == self.node:
                        census.refs.setdefault(entity_id(int(k)), OccupantRef(self.node, int(k), self.account_index))
            ctx.node_occupants[self.node] = census.refs

    def _census(self, lens: LensClient, now: float) -> tuple[NodeCensus, LensAnswer | None]:
        """(the node census, the answer when it was read THIS tick — None
        when a census of an earlier pass was reused). `nodeCensus`
        `plain` reads `node <i> --full` (no vitals); a book whose `nodeCensusEverySec` is
        above its `tickSeconds` reuses a census younger than that; every
        other book reads every pass (the default). The parse is shared by
        the books of one pass (the PassLens serves them the same answer)."""
        ctx = self.context
        cache = ctx.census if ctx is not None else None
        mode = self.census_mode
        held = cache.held(self.node, mode) if cache is not None else None
        every = int(self._cfg("nodeCensusEverySec", 0) or 0)
        if held is not None and every > int(self._cfg("tickSeconds", 30)) and held.pass_seq != cache.pass_seq and 0 <= now - held.read_at < every:
            return held, None
        ans = lens.node(self.node, with_vitals=(mode != PLAIN), full=True)
        if held is not None and held.pass_seq == cache.pass_seq and held.answer.data is ans.data:
            return held, ans
        census = parse_census(self.node, mode, ans, now, cache.pass_seq if cache is not None else 0)
        return (cache.keep(census) if cache is not None else census), ans

    def _attacker_vitals(self, lens: LensClient, obs: Observation, binding: list[int], notes: dict[str, Any]) -> list[int]:
        """Under the plain census: the liquidation cooldown (and level) of
        each present attacker that can bind — from the preview answer of
        this pass when there is one, else one `kami <index>` read per
        attacker per pass and node (shared by the books on it), at most
        `attackerVitalsPerTick`. An attacker over the cap, or whose read
        the lens refused, has NO cooldown here and therefore binds (the
        `cooldownHorizonSec` skip needs a served cooldown). Returns the
        previewed attackers whose level moved: due for a refresh."""
        ctx = self.context
        cache, previews = ctx.census, ctx.previews
        served = previews.pass_cooldowns.get(self.node, {})
        seen = cache.attacker_vitals.setdefault(self.node, {})
        entries = previews.entries(self.node, self.account_index)
        cap = max(0, int(self._cfg("attackerVitalsPerTick", 10)))
        cooldowns: dict[int, int] = {}
        moved: list[int] = []
        over: list[int] = []
        unread: list[int] = []
        calls = 0
        for a in binding:
            if a in served:
                cooldowns[a] = int(served[a])
                continue
            row = seen.get(a)
            if row is None:
                if int(cache.vitals_calls.get(self.node, 0)) >= cap:
                    over.append(a)
                    continue
                cache.vitals_calls[self.node] = int(cache.vitals_calls.get(self.node, 0)) + 1
                calls += 1
                row = seen[a] = {}
                try:
                    ans = lens.kami(a)
                except LensQueryError:
                    ans = None
                if ans is not None:
                    obs.answers.append(ans)
                    data = ans.data if isinstance(ans.data, dict) else {}
                    if _num(data.get("cooldownSec")):
                        row["cooldownSec"] = int(data["cooldownSec"])
                    if _num(data.get("level")):
                        row["level"] = int(data["level"])
            if "cooldownSec" not in row:
                unread.append(a)
                continue
            cooldowns[a] = int(row["cooldownSec"])
            e = entries.get(a)
            if e is not None and e.attacker_level is not None and row.get("level") is not None and int(row["level"]) != int(e.attacker_level):
                moved.append(a)
        obs.attacker_cooldowns = cooldowns
        notes["attackerVitalsCalls"] = calls
        if over:
            notes["attackerVitalsOverCap"] = over[:20]
        if unread:
            notes["attackerVitalsUnread"] = unread[:20]
        return moved

    # -- classes and bounds ------------------------------------------------------

    def _attackers(self, obs: Observation) -> list[dict[str, Any]]:
        """Present foreign occupants and remembered entries: [{kami, owner,
        entry, cooldownSec, present}]."""
        listed = self.listed_accounts()
        out: list[dict[str, Any]] = []
        ctx = self.context
        entries = ctx.previews.entries(self.node, self.account_index) if ctx is not None and ctx.previews is not None else {}
        present: set[int] = set()
        # under the plain census the rows carry no cooldown — it is what
        # the observation READ for the attackers that can bind, and 0 (the
        # attacker binds) for one it did not read
        read = obs.attacker_cooldowns
        for i, o in sorted(obs.occupants.items()):
            if o.account_index is not None and int(o.account_index) in listed:
                continue
            present.add(i)
            out.append({"kami": i, "owner": o.account_index, "entry": entries.get(i), "cooldownSec": int(o.cooldown_sec) if read is None else int(read.get(i, 0)), "present": True})
        for a, e in sorted(entries.items()):
            if a not in present and e.remembered and not (e.attacker_account is not None and int(e.attacker_account) in listed):
                out.append({"kami": a, "owner": e.attacker_account, "entry": e, "cooldownSec": 0, "present": False})
        return out

    def _hostile_flags(self, owner: int | None, kami: int | None, head: int | None) -> tuple[bool, bool, bool]:
        """(owner hostile in the window, kami hostile in the window, owner
        on the standing list) — the owner killed / cast on this node,
        or killed OURS on any node, measured from its LAST such kill; the
        attacker KAMI itself did, whoever owns it now. One window for both.
        The third flag is CONFIG, not ledger — it is computed even
        without a context or an owner ledger."""
        standing = owner is not None and int(owner) in self.standing_owners
        ctx = self.context
        if ctx is None or ctx.owners is None:
            return False, False, standing
        owner_hostile = False
        if owner is not None:
            owner_hostile = ctx.owners.killed_here_in_window(int(owner), self.node, self.window_hours, head) or ctx.owners.killed_ours_in_window(int(owner), self.account_index, self.window_hours, head)
        kami_hostile = kami is not None and ctx.owners.kami_hostile_in_window(int(kami), self.node, self.account_index, self.window_hours, head)
        return bool(owner_hostile), bool(kami_hostile), bool(standing)

    def _class_of(self, owner: int | None, attackers: list[dict[str, Any]], head: int | None, kami: int | None = None, max_thr_of: dict[Any, int] | None = None, memo: dict[str, Any] | None = None) -> tuple[OwnerClass, str | None]:
        """The class of ONE attacker (per attacker kami, not per owner)
        and what made it bind with the safety margin (`owner` | `kami` |
        `both` | `standing` | None). With
        `kami` None it is the owner-level class (`render_owner_class`).
        `max_threshold` stays the OWNER's served maximum over its attackers
        against the book's kamis — `no-threshold` is a statement about the
        owner (P3a), and that part of the definition did not move."""
        # `memo`: per `bounds()` call — the listed set and each owner's
        # hostility are the same for every attacker of the call; on a crowded
        # node both would otherwise be rebuilt per attacker
        listed = memo.setdefault("listed", self.listed_accounts()) if memo is not None else self.listed_accounts()
        if owner is not None and int(owner) in listed:
            return OwnerClass.FRIEND, None  # a listed owner is a friend whatever kami it holds
        # `max_thr_of`: the per-owner maximum is the same for
        # every attacker of one owner — `bounds()` passes one dict per call so
        # a crowded node costs one pass per OWNER, not one per attacker
        if max_thr_of is not None and owner in max_thr_of:
            max_thr = max_thr_of[owner]
        else:
            owned = self.owned_kamis()
            max_thr = 0
            for a in attackers:
                if a["owner"] == owner and a["entry"] is not None:
                    max_thr = max(max_thr, a["entry"].max_clamped(owned))
            if max_thr_of is not None:
                max_thr_of[owner] = max_thr
        if memo is not None and owner is not None:
            # the memo caches the OWNER pair (hostile in the window,
            # on the standing list) — both are owner facts, so one
            # `_hostile_flags` call per owner per `bounds()` call
            flags = memo.setdefault("ownerHostile", {})
            if owner not in flags:
                o_flags = self._hostile_flags(owner, None, head)
                flags[owner] = (o_flags[0], o_flags[2])
            owner_hostile, standing = flags[owner]
            kami_hostile = self._hostile_flags(None, kami, head)[1]
        else:
            owner_hostile, kami_hostile, standing = self._hostile_flags(owner, kami, head)
        cls = classify(listed=False, max_threshold=max_thr, owner_hostile_in_window=owner_hostile, kami_hostile_in_window=kami_hostile, standing=standing)
        return cls, hostile_by(owner_hostile_in_window=owner_hostile, kami_hostile_in_window=kami_hostile, standing=standing)

    def _cached_attackers(self) -> list[dict[str, Any]]:
        """The attacker rows `_class_of` needs, built from the CACHED
        previews alone — no read, no node census, no presence
        reconciliation. It is shared with `render_owner_class` so the
        rendered class and `owner_binds_now` cannot diverge: both
        classify the same owner over the same rows."""
        ctx = self.context
        entries = ctx.previews.entries(self.node, self.account_index) if ctx is not None and ctx.previews is not None else {}
        return [{"kami": a, "owner": e.attacker_account, "entry": e, "cooldownSec": 0, "present": True} for a, e in entries.items()]

    def render_owner_class(self, owner: int | None, head: int | None = None) -> str:
        """The rendered class of an owner against this book right now (the
        `kill` row's `ownerClassAfter`); previews as cached, no read."""
        return render(self._class_of(owner, self._cached_attackers(), head)[0], self.window_hours)

    def owner_binds_now(self, owner: int | None, head: int | None = None) -> bool:
        """Does this owner bind this book right now — the class
        `render_owner_class` would render as `killed-here-<window>`? Same
        classification, same cached previews, no read. A `standing` owner
        is NOT included: it already bound this book before the kill, so a
        kill of its does not raise any line and makes no kami newly
        exposed."""
        return self._class_of(owner, self._cached_attackers(), head)[0] is OwnerClass.HOSTILE

    def bounds(self, obs: Observation, view_of: dict[int, KamiView], passive_binds: bool | None = None) -> tuple[dict[int, dict[str, Any]], dict[int, OwnerClass], list[dict[str, Any]], list[int]]:
        """Per owned kami: {bound, boundBy, unknown, unknownNoRows, proxied,
        floor};
        `unknownNoRows` is the part of `unknown` whose entry exists
        and has no rows; `proxied` is the unread attackers whose OWNER
        has previews with rows here — their threshold is the owner's served
        maximum, so they BIND and are not `unknown`; the classes PER
        ATTACKER KAMI (each attacker dict also carries `class` and
        `hostileBy`); the attackers; the attackers skipped for cooldown.
        `passive_binds` None is the book's `passiveOwnersBind`;
        True / False overrides it for ONE call, which is how a
        ladder book asks for both bounds of the same read."""
        cfg = self.book.config
        head = obs.basis.lensBlock
        attackers = self._attackers(obs)
        classes: dict[int, OwnerClass] = {}
        # the per-owner served
        # maximum is filled in ONE pass over the attackers — the lazy fill in
        # `_class_of` walks every attacker once per OWNER, which is quadratic
        # on a node of many small owners
        max_thr_of: dict[Any, int] = {}
        # and the per-owner maximum served RATIO (threshold / that
        # occupant's served max HP) in the same pass — what an unread
        # attacker's stand-in scales by the resting kami's own max HP
        max_ratio_of: dict[Any, float] = {}
        owned = self.owned_kamis()
        # the book's own max HP per kami — the denominator of a legacy
        # row, which carries no `hpTotal` of its own. An
        # ABSENT remembered entry is never refreshed (`due()` iterates the
        # PRESENT occupants), so without this its legacy rows would stand in
        # at the ABSOLUTE maximum for ever, holding the book's low-HP kamis
        # permanently
        hp_totals = {int(k): int(view.hp_total) for k, view in view_of.items() if view.hp_total}
        for a in attackers:
            top = a["entry"].max_clamped(owned) if a["entry"] is not None else 0
            if top > max_thr_of.get(a["owner"], -1):
                max_thr_of[a["owner"]] = top
            ratio = a["entry"].max_ratio(owned, hp_totals) if a["entry"] is not None else 0.0
            if ratio > max_ratio_of.get(a["owner"], -1.0):
                max_ratio_of[a["owner"]] = ratio
        memo: dict[str, Any] = {}
        for a in attackers:
            a["class"], a["hostileBy"] = self._class_of(a["owner"], attackers, head, a["kami"], max_thr_of, memo)
            classes[int(a["kami"])] = a["class"]
        horizon = int(self._cfg("cooldownHorizonSec", 600))
        safety = int(self._cfg("safetyMarginPct", 5))
        passive = int(self._cfg("passiveMarginPct", 2))
        floor_pct = int(self._cfg("restFloorPct", 10))
        passive_binds = bool(self._cfg("passiveOwnersBind", True)) if passive_binds is None else bool(passive_binds)
        skipped = sorted({a["kami"] for a in attackers if a["cooldownSec"] > horizon})
        # which attackers can bind at all is a fact of the attacker, not
        # of the kami — filtered once, not once per owned kami (kamis x
        # occupants x books a pass on a crowded node)
        binding: list[dict[str, Any]] = []
        for a in attackers:
            if a["cooldownSec"] > horizon:
                continue
            cls = a["class"]
            if cls in (OwnerClass.FRIEND, OwnerClass.HARMLESS):
                continue
            if cls is OwnerClass.ARMED_PASSIVE and not passive_binds:
                # passiveOwnersBind false: a threshold-no-kill owner's
                # thresholds are read and kept (the class can flip on the
                # first kill row) but do not bind or hold this kami
                continue
            binding.append(a)
        out: dict[int, dict[str, Any]] = {}
        for k, view in view_of.items():
            hp_total = view.hp_total
            bound = 0.0
            bound_by: dict[str, Any] | None = None
            unknown: list[int] = []
            # the part of `unknown` that is an entry WITHOUT rows — a
            # preview fetched while nothing of the account was on the node.
            # A refetch answers the same nothing until one of ours is there
            unknown_no_rows: list[int] = []
            # the attackers bound through their OWNER's served maximum —
            # unread themselves, but not unknown: they bind and hold nothing
            proxied: list[int] = []
            for a in binding:
                cls = a["class"]
                entry = a["entry"]
                thr = entry.clamped(k) if entry is not None else None
                proxy = False
                proxy_owner = False
                proxy_scaled = False
                owner_scaled = False
                if thr is None and entry is not None and entry.rows:
                    # the preview serves thresholds against OCCUPANTS only: a
                    # resting kami (or one deployed after the fetch) has no
                    # row, so the attacker's served maximum over the
                    # account's occupants stands in until the next refresh —
                    # a lens number for another kami, never a computed one.
                    # the served line is a RATIO of the victim's max HP,
                    # so the stand-in is the highest served ratio times THIS
                    # kami's max HP — the absolute maximum (the row of the
                    # largest kami) would hold smaller kamis near their full HP.
                    # A legacy row's denominator is `hp_totals`, so a legacy
                    # entry (never refreshed while its attacker is absent)
                    # scales too
                    thr, proxy_scaled = entry.stand_in(hp_total, hp_totals=hp_totals)
                    proxy = True
                if thr is None and max_thr_of.get(a["owner"], 0) > 0:
                    # an attacker never previewed (or previewed with no
                    # rows) whose OWNER already has previews on this node: the
                    # owner's served maximum over this book's kamis stands in
                    # — a lens number for another attacker of the same owner,
                    # never a computed one — until its own preview lands. It
                    # is `proxied`, NOT `unknown`: it binds like a known
                    # threshold, so an owner moving fresh attackers onto the
                    # node cannot idle the book by arrivals alone. Scaled
                    # by this kami's own max HP, like proxy 1.
                    if max_ratio_of.get(a["owner"], 0.0) > 0:
                        thr = int(math.ceil(max_ratio_of[a["owner"]] * hp_total))
                        owner_scaled = True
                    else:
                        thr = max_thr_of[a["owner"]]
                        owner_scaled = False
                    proxy_owner = True
                if proxy_owner:
                    proxied.append(a["kami"])
                elif thr is None:
                    unknown.append(a["kami"])
                    if entry is not None and not entry.rows:
                        unknown_no_rows.append(a["kami"])
                    continue
                # a STANDING attacker takes the same safety margin as a
                # killed-here one — that is what the standing list means
                m = thr + (safety if cls in (OwnerClass.HOSTILE, OwnerClass.STANDING) else passive) * hp_total / 100.0
                if m > bound:
                    bound = m
                    bound_by = {"attackerKami": a["kami"], "ownerClass": render(cls, self.window_hours), "threshold": thr, "present": a["present"]}
                    if cls in (OwnerClass.HOSTILE, OwnerClass.STANDING):
                        bound_by["hostileBy"] = a["hostileBy"]  # owner | kami | both | standing
                    if entry is not None and entry.lift_ratio > 0:
                        # `threshold` above is already the LIFTED
                        # line (`entry.clamped`); this says how much of it
                        # the chain measured through a kill of ours
                        bound_by["liftPct"] = round(entry.lift_ratio * 100, 1)
                    if proxy:
                        bound_by["thresholdFrom"] = "max served ratio over the account's occupants x this kami's max HP (no row for this kami)" if proxy_scaled else "max over the account's occupants (no row for this kami)"
                    elif proxy_owner:
                        bound_by["thresholdFrom"] = "max served ratio over the owner's previewed attackers x this kami's max HP (this attacker not yet previewed)" if owner_scaled else "max over the owner's previewed attackers (this attacker not yet previewed)"
            out[k] = {"bound": int(math.ceil(bound)), "boundBy": bound_by, "unknown": unknown, "unknownNoRows": unknown_no_rows, "proxied": proxied, "floor": int(math.ceil(floor_pct * hp_total / 100.0))}
        return out, classes, attackers, skipped
