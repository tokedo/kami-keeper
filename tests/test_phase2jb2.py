"""The food ladder on `harvestAndFeed`. The ladder is
`[11313, 11304, 11312, 11303, 11301]` — Golden Apple +150, Gakki Sticks
+100, Honeydew Scale +75, Pom-Pom +50, Ghost Gum +25:

  1. a book WITHOUT `foodLadder` reads and decides as a static book, and
     never enters the node-threat paths;
  2. the chooser with nobody on the node — the dip cap (`ladderMinFloorPct`
     60) is what most kamis end on;
  3. the feed point is the no-overheal point and not a HP earlier;
  4. stock — the next smaller rung, then the failsafe when nothing is left;
  5. a killed-here attacker arrives: tier-down the same tick, and the
     emergency top-up inside `maxOverhealHp` when the kami is already under;
  6. the pending-feed rule holds the emergency until the last feed is visible;
  7. a threshold-no-kill owner never parks the kami; a killed-here one
     does;
  8. tier-up: one rung per `tierUpCooldownSec`, and `ladder` round-trips;
  9. an attacker that binds with no preview: the smallest rung;
 10. the stale and chain branches feed the rung in force;
 11. the signer whitelist takes every rung and nothing else;
 12. the config refusals;
 13. 36 ladder books on one node: ONE census read, previews under the cap;
 14. a kill on the node is scanned for a ladder book, the class flips and
     the rung drops;
 15. scrub over describe / status / logs / ledger.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import HAF_CONFIG, Env
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.books import BookError, validate_config
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.describe import describe_type
from keeper.lens import LensClient
from keeper.runtime import runtime_path
from keeper.scrub import collect_strings, forbidden_in
from keeper.signer import Intent, WhitelistRefused, check_whitelist
from keeper.strategies.base import HARVESTING, RESTING

NODE = 62
ACCT = 4242
KAMI = 1001
APPLE, STICKS, HONEYDEW, POM, GUM = 11313, 11304, 11312, 11303, 11301
LADDER = [APPLE, STICKS, HONEYDEW, POM, GUM]
HEAL = {APPLE: 150, STICKS: 100, HONEYDEW: 75, POM: 50, GUM: 25}
T = 2_000_000_000.0
# the ladder keys that have a default: a stored config without them must
# read and decide exactly as one that has them
NEW_KEYS = ("ladderMinFloorPct", "maxOverhealHp", "tierUpCooldownSec", "foodLowUnits", "friends", "safetyMarginPct", "passiveMarginPct", "passiveOwnersBind", "hostileWindowHours", "restFloorPct", "cooldownHorizonSec", "previewRefreshSec", "previewsPerTick", "previewPolicy", "nodeCensus", "attackerVitalsPerTick", "logScanBlocks")


# ------------------------------------------------------------ the world


def _harvest(kami: int, account: int, hp: tuple[int, int], level: int = 20, cooldown: int = 0) -> dict[str, Any]:
    return {
        "id": "0x" + hex(harvest_entity_id(kami))[2:].zfill(64), "state": "ACTIVE",
        "kami": {"id": "0x" + hex(kami_entity_id(kami))[2:].zfill(64), "name": f"kami-{kami}", "index": kami},
        "account": {"index": account, "name": f"acct-{account}"},
        "vitals": {"hp": {"current": hp[0], "total": hp[1], "percent": round(100 * hp[0] / hp[1])}, "hpRatePerHr": "-10.00", "musuAccrued": 1000, "cooldownSec": cooldown, "cooldownUntil": 0, "level": level, "xp": 0, "xpRequired": 1, "levelUpReady": False, "levelUpBlockedBy": "EXPERIENCE", "skillPoints": 0},
    }


def _node_doc(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"index": NODE, "name": "fixture-node", "type": "HARVEST", "affinity": ["NORMAL"], "roomIndex": 61, "harvestsTotal": len(rows), "harvestsServed": len(rows), "harvests": rows}


def _world(e: Env, ours: dict[int, tuple[int, int]] | None = None, foreign: list[tuple[int, int, tuple[int, int]]] | None = None, balances: dict[int, int] | None = None) -> None:
    """Node 62 with `ours` (kami -> (hp, total)) harvesting on it and
    `foreign` (kami, account, hp) beside them; the ladder in inventory."""
    fake = e.lens
    ours = {KAMI: (330, 330)} if ours is None else ours
    rows = []
    for k, hp in ours.items():
        fake.set_kami(k, state=HARVESTING, node=NODE, hp=hp, account=ACCT)
        rows.append(_harvest(k, ACCT, hp, level=12))  # the level the `kami` answer carries: a mismatch would refresh the preview every tick
    for k, acct, hp in foreign or []:
        rows.append(_harvest(k, acct, hp))
    fake.nodes[NODE] = _node_doc(rows)
    fake.previews[NODE] = {}
    bal = {i: 500 for i in LADDER} if balances is None else balances
    fake.inventory[ACCT] = {"account": {"index": ACCT, "name": "lab"}, "items": [{"balance": b, "item": {"id": f"{i}001", "index": i, "name": f"item-{i}", "type": "FOOD", "for": "KAMI", "rarity": 1}} for i, b in bal.items()]}


def _env(make_keeper, tmp_path=None, chain: FakeChain | None = None, cap: int = 4) -> Env:
    e = make_keeper(cap=cap)
    if chain is not None:
        cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "kg"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": cap, "requestTimeoutSec": 5}, env={})
        k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
        k.boot()
        e.keeper.stop_engine()
        e.keeper = k
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    assert e.keeper.handle("grant", {"account": "lab"})["ok"]
    return e


def _ladder_config(**over: Any) -> dict[str, Any]:
    cfg = {"kamiIndex": KAMI, "nodeIndex": NODE, "foodItem": GUM, "accountIndex": ACCT, "foodLadder": list(LADDER)}
    cfg.update(over)
    return cfg


def _start(e: Env, **over: Any):
    r = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": _ladder_config(**over)})
    assert r["ok"], r
    return e.keeper.store.get(r["data"]["book"]["id"])


def _rows(e: Env, book, kind: str | None = None) -> list[dict[str, Any]]:
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def _last_tick(e: Env, book) -> dict[str, Any]:
    return _rows(e, book, "tick")[-1]["summary"]


def _decisions(e: Env, book, kind: str | None = None) -> list[dict[str, Any]]:
    return [r["decision"] for r in _rows(e, book, "decision") if kind is None or r["decision"]["kind"] == kind]


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_book(book, now=e.clock["now"])


def _requests(e: Env) -> list[str]:
    return [(r["query"] + " " + " ".join(r["args"])).strip() for r in e.lens.requests]


def _rung(e: Env, book) -> int | None:
    return (e.keeper.runtime[book.id].get("ladder") or {}).get("item")


# ------------------------------------------------- 1. a static book is untouched


def _static_run(make_keeper, strip: bool) -> tuple[list[str], list[dict[str, Any]], dict[str, Any], bool]:
    """One tick of a plain `harvestAndFeed` book below its floor. With
    `strip` the stored config is cut back to a config without any of the
    ladder keys."""
    e = make_keeper()
    e.keeper.clock = lambda: T
    e.grant()
    e.lens.set_kami(KAMI, state=HARVESTING, node=NODE, hp=(40, 200), account=ACCT)
    book = e.keeper.store.get(e.start_haf()["data"]["book"]["id"])
    if strip:
        for k in NEW_KEYS:
            book.config.pop(k, None)
        e.keeper.store.save(book)
    else:
        assert all(k in book.config for k in NEW_KEYS)  # they ARE materialised at start
    strategy = e.keeper.strategies[book.id]
    e.lens.requests.clear()
    summary = e.keeper.tick_book(book, now=T)
    return _requests(e), _decisions(e, book), summary, strategy.watches_node() or bool(e.keeper._auto_books())


def test_a_static_book_reads_and_decides_exactly_as_before(make_keeper):
    with_keys = _static_run(make_keeper, strip=False)
    without = _static_run(make_keeper, strip=True)
    assert with_keys == without
    reads, decisions, summary, node_paths = with_keys
    # the reads are the two of a static book: no node census, no preview
    assert [r.split()[0] for r in reads] == ["status", "kami", "inventory"] and not any(r.startswith("node") for r in reads)
    assert [d["kind"] for d in decisions] == ["feed"] and decisions[0]["parameters"]["item"] == HAF_CONFIG["foodItem"]
    assert set(decisions[0]["parameters"]) == {"kami", "hp", "hpMax", "item", "heal", "inventoryBalance"}
    # and it is in none of the node-threat paths: no kill scan, no `ladder`
    assert node_paths is False and "killScan" not in summary and "ladder" not in summary


def test_a_static_book_status_carries_only_the_collect_keys(env):
    env.grant()
    env.lens.set_kami(KAMI, state=HARVESTING, node=NODE, hp=(150, 200), account=ACCT)
    book = env.keeper.store.get(env.start_haf()["data"]["book"]["id"])
    env.keeper.tick_book(book, now=T)
    st = env.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert set(st) == {"nextCollectAt", "collectIntervalSec"}


# --------------------------------------------- 2. the chooser with a clear node


@pytest.mark.parametrize("total,item,floor", [(330, STICKS, 230), (250, STICKS, 150), (190, HONEYDEW, 115)])
def test_with_nobody_on_the_node_the_dip_cap_sets_the_rung(make_keeper, total, item, floor):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (total, total)})
    book = _start(e)
    s = _tick(e, book)
    lad = s["ladder"]
    assert (lad["item"], lad["heal"], lad["floor"]) == (item, HEAL[item], floor)
    assert lad["need"] == -(-60 * total // 100) and lad["boundBy"] is None and lad["reason"] == "largest rung the bound allows"
    assert floor * 100 >= 60 * total and (total - 150) * 100 < 60 * total or item == APPLE  # the apple is what the cap excludes
    assert _rung(e, book) == item and s["decisions"] == []  # full HP: nothing to feed yet
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert st["ladderItem"] == item and st["ladderFloorHp"] == floor and st["foodStock"] == {str(i): 500 for i in LADDER} and st["foodLow"] == []


def test_a_low_dip_cap_lets_the_top_rung_through(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e, ladderMinFloorPct=50)
    assert _tick(e, book)["ladder"]["item"] == APPLE  # floor 180 = 54.5 %, over 50 now


# --------------------------------------------------------- 3. the feed point


def test_it_feeds_at_the_no_waste_point_and_not_one_hp_earlier(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e)
    assert _tick(e, book)["decisions"] == []
    _world(e, ours={KAMI: (231, 330)})  # one above the sticks floor
    assert _tick(e, book)["decisions"] == []
    _world(e, ours={KAMI: (230, 330)})
    s = _tick(e, book)
    assert s["decisions"] == ["feed"]
    p = _decisions(e, book, "feed")[-1]["parameters"]
    assert (p["item"], p["heal"], p["rung"], p["floor"], p["bound"], p["boundBy"]) == (STICKS, 100, STICKS, 230, 198, None)
    assert p["ladderReason"] == "largest rung the bound allows" and "emergency" not in p and p["inventoryBalance"] == 500
    assert [r.system for r in e.keeper.signer.recorded][-1:] == ["system.kami.use.item"]
    assert e.keeper.signer.recorded[-1].params["itemIndex"] == STICKS


# ------------------------------------------------------------------ 4. stock


def test_a_rung_at_zero_balance_falls_to_the_next_smaller_one(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)}, balances={APPLE: 500, STICKS: 0, HONEYDEW: 500, POM: 500, GUM: 90})
    book = _start(e, foodLowUnits=100)
    s = _tick(e, book)
    assert s["ladder"]["item"] == HONEYDEW and s["ladder"]["floor"] == 255
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert st["foodStock"] == {str(APPLE): 500, str(STICKS): 0, str(HONEYDEW): 500, str(POM): 500, str(GUM): 90}
    assert st["foodLow"] == [GUM]  # 0 is not low, it is out


def test_an_empty_ladder_inventory_takes_the_failsafe_stop(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (240, 330)}, balances={i: 0 for i in LADDER})
    book = _start(e)
    s = _tick(e, book)
    assert s["decisions"] == ["failsafe-stop"] and s["ladder"]["item"] is None and s["ladder"]["reason"] == "no rung in inventory"
    p = _decisions(e, book, "failsafe-stop")[-1]["parameters"]
    assert p["item"] == GUM and p["inventoryBalance"] == 0 and p["reason"] == "no rung in inventory"
    assert [r.system for r in e.keeper.signer.recorded][-1] == "system.harvest.stop"
    assert e.keeper.store.get(book.id).plan[str(KAMI)] == {"state": RESTING, "node": None}


def test_failsafe_rest_false_leaves_an_empty_inventory_alone(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (240, 330)}, balances={i: 0 for i in LADDER})
    book = _start(e, failsafeRest=False)
    assert _tick(e, book)["decisions"] == []


# ----------------------------------------------------------- 12. the refusals


def test_the_ladder_config_refusals():
    base = {"kamiIndex": KAMI, "nodeIndex": NODE, "foodItem": GUM, "accountIndex": ACCT, "foodLadder": list(LADDER)}
    assert validate_config("harvestAndFeed", dict(base), "lab")["foodLadder"] == LADDER
    for over, where in (
        ({"accountIndex": None}, "accountIndex"),
        ({"foodItem": 11302}, "foodItem"),
        ({"feedBelowHp": 100}, "feedBelowHp"),
        ({"foodLadder": [11313, 19001]}, "foodLadder"),
        ({"feedMode": "interval"}, "feedMode"),
        ({"passiveOwnersBind": True, "previewPolicy": "killed-here"}, "previewPolicy"),
        ({"passiveOwnersBind": True, "nodeCensus": "plain"}, "nodeCensus"),
    ):
        cfg = {k: v for k, v in dict(base, **over).items() if v is not None}
        with pytest.raises(BookError) as err:
            validate_config("harvestAndFeed", cfg, "lab")
        assert f"config invalid at {where}" in str(err.value) and not forbidden_in([str(err.value)])
    # a static book is unchanged: feedBelowHp stays required when feedMode is hp
    with pytest.raises(BookError, match="feedBelowHp"):
        validate_config("harvestAndFeed", {k: v for k, v in HAF_CONFIG.items() if k != "feedBelowHp"}, "lab")
    assert validate_config("harvestAndFeed", dict(HAF_CONFIG), "lab")["feedBelowHp"] == 100
    # an empty ladder, or one over eight rungs, is a schema refusal
    for bad in ([], [11301, 11302, 11303, 11304, 11311, 11312, 11313, 11314, 11301]):
        with pytest.raises(BookError):
            validate_config("harvestAndFeed", dict(base, foodLadder=bad), "lab")


def test_describe_names_the_ladder_keys_and_stays_clean():
    d = describe_type("harvestAndFeed", "lab")
    assert set(d["defaults"]) >= {"ladderMinFloorPct", "maxOverhealHp", "tierUpCooldownSec", "foodLowUnits", "safetyMarginPct", "previewsPerTick"}
    assert "foodLadder" not in d["defaults"] and "accountIndex" not in d["defaults"]
    assert "- foodLadder (array, item index):" in d["description"] and "default 60" in [l for l in d["description"].splitlines() if l.startswith("- ladderMinFloorPct ")][0]
    assert not forbidden_in(d)


# ------------------------------------------------------- 11. the whitelist


def test_every_rung_is_signable_and_nothing_else_is():
    cfg = validate_config("harvestAndFeed", _ladder_config(reviveItems=[11001]), "lab")
    for item in LADDER + [11001]:
        check_whitelist(Intent("system.kami.use.item", "lab", KAMI, {"itemIndex": item}), cfg)
    for item in (11302, 11314, 19001):
        with pytest.raises(WhitelistRefused, match="foodLadder"):
            check_whitelist(Intent("system.kami.use.item", "lab", KAMI, {"itemIndex": item}), cfg)
    # a static book still takes its foodItem only
    static = validate_config("harvestAndFeed", dict(HAF_CONFIG), "lab")
    check_whitelist(Intent("system.kami.use.item", "lab", KAMI, {"itemIndex": HAF_CONFIG["foodItem"]}), static)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.kami.use.item", "lab", KAMI, {"itemIndex": APPLE}), static)


# ----------------------------------------- 5-7. an attacker on the node


ATTACKER, OWNER = 2002, 7777


def _kill_row(block: int, owner: int | None, kami: int | None, victim: int = 5946) -> dict[str, Any]:
    return {"block": block, "txHash": "0x" + f"{block:064x}", "logIndex": 0, "node": NODE, "victimKami": victim, "victimAccount": ACCT, "attackerKami": kami, "attackerAccount": owner, "attribution": "occupant"}


def _with_attacker(e: Env, threshold: int, hp: tuple[int, int] = (330, 330), killed_here: bool = False, extra_foreign: list[tuple[int, int, tuple[int, int]]] | None = None) -> None:
    _world(e, ours={KAMI: hp}, foreign=[(ATTACKER, OWNER, (200, 240))] + (extra_foreign or []))
    e.lens.synth_thresholds[(ATTACKER, KAMI)] = threshold
    if killed_here:
        e.keeper.owners.record_kill(_kill_row(e.lens.block - 1, OWNER, ATTACKER))


def test_a_killed_here_arrival_drops_the_rung_in_the_same_tick(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e)
    assert _tick(e, book)["ladder"]["item"] == STICKS  # nobody on the node
    _with_attacker(e, threshold=220, killed_here=True)
    s = _tick(e, book)
    lad = s["ladder"]
    # 220 + 5 % x 330 = 236.5 -> 237: the sticks floor 230 no longer clears it
    assert lad["need"] == 237 and lad["item"] == HONEYDEW and lad["floor"] == 255
    assert lad["boundBy"] == {"attackerKami": ATTACKER, "ownerClass": "killed-here-48h", "threshold": 220, "present": True, "hostileBy": "both"}
    # a full kami is above both feed points: the drop is the rung, not a feed
    assert s["decisions"] == [] and e.keeper.signer.recorded == [] and _rung(e, book) == HONEYDEW


def test_the_emergency_top_up_takes_the_largest_rung_inside_max_overheal(make_keeper):
    e = _env(make_keeper)
    _with_attacker(e, threshold=220, hp=(230, 330), killed_here=True)
    book = _start(e)
    s = _tick(e, book)
    assert s["ladder"]["item"] == HONEYDEW and s["ladder"]["need"] == 237
    p = _decisions(e, book, "feed")[-1]["parameters"]
    # HP 230 is UNDER the bound 237: the largest rung whose overheal is at
    # most 25 — the apple would overheal 50, the sticks nothing
    assert p["emergency"] is True and (p["item"], p["rung"], p["floor"]) == (STICKS, STICKS, 230)
    assert p["hp"] + HEAL[p["item"]] - p["hpMax"] <= 25
    assert e.keeper.signer.recorded[-1].params["itemIndex"] == STICKS


def test_a_tight_overheal_cap_falls_back_to_the_smallest_rung(make_keeper):
    e = _env(make_keeper)
    _with_attacker(e, threshold=330, hp=(329, 330))  # a bound above max HP: every tick is an emergency
    book = _start(e, maxOverhealHp=0)
    assert _tick(e, book)["decisions"] == ["feed"]
    p = _decisions(e, book, "feed")[-1]["parameters"]
    assert p["emergency"] is True and p["item"] == GUM  # every rung overheals: the smallest


def test_a_threshold_no_kill_owner_never_parks_the_kami_and_a_killed_here_one_does(make_keeper):
    passive = _env(make_keeper)
    _with_attacker(passive, threshold=310)
    book = _start(passive)
    s = _tick(passive, book)
    # 310 + 2 % x 330 = 316.6 -> 317: over every floor, the gum's 305 included
    assert s["ladder"]["need"] == 317 and s["ladder"]["item"] == GUM and s["ladder"]["reason"] == "no rung clears the bound: smallest rung"
    assert s["decisions"] == [] and passive.keeper.signer.recorded == []  # it keeps harvesting on the smallest rung
    assert passive.keeper.store.get(book.id).plan[str(KAMI)] == {"state": HARVESTING, "node": NODE}
    hostile = _env(make_keeper)
    _with_attacker(hostile, threshold=310, killed_here=True)
    book2 = _start(hostile)
    s2 = _tick(hostile, book2)
    assert s2["decisions"] == ["failsafe-stop"] and s2["ladder"]["item"] is None
    p = _decisions(hostile, book2, "failsafe-stop")[-1]["parameters"]
    assert p["reason"] == "bound above every rung" and p["bound"] == 327 and p["boundBy"]["ownerClass"] == "killed-here-48h"
    assert hostile.keeper.store.get(book2.id).plan[str(KAMI)] == {"state": RESTING, "node": None}


# ------------------------------------------------------------- 6. the pending-feed rule


def test_the_guard_holds_an_emergency_until_the_last_feed_is_visible(make_keeper):
    e = _env(make_keeper)
    _with_attacker(e, threshold=330, hp=(300, 330))  # bound 337: above max HP, so every tick is an emergency
    book = _start(e)
    s = _tick(e, book)
    assert s["decisions"] == ["feed"] and len(e.keeper.signer.recorded) == 1
    assert _decisions(e, book, "feed")[-1]["parameters"]["item"] == POM  # 300 + 50 - 330 = 20, inside the 25 cap
    # the lens has not applied it: the emergency is withheld like any feed
    s = _tick(e, book)
    assert s["acted"] is False and s["feedWithheld"]["reason"] == "feed not yet visible in the lens HP read"
    assert len(e.keeper.signer.recorded) == 1
    _with_attacker(e, threshold=330, hp=(328, 330))  # the feed is visible
    s = _tick(e, book)
    assert s["decisions"] == ["feed"] and len(e.keeper.signer.recorded) == 2
    p = _decisions(e, book, "feed")[-1]["parameters"]
    assert p["emergency"] is True and p["item"] == GUM  # 328 + 25 - 330 = 23; anything larger overheals more than 25


# ------------------------------------------------------------ 8. the tier-up


def test_one_rung_up_per_tier_up_cooldown_and_the_rung_survives_a_restart(make_keeper, tmp_path):
    e = _env(make_keeper)
    _with_attacker(e, threshold=260, hp=(330, 330))
    book = _start(e)
    s = _tick(e, book)
    assert s["ladder"]["need"] == 267 and s["ladder"]["item"] == POM  # 260 + 2 % x 330
    stepped = e.keeper.runtime[book.id]["ladder"]["steppedAt"]
    _world(e, ours={KAMI: (330, 330)})  # the attacker leaves; nothing binds
    for dt in (30.0, 600.0, 2000.0):
        s = _tick(e, book, dt)
        assert s["ladder"]["item"] == POM and s["ladder"]["reason"] == "held at the last rung"
        assert e.keeper.runtime[book.id]["ladder"]["steppedAt"] == stepped
    s = _tick(e, book, 1000.0)  # 3630 s after the step: ONE rung
    assert s["ladder"]["item"] == HONEYDEW and s["ladder"]["reason"] == "one rung up"
    s = _tick(e, book, 30.0)
    assert s["ladder"]["item"] == HONEYDEW and s["ladder"]["reason"] == "held at the last rung"
    s = _tick(e, book, 3600.0)
    assert s["ladder"]["item"] == STICKS and s["ladder"]["reason"] == "one rung up"
    s = _tick(e, book, 3600.0)  # the apple is out on the dip cap: sticks is the top, and stays
    assert s["ladder"]["item"] == STICKS and s["ladder"]["reason"] == "largest rung the bound allows"
    held = dict(e.keeper.runtime[book.id]["ladder"])
    assert json.loads(runtime_path(e.data_dir / "books" / book.id).read_text())["runtime"]["ladder"] == held
    k2 = Keeper(load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "requestTimeoutSec": 5}, env={}), lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    assert k2.runtime[book.id]["ladder"] == held
    k2.stop_engine()


def test_a_tier_down_is_immediate_whatever_the_cooldown(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e)
    assert _tick(e, book)["ladder"]["item"] == STICKS
    _with_attacker(e, threshold=260, hp=(330, 330))
    assert _tick(e, book)["ladder"]["item"] == POM  # down two rungs in one tick, no cooldown


# ------------------------------------------------- 9. an attacker with no preview


def test_an_attacker_that_binds_without_a_preview_takes_the_smallest_rung(make_keeper):
    e = _env(make_keeper)
    _with_attacker(e, threshold=100, extra_foreign=[(2003, OWNER, (200, 240))])
    e.lens.synth_thresholds[(2003, KAMI)] = 100
    book = _start(e, previewsPerTick=1)
    s = _tick(e, book)
    # the ladder follows the same rule as `auto` — an attacker whose own
    # preview has not landed is PROXIED on its owner's served maximum, so the
    # bound is KNOWN and the ladder sizes to that bound (sticks here, under the
    # flat dip cap 198); "bound unknown: smallest rung" fires only for an owner
    # with no preview on the node at all
    assert s["previewsPending"] == 1 and s["ladder"]["need"] == 198 and s["ladder"]["item"] == STICKS and s["ladder"]["reason"] == "largest rung the bound allows"
    # the second preview lands and changes nothing: the stand-in already served
    # the same threshold (100 + 2 % x 330 = 107, under the dip cap 198)
    s = _tick(e, book)
    assert s["previewsPending"] == 0 and s["ladder"]["need"] == 198 and s["ladder"]["item"] == STICKS and s["ladder"]["reason"] == "largest rung the bound allows"
    # and the rule that is LEFT: an owner with NO preview on the node at all is
    # still unknown and still drops the ladder to the smallest rung (a step down
    # is immediate). Its hostility then comes from the kill ledger, not from a
    # threshold — an owner whose class is armed BY a preview always has a served
    # maximum for its unread attackers to stand on
    e.keeper.previews.entries(NODE, ACCT).clear()
    e.lens.synth_default = None  # every preview read is refused: nothing is ever served for 7777
    e.keeper.owners.record_kill(_kill_row(e.lens.block - 1, OWNER, ATTACKER))
    s = _tick(e, book)
    assert s["ladder"]["item"] == GUM and s["ladder"]["reason"] == "bound unknown: smallest rung"


# -------------------------------------------- 10. the stale and chain branches


def test_under_a_stale_lens_the_book_feeds_the_rung_in_force(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e)
    assert _tick(e, book)["ladder"]["item"] == STICKS
    e.lens.mode, e.lens.lag = "stale", 500
    _world(e, ours={KAMI: (240, 330)})  # above the sticks floor 230: nothing yet
    s = _tick(e, book)
    assert s["underStale"] is True and s["decisions"] == [] and e.keeper.signer.recorded == []
    _world(e, ours={KAMI: (229, 330)})
    s = _tick(e, book)
    assert s["underStale"] is True and s["decisions"] == ["feed"]
    p = _decisions(e, book, "feed")[-1]["parameters"]
    assert p["item"] == STICKS and p["underStale"] is True and "rung" not in p
    # the rung in force is out of stock: the smallest in stock stands in
    _world(e, ours={KAMI: (229, 330)}, balances={APPLE: 0, STICKS: 0, HONEYDEW: 0, POM: 500, GUM: 500})
    s = _tick(e, book, 3600.0)
    assert s["decisions"] == ["feed"] and _decisions(e, book, "feed")[-1]["parameters"]["item"] == GUM


def test_under_a_lens_outage_the_chain_branch_feeds_the_rung_in_force(make_keeper, tmp_path):
    from test_chainhp import FakeHpReader

    reader = FakeHpReader({KAMI: (HARVESTING, 300, 330, NODE)})
    e = _env(make_keeper, tmp_path, chain=FakeChain(head=12278391))
    e.keeper.chainhp = reader
    _world(e, ours={KAMI: (330, 330)})
    book = _start(e, chainFallbackAfterSec=0)
    assert _tick(e, book)["ladder"]["item"] == STICKS
    e.lens.stop()
    s = _tick(e, book)  # HP 300 is above the sticks floor 230: nothing
    assert s["underChain"] is True and s["decisions"] == [] and reader.reads == 1
    reader.script[KAMI] = (HARVESTING, 229, 330, NODE)
    s = _tick(e, book)
    assert s["decisions"] == ["feed"] and s["chainHp"] == {str(KAMI): 229}
    p = _decisions(e, book, "feed")[-1]["parameters"]
    assert p["item"] == STICKS and p["underChain"] is True and p["hpProjected"] == 229 and p["inventoryBalance"] is None
    assert e.keeper.signer.recorded[-1].params["itemIndex"] == STICKS


# -------------------------------------------- 13. thirty-six books on one node


def test_thirty_six_ladder_books_on_one_node_read_one_census_and_share_the_preview_cap(make_keeper):
    e = _env(make_keeper, cap=40)
    kamis = list(range(1001, 1037))
    foreign = [(3000 + i, 7000 + i, (200, 240)) for i in range(12)]
    _world(e, ours={k: (330, 330) for k in kamis}, foreign=foreign)
    for a, _acct, _hp in foreign:
        e.lens.synth_thresholds[(a, kamis[0])] = 10
    books = []
    for k in kamis:
        r = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": _ladder_config(kamiIndex=k)})
        assert r["ok"], r
        books.append(e.keeper.store.get(r["data"]["book"]["id"]))
    assert len(e.keeper._auto_books()) == 36
    e.clock["now"] += 30
    e.lens.requests.clear()
    assert len(e.keeper.tick_due(now=e.clock["now"])) == 36
    reqs = _requests(e)
    census = [r for r in reqs if r.startswith(f"node {NODE} --with-vitals")]
    previews = [r for r in reqs if r.startswith(f"node {NODE} ") and not r.startswith(f"node {NODE} --")]
    assert len(census) == 1 and len(previews) == 10  # one census; the cap is per pass and node, not per book
    assert len([r for r in reqs if r.startswith("kami ")]) == 36 and len([r for r in reqs if r.startswith("inventory ")]) == 1
    for b in books:
        # every attacker holds its own owner's only kami, so the two still
        # unpreviewed serve 0 and bind nothing: the bound is the dip cap
        lad = _last_tick(e, b)["ladder"]
        assert (lad["item"], lad["heal"], lad["floor"], lad["need"], lad["reason"]) == (STICKS, 100, 230, 198, "largest rung the bound allows")
    e.clock["now"] += 30
    e.lens.requests.clear()
    e.keeper.tick_due(now=e.clock["now"])
    assert len([r for r in _requests(e) if r.startswith(f"node {NODE} ") and not r.startswith(f"node {NODE} --")]) == 2
    assert _last_tick(e, books[0])["previewsPending"] == 0


# ------------------------------------------------------- 14. the kill scan


def test_a_kill_on_the_node_is_scanned_for_a_ladder_book_and_the_rung_drops(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain=chain)
    _world(e, ours={KAMI: (330, 330), 5946: (30, 200)}, foreign=[(ATTACKER, OWNER, (200, 240))])
    e.lens.synth_thresholds[(ATTACKER, KAMI)] = 220
    e.keeper.owners.set_last_scanned(chain.head - 1)
    book = _start(e)
    e.clock["now"] += 30
    e.keeper.tick_due(now=e.clock["now"])
    s = _last_tick(e, book)
    # 220 + 2 % x 330 = 226.6 -> 227: the sticks floor 230 still clears it
    assert s["ladder"]["item"] == STICKS and s["ladder"]["boundBy"]["ownerClass"] == "threshold-no-kill" and s["killScan"]["kills"] == 0
    chain.head += 1
    tx = "0x" + "ab" * 32
    chain.logs = [liquidate_log(chain.head, tx, kami_entity_id(5946))]
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(5946), kami_entity_id(ATTACKER))
    e.clock["now"] += 30
    e.keeper.tick_due(now=e.clock["now"])
    kills = _rows(e, book, "kill")
    assert len(kills) == 1 and kills[0]["kill"]["attackerAccount"] == OWNER and kills[0]["kill"]["ownerClassAfter"] == "killed-here-48h"
    s = _last_tick(e, book)
    # the margin is now safetyMarginPct: 220 + 16.5 -> 237, and sticks is out
    assert s["ladder"]["need"] == 237 and s["ladder"]["item"] == HONEYDEW and s["ladder"]["boundBy"]["ownerClass"] == "killed-here-48h"


# ------------------------------------------------------------- 15. the scrub


def test_every_new_surface_is_clean(make_keeper):
    e = _env(make_keeper)
    _with_attacker(e, threshold=310, hp=(300, 330), killed_here=True)
    book = _start(e)
    _tick(e, book)  # the failsafe stop, the kill-fed classes, the ladder note
    _world(e, ours={KAMI: (229, 330)}, foreign=[(ATTACKER, OWNER, (200, 240))])
    e.lens.synth_thresholds[(ATTACKER, KAMI)] = 10
    _tick(e, book)
    e.lens.mode, e.lens.lag = "stale", 500
    _tick(e, book)
    e.lens.mode = "live"
    frames = [e.keeper.handle("describe", {"type": "harvestAndFeed"}), e.keeper.handle("status"), e.keeper.handle("books", {"account": "lab"}), e.keeper.handle("logs"), e.keeper.handle("ledger", {"book": book.id})]
    frames.append(e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": _ladder_config(kamiIndex=1002, feedBelowHp=10)}))
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    ticks = [r["summary"] for r in _rows(e, book, "tick") if "ladder" in r["summary"]]
    # `dipFrom` in the note: where the dip cap came from
    assert ticks and all(set(t["ladder"]) == {"item", "heal", "floor", "need", "boundBy", "reason", "dipFrom"} for t in ticks)


# ------------------------------------------ the failsafe stop and the restart gate


def test_h1_an_empty_inventory_stops_the_kami_only_at_the_point_it_would_have_fed(make_keeper):
    """One empty inventory read must not take a kami off the node at full
    HP: the failsafe stop waits for the feed point (the smallest rung's
    floor here, 330 - 25 = 305), as a static book stops only once it wants
    a feed."""
    e = _env(make_keeper)
    _world(e, ours={KAMI: (320, 330)}, balances={i: 0 for i in LADDER})
    book = _start(e)
    s = _tick(e, book)
    assert s["decisions"] == [] and s["ladder"]["item"] is None and s["ladder"]["reason"] == "no rung in inventory"
    assert e.keeper.signer.recorded == []
    _world(e, ours={KAMI: (305, 330)}, balances={i: 0 for i in LADDER})
    assert _tick(e, book)["decisions"] == ["failsafe-stop"]


def test_h2_a_kami_the_failsafe_stopped_does_not_restart_while_the_bound_is_above_every_rung(make_keeper):
    """Without the restart gate the kami is at full HP a tick after the
    stop, starts, is stopped again, and flaps for the whole window."""
    e = _env(make_keeper)
    _with_attacker(e, threshold=310, killed_here=True)
    book = _start(e)
    assert _tick(e, book)["decisions"] == ["failsafe-stop"]
    # the stop landed: the kami rests at full HP, the attacker is still there
    e.lens.set_kami(KAMI, state=RESTING, node=None, hp=(330, 330), account=ACCT)
    e.lens.nodes[NODE] = _node_doc([_harvest(ATTACKER, OWNER, (200, 240))])
    n = len(e.keeper.signer.recorded)
    for _ in range(3):
        s = _tick(e, book)
        assert s["decisions"] == [] and s["ladder"]["startHeld"] == "bound above every rung"
    assert len(e.keeper.signer.recorded) == n
    # the attacker leaves and is not remembered past the window: the start goes
    e.lens.nodes[NODE] = _node_doc([])
    e.keeper.owners.kills.clear()
    e.keeper.owners.killed_accounts.clear()
    e.keeper.owners.hostile_kamis.clear()
    e.keeper.previews.entries(NODE, ACCT).clear()
    s = _tick(e, book)
    assert s["decisions"] == ["start"] and s["ladder"]["startHeld"] is None


def test_h2_an_empty_inventory_holds_the_restart_too(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={}, balances={i: 0 for i in LADDER})
    e.lens.set_kami(KAMI, state=RESTING, node=None, hp=(330, 330), account=ACCT)
    book = _start(e)
    s = _tick(e, book)
    assert s["decisions"] == [] and s["ladder"]["startHeld"] == "no rung in inventory"
