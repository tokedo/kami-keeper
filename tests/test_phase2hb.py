"""The measured line: a kill above the served threshold lifts the
attacker owner's lines by what the kill measured.

  1. the entry arithmetic — `clamped` lifts each row by ceil(ratio x its
     OWN max HP), a legacy row by 0, a non-positive served threshold is 0
     then lifted; `max_ratio` adds the ratio and `stand_in` follows;
     `lift()` only grows; the cap and its flag; the dict round-trip and
     the old-style dict;
  2. the cache — `fetch()` carries the lift over a replacement entry,
     `forget()` drops it, `lift_owner` reaches the owner's present AND
     remembered entries and nobody else's, `mark_refetch` flags the
     present ones, `expire_lifts` drops a lift past its hold, `due()`
     offers a flagged entry first and `next_due_at` calls it due;
  3. a replay through the keeper — served 108
     against a 280-HP kami at `safetyMarginPct` 1 (bound 111), the kami
     seen at 114 on pass N, killed by attacker A of owner O: the kill row
     carries the measurement, A's and B's lines rise by 7, the owner's
     present entries are refetched in the SAME pass, and that pass rests
     the second kami at 116 and leaves the third at 130;
  4. a kill at or below the served line — nothing measured, "a late
     rest", no entry lifted, the owner's lines still refetched;
  5. the skips — unattributed, another account's kami (`learned` null),
     a victim with no previous-pass view, an attacker with no line (and
     the arrival path previews it that pass anyway);
  6. growth and persistence — a second kill lifts again from the SERVED
     threshold, a refresh keeps the lift, it survives a restart, the hold
     expires and a new kill renews it, and an old reader loads the file;
  7. scrub over every formatted surface of 3–6.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.previews import LIFT_CAP, LIFT_HOLD_SEC, PreviewEntry
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.auto import Auto
from keeper.strategies.base import DEAD, HARVESTING
from test_phase2a import CAROL, NODE, T, _add_occupant, _decisions, _last_tick, _preview_threshold, _rows, _seed, _set_party, _start
from test_phase2g import LAZY, _kami_reads, _preview_reads, _serve_kami

# The world, on the recorded fixtures: one owner holds three kamis on
# the node — two of them (A, B) are the pack, the third is taken
# off the node before anything is previewed so that the owner has exactly
# two entries. The book holds three kamis of 280 max HP, all three served
# the same served line (108).
OWNER = 2481
ATT_A = 601
ATT_B = 2865
ATT_C = 4016
VICTIM = 68017
SECOND = 66655
THIRD = 68696
KAMIS = [VICTIM, SECOND, THIRD]
THR = 108
MAXHP = 280
# 108 + `safetyMarginPct` 1 % x 280 = 110.8 -> 111 is what the keeper
# guarded; the kill at 114 lifts the line by ceil((6 + 1) / 280 x 1000) /
# 1000 = 2.5 % -> ceil(0.025 x 280) = 7 HP, so 115 + 2.8 = 117.8 -> 118
BOUND = 111
LIFTED_THR = 115
LIFTED_BOUND = 118
LIFT_PCT = 2.5


# ------------------------------------------------------------ helpers


def _entry(rows: dict[int, dict[str, Any]], attacker: int = ATT_A, owner: int = OWNER, fetched_at: float = T) -> PreviewEntry:
    return PreviewEntry(NODE, attacker, fetched_at, 11_878_391, 48, owner, {int(k): dict(v) for k, v in rows.items()})


def _row(threshold: int, hp_total: int | None = MAXHP) -> dict[str, Any]:
    r: dict[str, Any] = {"threshold": threshold, "level": 48, "reason": None}
    if hp_total is not None:
        r["hpTotal"] = hp_total
    return r


def _source(at: float = T, attacker: int = ATT_A, victim: int = VICTIM) -> dict[str, Any]:
    return {"victim": victim, "hpAtKill": 114, "served": THR, "gapHp": 6, "block": 12_278_392, "txHash": None, "at": at, "attacker": attacker}


def _leave(fake, kami: int) -> None:
    fake.nodes[NODE]["harvests"] = [h for h in fake.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


def _env(make_keeper, tmp_path, chain: FakeChain, standing: tuple[int, ...] = (OWNER,)) -> Env:
    """A lab keeper with the chain scan wired, `standingOwners` from the
    environment, the recorded world seeded as above, and NO
    book yet. The clock is at T."""
    env = {"KAMI_KEEPER_STANDING_OWNERS": ",".join(str(a) for a in standing)} if standing else {}
    e = make_keeper(cap=8, env=env)
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "khb"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 8, "requestTimeoutSec": 5}, env=env)
    assert cfg.standingOwners == list(standing)
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    clock = {"now": T}
    k.clock = lambda: clock["now"]
    k.boot()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(cfg.dataDir)
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    _leave(e.lens, ATT_C)
    for kami, hp in ((VICTIM, 114), (SECOND, 116), (THIRD, 130)):
        _set_party(e.lens, kami, state=HARVESTING, node=NODE, hp=(hp, MAXHP))
    for att in (ATT_A, ATT_B):
        _serve_kami(e.lens, att)
        for kami in KAMIS:
            _preview_threshold(e.lens, att, kami, THR)
    k.owners.set_last_scanned(chain.head - 1)
    assert k.handle("grant", {"account": "lab"})["ok"]
    e.books = []  # type: ignore[attr-defined]
    return e


def _book(e: Env, kamis: list[int] | None = None, **over: Any):
    b = _start(e, kamis or KAMIS, **dict(LAZY, safetyMarginPct=1, reviveOnDeath=False, **over))
    e.books.append(b)
    return b


def _tick(e: Env, dt: float = 30.0) -> list[str]:
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_due(now=e.clock["now"])


def _kill_on_chain(e: Env, chain: FakeChain, victim: int, attacker: int | None, tx: str, form: str = "typed") -> None:
    """The victim's DEAD write in the next block, with the liquidate
    calldata that names `attacker` (`attacker` None / `form` "other": the
    unattributed shape), and the world reads the kami DEAD and off the
    node."""
    chain.head += 1
    chain.logs = [liquidate_log(chain.head, tx, kami_entity_id(victim))]
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(victim), kami_entity_id(attacker or 0), form)
    row = next((r for r in e.lens.party[CAROL]["kamis"] if r["index"] == victim), None)
    if row is not None:
        _set_party(e.lens, victim, state=DEAD, hp=(0, row["hp"]["total"]))
        _set_party(e.lens, victim, node=None)
    _leave(e.lens, victim)


def _entries(e: Env) -> dict[int, PreviewEntry]:
    return e.keeper.previews.entries(NODE, CAROL)


def _kill_rows(e: Env, book) -> list[dict[str, Any]]:
    return [r["kill"] for r in _rows(e, book, "kill")]


def _views(e: Env, book) -> tuple[Any, dict[int, Any]]:
    """(the observation, the complete views) — what `bounds()` takes."""
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    return obs, {k: v for k, v in obs.kamis.items() if not v.missing}


def _lines(e: Env) -> list[str]:
    return [ln["line"] for ln in e.keeper.handle("logs")["data"]["lines"]]


# ------------------------------------------------- 1. the entry arithmetic


def test_t1_clamped_lifts_every_row_by_the_ceiling_of_the_ratio_times_its_own_max_hp():
    e = _entry({VICTIM: _row(THR), SECOND: _row(100, 260), THIRD: _row(THR, None), 69376: _row(-18)})
    assert [e.clamped(k) for k in (VICTIM, SECOND, THIRD, 69376)] == [108, 100, 108, 0]
    assert e.lift(0.025, _source()) is True
    # ceil(0.025 x 280) = 7, ceil(0.025 x 260) = 7 (6.5), a row with no
    # `hpTotal` (an older entry) is lifted by 0 and that is all, and a
    # non-positive served threshold is clamped to 0 FIRST, then lifted
    assert [e.lift_hp(k) for k in (VICTIM, SECOND, THIRD, 69376)] == [7, 7, 0, 7]
    assert [e.clamped(k) for k in (VICTIM, SECOND, THIRD, 69376)] == [115, 107, 108, 7]
    assert e.clamped(99999) is None  # a missing row is still unknown, never a lift
    assert e.max_clamped() == 115 and e.max_clamped([THIRD]) == 108


def test_t1_max_ratio_adds_the_lift_and_stand_in_scales_off_the_lifted_line():
    e = _entry({SECOND: _row(100, 200)})
    assert e.max_ratio() == 0.5 and e.stand_in(260) == (130, True)
    assert e.lift(0.025, _source()) is True
    assert e.max_ratio() == pytest.approx(0.525) and e.stand_in(260) == (137, True)  # ceil(0.525 x 260) = 136.5 -> 137
    # the UNSCALED branch is reached only when no counted row's max HP is
    # known by either source — and such a row is lifted by 0 by rule, so
    # `max_clamped` there is the served number: structurally lifted
    # (through `clamped`), numerically nothing to lift by
    legacy = _entry({SECOND: _row(100, None)})
    assert legacy.stand_in(260) == (100, False)
    assert legacy.lift(0.025, _source()) is True and legacy.stand_in(260) == (100, False)
    assert legacy.stand_in(260, hp_totals={SECOND: 200}) == (137, True)  # the denominator from the book's own max HP makes it scale, and it lifts


def test_t1_a_lift_only_ever_grows_and_the_cap_is_reported():
    e = _entry({VICTIM: _row(THR)})
    assert e.lift(0.025, _source(at=T)) is True and e.lift_ratio == 0.025
    assert e.lift_from["gapHp"] == 6 and e.lift_from["at"] == T and e.lift_until == T + LIFT_HOLD_SEC
    # not higher: False, and NOTHING changes
    assert e.lift(0.02, _source(at=T + 60)) is False and e.lift(0.025, _source(at=T + 60)) is False
    assert e.lift_ratio == 0.025 and e.lift_from["at"] == T and e.lift_until == T + LIFT_HOLD_SEC
    # higher: it grows and the hold restarts
    assert e.lift(0.03, _source(at=T + 120)) is True and e.lift_ratio == 0.03 and e.lift_until == T + 120 + LIFT_HOLD_SEC
    assert "capped" not in e.lift_from
    # a measured gap above a quarter of a kami's max HP is a wrong
    # measurement: the lift is capped and the cap is reported
    assert e.lift(0.5, _source(at=T + 180)) is True and e.lift_ratio == LIFT_CAP == 0.25
    assert e.lift_from["capped"] is True and e.clamped(VICTIM) == 108 + 70


def test_t1_the_lift_round_trips_through_the_dict_and_an_old_style_dict_loads_without_one():
    e = _entry({VICTIM: _row(THR)})
    assert e.to_dict()["liftRatio"] == 0.0 and e.to_dict()["liftFrom"] == {} and e.to_dict()["liftUntil"] is None and e.to_dict()["refetch"] is False
    e.lift(0.025, _source())
    e.refetch = True
    d = e.to_dict()
    assert d["liftRatio"] == 0.025 and d["liftFrom"] == _source() and d["liftUntil"] == T + LIFT_HOLD_SEC and d["refetch"] is True
    back = PreviewEntry.from_dict(json.loads(json.dumps(d)))
    assert back.lift_ratio == 0.025 and back.lift_from == _source() and back.lift_until == T + LIFT_HOLD_SEC and back.refetch is True
    assert back.clamped(VICTIM) == LIFTED_THR
    # an older file carries none of the four keys
    old = {k: v for k, v in d.items() if k not in ("liftRatio", "liftFrom", "liftUntil", "refetch")}
    fresh = PreviewEntry.from_dict(old)
    assert fresh.lift_ratio == 0.0 and fresh.lift_from == {} and fresh.lift_until is None and fresh.refetch is False
    assert fresh.clamped(VICTIM) == THR


# ------------------------------------------------------------ 2. the cache


def test_t2_lift_owner_reaches_the_owners_present_and_remembered_entries_and_nobody_elses(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    # another owner, with ONE kami on the node, previewed through its own kill
    _add_occupant(e.lens, 7777, 4444, hp=(250, 300), level=39)
    _serve_kami(e.lens, 7777)
    book = _book(e)
    e.keeper.owners.record_kill({"block": chain.head, "txHash": "0x01", "logIndex": 0, "node": NODE, "victimKami": SECOND, "victimAccount": CAROL, "attackerKami": 7777, "attackerAccount": 4444, "attribution": "occupant"})
    _tick(e)
    assert sorted(_entries(e)) == sorted([ATT_A, ATT_B, 7777])
    # ATT_B leaves: remembered (its owner is on the standing list)
    _leave(e.lens, ATT_B)
    _tick(e)
    assert _entries(e)[ATT_B].remembered is True
    previews = e.keeper.previews
    rose = previews.lift_owner(NODE, CAROL, OWNER, 0.025, _source(at=e.clock["now"]))
    assert rose == [ATT_A, ATT_B]  # present AND remembered
    assert _entries(e)[ATT_A].lift_ratio == 0.025 and _entries(e)[ATT_B].lift_ratio == 0.025
    assert _entries(e)[7777].lift_ratio == 0.0 and _entries(e)[7777].lift_from == {}  # another owner is untouched
    # the killer carries the measurement as given; a sibling says through whom
    assert "viaAttacker" not in _entries(e)[ATT_A].lift_from and _entries(e)[ATT_B].lift_from["viaAttacker"] == ATT_A
    # only the PRESENT entries are flagged for a refetch
    assert _entries(e)[ATT_A].refetch is True and _entries(e)[ATT_B].refetch is False and _entries(e)[7777].refetch is False
    # an owner with no entry in the scope has no line to lift yet
    assert previews.lift_owner(NODE, CAROL, 9999, 0.1, _source(at=e.clock["now"], attacker=8888)) == []
    # a second call that is not higher returns nothing risen
    assert previews.lift_owner(NODE, CAROL, OWNER, 0.02, _source(at=e.clock["now"])) == []
    assert _entries(e)[ATT_A].lift_ratio == 0.025


def test_t2_a_refetch_carries_the_lift_and_clears_the_flag_and_forget_drops_it(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    book = _book(e)
    _tick(e)
    previews = e.keeper.previews
    previews.lift_owner(NODE, CAROL, OWNER, 0.025, _source(at=e.clock["now"]))
    assert _entries(e)[ATT_A].clamped(SECOND) == LIFTED_THR and _entries(e)[ATT_A].refetch is True
    occ = _views(e, book)[0].occupants[ATT_A]
    entry, changed, _ans = previews.fetch(e.keeper.lens, NODE, ATT_A, occ, CAROL, e.clock["now"], targets=KAMIS + [ATT_A])
    # the lens serves the SAME low number, so nothing `changed`; the
    # measured lift and its hold ride onto the replacement entry, and the
    # fetch itself clears the refetch flag
    assert changed is False and entry.rows[SECOND]["threshold"] == THR
    assert entry.lift_ratio == 0.025 and entry.lift_from["gapHp"] == 6 and entry.lift_until == e.clock["now"] + LIFT_HOLD_SEC
    assert entry.clamped(SECOND) == LIFTED_THR and entry.refetch is False
    # the lift leaves with the entry
    previews.forget(NODE, CAROL, ATT_A)
    assert ATT_A not in _entries(e)


def test_t2_a_flagged_entry_is_offered_first_and_is_due_now(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    book = _book(e)
    _tick(e)
    previews = e.keeper.previews
    _add_occupant(e.lens, 360, 806, hp=(250, 300), level=39)  # an arrival, nobody's pack
    # the book offers only the pack under `previewPolicy` killed-here, so
    # that is what `due()` is asked about here (the observation is taken
    # BEFORE the flag: an `observe` would answer the refetch itself)
    occupants = _views(e, book)[0].occupants
    pack = {i: o for i, o in occupants.items() if i in (ATT_A, ATT_B)}
    with_arrival = {i: o for i, o in occupants.items() if i in (ATT_A, ATT_B, 360)}
    now = e.clock["now"]
    assert previews.due(NODE, CAROL, pack, {}, 3600, now) == []  # fresh entries, nothing due
    assert previews.due(NODE, CAROL, with_arrival, {}, 3600, now) == [(360, "arrival")]
    assert previews.next_due_at(NODE, CAROL, 3600) == pytest.approx(now + 3600)
    flagged = previews.mark_refetch(NODE, CAROL, OWNER)
    assert flagged == [ATT_A, ATT_B]
    assert previews.due(NODE, CAROL, pack, {}, 3600, now) == [(ATT_A, "kill by this owner: refetch"), (ATT_B, "kill by this owner: refetch")]
    # an arrival follows the flagged entries, never precedes them
    assert previews.due(NODE, CAROL, with_arrival, {}, 3600, now) == [(ATT_A, "kill by this owner: refetch"), (ATT_B, "kill by this owner: refetch"), (360, "arrival")]
    assert previews.next_due_at(NODE, CAROL, 3600) == 0.0


def test_t2_expire_lifts_drops_a_lift_past_its_hold_and_leaves_a_live_one(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _book(e)
    _tick(e)
    previews = e.keeper.previews
    at = e.clock["now"]
    previews.lift_owner(NODE, CAROL, OWNER, 0.025, _source(at=at))
    assert previews.expire_lifts(NODE, CAROL, at + LIFT_HOLD_SEC - 1) == []
    assert _entries(e)[ATT_A].lift_ratio == 0.025
    assert previews.expire_lifts(NODE, CAROL, at + LIFT_HOLD_SEC) == [ATT_A, ATT_B]
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].lift_ratio == 0.0 and _entries(e)[a].lift_from == {} and _entries(e)[a].lift_until is None
        assert _entries(e)[a].clamped(SECOND) == THR
    assert previews.expire_lifts(NODE, CAROL, at + 2 * LIFT_HOLD_SEC) == []  # nothing left to expire


# ------------------------------------------------------------ 3. the replay


def _replay(make_keeper, tmp_path) -> tuple[Env, Any, FakeChain]:
    """Pass N: the three kamis harvest at 114 / 116 / 130 against a served
    108 and a bound of 111, so none of them rests. Pass N+1: attacker A of
    owner O liquidates the one at 114."""
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    book = _book(e)
    _tick(e)
    s = _last_tick(e, book)
    assert sorted(_preview_reads(e)) == [ATT_A, ATT_B] and s["previewCalls"] == 2
    strat: Auto = e.keeper.strategies[book.id]
    assert sorted(strat._last_views) == sorted(KAMIS) and strat._last_views[VICTIM].hp_current == 114
    lim = strat.bounds(*_views(e, book))[0]
    assert [lim[k]["bound"] for k in KAMIS] == [BOUND, BOUND, BOUND]
    assert _decisions(e, book, "rest") == [] and "liftedAttackers" not in s and "liftMaxPct" not in s
    _kill_on_chain(e, chain, VICTIM, ATT_A, "0x" + "ab" * 32)
    return e, book, chain


def test_t3_the_kill_row_carries_the_measurement_and_the_owners_lines_rise_by_it(make_keeper, tmp_path):
    e, book, _chain = _replay(make_keeper, tmp_path)
    _tick(e)
    kills = _kill_rows(e, book)
    assert len(kills) == 1 and kills[0]["victimKami"] == VICTIM and kills[0]["attackerKami"] == ATT_A
    assert kills[0]["learned"] == {"hpAtKill": 114, "served": THR, "gapHp": 6, "liftPct": LIFT_PCT, "lifted": [ATT_A, ATT_B], "owner": OWNER}
    # every line of the OWNER rose by ceil(0.025 x the row's own max HP)
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].lift_ratio == 0.025
        assert [_entries(e)[a].clamped(k) for k in (SECOND, THIRD)] == [LIFTED_THR, LIFTED_THR]
    # the measurement itself, on the killer's entry and on its sibling's
    assert _entries(e)[ATT_A].lift_from["victim"] == VICTIM and _entries(e)[ATT_A].lift_from["hpAtKill"] == 114
    assert _entries(e)[ATT_B].lift_from["viaAttacker"] == ATT_A
    # and the log line says what was measured
    assert any(f"measured: victim HP 114 vs served {THR} (+6) -> lift {LIFT_PCT} % on 2 attacker(s) of owner {OWNER}" in ln for ln in _lines(e))


def test_t3_the_same_pass_rests_the_kami_the_lifted_line_now_reaches_and_not_the_one_above_it(make_keeper, tmp_path):
    e, book, _chain = _replay(make_keeper, tmp_path)
    _tick(e)
    rests = _decisions(e, book, "rest")
    assert [r["parameters"]["kamis"] for r in rests] == [[SECOND]]  # 116 <= 118; the third reads 130
    detail = rests[0]["parameters"]["detail"][0]
    assert detail["bound"] == LIFTED_BOUND and detail["hp"] == 116
    assert detail["boundBy"] == {"attackerKami": ATT_A, "ownerClass": "killed-here-48h", "threshold": LIFTED_THR, "present": True, "hostileBy": "both", "liftPct": LIFT_PCT}
    s = _last_tick(e, book)
    assert s["liftedAttackers"] == 2 and s["liftMaxPct"] == LIFT_PCT
    lim = e.keeper.strategies[book.id].bounds(*_views(e, book))[0]
    assert lim[SECOND]["bound"] == LIFTED_BOUND and lim[THIRD]["bound"] == LIFTED_BOUND


def test_t3_the_kill_refetches_the_owners_present_lines_in_the_same_pass(make_keeper, tmp_path):
    e, book, _chain = _replay(make_keeper, tmp_path)
    _tick(e)
    # the hourly refresh is nowhere near due, so these two reads are the
    # kill's own refetch — the lens most likely serves the right lines
    # inside the item's window, and the measured lift is the floor
    assert sorted(_preview_reads(e)) == [ATT_A, ATT_B] and _last_tick(e, book)["previewCalls"] == 2
    assert _kami_reads(e) == []  # the previews of the pass served the cooldown
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].refetch is False and _entries(e)[a].lift_ratio == 0.025  # the flag is answered, the lift is kept
    _tick(e)
    assert _preview_reads(e) == [] and _entries(e)[ATT_A].lift_ratio == 0.025


# ------------------------------------------------------------ 4. a late rest


def test_t4_a_kill_at_or_below_the_served_line_measures_nothing_and_still_refetches(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _set_party(e.lens, VICTIM, state=HARVESTING, node=NODE, hp=(THR, MAXHP))  # at the line, not above it
    book = _book(e)
    _tick(e)
    _kill_on_chain(e, chain, VICTIM, ATT_A, "0x" + "ba" * 32)
    _tick(e)
    assert _kill_rows(e, book)[0]["learned"] == {"hpAtKill": THR, "served": THR, "gapHp": 0, "lift": None, "note": "at or below the served line: a late rest"}
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].lift_ratio == 0.0 and _entries(e)[a].lift_from == {} and _entries(e)[a].lift_until is None
        assert _entries(e)[a].clamped(SECOND) == THR
    s = _last_tick(e, book)
    assert "liftedAttackers" not in s and "liftMaxPct" not in s
    assert all("liftPct" not in (row.get("boundBy") or {}) for d in _decisions(e, book, "rest") for row in d["parameters"]["detail"])
    # a kill by the owner is reason enough to re-read its lines
    assert sorted(_preview_reads(e)) == [ATT_A, ATT_B]
    assert any(f"measured: victim HP {THR} vs served {THR} (0): a late rest" in ln for ln in _lines(e))


# ------------------------------------------------------------ 5. the skips


def test_t5_an_unattributed_kill_of_ours_is_skipped_and_nothing_is_lifted(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    book = _book(e)
    _tick(e)
    _kill_on_chain(e, chain, VICTIM, ATT_A, "0x" + "ac" * 32, form="other")  # an unknown selector
    _tick(e)
    k = _kill_rows(e, book)[0]
    assert k["attackerAccount"] is None and k["learned"] == {"skipped": "unattributed"}
    assert all(_entries(e)[a].lift_ratio == 0.0 and _entries(e)[a].refetch is False for a in (ATT_A, ATT_B))
    assert any("measured: skipped (unattributed)" in ln for ln in _lines(e))


def test_t5_a_kill_of_another_accounts_kami_carries_no_measurement(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 7777, 4444, hp=(30, 200))  # another account's kami on the book's node
    book = _book(e)
    _tick(e)
    _kill_on_chain(e, chain, 7777, ATT_A, "0x" + "ad" * 32)
    _tick(e)
    k = _kill_rows(e, book)[0]
    assert k["victimAccount"] == 4444 and k["learned"] is None
    assert all(_entries(e)[a].lift_ratio == 0.0 and _entries(e)[a].refetch is False for a in (ATT_A, ATT_B))
    assert not any("measured:" in ln for ln in _lines(e))


def test_t5_a_victim_the_book_held_no_view_of_is_skipped(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))  # the book's account, outside the book
    book = _book(e)
    _tick(e)
    _kill_on_chain(e, chain, 5946, ATT_A, "0x" + "ae" * 32)
    _tick(e)
    assert _kill_rows(e, book)[0]["learned"] == {"skipped": "no view of the victim from the previous pass"}
    assert all(_entries(e)[a].lift_ratio == 0.0 for a in (ATT_A, ATT_B))


def test_t5_an_attacker_with_no_served_line_lifts_nothing_and_is_previewed_as_an_arrival_that_pass(make_keeper, tmp_path):
    chain = FakeChain(head=12_278_391)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 360, 806, hp=(250, 300), level=39)  # another owner: not standing, never previewed
    _serve_kami(e.lens, 360)
    book = _book(e)
    _tick(e)
    assert 360 not in _entries(e)
    _kill_on_chain(e, chain, VICTIM, 360, "0x" + "af" * 32)
    _tick(e)
    assert _kill_rows(e, book)[0]["learned"] == {"skipped": "no served line for the pair"}
    assert all(_entries(e)[a].lift_ratio == 0.0 for a in (ATT_A, ATT_B))  # it is not the pack's owner either way
    # the kill made that owner killed-here, so its present attackers are ARRIVALS
    # and previewed this very pass — on a FRESH lens read, with no lift
    assert 360 in _preview_reads(e) and _entries(e)[360].lift_ratio == 0.0 and _entries(e)[360].lift_from == {}
    assert any("measured: skipped (no served line for the pair)" in ln for ln in _lines(e))


# ------------------------------------------ 6. growth, persistence, the hold


def test_t6_a_second_kill_above_the_lifted_line_lifts_again_from_the_served_threshold(make_keeper, tmp_path):
    e, book, chain = _replay(make_keeper, tmp_path)
    _tick(e)
    assert _entries(e)[ATT_A].lift_ratio == 0.025
    # the third kami reads 130 — above the lifted 115 as well — and dies
    # there: the gap is measured from the SERVED 108 again, not from 115
    _kill_on_chain(e, chain, THIRD, ATT_B, "0x" + "bb" * 32)
    at = e.clock["now"]
    _tick(e)
    learned = _kill_rows(e, book)[-1]["learned"]
    assert learned == {"hpAtKill": 130, "served": THR, "gapHp": 22, "liftPct": 8.3, "lifted": [ATT_A, ATT_B], "owner": OWNER}  # ceil(23 / 280 x 1000) = 83
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].lift_ratio == 0.083 and _entries(e)[a].clamped(SECOND) == THR + 24  # ceil(0.083 x 280) = 23.24 -> 24
        assert _entries(e)[a].lift_until == at + 30.0 + LIFT_HOLD_SEC  # the hold restarts on the renewed measurement
    assert _entries(e)[ATT_B].lift_from["victim"] == THIRD and "viaAttacker" not in _entries(e)[ATT_B].lift_from
    assert _entries(e)[ATT_A].lift_from["viaAttacker"] == ATT_B
    assert _last_tick(e, book)["liftMaxPct"] == 8.3


def test_t6_the_lift_survives_a_refresh_a_restart_and_is_read_back_by_an_old_reader(make_keeper, tmp_path):
    e, book, _chain = _replay(make_keeper, tmp_path)
    _tick(e)
    at = e.clock["now"]
    assert _entries(e)[ATT_A].lift_until == at + LIFT_HOLD_SEC
    # an hourly refresh re-reads the same served numbers and keeps the lift
    _tick(e, dt=3600.0)
    assert sorted(_preview_reads(e)) == [ATT_A, ATT_B]
    assert _entries(e)[ATT_A].rows[SECOND]["threshold"] == THR and _entries(e)[ATT_A].clamped(SECOND) == LIFTED_THR
    assert _entries(e)[ATT_A].lift_until == at + LIFT_HOLD_SEC  # a refresh is not a renewal
    # it is on disk, and a second keeper on the same dataDir reads it back
    doc = json.loads((e.data_dir / "previews" / f"{NODE}-{CAROL}.json").read_text())
    saved = doc["entries"][str(ATT_A)]
    assert saved["liftRatio"] == 0.025 and saved["liftFrom"]["gapHp"] == 6 and saved["liftUntil"] == at + LIFT_HOLD_SEC and saved["refetch"] is False
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 8, "requestTimeoutSec": 5}, env={"KAMI_KEEPER_STANDING_OWNERS": str(OWNER)})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=_chain)
    k2.clock = lambda: e.clock["now"]
    k2.boot()
    e.keeper.stop_engine()
    e.keeper = k2
    assert k2.handle("grant", {"account": "lab"})["ok"]
    assert _entries(e)[ATT_A].lift_ratio == 0.025 and _entries(e)[ATT_A].lift_until == at + LIFT_HOLD_SEC
    assert _entries(e)[ATT_A].clamped(SECOND) == LIFTED_THR
    b2 = k2.store.get(book.id)
    _tick(e)
    assert _last_tick(e, b2)["liftMaxPct"] == LIFT_PCT
    # an older reader loads the same file: it knows none of the four keys,
    # so it loads the served line and nothing else — and is not refused
    old = {kk: vv for kk, vv in saved.items() if kk not in ("liftRatio", "liftFrom", "liftUntil", "refetch")}
    assert PreviewEntry.from_dict(old).clamped(SECOND) == THR


def test_t6_the_lift_expires_at_its_hold_and_a_new_kill_renews_it(make_keeper, tmp_path):
    e, book, chain = _replay(make_keeper, tmp_path)
    _tick(e)
    at = e.clock["now"]
    # a tick before the hold passes: still lifted, nothing expired
    _tick(e, dt=LIFT_HOLD_SEC - 60.0)
    s = _last_tick(e, book)
    assert "liftsExpired" not in s and s["liftMaxPct"] == LIFT_PCT and _entries(e)[ATT_A].clamped(SECOND) == LIFTED_THR
    # past it: the pack is back on the number the lens serves
    _tick(e, dt=120.0)
    s = _last_tick(e, book)
    assert s["liftsExpired"] == [ATT_A, ATT_B]
    assert "liftedAttackers" not in s and "liftMaxPct" not in s
    for a in (ATT_A, ATT_B):
        assert _entries(e)[a].lift_ratio == 0.0 and _entries(e)[a].lift_until is None and _entries(e)[a].clamped(SECOND) == THR
    assert e.keeper.strategies[book.id].bounds(*_views(e, book))[0][SECOND]["bound"] == BOUND
    # a new kill measures again and the hold starts over
    _kill_on_chain(e, chain, SECOND, ATT_A, "0x" + "bc" * 32)
    before = e.clock["now"]
    _tick(e)
    assert _kill_rows(e, book)[-1]["learned"] == {"hpAtKill": 116, "served": THR, "gapHp": 8, "liftPct": 3.3, "lifted": [ATT_A, ATT_B], "owner": OWNER}  # ceil(9 / 280 x 1000) = 33
    assert _entries(e)[ATT_A].lift_until == before + 30.0 + LIFT_HOLD_SEC
    assert at + LIFT_HOLD_SEC < _entries(e)[ATT_A].lift_until


# ------------------------------------------------------------ 7. scrub


def test_t7_every_formatted_surface_of_the_measured_line_is_clean(make_keeper, tmp_path):
    e, book, chain = _replay(make_keeper, tmp_path)
    _tick(e)
    _kill_on_chain(e, chain, THIRD, ATT_B, "0x" + "bd" * 32)
    _tick(e)
    _kill_on_chain(e, chain, SECOND, ATT_A, "0x" + "be" * 32, form="other")
    _tick(e)
    _tick(e, dt=LIFT_HOLD_SEC + 60.0)
    frames = [
        e.keeper.handle("status"),
        e.keeper.handle("status", {"book": book.id}),
        e.keeper.handle("books", {"account": "lab"}),
        e.keeper.handle("logs"),
        e.keeper.handle("ledger", {"book": book.id}),
        e.keeper.handle("ledger", {"book": book.id, "kind": "kill"}),
        e.keeper.handle("describe", {"type": "auto"}),
    ]
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    # the measurement reached the rows and the log, and says so in words
    learned = [k["learned"] for k in _kill_rows(e, book)]
    assert any(isinstance(l, dict) and "liftPct" in l for l in learned) and any(isinstance(l, dict) and l.get("skipped") for l in learned)
    assert any("measured:" in ln for ln in _lines(e))
    assert forbidden_in({"lines": _lines(e)}) == []
