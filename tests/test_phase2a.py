"""The `auto` strategy on the recorded fixtures (a full node read, two
attacker previews against it, the account's full party read, all at one
block) and the recorded liquidate receipt:

  1. fixture-driven observe (147 KamiViews, 45 foreign over 3 owners,
     validity verdicts);
  2. the preview cache through the book (cap, pending, hold);
  3. classes and bounds (two threshold-no-kill owners, one no-threshold;
     threshold + 2 %; a kill -> killed-here-48h + 5 %; a friend listing;
     cooldown skip);
  4. decisions (rest at the bound / at the floor, batched <= 12
     AllowFailure, the pending plan of an AllowFailure stop, FEED with the
     feed withhold, failsafe rest, start >= resume with the hold rule, batched
     <= 10, backoff on a refused start, autoCollect pooled, the stale
     branch, P7 for a set, adopt / refuse on start);
  5. the kill scan through the core (the invariant replay: class flip and
     the rest within one tick; flags that do not gate; persistence);
  6. the per-pass read cache (three books -> one node, one party read);
  7. scrub over every new surface;
  8. the margin derivation, and the live oracle parity (self-skips).
"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from fakechain import FakeChain, cast_log, liquidate_log, liquidate_tx
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.kills import BLOCKS_PER_HOUR
from keeper.lens import LensClient
from keeper.owner_class import is_observation_name
from keeper.scrub import collect_strings, forbidden_in
from keeper.signer import DryRunSigner
from keeper.strategies.auto import Auto, margin_needed
from keeper.strategies.base import HARVESTING, RESTING

FIX = Path(__file__).with_name("fixtures") / "lens"
NODE79 = json.loads((FIX / "node-79-full-2026-09-15.json").read_text())["data"]
ATT92 = json.loads((FIX / "node-79-preview-att360-2026-09-15.json").read_text())["data"]
ATT157 = json.loads((FIX / "node-79-preview-att448-2026-09-15.json").read_text())["data"]
PARTY = json.loads((FIX / "party-2849-full-2026-09-15.json").read_text())["data"]
RECEIPT = json.loads((Path(__file__).with_name("fixtures") / "receipts" / "liquidate_12143977.json").read_text())

CAROL = 2849
NODE = 79
T = 2_000_000_000.0
# the three books of the recorded account (kami sets of the fixture party)
BOOK_15625 = [66655, 67338, 68017, 68696, 69376, 70056, 70735, 72774, 73455, 74136, 74818, 75497, 76179, 76859, 77541, 78220, 78903, 79586, 80269, 80950, 81635, 82316, 82995, 83675, 84355, 85036, 85717, 86396, 87079, 87760, 88440, 89119, 89798, 90477, 91157, 91836, 92519, 93199, 93881, 94559, 95241, 95922, 96602, 97280, 98640, 99320, 99998]
BOOK_11251 = [24454, 25133, 25814, 27850, 28532, 29213, 29891, 30574, 31929, 32611, 33972, 35338, 36024, 36703, 37383, 38064, 38749, 39427, 40788, 41466, 42143, 43502, 44860, 45539, 46897, 48261, 48941, 49621, 50305, 50986, 51668, 52351, 53031, 53710, 54391, 55081, 55767, 56446, 57130, 57810, 58493, 59170, 59852, 61212, 61891, 62579, 63260, 63940, 64617, 65297]
BOOK_43 = [263, 508, 699, 1008, 1248, 1362, 1473, 1820, 1941, 2065, 2180, 2296, 2405, 3098, 3451, 3789, 4129, 4358, 4579, 4802, 4916, 5034, 5146, 5607, 5721, 6288, 6399, 6736, 7078, 7420, 7534, 7771, 7886, 7999, 10004, 10694, 11400, 12796, 13485, 14173, 16245, 16932, 17622, 18992, 19684, 20369, 21052, 21732, 23093, 23770]
# friends (account indices; never names)
FRIENDS = [1009, 1382, 1933, 2127, 2307, 2663, 3036, 3221, 3878, 3523, 3703, 4059]


def _config(kamis: list[int], **over: Any) -> dict[str, Any]:
    # resumeHpPct 80 explicitly: the fixtures' assertions are written against 80;
    # the schema default is 100
    cfg = {"kamiIndices": list(kamis), "nodeIndex": NODE, "accountIndex": CAROL, "regenMethod": "REST", "autoCollect": False, "safetyMarginPct": 5, "reviveItems": [11001], "friends": list(FRIENDS), "resumeHpPct": 80}
    cfg.update(over)
    return cfg


def _seed(fake, food_balance: int = 5) -> None:
    fake.nodes[NODE] = copy.deepcopy(NODE79)
    fake.previews[NODE] = {360: copy.deepcopy(ATT92), 448: copy.deepcopy(ATT157)}
    fake.party[CAROL] = copy.deepcopy(PARTY)
    fake.inventory[CAROL] = {"account": {"index": CAROL, "name": "carol"}, "items": [{"balance": food_balance, "item": {"id": "11304001", "index": 11304, "name": "Gakki Cookie Sticks", "type": "FOOD", "for": "KAMI", "rarity": 1}}, {"balance": 3, "item": {"id": "11001001", "index": 11001, "name": "Red Ribbon Gummy", "type": "REVIVE", "for": "KAMI", "rarity": 2}}]}


def _party_row(fake, kami: int) -> dict[str, Any]:
    return next(r for r in fake.party[CAROL]["kamis"] if r["index"] == kami)


def _set_party(fake, kami: int, **fields: Any) -> None:
    """state=, hp=(cur, total), node=index|None, musu=, level="""
    r = _party_row(fake, kami)
    if "state" in fields:
        r["state"] = fields["state"]
    if "hp" in fields:
        cur, tot = fields["hp"]
        r["hp"] = {"current": cur, "total": tot, "percent": round(100 * cur / tot) if tot else 0}
    if "node" in fields:
        if fields["node"] is None:
            r.pop("node", None)
        else:
            r["node"] = {"index": fields["node"], "name": f"node-{fields['node']}"}
    if "musu" in fields:
        r["musu"]["accrued"] = fields["musu"]
    if "level" in fields:
        r["level"] = fields["level"]
    # keep the node vitals of an occupant in step with the party read
    for h in fake.nodes[NODE]["harvests"]:
        if h["kami"]["index"] == kami:
            if "hp" in fields:
                h["vitals"]["hp"] = dict(r["hp"])
            if "level" in fields:
                h["vitals"]["level"] = fields["level"]


def _node_row(fake, kami: int) -> dict[str, Any]:
    return next(h for h in fake.nodes[NODE]["harvests"] if h["kami"]["index"] == kami)


def _add_occupant(fake, kami: int, account: int, hp: tuple[int, int] = (200, 240), level: int = 30, cooldown: int = 0) -> None:
    fake.nodes[NODE]["harvests"].append({"id": "0x" + hex(harvest_entity_id(kami))[2:].zfill(64), "state": "ACTIVE", "kami": {"id": "0x" + hex(kami_entity_id(kami))[2:].zfill(64), "name": f"kami-{kami}", "index": kami}, "account": {"index": account, "name": f"acct-{account}"}, "vitals": {"hp": {"current": hp[0], "total": hp[1], "percent": round(100 * hp[0] / hp[1])}, "hpRatePerHr": "-10.00", "musuAccrued": 10, "cooldownSec": cooldown, "cooldownUntil": 0, "level": level, "xp": 0, "xpRequired": 1, "levelUpReady": False, "levelUpBlockedBy": "EXPERIENCE", "skillPoints": 0}})


def _preview_threshold(fake, attacker: int, kami: int, threshold: int) -> None:
    """Set the served threshold of `attacker` against `kami` (a recorded
    preview when there is one, the synthesized one otherwise)."""
    if attacker in fake.previews[NODE]:
        for h in fake.previews[NODE][attacker]["harvests"]:
            if h["kami"]["index"] == kami:
                h["liquidation"]["threshold"] = threshold
                return
    fake.synth_thresholds[(attacker, kami)] = threshold


def _start(e: Env, kamis: list[int], **over: Any):
    r = e.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": _config(kamis, **over)})
    assert r["ok"], r
    return e.keeper.store.get(r["data"]["book"]["id"])


def _rows(e: Env, book, kind: str | None = None) -> list[dict[str, Any]]:
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def _last_tick(e: Env, book) -> dict[str, Any]:
    return _rows(e, book, "tick")[-1]["summary"]


def _last_tick_flags(e: Env, book) -> list[str]:
    return _rows(e, book, "tick")[-1]["degraded"]


def _decisions(e: Env, book, kind: str | None = None) -> list[dict[str, Any]]:
    return [r["decision"] for r in _rows(e, book, "decision") if kind is None or r["decision"]["kind"] == kind]


@pytest.fixture
def carol(make_keeper):
    """A lab keeper with the recorded account seeded: three auto books, the
    clock at T (advance `clock["now"]` and tick with it)."""
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    _seed(e.lens)
    e.grant()
    books = [_start(e, BOOK_15625), _start(e, BOOK_11251), _start(e, BOOK_43)]
    e.books = books  # type: ignore[attr-defined]
    e.clock = clock  # type: ignore[attr-defined]
    return e


def _tick_all(e, dt: float = 30.0) -> list[str]:
    e.clock["now"] += dt
    return e.keeper.tick_due(now=e.clock["now"])


def _apply_plans_to_world(e) -> None:
    """The fake world applies what the books signed: a planned HARVESTING
    kami reads HARVESTING on the node (and occupies it), a planned RESTING
    one reads RESTING — so a static fixture does not contradict the plan."""
    for b in e.books:
        for k, p in b.plan.items():
            k = int(k)
            if p.get("state") == HARVESTING:
                _set_party(e.lens, k, state=HARVESTING, node=NODE)
                if not any(h["kami"]["index"] == k for h in e.lens.nodes[NODE]["harvests"]):
                    r = _party_row(e.lens, k)
                    _add_occupant(e.lens, k, CAROL, hp=(r["hp"]["current"], r["hp"]["total"]), level=r["level"])
            elif p.get("state") == RESTING:
                _set_party(e.lens, k, state=RESTING, node=None)
                e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != k]


def _settle_previews(e) -> None:
    """Tick until every arrival is previewed (45 foreign at 10 per pass);
    the world applies the starts the books make meanwhile."""
    for _ in range(5):
        _tick_all(e)
        _apply_plans_to_world(e)
    assert _last_tick(e, e.books[0])["previewsPending"] == 0


# ------------------------------------------------------------ 1. observe


def test_observe_yields_the_owned_views_the_foreign_occupants_and_the_validity_verdicts(carol):
    e = carol
    seen: dict[int, Any] = {}
    for b in e.books:
        obs = e.keeper.strategies[b.id].observe(e.keeper.lens)
        assert set(obs.kamis) == set(b.config["kamiIndices"]) and obs.incomplete() == []
        seen.update(obs.kamis)
        assert len(obs.occupants) == 152
        foreign = {i: o for i, o in obs.occupants.items() if o.account_index != CAROL}
        assert len(foreign) == 45 and {o.account_index for o in foreign.values()} == {806, 997, 2481}
        assert obs.notes["foreignOccupants"] == 45 and obs.inventory == {11304: 5, 11001: 3}
    assert len(seen) == 147
    assert sum(1 for v in seen.values() if v.state == HARVESTING and v.node_index == NODE) == 107 and sum(1 for v in seen.values() if v.state == RESTING) == 40
    node_reads = [r["args"] for r in e.lens.requests if r["query"] == "node"]
    assert node_reads and all(a[-2:] == ["--with-vitals", "--full"] for a in node_reads)  # every node read (plain or preview) is --full
    # validity: a kami absent from the party answer is an unknown, never RESTING
    e.lens.party[CAROL]["kamis"] = [r for r in e.lens.party[CAROL]["kamis"] if r["index"] != 263]
    obs = e.keeper.strategies[e.books[2].id].observe(e.keeper.lens)
    assert obs.incomplete() == ["kami 263: missing row (absent from the party answer)"] and obs.kamis[263].state == ""
    _seed(e.lens)
    _party_row(e.lens, 508).pop("node")
    obs = e.keeper.strategies[e.books[2].id].observe(e.keeper.lens)
    assert obs.incomplete() == ["kami 508: missing node.index"]
    # a FOREIGN row lacking vitals is not a gated tick: noted, threshold unknown
    _seed(e.lens)
    _node_row(e.lens, 360).pop("vitals")
    obs = e.keeper.strategies[e.books[2].id].observe(e.keeper.lens)
    assert obs.incomplete() == [] and obs.notes["occupantsWithoutVitals"] == [360] and obs.occupants[360].vitals_missing == ("vitals",)
    s = e.keeper.tick_book(e.books[2], T + 1)
    assert s.get("reason") != "perception gated"


# ------------------------------------------------------------ 6. the pass cache


def test_three_books_on_one_node_cost_one_node_and_one_party_read_per_pass(carol):
    e = carol
    e.lens.requests.clear()
    _tick_all(e)
    reads = [r for r in e.lens.requests if r["query"] in ("node", "party", "inventory")]
    assert sum(1 for r in reads if r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 1) == 1
    assert sum(1 for r in reads if r["query"] == "party") == 1 and sum(1 for r in reads if r["query"] == "inventory") == 1
    previews = [r for r in reads if r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 2]
    assert len(previews) == 10  # previewsPerTick is one cap per pass and node, shared by the three books; arrivals first
    s1, s2, s3 = (_last_tick(e, b) for b in e.books)
    assert s1["lensCalls"] == 3 + 10 and s1["lensCallsServedFromPass"] == 0 and s1["lensBytes"] > 300_000
    assert s2["lensCalls"] == 0 and s2["lensCallsServedFromPass"] == 3 and s3["lensCallsServedFromPass"] == 3 and s3["lensBytes"] == 0
    assert s1["previewCalls"] == 10 and s1["previewsPending"] == 35 and s2["previewCalls"] == 0 and s2["previewsPending"] == 35 and s3["previewsPending"] == 35
    # a direct tick (no pass) reads for itself
    e.lens.requests.clear()
    e.keeper.tick_book(e.books[0], e.clock["now"] + 1)
    assert sum(1 for r in e.lens.requests if r["query"] == "party") == 1


# ------------------------------------------------------------ 2. previews through the book


def test_previews_settle_over_five_ticks_and_the_hold_rule_lifts(carol):
    e = carol
    _tick_all(e)
    s = _last_tick(e, e.books[2])
    # unread attackers of an owner with previews are proxied on the owner's maximum — nothing holds
    assert s["previewsPending"] == 35 and s["kamis"]["held"] == 0 and s["decisions"] == ["start"] and "held" not in s
    for _ in range(4):
        _tick_all(e)
        _apply_plans_to_world(e)
    s = _last_tick(e, e.books[2])
    assert s["previewsPending"] == 0 and s["kamis"]["held"] == 0
    assert len((e.data_dir / "previews").glob(f"79-{CAROL}.json") and list((e.data_dir / "previews").glob(f"79-{CAROL}.json"))) == 1
    st = e.keeper.handle("status")["data"]["books"]
    auto = [b["strategy"] for b in st if b["strategyType"] == "auto"]
    assert all(a["previewsPending"] == 0 and a["nextPreviewDueAt"] and a["kamis"]["harvesting"] > 0 for a in auto)
    # the summary's measurement keys do not write a tick row every tick (a steady world: no new row)
    _tick_all(e)
    n = len(_rows(e, e.books[2], "tick"))
    _tick_all(e)
    assert len(_rows(e, e.books[2], "tick")) == n


# ------------------------------------------------------------ 3. classes and bounds


def test_fixture_classes_and_the_bound_arithmetic(carol):
    e = carol
    _settle_previews(e)
    s = _last_tick(e, e.books[0])
    assert s["owners"] == {"listed": 0, "no-threshold": 1, "threshold-no-kill": 2, "killed-here": 0, "standing": 0}
    assert s["attackers"] == 45 and s["cooldownSkipped"] == [] and s["remembered"] == []
    strat: Auto = e.keeper.strategies[e.books[0].id]
    obs = strat.observe(e.keeper.lens)
    views = {k: v for k, v in obs.kamis.items() if not v.missing}
    lim, classes, attackers, skipped = strat.bounds(obs, views)
    # `classes` is keyed per ATTACKER KAMI; the owner view is the attackers' own rows
    assert set(classes) == {a["kami"] for a in attackers} and len(classes) == 45
    assert {a["owner"]: a["class"].value for a in attackers} == {806: "armed-passive", 997: "armed-passive", 2481: "harmless"}
    # a book kami served 6 by one attacker and 10 by the other (the fixture max), hpTotal 270 -> 10 + 2 % x 270 = 15.4 -> 16
    assert lim[68017]["bound"] == 16 and lim[68017]["boundBy"]["attackerKami"] == 448 and lim[68017]["boundBy"]["ownerClass"] == "threshold-no-kill" and lim[68017]["boundBy"]["threshold"] == 10
    assert lim[68017]["floor"] == 27 and lim[68017]["unknown"] == []
    # BOOK_15625[0]: max(0, served) over the two recorded attackers (the synthesized rest serve 0) + 2 % x hpTotal, ceiling
    v = views[66655]
    served = [max(0, next(h["liquidation"]["threshold"] for h in att["harvests"] if h["kami"]["index"] == 66655)) for att in (ATT92, ATT157)]
    assert lim[66655]["bound"] == -(-(100 * max(served) + 2 * v.hp_total) // 100) and lim[66655]["boundBy"]["threshold"] == max(served)
    zero = next(k for k in BOOK_15625 if k in views and all(next(h["liquidation"]["threshold"] for h in att["harvests"] if h["kami"]["index"] == k) <= 0 for att in (ATT92, ATT157)))
    assert lim[zero]["bound"] == -(-2 * views[zero].hp_total // 100) and lim[zero]["boundBy"]["threshold"] == 0
    assert s["boundKamis"] == len(BOOK_15625) and s["minMarginHp"] == min(views[k].hp_current - lim[k]["bound"] for k in views if views[k].state == HARVESTING)
    # a kill by one owner on this node inside the window: killed-here-48h and the 5 % margin
    e.keeper.owners.record_kill({"block": obs.basis.lensBlock - 100, "txHash": "0x01", "logIndex": 0, "node": NODE, "victimKami": 5, "victimAccount": 2481, "attackerKami": 360, "attackerAccount": 806, "attribution": "occupant"})
    lim2, classes2, _a, _s = strat.bounds(obs, views)
    assert classes2[360].value == "hostile" and strat.render_owner_class(806, obs.basis.lensBlock) == "killed-here-48h"  # per attacker kami (the first previewed attacker is that owner's); the default window is 48
    # the same kami: the killed-here attacker now binds at 6 + 5 % x 270 = 19.5 -> 20, over the other's 10 + 2 % x 270 = 15.4
    assert lim2[68017]["bound"] == 20 and lim2[68017]["boundBy"]["attackerKami"] == 360 and lim2[68017]["boundBy"]["ownerClass"] == "killed-here-48h" and lim2[68017]["boundBy"]["threshold"] == 6
    # a friend listing overrides everything: listed, never previewed, never binds
    _tick_all(e)
    assert _last_tick(e, e.books[0])["owners"]["killed-here"] == 1
    listed = _start(e, [66655, 68017], friends=FRIENDS + [806])
    e.lens.requests.clear()
    e.keeper.tick_book(listed, e.clock["now"] + 1)
    s = _last_tick(e, listed)
    assert s["owners"]["listed"] == 1 and s["owners"]["killed-here"] == 0 and s["attackers"] == 45 - 22
    assert not any(r["query"] == "node" and r["args"][:2] == ["79", "360"] for r in e.lens.requests)
    strat2: Auto = e.keeper.strategies[listed.id]
    obs2 = strat2.observe(e.keeper.lens)
    lim3, _c, _a, _s = strat2.bounds(obs2, {k: v for k, v in obs2.kamis.items()})
    assert lim3[68017]["boundBy"]["attackerKami"] == 448 and lim3[68017]["bound"] == 16
    # cooldown skip: an attacker on a 700 s cooldown is not counted this tick
    _node_row(e.lens, 448)["vitals"]["cooldownSec"] = 700
    obs3 = strat2.observe(e.keeper.lens)
    lim4, _c, _a, skipped4 = strat2.bounds(obs3, {k: v for k, v in obs3.kamis.items()})
    assert skipped4 == [448] and lim4[68017]["boundBy"]["attackerKami"] != 448


# ------------------------------------------------------------ 4. decisions


def test_rest_at_the_bound_single_stop_and_the_pending_plan_of_an_allow_failure_stop(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    _preview_threshold(e.lens, 360, 263, 170)  # BOOK_43[0] reads 171 / 240: bound 170 + 4.8 -> 175
    e.keeper.previews.entries(NODE, CAROL)[360].fetched_at = 0.0  # force the refresh
    _tick_all(e)
    d = _decisions(e, book, "rest")
    assert len(d) == 1 and d[0]["parameters"]["kamis"] == [263] and d[0]["parameters"]["batchSize"] == 1
    det = d[0]["parameters"]["detail"][0]
    assert det == {"kami": 263, "hp": 171, "hpTotal": 240, "bound": 175, "restFloor": 24, "boundBy": {"attackerKami": 360, "ownerClass": "threshold-no-kill", "threshold": 170, "present": True}, "drainPerHr": 12.97}  # the drain rate rides the rest row
    tx = [r["tx"] for r in _rows(e, book, "tx")][-1]
    assert tx["system"] == "system.harvest.stop" and tx["fn"] == "executeTyped" and tx["kami"] == 263 and tx["status"] == "dry_run"
    assert book.plan["263"] == {"state": RESTING, "node": None, "pending": True}
    assert _last_tick(e, book)["minMarginHp"] == -4
    # the stop applied: the next read shows RESTING and settles the plan (no external change)
    _set_party(e.lens, 263, state=RESTING, node=None, hp=(171, 240))
    _tick_all(e)
    s = _last_tick(e, book)
    assert book.plan["263"] == {"state": RESTING, "node": None} and "contradiction" not in s and book.state == "running"
    # the AllowFailure member the stop did NOT apply to: still harvesting, re-decided alone, never an external change
    _set_party(e.lens, 263, state=HARVESTING, node=NODE, hp=(171, 240))
    book.plan["263"] = {"state": RESTING, "node": None, "pending": True}
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["restNotApplied"] == [263] and "contradiction" not in s and book.state == "running"
    assert _decisions(e, book, "rest")[-1]["parameters"]["kamis"] == [263]


def test_rest_at_the_floor_and_batched_stops_of_at_most_twelve(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    low = BOOK_43[:15]
    for k in low:
        _set_party(e.lens, k, hp=(20, 220))
    _tick_all(e)
    d = _decisions(e, book, "rest")
    assert [x["parameters"]["batchSize"] for x in d] == [12, 3] and [k for x in d for k in x["parameters"]["kamis"]] == low
    det = d[0]["parameters"]["detail"][0]
    assert det["boundBy"] is None or det["bound"] < det["restFloor"]
    assert det["restFloor"] == 22 and det["hp"] == 20
    txs = [r["tx"] for r in _rows(e, book, "tx")]
    assert [(t["fn"], t["count"]) for t in txs if t["system"] == "system.harvest.stop"] == [("executeBatchedAllowFailure", 12), ("executeBatchedAllowFailure", 3)]
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True} for k in low)
    s = _last_tick(e, book)
    assert s["acted"] is True and s["decisions"] == ["rest", "rest"] and s["kamis"]["harvesting"] == sum(1 for k in BOOK_43 if _party_row(e.lens, k)["state"] == HARVESTING)


def test_feed_path_with_the_1c_guard_and_the_failsafe_rest(carol):
    e = carol
    _settle_previews(e)
    feed = _start(e, [263, 508, 699], regenMethod="FEED", foodItem=11304)
    _set_party(e.lens, 263, hp=(20, 240))
    _set_party(e.lens, 508, hp=(20, 220))
    _tick_all(e)
    d = _decisions(e, feed, "feed")
    assert [x["parameters"]["kami"] for x in d] == [263, 508] and d[0]["parameters"]["item"] == 11304 and d[0]["parameters"]["heal"] == 100 and d[0]["parameters"]["inventoryBalance"] == 5
    assert d[0]["parameters"]["restFloor"] == 24 and "bound" in d[0]["parameters"]
    txs = [r["tx"] for r in _rows(e, feed, "tx")]
    assert [t["system"] for t in txs] == ["system.kami.use.item"] * 2 and all(t["count"] == 1 for t in txs)  # feeds are never batched
    assert _decisions(e, feed, "rest") == []
    # the lens has not applied the feed: withheld, not fed again inside feedCooldownSec
    _tick_all(e)
    s = _last_tick(e, feed)
    assert len(_decisions(e, feed, "feed")) == 2 and s["feedWithheld"] and all(w["reason"] == "feed not yet visible in the lens HP read" for w in s["feedWithheld"])
    # no food in inventory: the failsafe rest, never a purchase
    e.lens.inventory[CAROL]["items"][0]["balance"] = 0
    e.clock["now"] += 400
    _tick_all(e)
    d = _decisions(e, feed, "rest")
    assert len(d) == 1 and d[0]["parameters"]["kamis"] == [263, 508] and d[0]["parameters"]["detail"][0]["failsafe"] == "no food in inventory"
    assert all(t["system"] in ("system.kami.use.item", "system.harvest.stop") for t in [r["tx"] for r in _rows(e, feed, "tx")])


def test_start_at_resume_with_the_hold_rule_batched_at_most_thirty_one(carol):
    e = carol
    _settle_previews(e)
    resting = [r for r in PARTY["kamis"] if r["state"] == RESTING]
    assert len(resting) == 40 and sum(1 for r in resting if r["hp"]["current"] * 100 >= 80 * r["hp"]["total"]) == 13
    assert sum(1 for b in e.books for k, p in b.plan.items() if p["state"] == HARVESTING) == 107 + 13  # the settle ticks deployed the 13 at or above 80 %
    # re-seed the party so this test reads the resting kamis fresh; every kami of BOOK_43 resting at 100 % so one batch exceeds the start cap (10)
    _seed(e.lens)
    book = e.books[2]
    for k in BOOK_43:
        _set_party(e.lens, k, state=RESTING, node=None, hp=(240, 240))
    for h in list(e.lens.nodes[NODE]["harvests"]):
        if h["kami"]["index"] in BOOK_43:
            e.lens.nodes[NODE]["harvests"].remove(h)
    for b in e.books:
        for k in b.config["kamiIndices"]:
            b.plan[str(k)] = {"state": RESTING, "node": None} if _party_row(e.lens, k)["state"] == RESTING else {"state": HARVESTING, "node": NODE}
    # the hold rule: BOOK_43[0] at 225 / 240 (94 %) with a threshold of 210 -> bound 215, plus its last drain (12.97 / h) = 227.97 > 225
    _set_party(e.lens, 263, hp=(225, 240))
    _preview_threshold(e.lens, 360, 263, 210)
    e.keeper.previews.entries(NODE, CAROL)[360].fetched_at = 0.0
    n0 = len(_decisions(e, book, "start"))
    t0 = len([t for t in (r["tx"] for r in _rows(e, book, "tx")) if t["system"] == "system.harvest.start"])
    _tick_all(e)
    d = _decisions(e, book, "start")[n0:]
    started = [k for x in d for k in x["parameters"]["kamis"]]
    # the proxied bound changes what the settle ticks deploy: one kami (240 / 240)
    # harvested and recorded 25.2 HP/h, so its need (215 + 25.2) exceeds its HP
    # and it is held with BOOK_43[0]; the start rule itself is the same
    assert [x["parameters"]["batchSize"] for x in d] == [10, 10, 10, 10, 8] and set(started) == set(BOOK_43) - {263, 7420}
    assert d[0]["parameters"]["node"] == NODE and d[0]["parameters"]["detail"][0]["resumeHpPct"] == 80
    txs = [t for t in (r["tx"] for r in _rows(e, book, "tx")) if t["system"] == "system.harvest.start"][t0:]
    assert [(t["fn"], t["count"]) for t in txs] == [("executeBatched", 10)] * 4 + [("executeBatched", 8)]  # the same 48 kamis, chunked at the start cap
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in started)
    s = _last_tick(e, book)
    assert s["kamis"]["held"] == 2 and s["held"][0] == {"kami": 263, "reason": "bound above resume HP", "hp": 225, "bound": 215, "drainPerHr": 12.97}
    assert s["held"][1] == {"kami": 7420, "reason": "bound above resume HP", "hp": 240, "bound": 215, "drainPerHr": 25.2}
    assert book.plan["263"] == {"state": RESTING, "node": None}


def test_a_refused_start_backs_the_kami_off_after_the_batch_falls_back_to_singles(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]

    class RefuseStart(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for i, r in zip(intents, recs):
                if r.system == "system.harvest.start" and 1941 in i.kamis:
                    r.status, r.precheck, r.reason = "not_sent", "reverted", "Harvest: kami not in room [code 3]"
            return recs

    signer = RefuseStart()
    signer._grants = e.keeper.signer._grants
    e.keeper.signer = signer
    for k in (1941, 2065, 5146):  # three kamis of the book resting at >= 80 % -> one batch of 3
        _set_party(e.lens, k, state=RESTING, node=None, hp=(230, 240))
        e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != k]
        book.plan[str(k)] = {"state": RESTING, "node": None}
    n0 = len([t for t in (r["tx"] for r in _rows(e, book, "tx")) if t["system"] == "system.harvest.start"])
    _tick_all(e)
    txs = [t for t in (r["tx"] for r in _rows(e, book, "tx")) if t["system"] == "system.harvest.start"][n0:]
    # the batch (pre-check refused as a whole), then one single per member in the book's order: the first refused, the other two land
    assert [(t["count"], t["status"], t.get("batchFallback", False)) for t in txs] == [(3, "not_sent", False), (1, "not_sent", True), (1, "dry_run", True), (1, "dry_run", True)]
    assert {t["kami"] for t in txs if t.get("batchFallback")} == {1941, 2065, 5146}
    dev = _rows(e, book, "deviation")
    assert len(dev) == 1 and dev[0]["deviation"]["kami"] == 1941 and dev[0]["deviation"]["action"] == "start" and dev[0]["deviation"]["backoffSec"] == 3600 and "not in room" in dev[0]["deviation"]["reason"]
    assert book.plan["2065"] == {"state": HARVESTING, "node": NODE} and book.plan["1941"] == {"state": RESTING, "node": None}
    s = _last_tick(e, book)
    assert s["batchFallbackTx"] == 3 and s["decisions"] == ["start"]
    assert e.keeper.runtime[book.id]["backoff"]["1941"]["kind"] == "start"
    assert json.loads((e.data_dir / "books" / book.id / "runtime.json").read_text())["runtime"]["backoff"]["1941"]["until"] == e.clock["now"] + 3600
    # not retried every tick; offered again after startBackoffSec
    _set_party(e.lens, 2065, state=HARVESTING, node=NODE)
    _set_party(e.lens, 5146, state=HARVESTING, node=NODE)
    n = len(_decisions(e, book, "start"))
    _tick_all(e)
    assert len(_decisions(e, book, "start")) == n and _last_tick(e, book)["kamis"]["backoff"] == 1 and _last_tick(e, book)["backoffKamis"] == [1941]
    _tick_all(e, dt=3600)
    assert _decisions(e, book, "start")[-1]["parameters"]["kamis"] == [1941]


def test_auto_collect_is_pooled_into_the_accounts_batched_collect(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [263, 508], autoCollect=True, bountyCollectThreshold=10000)
    _set_party(e.lens, 263, musu=12000)
    _tick_all(e)
    d = _decisions(e, book, "collect")
    assert len(d) == 1 and d[0]["parameters"] == {"kami": 263, "musuAccrued": 12000, "bountyCollectThreshold": 10000}
    s = _last_tick(e, book)
    assert s["collectPooled"] == 1 and s["acted"] is True
    rec = [r for r in e.keeper.signer.recorded if r.system == "system.harvest.collect"]
    assert len(rec) == 1 and rec[0].kami == 263 and rec[0].batchSize == 1
    assert [r["tx"]["system"] for r in _rows(e, book, "tx")] == ["system.harvest.collect"]
    assert "intensity" in e.keeper.handle("describe", {"type": "auto"})["data"]["description"]


def test_under_a_stale_lens_a_rest_book_is_idle_and_a_feed_book_feeds(carol):
    e = carol
    _settle_previews(e)
    feed = _start(e, [263, 508], regenMethod="FEED", foodItem=11304)
    _set_party(e.lens, 263, hp=(20, 240))
    _set_party(e.lens, 66655, hp=(20, 270))
    e.lens.mode, e.lens.lag = "stale", 200
    before = len(_decisions(e, e.books[0]))
    _tick_all(e)
    rest = _last_tick(e, e.books[0])
    assert rest["underStale"] is True and rest["decisions"] == [] and rest["underStaleIdle"].startswith("REST book") and rest["degraded"] == ["lens-stale:203"]
    assert len(_decisions(e, e.books[0])) == before  # no rest, start or collect on a stale HP read (BOOK_15625[0] at 20 / 270 is below the floor)
    f = _last_tick(e, feed)
    assert f["underStale"] is True and f["decisions"] == ["feed"]
    d = _decisions(e, feed, "feed")
    assert d[0]["parameters"]["kami"] == 263 and d[0]["parameters"]["underStale"] is True
    row = [r for r in _rows(e, feed, "decision")][-1]
    assert row["degraded"] == ["lens-stale:203"]
    tx = _rows(e, feed, "tx")[-1]["tx"]
    assert tx["underStale"] is True and tx["system"] == "system.kami.use.item"
    _tick_all(e)
    assert len(_decisions(e, feed, "feed")) == 1  # once per staleFeedCooldownSec
    assert _last_tick(e, feed)["feedWithheld"]["reason"] == "lens stale; cooldown not elapsed"


def test_p7_for_a_set_and_the_adopt_refuse_rules_on_start(carol):
    e = carol
    _settle_previews(e)
    book = e.books[2]
    _set_party(e.lens, 263, state=HARVESTING, node=80)
    for _ in range(3):
        _tick_all(e)
    assert book.state == "paused" and book.pausedReason == "external-change"
    events = [r["event"] for r in _rows(e, book, "lifecycle")]
    assert events == ["started", "external-change", "paused"]
    # start: a kami harvesting elsewhere refuses the whole start, naming it; nothing is stored
    n = len(e.keeper.store.all())
    r = e.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": _config([263, 508])})
    assert r["ok"] is False and r["error"]["code"] == "BAD_ARGS" and "kami 263 is HARVESTING on node 80" in r["error"]["message"]
    assert len(e.keeper.store.all()) == n
    # adopt: HARVESTING on the node and RESTING are owned as observed
    _set_party(e.lens, 263, state=HARVESTING, node=NODE)
    _set_party(e.lens, 1941, state=RESTING, node=None)
    b = _start(e, [263, 1941])
    assert b.plan == {"263": {"state": HARVESTING, "node": NODE}, "1941": {"state": RESTING, "node": None}}


# ------------------------------------------------------------ 5. the kill scan through the core


def _chain_env(make_keeper, tmp_path, chain: FakeChain) -> Env:
    e = make_keeper(cap=6)
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "kc"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    clock = {"now": T}
    k.clock = lambda: clock["now"]
    k.boot()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(cfg.dataDir)
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    assert k.handle("grant", {"account": "lab"})["ok"]
    e.books = [_start(e, BOOK_15625), _start(e, BOOK_11251), _start(e, BOOK_43)]  # type: ignore[attr-defined]
    return e


def _fixture_logs() -> list[dict]:
    return [{"topics": ["0x" + t for t in l["topics"]], "data": "0x" + l["data"], "blockNumber": RECEIPT["block"], "transactionHash": "0x" + RECEIPT["tx_hash"], "logIndex": i, "address": l["address"]} for i, l in enumerate(RECEIPT["logs"])]


def test_the_invariant_replay_a_liquidate_log_flips_the_owner_class_within_one_tick_and_rests_the_bound_kamis(make_keeper, tmp_path):
    chain = FakeChain(head=12143991)
    e = _chain_env(make_keeper, tmp_path, chain)
    # the receipt's victim (the account's kami, outside the books) and its killer (another account) are occupants of the node
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    _add_occupant(e.lens, 42823, 3878, hp=(250, 300), level=45)
    _preview_threshold(e.lens, 42823, 263, 165)  # BOOK_43[0] reads 171 / 240: 165 + 2 % = 170 (no rest); 165 + 5 % = 177 (rest)
    friends = [f for f in FRIENDS if f != 3878]
    for b in e.books:
        b.config["friends"] = friends
    e.keeper.owners.set_last_scanned(12143971)
    for _ in range(5):
        _tick_all(e)
        _apply_plans_to_world(e)
    s = _last_tick(e, e.books[2])
    assert s["previewsPending"] == 0 and s["owners"]["threshold-no-kill"] == 3 and s["owners"]["killed-here"] == 0 and _decisions(e, e.books[2], "rest") == []
    assert s["killScan"]["kills"] == 0 and s["killScan"]["lastScanned"] == 12143991 and _last_tick_flags(e, e.books[2]) == []
    # the recorded liquidate lands in the next block
    chain.head = 12143992
    chain.logs = [dict(l, blockNumber=12143992) for l in _fixture_logs()]
    chain.txs["0x" + RECEIPT["tx_hash"]] = {"hash": "0x" + RECEIPT["tx_hash"], "input": "0x" + RECEIPT["input"], "from": RECEIPT["from"], "to": RECEIPT["to"], "blockNumber": 12143992}
    _tick_all(e)
    for b in e.books:
        kills = _rows(e, b, "kill")
        assert len(kills) == 1
        k = kills[0]["kill"]
        assert k == {"event": "liquidate", "victimKami": 5946, "victimAccount": CAROL, "attackerKami": 42823, "attackerAccount": 3878, "block": 12143992, "txHash": "0x" + RECEIPT["tx_hash"], "ownerClassAfter": "killed-here-48h", "attribution": "occupant", "blockTs": None, "learned": {"skipped": "no view of the victim from the previous pass"}}  # the fake node serves no header for this block, so the row is UNDATED and the block floor dates it; the victim is the account's but in no book's kami set, so no book held a view of it
        assert is_observation_name(k["ownerClassAfter"])
    s = _last_tick(e, e.books[2])
    assert s["owners"]["killed-here"] == 1 and s["owners"]["threshold-no-kill"] == 2 and s["killScan"]["kills"] == 1
    d = _decisions(e, e.books[2], "rest")
    assert len(d) == 1 and d[0]["parameters"]["kamis"] == [263]
    assert d[0]["parameters"]["detail"][0]["boundBy"] == {"attackerKami": 42823, "ownerClass": "killed-here-48h", "threshold": 165, "present": True, "hostileBy": "both"} and d[0]["parameters"]["detail"][0]["bound"] == 177
    assert e.keeper.owners.killed_ours_in_window(3878, CAROL, 48, None) and e.keeper.owners.killed_accounts == {3878: {CAROL: 12143992}}
    doc = json.loads((e.data_dir / "owners.json").read_text())
    assert doc["owners"]["lastScannedBlock"] == 12143992 and doc["owners"]["killedAccounts"] == {"3878": {str(CAROL): 12143992}}
    # the attacker leaves: remembered (its owner is killed-here) and it binds exactly like a present one (the stop did not apply to BOOK_43[0]: rested again, bound by the absent killer)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 42823]
    _tick_all(e)
    s = _last_tick(e, e.books[2])
    assert s["remembered"] == [42823] and s["attackers"] == 46 and s["owners"]["killed-here"] == 1 and s["restNotApplied"] == [263]
    assert _last_tick(e, e.books[0])["previewsRemembered"] == [42823]  # noted by the first book of the pass, whose read reconciled the presence
    d = _decisions(e, e.books[2], "rest")
    assert len(d) == 2 and d[1]["parameters"]["detail"][0]["boundBy"] == {"attackerKami": 42823, "ownerClass": "killed-here-48h", "threshold": 165, "present": False, "hostileBy": "both"}
    _set_party(e.lens, 263, state=RESTING, node=None, hp=(171, 240))
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 263]
    # a restart keeps the ledger and the previews: the class holds without a refetch
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k2.clock = lambda: e.clock["now"]
    k2.boot()
    assert k2.owners.last_scanned_block == 12143992 and k2.owners.killed_ours_in_window(3878, CAROL, 48, None)
    assert any("owners.json restored" in l["line"] for l in k2.logs)
    k2.handle("grant", {"account": "lab"})
    e.lens.requests.clear()
    e.clock["now"] += 30
    k2.tick_due(now=e.clock["now"])
    assert not any(r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 2 for r in e.lens.requests)  # no refetch storm
    b2 = k2.store.get(e.books[2].id)
    rows = k2.handle("ledger", {"book": b2.id})["data"]["rows"]
    assert [r for r in rows if r["kind"] == "tick"][-1]["summary"]["owners"]["killed-here"] == 1


def test_unattributed_kills_and_casts(make_keeper, tmp_path):
    chain = FakeChain(head=100)
    e = _chain_env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL)
    e.keeper.owners.set_last_scanned(90)
    _tick_all(e)
    _apply_plans_to_world(e)
    # an unknown selector: a row and a note, no class changed
    chain.head = 101
    chain.logs = [liquidate_log(101, "0x02", kami_entity_id(5946), 0)]
    chain.txs["0x02"] = liquidate_tx("0x02", harvest_entity_id(5946), kami_entity_id(42823), "other")
    _tick_all(e)
    k = _rows(e, e.books[0], "kill")[-1]["kill"]
    assert k["attackerAccount"] is None and k["attackerKami"] is None and k["ownerClassAfter"] is None and k["attribution"] == "selector not a liquidate"
    s = _last_tick(e, e.books[0])
    assert s["killScan"]["unattributed"] == 1 and s["owners"]["killed-here"] == 0 and e.keeper.owners.killed_accounts == {}
    assert any("attacker unresolved" in l["line"] and "no class changed" in l["line"] for l in e.keeper.logs)
    # a KAMI_CAST on one of the book's kamis: a `cast` row, killed-here for the caster's account
    e.lens.accounts["0x" + (0xabc).to_bytes(20, "big").hex()] = {"index": 997, "name": "acct-997", "stamina": {"current": 1}}
    chain.head = 102
    chain.logs.append(cast_log(102, "0x03", 0xabc, kami_entity_id(263), 21202, NODE))
    _tick_all(e)
    c = _rows(e, e.books[2], "kill")[-1]["kill"]
    assert c["event"] == "cast" and c["victimKami"] == 263 and c["attackerAccount"] == 997 and c["item"] == 21202 and c["ownerClassAfter"] == "killed-here-48h"
    assert _last_tick(e, e.books[2])["killScan"]["casts"] == 1 and e.keeper.owners.killed_ours_in_window(997, CAROL, 48, None)
    # a cast on a FOREIGN occupant of the node is not a cast on the book's kamis — no row,
    # no class change, counted as castsOnOthers
    before = len(_rows(e, e.books[2], "kill"))
    chain.logs.append(cast_log(103, "0x04", 0xdef, kami_entity_id(360), 19001, NODE))
    chain.head = 103
    _tick_all(e)
    s2 = _last_tick(e, e.books[2])
    assert s2["killScan"]["casts"] == 0 and s2["killScan"]["castsOnOthers"] == 1 and len(_rows(e, e.books[2], "kill")) == before


def test_scan_flags_ride_the_auto_ticks_and_never_gate(make_keeper, tmp_path):
    chain = FakeChain(head=1000)
    e = _chain_env(make_keeper, tmp_path, chain)
    e.keeper.owners.set_last_scanned(990)
    _tick_all(e)
    _apply_plans_to_world(e)
    assert _last_tick_flags(e, e.books[0]) == [] and e.keeper.owners.last_scanned_block == 1000
    # a gap larger than one call's span: ws-logs-gap:<N>, catch-up capped at 25 calls x 40 blocks per pass, the tick still assesses
    # the pass starts `killScanOverlapBlocks` (12) BEHIND the cursor, so a
    # capped catch-up ends 12 blocks short of the span and the remainder is
    # 490 + 12; the call count is the cap
    chain.head = 1000 + 25 * 40 + 490
    _tick_all(e)
    _apply_plans_to_world(e)
    s = _last_tick(e, e.books[0])
    assert _last_tick_flags(e, e.books[0]) == ["ws-logs-gap:502"] and s.get("reason") != "perception gated" and "kamis" in s and s["killScan"]["logCalls"] == 25
    assert e.keeper.handle("status")["data"]["books"][0]["degraded"] == ["ws-logs-gap:502"]
    _tick_all(e)
    assert _last_tick_flags(e, e.books[0]) == [] and e.keeper.owners.last_scanned_block == chain.head
    # an RPC failure on the scan: rpc-unavailable on the tick and its rows, still not gated, lastScanned unmoved
    chain.fail_reads = True
    chain.head += 10
    _set_party(e.lens, 66655, hp=(20, 270))
    _tick_all(e)
    s = _last_tick(e, e.books[0])
    assert _last_tick_flags(e, e.books[0]) == ["rpc-unavailable"] and s["decisions"] == ["rest"] and s["killScan"]["rpcError"]
    assert _rows(e, e.books[0], "decision")[-1]["degraded"] == ["rpc-unavailable"]
    assert e.keeper.owners.last_scanned_block == chain.head - 10
    chain.fail_reads = False
    _tick_all(e)
    assert _last_tick_flags(e, e.books[0]) == [] and e.keeper.owners.last_scanned_block == chain.head


def test_a_keeper_without_a_chain_says_so_on_every_auto_tick(carol):
    e = carol
    _tick_all(e)
    assert _last_tick(e, e.books[0])["killScan"] == "no chain configured; owner classes rest on the persisted ledger only"


# ------------------------------------------------------------ 7. scrub


def test_every_new_surface_is_clean(carol):
    e = carol
    _settle_previews(e)
    e.keeper.owners.record_kill({"block": 1, "txHash": "0x01", "logIndex": 0, "node": NODE, "victimKami": 5, "victimAccount": 2481, "attackerKami": 360, "attackerAccount": 806, "attribution": "occupant"})
    _set_party(e.lens, 263, hp=(20, 240))
    feed = _start(e, [508, 699], regenMethod="FEED", foodItem=11304)
    _set_party(e.lens, 508, hp=(20, 220))
    _tick_all(e)
    e.lens.mode, e.lens.lag = "stale", 200
    _tick_all(e)
    e.lens.mode = "live"
    frames = [e.keeper.handle("describe", {"type": "auto"}), e.keeper.handle("status"), e.keeper.handle("books", {"account": "lab"}), e.keeper.handle("logs")]
    frames += [e.keeper.handle("ledger", {"book": b.id}) for b in e.books + [feed]]
    frames.append(e.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": _config([263], regenMethod="FEED")}))
    frames.append(e.keeper.handle("ledger", {"book": feed.id, "kind": "kill"}))
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    desc = e.keeper.handle("describe", {"type": "auto"})["data"]
    assert set(desc["defaults"]) >= {"restFloorPct", "cooldownHorizonSec", "previewRefreshSec", "previewsPerTick", "startBackoffSec", "logScanBlocks"} and "accountIndex" in desc["schema"]["required"]
    for row in (r for f in frames[4:8] for r in f["data"]["rows"]):
        for cls in [d["parameters"].get("boundBy", {}) or {} for d in [row.get("decision", {})] if d]:
            if cls.get("ownerClass"):
                assert is_observation_name(cls["ownerClass"])
        if row["kind"] == "tick" and "owners" in row["summary"]:
            assert set(row["summary"]["owners"]) == {"listed", "no-threshold", "threshold-no-kill", "killed-here", "standing"}


# ------------------------------------------------------------ 8. the margin derivation, and the live oracle parity


def test_the_default_five_percent_margin_covers_the_derived_need_for_every_fixture_kami():
    ratios = []
    for r in PARTY["kamis"]:
        rate = abs(float(r["hpRatePerHr"])) if r["state"] == HARVESTING else 0.0
        need = margin_needed(rate, 30, 90, 2.5)
        margin = 5 * r["hp"]["total"] / 100.0
        assert margin >= need, (r["index"], margin, need)
        if rate:
            ratios.append(margin / need)
    rates = [abs(float(r["hpRatePerHr"])) for r in PARTY["kamis"] if r["state"] == HARVESTING]
    assert max(rates) < 17 and len(rates) == 107  # the recorded drain rates read up to 16.72 HP/h on this party
    assert min(ratios) > 8  # the tightest fixture kami keeps > 8x the derived need at 90 blocks of lag
    assert margin_needed(13.0, 30, 90, 2.5) == pytest.approx(13.0 * (30 + 225 + 2.5) / 3600)




def test_passive_owners_bind_false_leaves_only_the_floor_until_the_first_kill(carol):
    """`passiveOwnersBind: false` — a
    threshold-no-kill owner's thresholds are read and kept but do not bind
    until that owner's first recorded kill on the node; killed-here always
    binds with the 5 % margin."""
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False)
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    views = {k: v for k, v in obs.kamis.items() if not v.missing}
    lim, classes, attackers, _s = strat.bounds(obs, views)
    assert {a["owner"]: a["class"].value for a in attackers if a["owner"] in (806, 997)} == {806: "armed-passive", 997: "armed-passive"}  # classes are per attacker kami
    assert lim[68017]["bound"] == 0 and lim[68017]["boundBy"] is None and lim[68017]["unknown"] == [] and lim[68017]["floor"] == 27
    e.lens.requests.clear()
    e.keeper.tick_book(book, e.clock["now"] + 1)
    s = _last_tick(e, book)
    assert s["owners"]["threshold-no-kill"] == 2 and s["boundKamis"] == 0 and s.get("held") is None
    # the owner's first kill on this node: killed-here-48h binds at threshold + 5 %
    e.keeper.owners.record_kill({"block": obs.basis.lensBlock - 100, "txHash": "0x02", "logIndex": 0, "node": NODE, "victimKami": 5, "victimAccount": 2481, "attackerKami": 360, "attackerAccount": 806, "attribution": "occupant"})
    lim2, classes2, _a, _s = strat.bounds(obs, views)
    assert classes2[360].value == "hostile" and lim2[68017]["bound"] == 20 and lim2[68017]["boundBy"]["attackerKami"] == 360 and lim2[68017]["boundBy"]["ownerClass"] == "killed-here-48h"
    assert lim2[68017]["boundBy"]["threshold"] == 6
