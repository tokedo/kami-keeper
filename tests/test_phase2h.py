"""The hostility policy:

  1. forgiveness — an owner whose LAST kill of ours is 47 h old binds, 49 h
     old does not, and a new kill re-binds for a fresh window;
  2. transfer — a kami that killed ours under owner A and now sits under a
     clean owner B is killed-here (`hostileBy: kami`); owner A arriving with
     a NEW kami is killed-here (`hostileBy: owner`); a listed owner holding
     such a kami is listed;
  3. remembered — an absent kami that killed stays remembered for the
     window under a clean owner (the cache rule and through the book);
  4. migration — an older `owners.json` with 44 ancient `killedAccounts`
     loads, binds nobody after the window, prunes to empty; a NEWER
     document is refused and kept aside;
  5. seed — a battles death older than the window plus one day seeds
     nothing; one inside it seeds the owner's LAST block and the killer's
     kami row;
  6. the ledger's per-kami table (every attributed kill, the two reads, the
     prune) and the rendered window of the book.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import Env
from keeper.kills import BLOCKS_PER_HOUR
from keeper.owner_class import OwnerClass, classify, hostile_by, is_observation_name, render, worst
from keeper.owners import OWNERS_SCHEMA_VERSION, OwnerLedger
from keeper.previews import PreviewCache, PreviewEntry
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.auto import Auto
from test_phase2a import BOOK_43, CAROL, FRIENDS, NODE, T, _add_occupant, _apply_plans_to_world, _decisions, _last_tick, _preview_threshold, _rows, _seed, _set_party, _settle_previews, _start, _tick_all, carol  # noqa: F401 — `carol` is the fixture
from test_phase2b import BATTLES, _battles_for, cp  # noqa: F401 — `cp` is the fixture

H = BLOCKS_PER_HOUR
OTHER_NODE = 62


def _kill(block: int, tx: str, owner: int | None, kami: int | None, victim_account: int = CAROL, node: int = NODE, victim: int = 5946) -> dict[str, Any]:
    return {"block": block, "txHash": tx, "logIndex": 0, "node": node, "victimKami": victim, "victimAccount": victim_account, "attackerKami": kami, "attackerAccount": owner, "attribution": "occupant" if owner is not None else "selector not a liquidate"}


def _bounds(e: Env, book) -> tuple[Auto, Any, dict[int, dict[str, Any]], dict[int, OwnerClass], list[dict[str, Any]]]:
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    views = {k: v for k, v in obs.kamis.items() if not v.missing}
    lim, classes, attackers, _s = strat.bounds(obs, views)
    return strat, obs, lim, classes, attackers


# ------------------------------------------------------------ the definition


def test_classify_is_per_attacker_owner_or_kami_and_the_closed_set_holds():
    assert classify(listed=True, max_threshold=999, owner_hostile_in_window=True, kami_hostile_in_window=True) is OwnerClass.FRIEND  # a listed owner whatever kami it holds
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=True) is OwnerClass.HOSTILE
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=True, kami_hostile_in_window=False) is OwnerClass.HOSTILE
    assert classify(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=False) is OwnerClass.HARMLESS
    assert classify(listed=False, max_threshold=7, owner_hostile_in_window=False, kami_hostile_in_window=False) is OwnerClass.ARMED_PASSIVE
    assert [hostile_by(owner_hostile_in_window=o, kami_hostile_in_window=k) for o, k in ((True, True), (True, False), (False, True), (False, False))] == ["both", "owner", "kami", None]
    assert worst([OwnerClass.HARMLESS, OwnerClass.HOSTILE, OwnerClass.ARMED_PASSIVE]) is OwnerClass.HOSTILE and worst([OwnerClass.HARMLESS]) is OwnerClass.HARMLESS
    # P3a: the name renders with the BOOK's window and stays in the closed set
    assert render(OwnerClass.HOSTILE, 48) == "killed-here-48h" and is_observation_name(render(OwnerClass.HOSTILE, 72))


# ------------------------------------------------------------ 1. forgiveness


def test_forgiveness_in_the_ledger_the_window_runs_from_the_last_kill():
    led = OwnerLedger("/nonexistent")
    led.record_kill(_kill(100_000, "0x01", 2481, 601, node=OTHER_NODE))
    assert led.killed_accounts == {2481: {CAROL: 100_000}}
    assert led.killed_ours_in_window(2481, CAROL, 48, 100_000 + 47 * H) is True
    assert led.killed_ours_in_window(2481, CAROL, 48, 100_000 + 49 * H) is False  # forgiven: no permanent class
    assert led.killed_ours_in_window(2481, CAROL, 48, None) is True  # no head: every recorded row counts
    assert led.killed_ours_in_window(2481, 2307, 48, 100_000 + H) is False and led.killed_ours_in_window(392, CAROL, 48, 100_000 + H) is False
    # a new kill re-binds for a FRESH window, measured from the last kill
    again = 100_000 + 49 * H
    led.record_kill(_kill(again, "0x02", 2481, 601, node=OTHER_NODE))
    assert led.killed_accounts == {2481: {CAROL: again}}
    assert led.killed_ours_in_window(2481, CAROL, 48, again + 47 * H) is True and led.killed_ours_in_window(2481, CAROL, 48, again + 49 * H) is False
    # an EARLIER row arriving late (a seed, a rescan) never lowers the last block
    led.record_kill(_kill(90_000, "0x03", 2481, 601, node=OTHER_NODE))
    assert led.killed_accounts == {2481: {CAROL: again}}
    # the owner read of the one window: killed here OR killed ours anywhere
    assert led.hostile_owners(NODE, 48, again + H) == set() and led.hostile_owners(NODE, 48, again + H, CAROL) == {2481}
    assert led.hostile_owners(NODE, 48, again + 49 * H, CAROL) == set()


def test_forgiveness_through_the_book_47h_binds_49h_does_not_and_a_new_kill_rebinds(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False)  # passiveOwnersBind false: an attacker with a positive threshold and no kill is ignored
    strat, obs, lim, _c, attackers = _bounds(e, book)
    head = obs.basis.lensBlock
    assert strat.window_hours == 48 and lim[68017]["bound"] == 0  # the schema default; nobody killed: only the floor
    # one owner killed OURS on another node 47 h ago; another did 49 h ago — with kamis that are not on this node
    e.keeper.owners.record_kill(_kill(head - 47 * H, "0x47", 806, 5555, node=OTHER_NODE))
    e.keeper.owners.record_kill(_kill(head - 49 * H, "0x49", 997, 6666, node=OTHER_NODE))
    strat, obs, lim, classes, attackers = _bounds(e, book)
    by_owner = {a["owner"]: (a["class"].value, a["hostileBy"]) for a in attackers}
    assert by_owner == {806: ("hostile", "owner"), 997: ("armed-passive", None), 2481: ("harmless", None)}
    assert lim[68017]["bound"] == 20 and lim[68017]["boundBy"] == {"attackerKami": 360, "ownerClass": "killed-here-48h", "threshold": 6, "present": True, "hostileBy": "owner"}
    assert strat.render_owner_class(806, head) == "killed-here-48h" and strat.render_owner_class(997, head) == "threshold-no-kill"
    # two hours on, the first owner's kill is 49 h old too: forgiven, only the floor binds again
    e.lens.advance(2 * H)
    strat, obs, lim, _c, attackers = _bounds(e, book)
    assert obs.basis.lensBlock == head + 2 * H
    assert {a["owner"]: a["class"].value for a in attackers} == {806: "armed-passive", 997: "armed-passive", 2481: "harmless"} and lim[68017]["bound"] == 0 and lim[68017]["boundBy"] is None
    # a new kill by the second owner re-binds it for a fresh window
    e.keeper.owners.record_kill(_kill(obs.basis.lensBlock - 10, "0x4a", 997, 6666, node=OTHER_NODE))
    strat, obs, lim, _c, attackers = _bounds(e, book)
    assert {a["owner"]: a["class"].value for a in attackers}[997] == "hostile" and lim[68017]["boundBy"]["attackerKami"] == 448 and lim[68017]["boundBy"]["hostileBy"] == "owner"


# ------------------------------------------------------------ 2. transfer


def test_transfer_a_kami_that_killed_is_bound_under_a_clean_owner_and_the_old_owner_with_a_new_kami_too(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False)
    strat, obs, lim, _c, _a = _bounds(e, book)
    head = obs.basis.lensBlock
    # the attacker killed OURS on another node under owner A an hour ago; it now sits on this node under owner B (clean)
    e.keeper.owners.record_kill(_kill(head - H, "0xaa", 4444, 360, node=OTHER_NODE))
    assert e.keeper.owners.hostile_kamis[360] == {"lastBlock": head - H, "node": OTHER_NODE, "victimAccount": CAROL, "ownerAtKill": 4444, "nodes": {OTHER_NODE: head - H}, "victimAccounts": {CAROL: head - H}, "lastTs": None, "nodesTs": {}, "victimAccountsTs": {}}  # an UNDATED row (no blockTs) carries the ts keys empty
    strat, obs, lim, classes, attackers = _bounds(e, book)
    row = {a["kami"]: a for a in attackers}
    assert classes[360] is OwnerClass.HOSTILE and row[360]["hostileBy"] == "kami" and row[360]["owner"] == 806
    assert classes[448] is OwnerClass.ARMED_PASSIVE and row[448]["hostileBy"] is None
    others_of_913 = [a for a in attackers if a["owner"] == 806 and a["kami"] != 360]
    assert others_of_913 and all(a["class"] is OwnerClass.ARMED_PASSIVE for a in others_of_913)  # the OWNER is clean: only the kami is remembered
    assert lim[68017]["bound"] == 20 and lim[68017]["boundBy"] == {"attackerKami": 360, "ownerClass": "killed-here-48h", "threshold": 6, "present": True, "hostileBy": "kami"}
    assert strat.render_owner_class(806, head) == "threshold-no-kill"  # the owner-level name is the owner's own
    # owner A arrives with a NEW kami: killed-here by its owner
    _add_occupant(e.lens, 7777, 4444, hp=(250, 300), level=40)
    _preview_threshold(e.lens, 7777, 68017, 30)
    _tick_all(e)
    _apply_plans_to_world(e)
    strat, obs, lim, classes, attackers = _bounds(e, book)
    row = {a["kami"]: a for a in attackers}
    assert classes[7777] is OwnerClass.HOSTILE and row[7777]["hostileBy"] == "owner"
    assert lim[68017]["boundBy"]["attackerKami"] == 7777 and lim[68017]["boundBy"]["hostileBy"] == "owner" and lim[68017]["bound"] == 30 + -(-5 * 270 // 100)
    # the tick row: owners still counted by OWNER (owner B under the worst of its kamis), plus the kami count
    _tick_all(e)
    s = _last_tick(e, book)
    # the book runs passiveOwnersBind false, so `start` fills previewPolicy killed-here and the summary carries the `unpreviewed` count (0: every owner here was read while the three default books warmed the node's cache)
    assert s["owners"] == {"listed": 0, "no-threshold": 1, "threshold-no-kill": 1, "killed-here": 2, "standing": 0, "unpreviewed": 0} and s["hostileKamis"] == 1
    assert s["boundKamis"] == 2 and sum(s["boundKamisBy"].values()) == 2 and set(s["boundKamisBy"]) <= {"killed-here-48h/owner", "killed-here-48h/kami"}
    # the new kami killed nothing itself; when BOTH hold it says so
    e.keeper.owners.record_kill(_kill(obs.basis.lensBlock - 5, "0xab", 4444, 7777, victim_account=2481))
    _s, _o, _l, _c, attackers = _bounds(e, book)
    assert {a["kami"]: a for a in attackers}[7777]["hostileBy"] == "both"
    # every string of the new surfaces stays inside the scrub boundary
    assert forbidden_in(collect_strings([r for r in _rows(e, book) if r["kind"] in ("tick", "decision")])) == []


def test_a_listed_owner_holding_a_kami_that_killed_is_listed(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False, friends=list(FRIENDS) + [806])
    strat, obs, _l, _c, _a = _bounds(e, book)
    e.keeper.owners.record_kill(_kill(obs.basis.lensBlock - H, "0xaa", 4444, 360, node=OTHER_NODE))
    strat, obs, lim, classes, attackers = _bounds(e, book)
    assert e.keeper.owners.kami_hostile_in_window(360, NODE, CAROL, 48, obs.basis.lensBlock) is True
    assert strat._class_of(806, attackers, obs.basis.lensBlock, 360) == (OwnerClass.FRIEND, None)
    assert 360 not in classes and all(a["owner"] != 806 for a in attackers)  # a listed owner's kamis are never attackers
    assert lim[68017]["boundBy"] is None or lim[68017]["boundBy"]["attackerKami"] != 360
    assert strat.render_owner_class(806, obs.basis.lensBlock) == "listed"


# ------------------------------------------------------------ 3. remembered


def test_an_absent_kami_that_killed_is_remembered_under_a_clean_owner(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    ents = cache.entries(79, CAROL)
    ents[360] = PreviewEntry(79, 360, 0.0, 1, 38, 806, {263: {"threshold": 100, "level": 38}}, 0, None)
    ents[448] = PreviewEntry(79, 448, 0.0, 1, 37, 997, {263: {"threshold": 100, "level": 37}}, 0, None)
    window = 48 * 3600.0
    out = cache.reconcile_presence(79, CAROL, {}, set(), 10.0, window, {360})  # no hostile OWNER at all: the kami itself
    assert out == {"remembered": [360], "dropped": [448], "expired": []} and ents[360].remembered and ents[360].absent_since == 10.0
    assert cache.reconcile_presence(79, CAROL, {}, set(), 10.0 + window - 1, window, {360})["expired"] == []
    assert cache.reconcile_presence(79, CAROL, {}, set(), 10.0 + window, window, {360})["expired"] == [360] and 360 not in ents
    # the call shape without a kami set is unchanged
    ents[360] = PreviewEntry(79, 360, 0.0, 1, 38, 806, {263: {"threshold": 100, "level": 38}}, 0, None)
    assert cache.reconcile_presence(79, CAROL, {}, set(), 20.0, window) == {"remembered": [], "dropped": [360], "expired": []}


def test_an_absent_kami_that_killed_is_remembered_through_the_book_and_binds_like_a_present_one(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False)
    _s, obs, _l, _c, _a = _bounds(e, book)
    e.keeper.owners.record_kill(_kill(obs.basis.lensBlock - H, "0xaa", 4444, 360, node=OTHER_NODE))  # under owner A, elsewhere; owner B is clean
    _tick_all(e)
    # the attacker leaves the node: remembered for the window although its owner never killed
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 360]
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["remembered"] == [360] and s["hostileKamis"] == 0  # `hostileKamis` counts the PRESENT ones
    assert _last_tick(e, e.books[0])["previewsRemembered"] == [360]
    strat, obs, lim, classes, attackers = _bounds(e, book)
    assert classes[360] is OwnerClass.HOSTILE and lim[68017]["boundBy"] == {"attackerKami": 360, "ownerClass": "killed-here-48h", "threshold": 6, "present": False, "hostileBy": "kami"}
    # a clean kami of the same owner that leaves is dropped, not remembered
    gone = next(a["kami"] for a in attackers if a["owner"] == 806 and a["kami"] != 360)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != gone]
    _tick_all(e)
    assert _last_tick(e, book)["remembered"] == [360] and gone not in e.keeper.previews.entries(NODE, CAROL)
    # past the window it expires
    e.clock["now"] += 48 * 3600.0
    _tick_all(e)
    assert _last_tick(e, book)["remembered"] == [] and 360 not in e.keeper.previews.entries(NODE, CAROL)


# ------------------------------------------------------------ 4. migration


def _old_owners_doc(head: int, owners: list[int]) -> dict[str, Any]:
    """`owners.json` as an older keeper wrote it: the package stamp 0.1.0,
    FIRST-kill blocks (and the seed's block 0 for an undated death), no
    `hostileKamis`."""
    killed = {str(o): {str(CAROL): (0 if i % 11 == 0 else head - (30 + i) * 24 * H)} for i, o in enumerate(owners)}
    return {"schemaVersion": "0.1.0", "savedAt": "2026-09-18T00:00:00.000Z", "owners": {"lastScannedBlock": head - 10, "kills": {}, "casts": {}, "unattributed": {}, "killedAccounts": killed, "seeded": {str(CAROL): {"kamis": [263, 508], "at": "2026-09-15T00:00:00.000Z", "block": head - 5000}}}}


def test_migration_an_old_owners_file_with_44_ancient_killed_accounts_loads_binds_nobody_and_prunes_to_empty(carol):
    e = carol
    _settle_previews(e)
    book = _start(e, [66655, 68017], passiveOwnersBind=False)
    _s, obs, _l, _c, _a = _bounds(e, book)
    head = obs.basis.lensBlock
    owners = [806, 997, 2481] + list(range(5000, 5041))
    assert len(owners) == 44
    (e.data_dir / "owners.json").write_text(json.dumps(_old_owners_doc(head, owners)))
    led = e.keeper.owners
    restore = led.load()
    assert restore == "restored (migrated from schemaVersion 0.1.0)" and led.loaded_version == "0.1.0"
    assert len(led.killed_accounts) == 44 and led.hostile_kamis == {} and led.seeded_kamis(CAROL) == {263, 508} and led.last_scanned_block == head - 10
    # the old blocks are kept as they are (first-kill blocks: at or below the truth) — and every one is outside the window
    assert led.killed_accounts[997][CAROL] == head - 31 * 24 * H and led.killed_accounts[806][CAROL] == 0
    assert not any(led.killed_ours_in_window(o, CAROL, 48, head) for o in owners) and led.hostile_owners(NODE, 48, head, CAROL) == set()
    strat, obs, lim, classes, attackers = _bounds(e, book)
    assert {a["owner"]: a["class"].value for a in attackers} == {806: "armed-passive", 997: "armed-passive", 2481: "harmless"}
    assert all(v["bound"] == 0 and v["boundBy"] is None for v in lim.values())  # binds nobody: the rests move to the floor
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["boundKamis"] == 0 and s["owners"]["killed-here"] == 0 and s["hostileKamis"] == 0 and s["boundKamisBy"] == {}
    # the load alone rewrites the file at this version; the prune (window + one day) empties the table
    assert led.save() is True and json.loads((e.data_dir / "owners.json").read_text())["schemaVersion"] == OWNERS_SCHEMA_VERSION
    assert led.prune(48 + 24, head) == 44 and led.killed_accounts == {}
    assert led.save() is True
    again = OwnerLedger(e.data_dir)
    assert again.load() == "restored" and again.killed_accounts == {} and again.seeded_kamis(CAROL) == {263, 508}
    # a new row after the migration raises an old block
    doc = _old_owners_doc(head, [806])
    doc["owners"]["killedAccounts"]["806"][str(CAROL)] = head - 60 * H
    (e.data_dir / "owners.json").write_text(json.dumps(doc))
    led2 = OwnerLedger(e.data_dir)
    led2.load()
    assert led2.killed_ours_in_window(806, CAROL, 48, head) is False
    led2.record_kill(_kill(head - 5, "0xbb", 806, 360))
    assert led2.killed_accounts == {806: {CAROL: head - 5}} and led2.killed_ours_in_window(806, CAROL, 48, head) is True


def test_a_newer_owners_document_is_refused_and_kept_aside(tmp_path):
    doc = _old_owners_doc(1_000_000, [806])
    doc["schemaVersion"] = "0.3.0"
    (tmp_path / "owners.json").write_text(json.dumps(doc))
    led = OwnerLedger(tmp_path)
    assert led.load() == f"refused: schemaVersion 0.3.0 is newer than {OWNERS_SCHEMA_VERSION}"
    assert led.killed_accounts == {} and led.last_scanned_block is None and led.save() is False  # nothing read, nothing written over it
    led.record_kill(_kill(1_000_100, "0x01", 2481, 601))
    assert led.save() is True
    assert json.loads((tmp_path / "owners.json.v0.3.0.refused").read_text())["schemaVersion"] == "0.3.0"
    assert json.loads((tmp_path / "owners.json").read_text())["schemaVersion"] == OWNERS_SCHEMA_VERSION
    # a file with no version at all is an old file
    (tmp_path / "owners.json").write_text(json.dumps({"owners": {"killedAccounts": {"5": {"7": 9}}}}))
    old = OwnerLedger(tmp_path)
    assert old.load().startswith("restored (migrated") and old.killed_accounts == {5: {7: 9}}


# ------------------------------------------------------------ 5. the battles seed


def test_seed_a_death_older_than_the_window_plus_one_day_seeds_nothing(cp):
    e = cp
    head = e.lens.block
    deaths = [r for r in BATTLES["kills"] if r["isDeath"]]
    keep = (48 + 24) * H
    e.lens.battles[263] = _battles_for(263, [dict(deaths[0], blockNumber=head - keep - 1), dict(deaths[1], blockNumber=1_000)])
    book = _start(e, [263])
    started = [r for r in _rows(e, book, "lifecycle") if r["event"] == "started"][0]
    assert started["seedFromBattles"] == {"status": "seeded", "kamisRead": 1, "deaths": 2, "ownersAdded": 0, "pending": 0, "deathsOutsideWindow": 2}
    o = e.keeper.owners
    assert o.killed_accounts == {} and o.hostile_kamis == {} and o.seeded_kamis(CAROL) == {263}  # read once, nothing re-created
    assert e.keeper.strategies[book.id].render_owner_class(2481, head) != "killed-here-48h"
    # a death INSIDE it seeds the owner's LAST block and the killer's kami row; an undated row seeds nothing
    undated = {k: v for k, v in deaths[1].items() if k not in ("blockNumber", "timestamp")}
    # a seeded row is judged by its own `timestamp` — the fixture's is years before the synthetic clock T, so the binding row is re-dated inside the window
    e.lens.battles[508] = _battles_for(508, [dict(deaths[0], blockNumber=head - keep + 10), dict(deaths[0], blockNumber=head - 100, timestamp=T - 3600), undated])
    book2 = _start(e, [508])
    s2 = [r for r in _rows(e, book2, "lifecycle") if r["event"] == "started"][0]["seedFromBattles"]
    assert s2 == {"status": "seeded", "kamisRead": 1, "deaths": 3, "ownersAdded": 1, "pending": 0, "deathsUndated": 1}
    assert o.killed_accounts == {2481: {CAROL: head - 100}}
    killer, room = deaths[0]["killer"]["index"], deaths[0]["roomIndex"]
    assert o.hostile_kamis[killer]["lastBlock"] == head - 100 and o.hostile_kamis[killer]["ownerAtKill"] == 2481 and o.hostile_kamis[killer]["victimAccounts"] == {CAROL: head - 100} and o.hostile_kamis[killer]["nodes"] == {room: head - 100}
    assert e.keeper.strategies[book2.id].render_owner_class(2481, head) == "killed-here-48h"
    # the book's own window is what the seed keeps: a 24-h book on a keeper with no larger one takes 24 + 24 h
    e.keeper.handle("stop", {"account": "lab", "book": book.id})
    e.keeper.handle("stop", {"account": "lab", "book": book2.id})
    e.lens.battles[699] = _battles_for(699, [dict(deaths[1], blockNumber=head - 49 * H)])
    book3 = _start(e, [699], hostileWindowHours=24)
    s3 = [r for r in _rows(e, book3, "lifecycle") if r["event"] == "started"][0]["seedFromBattles"]
    assert s3["ownersAdded"] == 0 and s3["deathsOutsideWindow"] == 1 and 392 not in o.killed_accounts
    assert e.keeper.strategies[book3.id].render_owner_class(2481, head) == "killed-here-24h"  # the name carries the BOOK's window


# ------------------------------------------------------------ 6. the per-kami table


def test_hostile_kamis_every_attributed_kill_the_two_reads_and_the_prune(tmp_path):
    led = OwnerLedger(tmp_path)
    base = 1_000_000
    # a kill of a STRANGER's kami on this node (mirrors killed_here_in_window: any victim on the node)
    assert led.record_kill(_kill(base, "0x01", 2481, 601, victim_account=806)) is True
    assert led.kami_hostile_in_window(601, NODE, CAROL, 48, base + H) is True and led.kami_hostile_in_window(601, OTHER_NODE, CAROL, 48, base + H) is False
    # a kill of OURS on another node counts on every node; a stranger's kami killed elsewhere does not
    led.record_kill(_kill(base + 10, "0x02", 2481, 800, node=OTHER_NODE))
    led.record_kill(_kill(base + 10, "0x03", 2481, 801, node=OTHER_NODE, victim_account=806))
    assert led.kami_hostile_in_window(800, NODE, CAROL, 48, base + H) is True and led.kami_hostile_in_window(801, NODE, CAROL, 48, base + H) is False
    assert led.hostile_kamis_for(NODE, CAROL, 48, base + H) == {601, 800} and led.hostile_kamis_for(NODE, CAROL, 48, None) == {601, 800}
    # the last row describes the LAST kill; the per-node / per-account blocks answer the read once a kami kills twice
    led.record_kill(_kill(base + 20 * H, "0x04", 4444, 800, node=50, victim_account=806))  # the second attacker changed hands and killed a stranger elsewhere
    row = led.hostile_kamis[800]
    assert (row["lastBlock"], row["node"], row["victimAccount"], row["ownerAtKill"]) == (base + 20 * H, 50, 806, 4444)
    assert row["victimAccounts"] == {CAROL: base + 10, 806: base + 20 * H} and led.kami_hostile_in_window(800, NODE, CAROL, 48, base + 21 * H) is True
    assert led.kami_hostile_in_window(800, NODE, CAROL, 48, base + 10 + 49 * H) is False  # its kill of OURS is past the window, whatever it did to others since
    # unattributed kills and casts (a KAMI_CAST names an account, never a kami) write no kami row
    led.record_kill(_kill(base + 30, "0x05", None, None))
    led.record_kill(dict(_kill(base + 31, "0x06", None, 999), attribution="killer resolved by index; account unresolved"))
    led.record_cast({"block": base + 32, "txHash": "0xc", "logIndex": 0, "node": NODE, "targetKami": 263, "targetAccount": CAROL, "casterAccount": 555, "item": 21202})
    assert set(led.hostile_kamis) == {601, 800, 801} and led.killed_accounts[555] == {CAROL: base + 32}
    # persistence round-trips the table; the prune drops it like any row (nothing is kept for ever)
    led.save()
    again = OwnerLedger(tmp_path)
    assert again.load() == "restored" and again.hostile_kamis == led.hostile_kamis and again.killed_accounts == led.killed_accounts
    dropped = again.prune(48 + 24, base + 20 * H + 73 * H)
    assert dropped > 0 and again.hostile_kamis == {} and again.killed_accounts == {} and again.kills == {} and again.casts == {}
    half = OwnerLedger(tmp_path)
    half.load()
    half.prune(48 + 24, base + 10 + 80 * H)  # the second attacker's kill of ours is old, its later kill is not: the row stays, the old part goes
    assert set(half.hostile_kamis) == {800} and half.hostile_kamis[800]["victimAccounts"] == {806: base + 20 * H} and half.killed_accounts == {4444: {806: base + 20 * H}}


def test_the_kill_scan_writes_the_kami_row_and_the_core_prunes_it(make_keeper, tmp_path):
    from fakechain import FakeChain
    from test_phase2a import RECEIPT, _chain_env, _fixture_logs

    chain = FakeChain(head=12143991)
    e = _chain_env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    _add_occupant(e.lens, 42823, 3878, hp=(250, 300), level=45)
    friends = [f for f in FRIENDS if f != 3878]
    for b in e.books:
        b.config["friends"] = friends
    e.keeper.owners.set_last_scanned(12143971)
    # an older-format permanent owner sits in the ledger: the scan pass prunes it
    e.keeper.owners.killed_accounts = {5000: {CAROL: 1_000}}
    for _ in range(5):
        _tick_all(e)
        _apply_plans_to_world(e)
    assert e.keeper.owners.killed_accounts == {}
    chain.head = 12143992
    chain.logs = [dict(l, blockNumber=12143992) for l in _fixture_logs()]
    chain.txs["0x" + RECEIPT["tx_hash"]] = {"hash": "0x" + RECEIPT["tx_hash"], "input": "0x" + RECEIPT["input"], "from": RECEIPT["from"], "to": RECEIPT["to"], "blockNumber": 12143992}
    _tick_all(e)
    assert e.keeper.owners.hostile_kamis == {42823: {"lastBlock": 12143992, "node": NODE, "victimAccount": CAROL, "ownerAtKill": 3878, "nodes": {NODE: 12143992}, "victimAccounts": {CAROL: 12143992}, "lastTs": None, "nodesTs": {}, "victimAccountsTs": {}}}  # the fake node serves no header, so the row is undated
    doc = json.loads((e.data_dir / "owners.json").read_text())
    assert doc["schemaVersion"] == OWNERS_SCHEMA_VERSION and doc["owners"]["hostileKamis"]["42823"]["ownerAtKill"] == 3878
    s = _last_tick(e, e.books[2])
    assert s["hostileKamis"] == 1 and s["owners"]["killed-here"] == 1


def test_boot_states_the_migration_and_the_refusal_in_the_log(cp):
    e = cp
    from keeper.config import load as load_config
    from keeper.core import Keeper
    from keeper.lens import LensClient

    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6, "requestTimeoutSec": 5}, env={})
    (e.data_dir / "owners.json").write_text(json.dumps(_old_owners_doc(e.lens.block, [806])))
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    assert any(l["line"] == f"owners.json restored (migrated from schemaVersion 0.1.0); kill scan resumes after block {e.lens.block - 10}" for l in k2.logs)
    doc = _old_owners_doc(e.lens.block, [806])
    doc["schemaVersion"] = "9.0.0"
    (e.data_dir / "owners.json").write_text(json.dumps(doc))
    k3 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k3.boot()
    assert any(l["line"].startswith("owners.json refused: schemaVersion 9.0.0 is newer than") and "the ledger starts empty" in l["line"] for l in k3.logs) and k3.owners.killed_accounts == {}
