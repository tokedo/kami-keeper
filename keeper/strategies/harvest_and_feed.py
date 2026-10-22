"""`harvestAndFeed` — one kami on one node (SPEC P2).

Per tick (observation -> action, no policy language):
  HARVESTING on the book's node:
    feedMode hp:       projected HP < feedBelowHp   -> use-item foodItem
    feedMode interval: feedInterval elapsed          -> use-item foodItem
    no food in inventory and failsafeRest            -> harvest stop
    the drawn next collect time reached              -> harvest collect
  RESTING:
    HP >= resumeHpPct % of max                       -> harvest start on nodeIndex
  DEAD: the core writes the death row; with `reviveItems` configured and
        one in inventory it signs use-item with it (inventory only).

Feed idempotence: a feed that landed is recorded per
kami (`record_feed`, in `on_records`) and the kami is not fed again until
the lens shows the feed applied or `feedCooldownSec` has elapsed
(`feed_withheld`). Under `lens-stale:<n>` alone the book may
still feed — cooldown-guarded, never start / stop / collect — through
`assess_under_stale`. Under a lens OUTAGE (unreachable, not
LIVE, or mirror known incomplete, for at least `chainFallbackAfterSec`)
it may feed from a chain point read of its own kami through
`assess_under_chain` — `chainFeedCooldownSec`-guarded, never anything
else (SPEC P4, the chain fallback).

Collect de-sync: the collect interval is DRAWN per book,
uniform in the config's window (`collect_window`: `collectEveryMinSec` ..
`collectEveryMaxSec`, or a `collectEvery` set alone pinning both), when
the collect clock starts and again after every collect / start that
LANDED. The drawn due time is `runtime.nextCollectAt`, persisted with the
draw (`collectIntervalSec`) and the draw counter (`collectDraws`): a
restart, a refused or reverted collect never redraws, the collect stays
due. The generator is `random.Random` seeded from the book id and the
draw counter — no wall clock, so a book's sequence is reproducible and
two books never share one.

The food ladder: a book whose config carries `foodLadder`
is in LADDER MODE. Without it NOTHING below happens — the book makes the
same lens reads and the same decisions as before. In it the book observes
the NODE as an `auto` book does (the `NodeThreat` mixin: the census, the
preview pass, the attacker classes and `bounds()`, each shared per node
by every book of the pass) and sizes every feed itself. With
`need = max(bound, restFloor, ladderMinFloorPct % of hpTotal)` and the
rungs sorted by heal, largest first — a rung is IN STOCK when the
inventory read is missing or its balance is above 0, ELIGIBLE when its
no-waste floor (`hpTotal - heal`) is at or above `need`:

  an attacker that binds with no preview yet -> the smallest rung in stock
  otherwise the largest eligible rung, at most ONE rung up per
    `tierUpCooldownSec` (a step DOWN is immediate; the first choice of a
    book is never held)
  no eligible rung and even the smallest one's floor below the bound a
    killed-here attacker sets -> the failsafe `harvest stop`
  no eligible rung otherwise (a threshold-no-kill owner, or the dip cap
    alone) -> the smallest rung in stock, never a stop
  nothing in stock at all -> the failsafe `harvest stop`, once the kami
    is at the point where it would have fed

The dip cap itself: `ladderMinFloorPct`
of max HP with `dipCapMode` `flat` (the default, and then nothing of the
watchlist is read); with `measured` the highest threshold a kami that
liquidated inside `dipWindowDays` serves against THIS kami, plus
`safetyMarginPct`, never under `dipMeasuredMinPct` — and the flat cap
whenever the list cannot answer. `_dip_cap` names which in `dipFrom`.

A RESTING kami at `resumeHpPct` starts only when the chooser would hold it
on the node: not while a killed-here bound is above every rung, nothing is
in stock, or an attacker that binds has no preview yet.

and it feeds at the no-waste point (`hp <= hpTotal - heal`), or at once
when a bound rose above the kami (`hp < need`) with the largest rung in
stock whose overheal stays within `maxOverhealHp`. Both paths go through
the feed-idempotence guard, which lets a feed through as soon as the last one
is visible in the lens HP and never lets two be in flight.
"""

from __future__ import annotations

import math
import random
from typing import Any

from ..books import collect_window
from ..lens import LensClient, LensQueryError
from ..runtime import iso as rt_iso
from ..signer import Intent
from .base import DEAD, HARVESTING, RESTING, Decision, Observation, Strategy, feed_withheld, food_heal, kami_view, kami_view_from_chain, record_feed, ticks_for
from .threat import NodeThreat


class HarvestAndFeed(NodeThreat, Strategy):
    type_name = "harvestAndFeed"

    @property
    def kami(self) -> int:
        return int(self.book.config["kamiIndex"])

    @property
    def node(self) -> int:
        return int(self.book.config["nodeIndex"])

    @property
    def account_index(self) -> int:
        """What the node and preview reads take. Ladder mode only —
        the key is required there and absent on a static book."""
        return int(self.book.config["accountIndex"])

    @property
    def ladder_mode(self) -> bool:
        return "foodLadder" in self.book.config

    def watches_node(self) -> bool:
        return self.ladder_mode

    def _cfg(self, key: str, default: Any) -> Any:
        v = self.book.config.get(key)
        return default if v is None else v

    def owned_kamis(self) -> list[int]:
        return [self.kami]

    def revive_items(self) -> list[int]:
        return [int(i) for i in self.book.config.get("reviveItems") or []]

    def observe(self, lens: LensClient) -> Observation:
        ans = lens.kami(self.kami)
        view = kami_view(ans.data)
        obs = Observation(basis=ans.basis(), kamis={view.index: view}, answers=[ans])
        if view.account_index is not None:
            try:
                inv = lens.inventory(view.account_index)
            except LensQueryError:
                inv = None
            if inv is not None:
                obs.answers.append(inv)
                obs.inventory = {int(r["item"]["index"]): int(r["balance"]) for r in (inv.data or {}).get("items", []) if isinstance(r, dict) and isinstance(r.get("item"), dict)}
        if self.ladder_mode:
            # the node half of the observation — the census, the preview
            # pass and the attacker notes, exactly as `auto` makes them and
            # shared per node by the books of the pass, so several ladder
            # books on one node share one census and one preview cap
            ctx = self.context
            now = float(ctx.clock()) if ctx is not None and ctx.clock is not None else 0.0
            census, node_ans = self._census(lens, now)
            if node_ans is not None:
                obs.answers.append(node_ans)
            obs.occupants = census.occupants
            notes: dict[str, Any] = {}
            # the `kami` answer stands where `Auto` passes the party answer:
            # only its block is read (the window below a reused census)
            self._observe_attackers(lens, obs, census, node_ans, ans, {view.index: view}, now, notes)
            # the watchlist half, LAST — a book whose `dipCapMode` is `flat`
            # makes no watchlist read at all, and its other reads keep the
            # same order. The cap is per pass and node like the preview
            # one, so the books on a node share it.
            if str(self._cfg("dipCapMode", "flat")) == "measured" and ctx is not None and ctx.watchlist is not None and ctx.watch is not None:
                killers = ctx.watchlist.active(int(self._cfg("dipWindowDays", 7)), self.listed_accounts(), now)
                ctx.watch.drop_missing(self.node, killers)
                due = ctx.watch.due(self.node, killers, int(self._cfg("watchRefreshSec", 3600)), now)
                room = max(0, max(1, int(self._cfg("watchPreviewsPerTick", 5))) - int(ctx.watch.pass_fetches.get(self.node, 0)))
                calls = 0
                for k in due[:room]:
                    w_ans = ctx.watch.fetch(lens, self.node, k, self.account_index, now)
                    if w_ans is not None:
                        obs.answers.append(w_ans)
                        calls += 1
                ctx.watch.save(self.node)
                notes.update({"watchKillers": len(killers), "watchCalls": calls, "watchPending": len(due[room:])})
            obs.notes = notes
        obs.basis = obs.latest_basis()
        return obs

    def expected_ticks_between_acts(self) -> int | None:
        cfg = self.book.config
        period = collect_window(cfg)[1]  # the MAX of the window
        if cfg.get("feedMode") == "interval":
            period = min(period, cfg["feedInterval"])
        return ticks_for(period, int(cfg["tickSeconds"]))

    # -- the collect clock -------------------------------------------------

    def _draw_collect(self, runtime: dict[str, Any], from_sec: float) -> None:
        """Draw the next collect interval, uniform in the window (whole
        seconds, both ends included), and set the due time `from_sec` + the
        draw. Seeded from the book id and the persisted draw counter: draw
        n of a book is the same number in every process, and no two books
        share a seed."""
        lo, hi = collect_window(self.book.config)
        n = int(runtime.get("collectDraws", 0))
        interval = random.Random(f"{self.book.id}:{n}").randint(lo, hi)
        runtime["collectDraws"] = n + 1
        runtime["collectIntervalSec"] = interval
        runtime["nextCollectAt"] = float(from_sec) + interval

    def status_extra(self, runtime: dict[str, Any]) -> dict[str, Any] | None:
        """SPEC P3: the drawn collect due time (ISO) and the draw it came
        from; both null until the collect clock has started. A ladder book
        only: the rung in force, its no-waste floor, the ladder
        balances of the last inventory read and the rungs running out."""
        out: dict[str, Any] = {"nextCollectAt": rt_iso(runtime.get("nextCollectAt")), "collectIntervalSec": runtime.get("collectIntervalSec")}
        if self.ladder_mode:
            st = runtime.get("ladderStatus") or {}
            out.update({"ladderItem": st.get("ladderItem"), "ladderFloorHp": st.get("ladderFloorHp"), "foodStock": st.get("foodStock"), "foodLow": st.get("foodLow")})
        return out

    # -- the food ladder ---------------------------------------------------

    def _ladder_items(self) -> list[int]:
        """The rungs by heal, LARGEST first (the config order is not read)."""
        return sorted((int(i) for i in self.book.config.get("foodLadder") or []), key=lambda i: (-int(food_heal(i) or 0), i))

    def _stock(self, inventory: dict[int, int] | None) -> dict[int, int | None]:
        """Per rung: the balance, or None when the inventory was not read."""
        return {i: (None if inventory is None else int(inventory.get(i, 0))) for i in self._ladder_items()}

    @staticmethod
    def _in_stock(stocked: dict[int, int | None]) -> list[int]:
        """The rungs that may be fed, largest heal first: an unread
        inventory holds nothing back."""
        return [i for i, bal in stocked.items() if bal is None or bal > 0]

    def _dip_cap(self, view: Any, now_sec: float) -> tuple[int, str]:
        """(the dip cap in HP, where it came from). `flat`:
        `ladderMinFloorPct` of max HP.
        `measured`: the highest threshold a kami that liquidated inside
        `dipWindowDays` serves against THIS kami, plus `safetyMarginPct`,
        never under `dipMeasuredMinPct` — and the FLAT cap stands whenever
        the list cannot answer (missing, older than `watchlistMaxAgeSec`, or
        a listed kami not yet read for this node). A measured cap can sit
        ABOVE the flat one: a kami whose line is above the flat share is
        one the flat cap does not cover."""
        # the served thresholds are clamped exactly as a preview's are;
        # importing at call time because `previews` imports this package
        from ..previews import clamp

        hp_total = int(view.hp_total)
        flat = int(math.ceil(int(self._cfg("ladderMinFloorPct", 60)) * hp_total / 100.0))
        ctx = self.context
        if str(self._cfg("dipCapMode", "flat")) != "measured" or ctx is None or ctx.watchlist is None or ctx.watch is None:
            return flat, "flat"
        if not ctx.watchlist.fresh(int(self._cfg("watchlistMaxAgeSec", 86400)), now_sec):
            return flat, "flat: watchlist stale"
        measured_min = int(math.ceil(int(self._cfg("dipMeasuredMinPct", 30)) * hp_total / 100.0))
        killers = ctx.watchlist.active(int(self._cfg("dipWindowDays", 7)), self.listed_accounts(), now_sec)
        if not killers:
            return measured_min, "measured: no active killer"
        entries = ctx.watch.entries(self.node)
        if any(k not in entries for k in killers):
            return flat, "flat: watchlist not read yet"
        top = 0
        for k in killers:
            rows = entries[k]["rows"]
            if not rows:
                return flat, "flat: no row for this kami"
            thr = clamp(rows.get(self.kami))
            if thr is None:
                # the kami was not on the node when that read was taken: the
                # highest line the same kami serves against the account's
                # occupants stands in, a lens number for another kami
                thr = max(int(clamp(v) or 0) for v in rows.values())
            top = max(top, int(thr))
        measured = top + int(math.ceil(int(self._cfg("safetyMarginPct", 5)) * hp_total / 100.0))
        return max(measured, measured_min), "measured"

    def _ladder(self, obs: Observation, view: Any, now_sec: float, runtime: dict[str, Any], store: bool = True) -> dict[str, Any]:
        """The chooser: the rung this kami feeds now, and what bound it
        against. `rung` None = no rung at all (the failsafe stop): either
        nothing is in stock, or the bound a killed-here attacker sets is
        above every rung's floor. Writes `runtime["ladder"]` (the rung in
        force, persisted) whenever a rung is chosen — `steppedAt` moves only
        when the ITEM changes, so it is the clock `tierUpCooldownSec` runs
        against."""
        k = self.kami
        # both bounds of the SAME read: the one every owner that binds sets
        # (what the rung must clear) and the one only a killed-here attacker
        # sets (what a harvest stop may rest on)
        lim_all = self.bounds(obs, {k: view}, passive_binds=True)[0][k]
        lim_hostile = self.bounds(obs, {k: view}, passive_binds=False)[0][k]
        hp_total = int(view.hp_total)
        # the dip cap applies ALWAYS, whatever the node reads: it does not
        # depend on presence
        dip, dip_from = self._dip_cap(view, now_sec)
        need = max(int(lim_all["bound"]), int(lim_all["floor"]), dip)
        stocked = self._stock(obs.inventory)
        floor_of = {i: hp_total - int(food_heal(i) or 0) for i in stocked}
        in_stock = self._in_stock(stocked)
        out: dict[str, Any] = {
            "rung": None, "heal": None, "floor": None, "bound": need, "boundBy": lim_all["boundBy"], "dipFrom": dip_from,
            "boundHostile": max(int(lim_hostile["bound"]), int(lim_hostile["floor"])), "reason": "no rung in inventory", "stocked": stocked,
            # what the restart gate reads (an attacker that binds with no
            # preview yet, and the part of it that is an entry without rows)
            "unknown": list(lim_all["unknown"]), "unknownNoRows": list(lim_all.get("unknownNoRows") or []),
        }
        if not in_stock:
            return out
        smallest = in_stock[-1]
        chosen, reason = smallest, "bound unknown: smallest rung"
        if not lim_all["unknown"]:
            eligible = [i for i in in_stock if floor_of[i] >= need]
            if not eligible:
                if floor_of[smallest] < int(out["boundHostile"]) and bool(self.book.config.get("failsafeRest", True)):
                    # the kami cannot be held by feeding: the failsafe stop,
                    # and it rests on the killed-here bound alone
                    out["reason"] = "bound above every rung"
                    out["bound"] = out["boundHostile"]
                    out["boundBy"] = lim_hostile["boundBy"]
                    return out
                reason = "no rung clears the bound: smallest rung"
            else:
                chosen, reason = eligible[0], "largest rung the bound allows"
                prev = runtime.get("ladder") or {}
                prev_item = prev.get("item")
                prev_heal = int(food_heal(int(prev_item)) or 0) if isinstance(prev_item, int) and not isinstance(prev_item, bool) and int(prev_item) in floor_of else None
                if prev_heal is not None and int(food_heal(chosen) or 0) > prev_heal:
                    if now_sec - float(prev.get("steppedAt", 0) or 0) >= int(self._cfg("tierUpCooldownSec", 3600)):
                        # one rung: the smallest eligible heal above the last
                        chosen, reason = [i for i in eligible if int(food_heal(i) or 0) > prev_heal][-1], "one rung up"
                    elif int(prev_item) in eligible:
                        chosen, reason = int(prev_item), "held at the last rung"
                    else:
                        not_larger = [i for i in eligible if int(food_heal(i) or 0) <= prev_heal]
                        chosen, reason = (not_larger[0] if not_larger else eligible[-1]), "held at the last rung"
        heal = int(food_heal(chosen) or 0)
        prev = runtime.get("ladder") or {}
        stepped = float(prev["steppedAt"]) if prev.get("item") == chosen and isinstance(prev.get("steppedAt"), (int, float)) else float(now_sec)
        if store:  # the restart gate asks without moving the rung in force
            runtime["ladder"] = {"item": int(chosen), "heal": heal, "steppedAt": stepped}
        out.update({"rung": int(chosen), "heal": heal, "floor": floor_of[chosen], "reason": reason})
        return out

    def _wants_feed_ladder(self, view: Any, lad: dict[str, Any]) -> tuple[int | None, bool]:
        """(the item to feed, emergency) in ladder mode — (None, False) when
        the kami is above both feed points. The no-waste point is the chosen
        rung's floor; a bound that rose ABOVE the kami is answered at once
        with the largest rung in stock inside `maxOverhealHp`."""
        in_stock = self._in_stock(lad["stocked"])
        if view.hp_current < int(lad["bound"]):
            cap = int(self._cfg("maxOverhealHp", 25))
            fits = [i for i in in_stock if view.hp_current + int(food_heal(i) or 0) - int(view.hp_total) <= cap]
            return (fits[0] if fits else in_stock[-1]), True
        if view.hp_current <= int(lad["floor"]):
            return int(lad["rung"]), False
        return None, False

    def _rung_in_force(self, runtime: dict[str, Any], inventory: dict[int, int] | None) -> int | None:
        """The rung the stale and chain branches feed — no preview is read
        there, so the last rung a full assessment justified stands while it
        is in stock (or the stock is unknown); else the smallest rung in
        stock, the highest floor the ladder has. None = nothing in stock."""
        stocked = self._stock(inventory)
        in_stock = self._in_stock(stocked)
        if not in_stock:
            return None
        held = (runtime.get("ladder") or {}).get("item")
        if isinstance(held, int) and not isinstance(held, bool) and int(held) in in_stock:
            return int(held)
        return in_stock[-1]

    # -- feed ------------------------------------------------------------

    def _cooldown(self) -> int:
        return int(self.book.config.get("feedCooldownSec", 300))

    def _stale_cooldown(self) -> int:
        """Under lens-stale the lens cannot show a feed applied, so the only
        guard is time — 30 min covers a 10–24 HP/h drain against 50–150 HP
        heals; at 300 s a feed could be repeated every few minutes before
        any of them is visible."""
        return int(self.book.config.get("staleFeedCooldownSec", 1800))

    def _feed_decision(self, view: Any, have: int | None, item: int, under_stale: bool, ladder: dict[str, Any] | None = None) -> Decision:
        food = int(item)
        params: dict[str, Any] = {"kami": self.kami, "hp": view.hp_current, "hpMax": view.hp_total, "item": food, "heal": food_heal(food), "inventoryBalance": have}
        if under_stale:
            params["underStale"] = True
        if ladder:
            params.update(ladder)
        return Decision("feed", params, [Intent("system.kami.use.item", self.book.account, self.kami, {"itemIndex": food, "source": "inventory"}, self.book.id)])

    def _wants_feed(self, view: Any, now_sec: float, runtime: dict[str, Any]) -> bool:
        cfg = self.book.config
        if cfg.get("feedMode", "hp") == "hp":
            return view.hp_current < int(cfg["feedBelowHp"])
        return now_sec - float(runtime.get("lastFeedAt", now_sec)) >= int(cfg["feedInterval"])

    def assess(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        cfg = self.book.config
        view = obs.kamis.get(self.kami)
        if view is None or view.state == DEAD:
            return []
        acct = self.book.account
        food = int(cfg["foodItem"])
        out: list[Decision] = []
        runtime.pop("withheld", None)
        if self.ladder_mode:
            runtime["summaryExtra"] = dict(obs.notes)
        if view.state == HARVESTING and view.node_index == self.node:
            runtime.setdefault("lastCollectAt", now_sec)
            runtime.setdefault("lastFeedAt", now_sec)
            if not isinstance(runtime.get("nextCollectAt"), (int, float)):
                # the clock starts (a new book), or a restored runtime
                # carries no due time — ONE draw from lastCollectAt; a due
                # time already past is due now
                self._draw_collect(runtime, float(runtime["lastCollectAt"]))
            if self.ladder_mode:
                lad = self._ladder(obs, view, now_sec, runtime)
                runtime["summaryExtra"]["ladder"] = {"item": lad["rung"], "heal": lad["heal"], "floor": lad["floor"], "need": lad["bound"], "boundBy": lad["boundBy"], "reason": lad["reason"], "dipFrom": lad["dipFrom"]}
                low = int(self._cfg("foodLowUnits", 100))
                runtime["ladderStatus"] = {
                    "ladderItem": lad["rung"], "ladderFloorHp": lad["floor"],
                    "foodStock": {str(i): bal for i, bal in lad["stocked"].items()},
                    "foodLow": [i for i, bal in lad["stocked"].items() if bal is not None and 0 < bal < low],
                }
                # with NOTHING in stock the stop waits for the
                # point at which the book would have fed (the floor of the rung
                # in force, else of the smallest rung) — as a static book stops
                # only once it wants a feed — so one empty inventory read does
                # not take every ladder kami off the node at full HP. A
                # killed-here bound above every rung stops at once.
                held_item = (runtime.get("ladder") or {}).get("item")
                feed_point = view.hp_total - int(food_heal(held_item if isinstance(held_item, int) and not isinstance(held_item, bool) and held_item in lad["stocked"] else self._ladder_items()[-1]) or 0)
                if lad["rung"] is None and lad["reason"] == "no rung in inventory" and view.hp_current > feed_point:
                    pass
                elif lad["rung"] is None:
                    if cfg.get("failsafeRest", True):
                        in_stock = self._in_stock(lad["stocked"])
                        item = in_stock[-1] if in_stock else self._ladder_items()[-1]
                        out.append(Decision(
                            "failsafe-stop",
                            {"kami": self.kami, "hp": view.hp_current, "hpMax": view.hp_total, "item": item, "inventoryBalance": lad["stocked"][item], "reason": lad["reason"], "bound": lad["bound"], "boundBy": lad["boundBy"]},
                            [Intent("system.harvest.stop", acct, self.kami, {"count": 1}, self.book.id)],
                            plan_after={str(self.kami): {"state": RESTING, "node": None}},
                        ))
                        return out
                else:
                    item, emergency = self._wants_feed_ladder(view, lad)
                    if item is not None:
                        have = None if obs.inventory is None else obs.inventory.get(item, 0)
                        withheld = feed_withheld(runtime, self.kami, view, now_sec, self._cooldown())
                        if withheld is None:
                            extra = {"rung": item, "ladderReason": lad["reason"], "floor": view.hp_total - int(food_heal(item) or 0), "bound": lad["bound"], "boundBy": lad["boundBy"]}
                            if emergency:
                                extra["emergency"] = True
                            out.append(self._feed_decision(view, have, item, under_stale=False, ladder=extra))
                        else:
                            runtime["withheld"] = withheld
            elif self._wants_feed(view, now_sec, runtime):
                have = None if obs.inventory is None else obs.inventory.get(food, 0)
                if have is None or have > 0:
                    withheld = feed_withheld(runtime, self.kami, view, now_sec, self._cooldown())
                    if withheld is None:
                        out.append(self._feed_decision(view, have, food, under_stale=False))
                    else:
                        runtime["withheld"] = withheld
                elif cfg.get("failsafeRest", True):
                    out.append(Decision(
                        "failsafe-stop",
                        {"kami": self.kami, "hp": view.hp_current, "hpMax": view.hp_total, "item": food, "inventoryBalance": 0},
                        [Intent("system.harvest.stop", acct, self.kami, {"count": 1}, self.book.id)],
                        plan_after={str(self.kami): {"state": RESTING, "node": None}},
                    ))
                    return out
            if now_sec >= runtime["nextCollectAt"]:
                out.append(Decision(
                    "collect",
                    {"kami": self.kami, "musuAccrued": view.musu_accrued, "sinceLastCollectSec": int(now_sec - runtime["lastCollectAt"]), "drawnIntervalSec": runtime.get("collectIntervalSec")},
                    [Intent("system.harvest.collect", acct, self.kami, {"count": 1}, self.book.id)],
                ))
                # the collect clock moves in on_records, on a LANDED record
                # only: a refused or reverted collect stays due — and keeps
                # its due time, nothing is redrawn
        elif view.state == RESTING:
            if view.hp_total > 0 and view.hp_current * 100 >= int(cfg["resumeHpPct"]) * view.hp_total:
                if self.ladder_mode:
                    # a ladder book restarts only onto a node
                    # it can HOLD the kami on. Without this gate a kami the
                    # failsafe stopped (a killed-here bound above every rung,
                    # or nothing in stock) is at full HP a tick later, starts,
                    # is stopped again, and flaps for the whole window. An
                    # attacker that binds with no preview yet holds the start
                    # too, as on `auto` — except when every unknown entry has
                    # no rows and nothing of the account is on the node, where
                    # a refetch can answer nothing until one is.
                    lad = self._ladder(obs, view, now_sec, runtime, store=False)
                    hold = None
                    if lad["rung"] is None:
                        hold = lad["reason"]
                    elif lad["unknown"] and not (all(a in lad["unknownNoRows"] for a in lad["unknown"]) and not obs.notes.get("ownOnNode")):
                        hold = "bound unknown: attacker not yet previewed"
                    runtime["summaryExtra"]["ladder"] = {"item": lad["rung"], "heal": lad["heal"], "floor": lad["floor"], "need": lad["bound"], "boundBy": lad["boundBy"], "reason": lad["reason"], "dipFrom": lad["dipFrom"], "startHeld": hold}
                    if hold is not None:
                        return out
                out.append(Decision(
                    "start",
                    {"kami": self.kami, "node": self.node, "hp": view.hp_current, "hpMax": view.hp_total, "resumeHpPct": int(cfg["resumeHpPct"])},
                    [Intent("system.harvest.start", acct, self.kami, {"nodeIndex": self.node, "count": 1}, self.book.id)],
                    plan_after={str(self.kami): {"state": HARVESTING, "node": self.node}},
                ))
        return out

    def assess_under_stale(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """Under `lens-stale:<n>` alone, ONE cooldown-guarded feed when the
        (complete) row reads HARVESTING on the book's node with HP below
        the floor and food in inventory. Nothing else. A ladder book reads no preview here — the rung in force (or the
        smallest in stock) sets the floor."""
        cfg = self.book.config
        view = obs.kamis.get(self.kami)
        runtime.pop("withheld", None)
        if view is None or view.state != HARVESTING or view.node_index != self.node or not view.complete:
            return []
        if self.ladder_mode:
            food = self._rung_in_force(runtime, obs.inventory)
            if food is None or view.hp_current > view.hp_total - int(food_heal(food) or 0):
                return []
        else:
            if cfg.get("feedMode", "hp") != "hp" or view.hp_current >= int(cfg["feedBelowHp"]):
                return []
            food = int(cfg["foodItem"])
        have = None if obs.inventory is None else obs.inventory.get(food, 0)
        if have is not None and have <= 0:
            return []
        withheld = feed_withheld(runtime, self.kami, view, now_sec, self._stale_cooldown(), stale=True)
        if withheld is not None:
            runtime["withheld"] = withheld
            return []
        return [self._feed_decision(view, have, food, under_stale=True)]

    # -- the chain-backed fallback -----------------------------------------

    def chain_fallback_feeds(self) -> bool:
        return bool(self.book.config.get("chainFallback", True)) and self.book.config.get("feedMode", "hp") == "hp"

    def _chain_cooldown(self) -> int:
        return int(self.book.config.get("chainFeedCooldownSec", 1800))

    def assess_under_chain(self, reading: Any, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        """Under a lens outage, from the chain point read: ONE cooldown-
        guarded feed (`chainFeedCooldownSec`, the guard's stale rule: only
        time clears it) when the chain says HARVESTING on the book's node
        and the projected HP is below `feedBelowHp` — in ladder mode, below
        the no-waste floor of the rung in force. Inventory is
        not read (no lens): the eth_call pre-check is the send path's own
        gate on food. Nothing else — no start, stop, collect or revive; a
        chain state DEAD is a tick note only."""
        cfg = self.book.config
        runtime.pop("withheld", None)
        notes: dict[str, Any] = {}
        k = reading.kamis.get(self.kami)
        if k is None or not k.read:
            notes["chainUnread"] = [self.kami]
            runtime["summaryExtra"] = notes
            return []
        if k.state == DEAD:
            notes["chainDead"] = [self.kami]
        if not k.harvesting_on(self.node) or cfg.get("feedMode", "hp") != "hp":
            runtime["summaryExtra"] = notes
            return []
        hp = int(reading.projected.get(self.kami, k.hp_sync))
        view = kami_view_from_chain(self.kami, k.state, hp, k.hp_max, k.node_index, k.level)
        notes["chainHp"] = {str(self.kami): hp}
        if self.ladder_mode:
            food = self._rung_in_force(runtime, None)
            floor = int(k.hp_max) - int(food_heal(food) or 0) if food is not None else None
            if food is None or hp > floor:
                runtime["summaryExtra"] = notes
                return []
        else:
            food = int(cfg["foodItem"])
            if hp >= int(cfg["feedBelowHp"]):
                runtime["summaryExtra"] = notes
                return []
        withheld = feed_withheld(runtime, self.kami, view, now_sec, self._chain_cooldown(), stale=True)
        if withheld is not None:
            withheld["reason"] = "chain fallback; cooldown not elapsed"
            runtime["withheld"] = withheld
            runtime["summaryExtra"] = notes
            return []
        d = self._feed_decision(view, None, food, under_stale=False)
        d.parameters["underChain"] = True
        d.parameters["chainBlock"] = reading.block
        d.parameters["hpProjected"] = hp
        runtime["summaryExtra"] = notes
        return [d]

    def on_records(self, decisions: list[Decision], records: list[Any], now_sec: float, runtime: dict[str, Any]) -> None:
        pos = 0
        for d in decisions:
            mine = records[pos:pos + len(d.intents)] if d.intents else records[pos:pos + 1]
            pos += len(d.intents) if d.intents else 1
            if not mine:
                continue
            rec = mine[0]
            not_refused = getattr(rec, "precheck", None) not in ("reverted",)
            if d.kind == "feed":
                landed = rec.status in ("success", "unconfirmed", "dry_run") and not_refused
                if landed:
                    record_feed(runtime, int(d.parameters["kami"]), now_sec, int(d.parameters["hp"]), int(d.parameters.get("hpMax") or 0), int(d.parameters["item"]))
            elif d.kind in ("collect", "start"):
                # the collect clock moves only on a record that
                # LANDED — `success` on chain, or a dry run whose pre-check
                # passed; a not_sent / reverted / unconfirmed record leaves it
                # where it was, so the collect is due again next tick
                if rec.status in ("success", "dry_run") and not_refused:
                    runtime["lastCollectAt"] = float(now_sec)
                    # the ONE place a due time is redrawn
                    self._draw_collect(runtime, float(now_sec))
