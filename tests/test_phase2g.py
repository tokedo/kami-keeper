"""The node-read and preview budget of `auto`, on the recorded node
fixture (107 own harvests, 45 foreign over three owners: 22, 20 and 3
kamis):

  1. the keys — conditional defaults at `start` (passiveOwnersBind false ->
     previewPolicy killed-here -> nodeCensus plain; nodeCensusEverySec =
     tickSeconds), the refusals, the lock, `describe`, the example book of
     tests/fixtures/deploy/example-auto.yaml (synthetic), and a STORED
     config without the keys keeping its reads byte for byte;
  2. lazy previews — a cold node with no kill: no preview, nothing pending,
     nothing `unknown`, nothing held, nobody bound, owners `unpreviewed`;
     the flip end to end through the chain scan (kill row -> arrivals ->
     fetched under the per-pass cap -> bound); an attacker whose window
     expired is not refreshed, kept until it leaves, then dropped;
  3. the plain census — `node <i> --full`, a row without vitals is a plain
     occupant; the cooldown of a killed-here attacker from the preview of
     the pass or one `kami` read (the horizon skip, the cap, a refused
     read: no cooldown = the attacker binds); a level that moved refreshes;
  4. the census interval — reuse with `censusAgeSec`, own kamis every pass,
     a kill row forces a read, a preview the lens refuses against a reused
     census holds (never a refused tick) and forces a read;
  5. the measured lines — `nodeReadBytes` / `attackerVitalsCalls` on the
     tick, `lens.passCalls` / `passBytes` on the status;
  6. scale — 1,750 foreign occupants, 50 owned kamis, 2 killed-here
     attackers: one observe + assess under 1.0 s, 3 lens calls + the
     previews, no preview of anybody else;
  7. scrub over every new surface.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.books import BookError, all_conditional_defaults, validate_config
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.kills import BLOCKS_PER_HOUR
from keeper.lens import LensClient
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.auto import Auto
from keeper.strategies.base import HARVESTING, node_occupant
from test_phase2a import CAROL, NODE, NODE79, T, _add_occupant, _apply_plans_to_world, _decisions, _last_tick, _preview_threshold, _rows, _seed, _set_party, _start
from test_phase2h import _kill

H = BLOCKS_PER_HOUR
OWNER_913 = [h["kami"]["index"] for h in NODE79["harvests"] if h["account"]["index"] == 806]
LAZY = {"passiveOwnersBind": False}  # what `start` turns into previewPolicy killed-here + nodeCensus plain


# ------------------------------------------------------------ helpers


def _env(make_keeper, tmp_path, chain: FakeChain | None = None) -> Env:
    """A lab keeper on the seeded recorded world with NO book yet (a cold
    node: no preview was ever fetched), the clock at T."""
    e = make_keeper(cap=8)
    if chain is not None:
        cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "kg"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 8, "requestTimeoutSec": 5}, env={})
        k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
        k.boot()
        e.keeper.stop_engine()
        e.keeper = k
        e.data_dir = Path(cfg.dataDir)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    assert e.keeper.handle("grant", {"account": "lab"})["ok"]
    e.books = []  # type: ignore[attr-defined]
    return e


def _book(e: Env, kamis: list[int], **over: Any):
    b = _start(e, kamis, **over)
    e.books.append(b)
    return b


def _tick(e: Env, dt: float = 30.0) -> list[str]:
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_due(now=e.clock["now"])


def _tick_one(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    """A direct tick of one book (its own pass): the summary as assessed —
    the measurement keys never write a tick row of their own."""
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_book(book, now=e.clock["now"])


def _node_reads(e: Env) -> list[list[str]]:
    return [r["args"] for r in e.lens.requests if r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 1]


def _preview_reads(e: Env) -> list[int]:
    return [int(r["args"][1]) for r in e.lens.requests if r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 2]


def _kami_reads(e: Env) -> list[int]:
    return [int(r["args"][0]) for r in e.lens.requests if r["query"] == "kami"]


def _serve_kami(fake, kami: int, cooldown: int = 0, level: int | None = None) -> None:
    """The lens `kami <index>` answer of an occupant of the node (level from
    its node row unless given)."""
    row = next(h for h in fake.nodes[NODE]["harvests"] if h["kami"]["index"] == kami)
    fake.set_kami(kami, state=HARVESTING, node=NODE, hp=(row["vitals"]["hp"]["current"], row["vitals"]["hp"]["total"]), account=row["account"]["index"])
    fake.kamis[kami]["level"] = row["vitals"]["level"] if level is None else level
    fake.kamis[kami]["cooldownSec"] = cooldown


def _held_kamis(summary: dict[str, Any]) -> list[int]:
    return sorted(h["kami"] for h in summary.get("held") or [])


# ------------------------------------------------------------ 1. the keys


def test_conditional_defaults_at_start_the_refusals_and_the_lock():
    base = {"kamiIndices": [263], "nodeIndex": NODE, "accountIndex": CAROL}
    full = validate_config("auto", dict(base), "lab")
    assert (full["passiveOwnersBind"], full["previewPolicy"], full["nodeCensus"], full["nodeCensusEverySec"], full["attackerVitalsPerTick"]) == (True, "all", "vitals", 30, 10)
    lazy = validate_config("auto", dict(base, passiveOwnersBind=False, tickSeconds=45), "lab")
    assert (lazy["previewPolicy"], lazy["nodeCensus"], lazy["nodeCensusEverySec"]) == ("killed-here", "plain", 45)  # the census interval defaults to the BOOK's tickSeconds
    # an explicit key always wins over a conditional default
    kept = validate_config("auto", dict(base, passiveOwnersBind=False, previewPolicy="all"), "lab")
    assert (kept["previewPolicy"], kept["nodeCensus"]) == ("all", "vitals")
    mixed = validate_config("auto", dict(base, passiveOwnersBind=False, previewPolicy="all", nodeCensus="plain", nodeCensusEverySec=120), "lab")
    assert (mixed["previewPolicy"], mixed["nodeCensus"], mixed["nodeCensusEverySec"]) == ("all", "plain", 120)
    # an owner that binds on its threshold needs the threshold (and its cooldown from the node rows)
    for bad, where in ((dict(base, previewPolicy="killed-here"), "previewPolicy"), (dict(base, passiveOwnersBind=True, previewPolicy="killed-here"), "previewPolicy"), (dict(base, nodeCensus="plain"), "nodeCensus"), (dict(base, nodeCensusEverySec=29), "nodeCensusEverySec"), (dict(base, tickSeconds=60, nodeCensusEverySec=30), "nodeCensusEverySec")):
        with pytest.raises(BookError) as err:
            validate_config("auto", bad, "lab")
        assert f"config invalid at {where}" in str(err.value) and not forbidden_in([str(err.value)])
    with pytest.raises(BookError):
        validate_config("auto", dict(base, previewPolicy="hostile"), "lab")  # that word is on the P9 scrub list: the value is the rendered class name
    # the conditional defaults are pinned like the plain ones
    lock = json.loads((Path(__file__).resolve().parents[1] / "keeper" / "schemas" / "defaults.lock.json").read_text())
    assert lock["conditionalDefaults"] == all_conditional_defaults()
    assert lock["conditionalDefaults"]["auto"] == {"previewPolicy": [{"when": {"passiveOwnersBind": False}, "default": "killed-here"}], "nodeCensus": [{"when": {"previewPolicy": "killed-here"}, "default": "plain"}], "nodeCensusEverySec": [{"sameAs": "tickSeconds"}]}
    assert lock["conditionalDefaults"]["harvestAndFeed"] == {} and lock["conditionalDefaults"]["craft"] == {}
    assert lock["defaults"]["auto"]["previewPolicy"] == "all" and lock["defaults"]["auto"]["nodeCensus"] == "vitals" and lock["defaults"]["auto"]["attackerVitalsPerTick"] == 10 and "nodeCensusEverySec" not in lock["defaults"]["auto"]


def test_the_node_86_example_validates_under_fleet_and_spells_out_what_start_would_fill_in():
    import yaml

    doc = yaml.safe_load((Path(__file__).with_name("fixtures") / "deploy" / "example-auto.yaml").read_text())
    assert [b["account"] for b in doc["books"]] == ["bob"]
    cfg = doc["books"][0]["config"]
    full = validate_config("auto", cfg, "fleet")
    assert (full["nodeIndex"], full["accountIndex"], full["passiveOwnersBind"], full["previewPolicy"], full["nodeCensus"], full["nodeCensusEverySec"]) == (86, 2663, False, "killed-here", "plain", 30)
    bare = validate_config("auto", {k: v for k, v in cfg.items() if k not in ("previewPolicy", "nodeCensus", "nodeCensusEverySec", "attackerVitalsPerTick")}, "fleet")
    assert bare == full  # the three keys are what the conditional defaults give a passiveOwnersBind-false book


def test_describe_states_the_conditional_defaults(env):
    d = env.keeper.handle("describe", {"type": "auto"})["data"]
    text = d["description"]
    assert 'default "all", default "killed-here" when passiveOwnersBind is false' in text
    assert 'default "vitals", default "plain" when previewPolicy is "killed-here"' in text
    assert "default: the value of tickSeconds" in text
    assert d["defaults"]["previewPolicy"] == "all" and "nodeCensusEverySec" not in d["defaults"]
    assert not forbidden_in(d)


def test_a_stored_config_without_the_keys_keeps_todays_reads_byte_for_byte(make_keeper, tmp_path):
    """Stored configs are complete as of THEIR start: a book stored before
    the keys existed has none of the four keys and must keep `all` + `vitals` + a census
    every pass until it is declared again — the same requests and the same
    tick summary as a book that names the old values."""
    runs = []
    for strip in (False, True):
        e = _env(make_keeper, tmp_path / ("s" if strip else "n"))
        book = _book(e, [263, 68017])
        assert (book.config["previewPolicy"], book.config["nodeCensus"], book.config["nodeCensusEverySec"]) == ("all", "vitals", 30)
        if strip:
            for key in ("previewPolicy", "nodeCensus", "nodeCensusEverySec", "attackerVitalsPerTick"):
                del book.config[key]
        summaries, requests = [], []
        for _ in range(3):
            _tick(e)
            requests.append([(r["query"], r["args"]) for r in e.lens.requests])
            s = dict(_last_tick(e, book))
            summaries.append(s)
        runs.append((summaries, requests))
        strat: Auto = e.keeper.strategies[book.id]
        assert strat.preview_policy == "all" and strat.census_mode == "vitals"
        assert _node_reads(e) == [[str(NODE), "--with-vitals", "--full"]]  # read every pass, with vitals
    assert runs[0] == runs[1]
    first = runs[0][0][0]
    assert first["previewCalls"] == 10 and first["previewsPending"] == 35 and "unpreviewed" not in first["owners"] and first["occupantsWithoutVitals"] == []


# ------------------------------------------------------------ 2. lazy previews


def test_a_cold_node_with_no_kill_reads_no_preview_and_nobody_holds_or_binds(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _set_party(e.lens, 66655, state="RESTING", node=None, hp=(220, 220))  # a resting kami at full HP: a start nobody may hold
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 66655]
    book = _book(e, [263, 66655, 68017], **LAZY)
    assert (book.config["previewPolicy"], book.config["nodeCensus"]) == ("killed-here", "plain")
    for _ in range(3):
        s = _tick_one(e, book)
        _apply_plans_to_world(e)  # the started kami harvests on the node from the next read on
        assert _preview_reads(e) == [] and _kami_reads(e) == []
        assert _node_reads(e) == [[str(NODE), "--full"]]  # the plain census
        assert s["previewCalls"] == 0 and s["previewsPending"] == 0 and s["previewBytes"] == 0 and s["attackerVitalsCalls"] == 0
        assert s["foreignOccupants"] == 45 and s["attackers"] == 45 and "occupantsWithoutVitals" not in s  # not applicable under plain
        # 45 attackers nobody previewed and nobody killed: never `unknown`, never a hold, never a bound
        assert "held" not in s and s["kamis"]["held"] == 0 and s["boundKamis"] == 0 and s["boundKamisBy"] == {} and s["minMarginHp"] is not None
        assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 0, "standing": 0, "unpreviewed": 3}
    starts = _decisions(e, book, "start")
    assert len(starts) == 1 and starts[0]["parameters"]["kamis"] == [66655] and starts[0]["parameters"]["detail"][0]["bound"] == 0
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    lim, classes, attackers, _s = strat.bounds(obs, {k: v for k, v in obs.kamis.items() if not v.missing})
    assert all(a["entry"] is None and a["class"].value == "harmless" for a in attackers) and all(l["unknown"] == [] and l["bound"] == 0 and l["boundBy"] is None for l in lim.values())
    assert e.keeper.previews.entries(NODE, CAROL) == {}
    # P3: the status block carries the same count; P3a: a kill row still renders from what is known (nothing: no-threshold)
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert st["owners"]["unpreviewed"] == 3 and st["previewsPending"] == 0 and st["nextPreviewDueAt"] is None
    assert strat.render_owner_class(806, obs.basis.lensBlock) == "no-threshold"


def test_the_flip_end_to_end_kill_row_then_arrivals_under_the_cap_then_bound(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))  # the victim: the book's account, outside the book
    _preview_threshold(e.lens, 360, 263, 165)  # the first kami reads 171 / 240: 165 + 5 % = 177 -> rest once the attacker is read
    for k in OWNER_913:
        _serve_kami(e.lens, k)
    e.keeper.owners.set_last_scanned(chain.head - 1)
    book = _book(e, [263, 68017], **LAZY)  # previewsPerTick 10 (the default): the owner holds 22 kamis on the node
    _tick(e)
    s = _last_tick(e, book)
    assert s["previewCalls"] == 0 and s["owners"]["unpreviewed"] == 3 and s["killScan"]["kills"] == 0 and _decisions(e, book, "rest") == []
    # the owner's attacker liquidates a kami of the account on the node in the next block
    chain.head += 1
    tx = "0x" + "ab" * 32
    chain.logs = [liquidate_log(chain.head, tx, kami_entity_id(5946))]
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(5946), kami_entity_id(360))
    _tick(e)
    kills = _rows(e, book, "kill")
    assert len(kills) == 1 and kills[0]["kill"]["attackerAccount"] == 806 and kills[0]["kill"]["ownerClassAfter"] == "killed-here-48h"
    s = _last_tick(e, book)
    # the tick after the kill row: the owner's 22 present attackers are ARRIVALS, ten fetched (the cap), twelve pending;
    # nobody of the two other owners is read; every book kami is `unknown` against the unread ones -> held at the floor
    assert sorted(_preview_reads(e)) == sorted(OWNER_913)[:10] and s["previewCalls"] == 10 and s["previewsPending"] == 12
    # the twelve unread attackers of an owner with previews are `proxied`,
    # not `unknown` — they bind on the owner's served maximum and hold nothing,
    # so nobody is held and the first kami (HP 171, bound 177 from the
    # attacker's preview) rests on THIS tick
    assert _held_kamis(s) == [] and "held" not in s
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "rest")] == [[263]]
    assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 1, "standing": 0, "unpreviewed": 2}
    # cooldown: the ten previews of the pass served it; the other twelve cost one `kami` read each up to attackerVitalsPerTick (10)
    assert sorted(_kami_reads(e)) == sorted(OWNER_913)[10:20] and s["attackerVitalsCalls"] == 10 and s["attackerVitalsOverCap"] == sorted(OWNER_913)[20:]
    _tick(e)
    s = _last_tick(e, book)
    assert sorted(_preview_reads(e)) == sorted(OWNER_913)[10:20] and s["previewsPending"] == 2 and _held_kamis(s) == []  # proxied, never held
    _tick(e)
    s = _last_tick(e, book)
    assert sorted(_preview_reads(e)) == sorted(OWNER_913)[20:] and s["previewsPending"] == 0 and "held" not in s
    rests = _decisions(e, book, "rest")
    # the first kami is bound from the tick the preview landed; this fixture never
    # applies the stop (it still reads HARVESTING at 171), so the same rest is
    # re-offered on each of the three ticks since — one kami, one bound
    assert [r["parameters"]["kamis"] for r in rests] == [[263], [263], [263]]
    assert rests[0]["parameters"]["detail"][0]["bound"] == 177 and rests[0]["parameters"]["detail"][0]["boundBy"] == {"attackerKami": 360, "ownerClass": "killed-here-48h", "threshold": 165, "present": True, "hostileBy": "both"}
    assert set(e.keeper.previews.entries(NODE, CAROL)) == set(OWNER_913)  # nobody else was ever fetched
    assert s["boundKamisBy"] == {"killed-here-48h/both": 2} and s["hostileKamis"] == 1  # the second kami too: threshold 6 + 5 % x 270 = 20, far below its HP


def test_an_attacker_whose_window_expired_is_not_refreshed_kept_until_it_leaves_then_dropped(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _serve_kami(e.lens, 360)
    book = _book(e, [263, 68017], previewRefreshSec=300, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - 47 * H, "0x47", 4444, 360, node=62))  # the attacker killed one of the account's kamis elsewhere 47 h ago, under an owner that is not here
    _tick(e)
    s = _last_tick(e, book)
    assert _preview_reads(e) == [360] and s["owners"]["killed-here"] == 1 and s["owners"]["unpreviewed"] == 2 and s["boundKamisBy"] == {"killed-here-48h/kami": 2}
    _tick(e, 301)
    assert _preview_reads(e) == [360]  # inside the window an aged entry is refreshed as before
    # two hours on the kill is 49 h old: the attacker is classed by what is known and never read again
    e.lens.advance(2 * H)
    fetched_at = e.keeper.previews.entries(NODE, CAROL)[360].fetched_at
    for _ in range(3):
        _tick(e, 3600)
        s = _last_tick(e, book)
        assert _preview_reads(e) == [] and _kami_reads(e) == [] and s["previewsPending"] == 0 and s["boundKamis"] == 0
    assert e.keeper.previews.entries(NODE, CAROL)[360].fetched_at == fetched_at  # kept, not refreshed
    assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 1, "killed-here": 0, "standing": 0, "unpreviewed": 2}  # the owner renders from its old preview (threshold 6)
    assert e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]["nextPreviewDueAt"] is None  # nothing is due for ever
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 360]
    _tick(e)
    assert _last_tick(e, book)["previewsDropped"] == [360] and e.keeper.previews.entries(NODE, CAROL) == {}


# ------------------------------------------------------------ 3. the plain census


def test_a_row_without_vitals_is_a_plain_occupant_only_when_none_were_asked_for():
    row = {k: v for k, v in NODE79["harvests"][0].items() if k != "vitals"}
    plain = node_occupant(row, vitals_expected=False)
    assert plain is not None and plain.vitals_missing == () and plain.cooldown_sec == 0 and plain.level is None and plain.account_index == row["account"]["index"]
    assert node_occupant(row).vitals_missing == ("vitals",)  # a vitals read that lacks them still says so
    assert node_occupant(NODE79["harvests"][0], vitals_expected=False).level is not None  # vitals served anyway are kept


def test_the_cooldown_of_a_killed_here_attacker_under_the_plain_census(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _preview_threshold(e.lens, 360, 263, 165)
    _serve_kami(e.lens, 360, cooldown=900)  # above cooldownHorizonSec 600
    book = _book(e, [263, 68017], autoCollect=False, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 4444, 360, node=62))
    # the pass that fetches the preview reads the cooldown off its answer (`attacker.cooldownSec` 0): no `kami` read, and the attacker binds
    _tick(e)
    s = _last_tick(e, book)
    assert _preview_reads(e) == [360] and _kami_reads(e) == [] and s["attackerVitalsCalls"] == 0 and s["cooldownSkipped"] == []
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "rest")] == [[263]]
    _set_party(e.lens, 263, hp=(171, 240))  # the dry run changed nothing in the world: the first kami still harvests at 171
    book.plan["263"] = {"state": HARVESTING, "node": NODE}
    # the next pass: one `kami` read of the attacker -> cooldown 900 > 600: skipped, the first kami is not bound this tick
    _tick(e)
    s = _last_tick(e, book)
    assert _preview_reads(e) == [] and _kami_reads(e) == [360] and s["attackerVitalsCalls"] == 1 and s["cooldownSkipped"] == [360] and s["boundKamis"] == 0
    assert len(_decisions(e, book, "rest")) == 1
    # over the cap (attackerVitalsPerTick 0): no read, NO cooldown -> the attacker binds
    book.config["attackerVitalsPerTick"] = 0
    _tick(e)
    s = _last_tick(e, book)
    assert _kami_reads(e) == [] and s["attackerVitalsOverCap"] == [360] and s["cooldownSkipped"] == [] and s["boundKamis"] == 2
    assert len(_decisions(e, book, "rest")) == 2
    # a read the lens refuses: no cooldown either -> binds
    book.config["attackerVitalsPerTick"] = 10
    book.plan["263"] = {"state": HARVESTING, "node": NODE}
    del e.lens.kamis[360]
    _tick(e)
    s = _last_tick(e, book)
    assert _kami_reads(e) == [360] and s["attackerVitalsUnread"] == [360] and s["cooldownSkipped"] == [] and s["boundKamis"] == 2
    assert len(_decisions(e, book, "rest")) == 3
    # the entry keeps the cooldown its preview served: the plain rows (no cooldown) never overwrite it
    assert e.keeper.previews.entries(NODE, CAROL)[360].cooldown_sec == 0


def test_an_attacker_level_that_moved_refreshes_its_preview_under_the_plain_census(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _serve_kami(e.lens, 360)
    book = _book(e, [263, 68017], **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 4444, 360, node=62))
    _tick(e)
    assert _preview_reads(e) == [360] and e.keeper.previews.entries(NODE, CAROL)[360].attacker_level == 38
    _tick(e)
    assert _preview_reads(e) == [] and _kami_reads(e) == [360]
    e.lens.kamis[360]["level"] = 39
    next(h for h in e.lens.previews[NODE][360]["harvests"] if h["kami"]["index"] == 360)["vitals"]["level"] = 39
    _tick(e)
    s = _last_tick(e, book)
    assert _kami_reads(e) == [360] and _preview_reads(e) == [360] and s["previewCalls"] == 1 and e.keeper.previews.entries(NODE, CAROL)[360].attacker_level == 39
    _tick(e)
    assert _preview_reads(e) == []


# ------------------------------------------------------------ 4. the census interval


def test_the_census_is_reused_between_reads_own_kamis_are_read_every_pass_and_a_kill_row_forces_a_read(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    e.keeper.owners.set_last_scanned(chain.head - 1)
    book = _book(e, [263, 68017], nodeCensusEverySec=120, **LAZY)
    seen = []
    for _ in range(5):
        s = _tick_one(e, book)
        seen.append((len(_node_reads(e)), s["censusAgeSec"], s["nodeReadBytes"] > 0, sum(1 for r in e.lens.requests if r["query"] == "party")))
    assert seen == [(1, 0, True, 1), (0, 30, False, 1), (0, 60, False, 1), (0, 90, False, 1), (1, 0, True, 1)]
    _tick(e)
    assert _node_reads(e) == []
    # a kill row on the node: the census is read on that pass, whatever its age
    chain.head += 1
    tx = "0x" + "cd" * 32
    chain.logs = [liquidate_log(chain.head, tx, kami_entity_id(5946))]
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(5946), kami_entity_id(448))
    _tick(e)
    s = _last_tick(e, book)
    assert len(_rows(e, book, "kill")) == 1 and len(_node_reads(e)) == 1 and s["censusAgeSec"] == 0 and s["owners"]["killed-here"] == 1
    # the census is a NODE fact: a second book on the node (its own interval 120) reuses the one the first read
    second = _book(e, [66655], nodeCensusEverySec=120, **LAZY)
    s2 = _tick_one(e, second)
    assert _node_reads(e) == [] and s2["censusAgeSec"] == 30 and s2["nodeReadBytes"] == 0
    # and it is never persisted: nothing of it sits in the data dir
    assert not [p for p in Path(e.data_dir).rglob("*") if "census" in p.name.lower()]


def test_a_kill_of_our_own_kami_deployed_after_the_census_is_still_attributed(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    e.keeper.owners.set_last_scanned(chain.head - 1)
    # the census of pass 1 does not list the second kami yet (it deploys right after); the party read says HARVESTING on the node
    row_15783 = next(h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] == 68017)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 68017]
    book = _book(e, [263, 68017], nodeCensusEverySec=120, **LAZY)
    _tick(e)
    assert kami_entity_id(68017) not in e.keeper.census.held(NODE, "plain").refs
    e.lens.nodes[NODE]["harvests"].append(row_15783)
    _tick(e)  # a reused census: the book's own harvesting kamis join the occupant table from the party read
    assert _node_reads(e) == [] and e.keeper.context.node_occupants[NODE][kami_entity_id(68017)].account_index == CAROL
    chain.head += 1
    tx = "0x" + "ef" * 32
    chain.logs = [liquidate_log(chain.head, tx, kami_entity_id(68017))]
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(68017), kami_entity_id(360))
    _tick(e)
    kills = _rows(e, book, "kill")
    assert len(kills) == 1 and kills[0]["kill"]["victimKami"] == 68017 and kills[0]["kill"]["attackerAccount"] == 806 and kills[0]["kill"]["ownerClassAfter"] == "killed-here-48h"
    assert e.keeper.owners.killed_ours_in_window(806, CAROL, 48, None)


def test_a_preview_refused_against_a_reused_census_holds_and_forces_a_read(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    book = _book(e, [263, 68017], nodeCensusEverySec=120, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    _tick(e)
    head = _rows(e, book, "tick")[-1]["lensBlock"]
    # an attacker turns killed-here and LEAVES the node before the next census: the reused census still lists it
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 4444, 3215, node=62))
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 3215]
    summary = _tick_one(e, book)
    assert _node_reads(e) == [] and summary.get("reason") is None and summary["previewsRefused"] == [3215] and summary["censusAgeSec"] == 30  # a tick, not a refused one
    assert _held_kamis(summary) == [263, 68017]  # unknown stays unknown: held at the floor
    assert e.keeper.census.held(NODE, "plain") is None
    _tick(e)
    s = _last_tick(e, book)
    assert len(_node_reads(e)) == 1 and s["censusAgeSec"] == 0 and s["attackers"] == 44 and "held" not in s and "previewsRefused" not in s
    assert strat.preview_policy == "killed-here"


# ------------------------------------------------------------ 5. the measured lines


def test_the_census_is_read_and_parsed_once_per_pass_and_the_status_carries_the_pass_totals(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    st = e.keeper.handle("status")["data"]["lens"]
    assert st["passCalls"] is None and st["passBytes"] is None  # no pass yet
    a = _book(e, [263, 68017], **LAZY)
    b = _book(e, [66655], **LAZY)
    before = e.keeper.lens.counters()
    _tick(e)
    after = e.keeper.lens.counters()
    sa, sb = _last_tick(e, a), _last_tick(e, b)
    assert len(_node_reads(e)) == 1 and sa["nodeReadBytes"] > 30_000 and sb["nodeReadBytes"] == 0 and sb["lensCallsServedFromPass"] >= 2 and sa["censusAgeSec"] == sb["censusAgeSec"] == 0
    full = copy.deepcopy(e.lens.nodes[NODE])
    # `nodeReadBytes` is the whole JSON LINE — the rows PLUS the envelope's
    # `meta`, which carries the `clockSample*` + `observed*` clock names
    # the 0.6.3 daemon serves (~210 B) — while `full` is the DATA alone. The claim is
    # about the rows, so the envelope is allowed on top of the half.
    assert sa["nodeReadBytes"] - 500 < len(json.dumps(full)) // 2  # the plain rows carry no vitals
    oa = e.keeper.strategies[a.id].observe(e.keeper.lens)
    assert e.keeper.context.node_occupants[NODE] is e.keeper.census.held(NODE, "plain").refs and len(oa.occupants) == 152
    st = e.keeper.handle("status")["data"]["lens"]
    assert st["passCalls"] == {"last": after["calls"] - before["calls"], "p95": after["calls"] - before["calls"], "passes": 1}
    assert st["passBytes"]["last"] == after["bytes"] - before["bytes"] and st["passBytes"]["p95"] == st["passBytes"]["last"]
    for _ in range(3):
        _tick(e)
    st = e.keeper.handle("status")["data"]["lens"]
    assert st["passCalls"]["passes"] == 4 and st["passCalls"]["p95"] >= st["passCalls"]["last"] > 0
    # a pass that ticks no book adds nothing
    e.keeper.tick_due(now=e.clock["now"])
    assert e.keeper.handle("status")["data"]["lens"]["passCalls"]["passes"] == 4
    # the new measurement keys never write a tick row of their own
    from keeper.core import TICK_KEY_IGNORED

    assert {"nodeReadBytes", "attackerVitalsCalls", "censusAgeSec"} <= TICK_KEY_IGNORED


# ------------------------------------------------------------ 6. scale


def _scale_world(fake, foreign: int = 1750, owned: int = 50, owners: int = 600) -> tuple[list[int], list[int]]:
    ours = list(range(100_000, 100_000 + owned))
    harvests = []
    party = []
    for k in ours:
        vit = {"hp": {"current": 200, "total": 240, "percent": 83}, "hpRatePerHr": "-12.00", "musuAccrued": 100, "cooldownSec": 0, "cooldownUntil": 0, "level": 30, "xp": 0, "xpRequired": 1, "levelUpReady": False, "levelUpBlockedBy": "EXPERIENCE", "skillPoints": 0}
        harvests.append({"id": f"h{k}", "state": "ACTIVE", "kami": {"id": f"k{k}", "name": f"kami-{k}", "index": k}, "account": {"index": CAROL, "name": "carol"}, "vitals": vit})
        party.append({"id": f"k{k}", "index": k, "name": f"kami-{k}", "state": HARVESTING, "level": 30, "hp": {"current": 200, "total": 240, "percent": 83}, "hpRatePerHr": "-12.00", "musu": {"accrued": 100, "spotRatePerHr": "100.0", "avgRatePerHr": "100.0"}, "cooldownSec": 0, "cooldownUntil": 0, "node": {"index": NODE, "name": "node-79"}, "account": {"index": CAROL, "name": "carol"}})
    theirs = list(range(200_000, 200_000 + foreign))
    for n, k in enumerate(theirs):
        vit = {"hp": {"current": 150, "total": 200, "percent": 75}, "hpRatePerHr": "-10.00", "musuAccrued": 10, "cooldownSec": 0, "cooldownUntil": 0, "level": 25, "xp": 0, "xpRequired": 1, "levelUpReady": False, "levelUpBlockedBy": "EXPERIENCE", "skillPoints": 0}
        harvests.append({"id": f"h{k}", "state": "ACTIVE", "kami": {"id": f"k{k}", "name": f"kami-{k}", "index": k}, "account": {"index": 10_000 + n % owners, "name": f"acct-{n % owners}"}, "vitals": vit})
    fake.nodes[NODE] = {"index": NODE, "name": "scale", "type": "HARVEST", "affinity": ["NORMAL"], "roomIndex": NODE, "harvestsTotal": len(harvests), "harvestsServed": len(harvests), "harvests": harvests}
    fake.previews[NODE] = {}
    fake.party[CAROL] = {"account": {"index": CAROL, "name": "carol"}, "kamis": party}
    return ours, theirs


def test_scale_1750_foreign_occupants_50_owned_kamis_2_killed_here_attackers(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    ours, theirs = _scale_world(e.lens)
    by_owner, by_kami = theirs[7], theirs[1234]  # one killed-here by its owner (a one-kami owner here: 600 owners, 1,750 kamis -> three each; pick both of its others too)
    book = _book(e, ours, friends=[], **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 99_999, by_kami, node=62))  # the kami killed one of the account's kamis elsewhere, under another owner
    e.keeper.owners.record_kill(_kill(head - H, "0x02", 99_998, by_owner, node=62))
    e.lens.synth_thresholds.update({(a, ours[0]): 190 for a in (by_owner, by_kami)})
    for a in (by_owner, by_kami):
        _serve_kami(e.lens, a)
    from keeper import census as census_module

    census_module.entity_id.cache_clear()  # the warming pass pays the 1,800 entity-id keccaks of a cold process
    timings = []
    for n in range(2):
        e.lens.requests.clear()
        e.clock["now"] += 30
        e.keeper.census.begin_pass()
        e.keeper.previews.begin_pass()
        t0 = time.perf_counter()
        obs = strat.observe(e.keeper.lens)
        rt: dict[str, Any] = {}
        decisions = strat.assess(obs, e.clock["now"], rt)
        timings.append(time.perf_counter() - t0)
        s = rt["summaryExtra"]
        world = [r for r in e.lens.requests if not (r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 2) and r["query"] != "kami"]
        assert [r["query"] for r in world] == ["party", "node", "inventory"] and s["foreignOccupants"] == 1750 and s["attackers"] == 1750
        if n == 0:
            # the pass that warms: 3 reads + the 2 previews (<= previewsPerTick), their answers carry the cooldown
            assert sorted(_preview_reads(e)) == sorted([by_owner, by_kami]) and _kami_reads(e) == [] and s["lensCalls"] == 5 and s["previewCalls"] == 2
        else:
            # a settled pass: 3 reads + one `kami` read per killed-here attacker (<= attackerVitalsPerTick), no preview
            assert _preview_reads(e) == [] and sorted(_kami_reads(e)) == sorted([by_owner, by_kami]) and s["lensCalls"] == 5 and s["attackerVitalsCalls"] == 2
        assert s["previewsPending"] == 0 and "held" not in s
        assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 2, "standing": 0, "unpreviewed": 598} and s["hostileKamis"] == 2
        assert s["boundKamis"] == 50 and [d.kind for d in decisions] == ["rest"] and decisions[0].parameters["kamis"] == [ours[0]]  # 190 + 5 % x 240 = 202 >= 200
    assert set(e.keeper.previews.entries(NODE, CAROL)) == {by_owner, by_kami}  # zero previews of anybody else
    assert max(timings) < 1.0, timings
    print(f"\n2g scale: observe + assess over 1,750 foreign / 50 owned / 2 killed-here: {timings[0]:.3f} s warming, {timings[1]:.3f} s settled")


# ------------------------------------------------------------ 7. scrub


def test_every_new_surface_is_clean(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _serve_kami(e.lens, 360)
    book = _book(e, [263, 68017], nodeCensusEverySec=60, attackerVitalsPerTick=0, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 4444, 360, node=62))
    for _ in range(3):
        _tick(e)
    surfaces = [e.keeper.handle("status")["data"], e.keeper.handle("ledger", {"book": book.id})["data"], e.keeper.handle("logs")["data"], e.keeper.handle("describe", {"type": "auto"})["data"], e.keeper.handle("books", {"account": "lab"})["data"]]
    assert collect_strings(surfaces) and forbidden_in(surfaces) == []
    assert book.config["previewPolicy"] == "killed-here"  # the stored value is served by `books` / `status` and is clean
