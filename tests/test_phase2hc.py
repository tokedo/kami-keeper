"""Standing kamis:

  1. the keeper-level key `standingKamis` — file, env, ints, duplicates,
     the one error naming the key, the boot log line;
  2. through the book — a standing kami that is NOT on the node is read
     (`kami`, then `node <i> <kami>`) on the first pass under
     `previewPolicy killed-here` with an EMPTY owner ledger, is a synthetic
     occupant (`standingKamisAbsent`), renders `standing`, binds with
     `safetyMarginPct` and rests the kami;
  3. while absent it is refreshed at `previewRefreshSec` — also when every
     line it serves is 0 (never the 86400-s no-threshold interval) — and
     never remembered, dropped or expired;
  4. the control — the same world without the key previews nobody;
  5. `listed` wins — a standing kami under a listed owner binds nothing;
  6. present on the node it is an ordinary occupant, classed standing;
  7. a kill by it renders `killed-here-<window>` (precedence) and it keeps
     binding past the window as `standing`;
  8. scrub — every formatted surface is clean.
  9. the lab's round 1 on 1.1.0 — the `kami` read refused, the cooldown it
     serves, the tick's read count and the cap it shares, two books on
     one node, the answers the tick rests on, the status's next refresh,
     and an index the lens does not know (it holds the book).
 10. the lab's round 2 on 1.1.0 — the count is the tick's own node's, two
     absent standing kamis (each read, each counted, each a unit of the
     cap), and the schema defaults (`previewPolicy all`, `nodeCensus
     vitals`): the kami is previewed in its turn, the book holds until
     then, and the cooldown is read off the synthetic row.
"""

from __future__ import annotations

from typing import Any

from conftest import Env
from keeper.config import load as load_config
from keeper.core import Keeper, _iso
from keeper.kills import BLOCKS_PER_HOUR
from keeper.lens import LensClient
from keeper.owner_class import OwnerClass, is_observation_name, render
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.auto import Auto
from test_phase2a import CAROL, FRIENDS, NODE, T, _add_occupant, _decisions, _preview_threshold, _rows, _seed, _set_party, _start
from test_phase2g import LAZY, _preview_reads, _serve_kami
from test_phase2h import _kill

H = BLOCKS_PER_HOUR
ABSENT = 900002  # the standing kami: an index no mint reaches, and the recorded node never shows it
OWNER = 2533
THR = 260  # against the test kami (268 / 270): 260 + 5 % x 270 = 274 rests it
BOUND = 274
KAMIS = [66655, 68017]


def _env(make_keeper, standing: tuple[int, ...] = (ABSENT,), owner: int = OWNER, cooldown: int = 0) -> Env:
    env = {"KAMI_KEEPER_STANDING_KAMIS": ",".join(str(k) for k in standing)} if standing else {}
    e = make_keeper(cap=8, env=env)
    assert e.keeper.cfg.standingKamis == list(standing)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    # the standing kami exists in the mirror, resting on no node, under `owner`
    e.lens.set_kami(ABSENT, state="RESTING", node=None, hp=(100, 100), account=owner)
    e.lens.kamis[ABSENT]["level"] = 56
    e.lens.kamis[ABSENT]["cooldownSec"] = cooldown
    _preview_threshold(e.lens, ABSENT, 68017, THR)
    assert e.keeper.handle("grant", {"account": "lab"})["ok"]
    return e


def _book(e: Env, **over: Any):
    return _start(e, KAMIS, **dict(LAZY, **over))


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_book(book, now=e.clock["now"])


def _kami_reads(e: Env) -> list[int]:
    return [int(r["args"][0]) for r in e.lens.requests if r["query"] == "kami"]


def _rows_of(e: Env, book, kami: int = 68017) -> tuple[Auto, dict[str, Any], dict[str, Any]]:
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    lim, _classes, attackers, _s = strat.bounds(obs, {k: v for k, v in obs.kamis.items() if not v.missing})
    return strat, lim[kami], {a["kami"]: a for a in attackers}


# ----------------------------------------------------------- the config key


def test_t1_standing_kamis_loads_as_kami_indices_from_file_and_env(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text("standingKamis: [900002, 900002, 900001]\nstandingOwners: [2481]\nmaxBooksPerAccount: 2\n", encoding="utf-8")
    cfg = load_config(flags={}, env={}, config_file=str(f))
    assert cfg.standingKamis == [900002, 900001] and all(type(k) is int for k in cfg.standingKamis)
    assert cfg.standingOwners == [2481] and cfg.sources["standingKamis"] == "file" and cfg.errors == []
    env = load_config(flags={"maxBooksPerAccount": 2}, env={"KAMI_KEEPER_STANDING_KAMIS": "900002,900001"})
    assert env.standingKamis == [900002, 900001] and env.sources["standingKamis"] == "env"
    none = load_config(flags={"maxBooksPerAccount": 2}, env={})
    assert none.standingKamis == [] and none.sources["standingKamis"] == "default" and none.errors == []
    bad = load_config(flags={"maxBooksPerAccount": 2}, env={"KAMI_KEEPER_STANDING_KAMIS": "900002,x,-1"})
    assert bad.standingKamis == [900002]
    assert bad.errors == ["standingKamis: expected kami indices (non-negative integers)"]


def test_t1_a_bad_value_is_one_error_naming_its_own_key_and_never_the_other_list(tmp_path):
    """The two lists share one parser: each bad list reports under its own
    key, and a bad list of one kind leaves the other's check untouched."""
    kamis_only = load_config(flags={"maxBooksPerAccount": 2}, env={"KAMI_KEEPER_STANDING_KAMIS": "900001,-3,name", "KAMI_KEEPER_STANDING_OWNERS": "2481"})
    assert kamis_only.standingKamis == [900001] and kamis_only.standingOwners == [2481]
    assert kamis_only.errors == ["standingKamis: expected kami indices (non-negative integers)"]
    owners_only = load_config(flags={"maxBooksPerAccount": 2}, env={"KAMI_KEEPER_STANDING_KAMIS": "900001", "KAMI_KEEPER_STANDING_OWNERS": "2481,x"})
    assert owners_only.standingKamis == [900001] and owners_only.standingOwners == [2481]
    assert owners_only.errors == ["standingOwners: expected account indices (non-negative integers)"]
    f = tmp_path / "k.yaml"
    f.write_text("standingKamis: [900001, oops]\nstandingOwners: [-1, 2481]\nmaxBooksPerAccount: 2\n", encoding="utf-8")
    both = load_config(flags={}, env={}, config_file=str(f))
    assert sorted(both.errors) == ["standingKamis: expected kami indices (non-negative integers)", "standingOwners: expected account indices (non-negative integers)"]
    assert both.standingKamis == [900001] and both.standingOwners == [2481]


def test_t1_the_boot_log_names_the_list(make_keeper):
    e = _env(make_keeper, standing=(ABSENT, 900001))
    lines = [ln["line"] for ln in e.keeper.handle("logs")["data"]["lines"]]
    assert "standing kamis: [900001, 900002]" in lines and forbidden_in({"lines": lines}) == []


# ------------------------------------------------------- through the book


def test_t2_an_absent_standing_kami_is_read_previewed_classed_standing_and_binds_at_the_safety_margin(make_keeper):
    e = _env(make_keeper)
    book = _book(e)
    assert e.keeper.owners.killed_accounts == {} and e.keeper.owners.hostile_kamis == {}
    assert not any(h["kami"]["index"] == ABSENT for h in e.lens.nodes[NODE]["harvests"])  # not on the node
    s = _tick(e, book)
    # one `kami` read (owner, level, cooldown) and one preview of it, first pass
    assert _kami_reads(e) == [ABSENT] and _preview_reads(e) == [ABSENT] and s["previewCalls"] == 1
    assert s["standingKamisAbsent"] == [ABSENT]
    # the owner summary counts its owner once, as standing; the node's three foreign owners (no kill) stay unread
    assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 0, "standing": 1, "unpreviewed": 3}
    assert s["boundKamisBy"] == {"standing/standing": 2} and s["hostileKamis"] == 0 and s["remembered"] == []
    ent = e.keeper.previews.entries(NODE, CAROL)[ABSENT]
    assert ent.attacker_account == OWNER and ent.attacker_level == 56 and not ent.remembered
    strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["class"] is OwnerClass.STANDING and row[ABSENT]["hostileBy"] == "standing"
    assert row[ABSENT]["present"] is True and row[ABSENT]["owner"] == OWNER
    assert render(row[ABSENT]["class"], strat.window_hours) == "standing"
    # the OWNER-level class (a kill row's `ownerClassAfter`) is the owner's own: it has lines and no kill — the KAMI is standing, not its owner
    assert strat.render_owner_class(OWNER) == "threshold-no-kill" and not strat.owner_binds_now(OWNER)
    assert lim["bound"] == BOUND
    assert lim["boundBy"] == {"attackerKami": ABSENT, "ownerClass": "standing", "threshold": THR, "present": True, "hostileBy": "standing"}
    rests = _decisions(e, book, "rest")
    assert [r["parameters"]["kamis"] for r in rests] == [[68017]]
    assert rests[0]["parameters"]["detail"][0]["bound"] == BOUND and rests[0]["parameters"]["detail"][0]["boundBy"]["ownerClass"] == "standing"


def test_t2_a_kami_read_that_answers_without_an_owner_still_previews_and_binds(make_keeper):
    """The read ANSWERS, with no account on it: the synthetic occupant has
    no owner and binds all the same (the refused read is its own test
    below)."""
    e = _env(make_keeper)
    del e.lens.kamis[ABSENT]  # the mirror has no `kami 900002` answer …
    e.lens.set_kami(ABSENT, state="RESTING", node=None, hp=(100, 100))  # … but serves its preview (any kami of the mirror)
    e.lens.kamis[ABSENT].pop("account", None)
    book = _book(e)
    s = _tick(e, book)
    assert _preview_reads(e) == [ABSENT] and s["boundKamisBy"] == {"standing/standing": 2}
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["owner"] is None and row[ABSENT]["class"] is OwnerClass.STANDING and lim["bound"] == BOUND


# ------------------------------------------------ refreshed while absent


def test_t3_an_absent_standing_kami_is_refreshed_at_preview_refresh_sec_and_never_remembered(make_keeper):
    e = _env(make_keeper)
    book = _book(e, previewRefreshSec=3600)
    _tick(e, book)
    s = _tick(e, book)  # 30 s later: nothing due, nothing remembered or dropped
    assert _preview_reads(e) == [] and s["previewCalls"] == 0
    assert "previewsRemembered" not in s and "previewsDropped" not in s and "previewsExpired" not in s
    s = _tick(e, book, dt=3600.0)
    assert _preview_reads(e) == [ABSENT] and s["previewCalls"] == 1 and s["standingKamisAbsent"] == [ABSENT]
    # 49 h of absence: still present (synthetic), still binding, never expired
    s = _tick(e, book, dt=49 * 3600.0)
    assert "previewsExpired" not in s and _rows_of(e, book)[1]["bound"] == BOUND
    assert not e.keeper.previews.entries(NODE, CAROL)[ABSENT].remembered


def test_t3_a_standing_kami_whose_lines_are_all_zero_is_still_refreshed_within_the_hour(make_keeper):
    e = _env(make_keeper)
    _preview_threshold(e.lens, ABSENT, 68017, 0)
    book = _book(e, previewRefreshSec=3600)
    _tick(e, book)
    assert e.keeper.previews.entries(NODE, CAROL)[ABSENT].max_clamped() == 0
    s = _tick(e, book, dt=3601.0)
    assert _preview_reads(e) == [ABSENT] and s["previewCalls"] == 1  # not 86400 s
    # the line comes back (the kami is alive again / off its cooldown): bound on the next refresh
    _preview_threshold(e.lens, ABSENT, 68017, THR)
    _tick(e, book, dt=3601.0)
    assert _rows_of(e, book)[1]["bound"] == BOUND


# ------------------------------------------------------------- control


def test_t4_the_same_world_without_the_key_reads_nobody_and_binds_nothing(make_keeper):
    e = _env(make_keeper, standing=())
    book = _book(e)
    s = _tick(e, book)
    assert _kami_reads(e) == [] and _preview_reads(e) == [] and s["previewCalls"] == 0 and "standingKamisAbsent" not in s
    _strat, lim, row = _rows_of(e, book)
    assert ABSENT not in row and lim["bound"] == 0 and _decisions(e, book, "rest") == []


# -------------------------------------------------------- listed wins


def test_t5_a_standing_kami_under_a_listed_owner_binds_nothing(make_keeper):
    e = _env(make_keeper, owner=FRIENDS[0])
    book = _book(e)
    s = _tick(e, book)
    assert _kami_reads(e) == [ABSENT] and _preview_reads(e) == [] and s["previewCalls"] == 0
    _strat, lim, row = _rows_of(e, book)
    assert ABSENT not in row and lim["bound"] == 0


# ---------------------------------------------------- present on the node


def test_t6_present_on_the_node_it_is_an_ordinary_occupant_classed_standing(make_keeper):
    e = _env(make_keeper)
    _add_occupant(e.lens, ABSENT, OWNER, hp=(100, 100), level=56)
    _serve_kami(e.lens, ABSENT)
    book = _book(e)
    s = _tick(e, book)
    assert "standingKamisAbsent" not in s and _preview_reads(e) == [ABSENT]
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["present"] is True and row[ABSENT]["class"] is OwnerClass.STANDING and lim["bound"] == BOUND
    # it leaves: synthetic again on the next pass, never remembered
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != ABSENT]
    s = _tick(e, book)
    assert s["standingKamisAbsent"] == [ABSENT] and "previewsRemembered" not in s and _rows_of(e, book)[1]["bound"] == BOUND


# ---------------------------------------------------------- precedence


def test_t7_a_kill_by_the_standing_kami_renders_killed_here_and_it_binds_past_the_window(make_keeper):
    e = _env(make_keeper)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    book = _book(e)
    _tick(e, book)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0xaa", OWNER, ABSENT, node=NODE, victim=5946))
    _tick(e, book)
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["class"] is OwnerClass.HOSTILE and row[ABSENT]["hostileBy"] == "both" and lim["bound"] == BOUND
    assert strat.render_owner_class(OWNER) == "killed-here-48h"
    e.lens.advance(49 * H)
    _tick(e, book)
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["class"] is OwnerClass.STANDING and row[ABSENT]["hostileBy"] == "standing" and lim["bound"] == BOUND


# ---------------------------------------------------------------- scrub


def test_t8_every_formatted_surface_is_clean(make_keeper):
    e = _env(make_keeper)
    book = _book(e)
    _tick(e, book)
    _tick(e, book, dt=3600.0)
    frames = [
        e.keeper.handle("status"),
        e.keeper.handle("status", {"book": book.id}),
        e.keeper.handle("books", {"account": "lab"}),
        e.keeper.handle("logs"),
        e.keeper.handle("ledger", {"book": book.id}),
        e.keeper.handle("describe", {"type": "auto"}),
    ]
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    names = {row["boundBy"]["ownerClass"] for d in _decisions(e, book) for row in (d.get("parameters") or {}).get("detail") or [] if row.get("boundBy")}
    assert names == {"standing"} and all(is_observation_name(n) for n in names)
    # a restart with the same list resumes the book and binds from the persisted entry at once
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 8, "requestTimeoutSec": 5}, env={"KAMI_KEEPER_STANDING_KAMIS": str(ABSENT)})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k.clock = lambda: e.clock["now"]
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    e.keeper.stop_engine()
    e.keeper = k
    book2 = e.keeper.store.get(book.id)
    _tick(e, book2)
    assert _rows_of(e, book2)[1]["bound"] == BOUND


# ------------------------------------------- what the lab's round 1 asked for

SECOND = 900001  # a second standing kami, put ON the node by the test that uses it
UNKNOWN = 900003  # an index the mirror does not know at all


def _refuse_kami_read(e: Env, kami: int) -> None:
    """The lens answers an error frame to `kami <kami>` and serves every
    other `kami` read (and every preview) as before."""

    def hook(args: list[str]) -> dict[str, Any]:
        idx = int(args[0])
        if idx == kami or idx not in e.lens.kamis:
            return {"__error__": "INCOMPLETE", "message": f"kami {idx} cannot be projected completely: missing joins"}
        return dict(e.lens.kamis[idx])

    e.lens.hooks["kami"] = hook


def _status_row(e: Env, book) -> dict[str, Any]:
    return next(b for b in e.keeper.handle("status", {"book": book.id})["data"]["books"] if b["id"] == book.id)


def test_t2_the_lens_refusing_the_kami_read_never_refuses_the_tick_and_the_kami_binds(make_keeper):
    """The read that names its owner, level and cooldown is REFUSED (an
    error frame) while its preview is served: the tick runs, the synthetic
    occupant has no owner and cooldown 0, it is classed standing and binds."""
    e = _env(make_keeper)
    _refuse_kami_read(e, ABSENT)
    book = _book(e)
    s = _tick(e, book)
    assert "error" not in s and _kami_reads(e) == [ABSENT] and _preview_reads(e) == [ABSENT]
    assert s["standingKamisAbsent"] == [ABSENT] and s["boundKamisBy"] == {"standing/standing": 2}
    ent = e.keeper.previews.entries(NODE, CAROL)[ABSENT]
    assert ent.attacker_account is None
    e.lens.kamis[ABSENT]["cooldownSec"] = 3000  # a cooldown the keeper never gets to read: unknown is not a reason to skip
    s = _tick(e, book)  # the pass after: no preview answer, the read refused again
    assert _kami_reads(e) == [ABSENT] and _preview_reads(e) == [] and s["attackerVitalsUnread"] == [ABSENT] and s["cooldownSkipped"] == []
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["owner"] is None and row[ABSENT]["cooldownSec"] == 0
    assert row[ABSENT]["class"] is OwnerClass.STANDING and row[ABSENT]["hostileBy"] == "standing" and lim["bound"] == BOUND


def test_t2_the_cooldown_the_kami_read_serves_decides_whether_the_absent_kami_binds(make_keeper):
    """On a pass without a preview answer (every pass but the refresh) the
    absent kami's cooldown is the one its `kami` read served: above
    `cooldownHorizonSec` it is skipped for the tick, below it binds."""
    e = _env(make_keeper, cooldown=1200)
    book = _book(e, cooldownHorizonSec=600)
    _tick(e, book)
    s = _tick(e, book)
    assert _preview_reads(e) == [] and _kami_reads(e) == [ABSENT]
    assert s["cooldownSkipped"] == [ABSENT] and "attackerVitalsUnread" not in s
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["cooldownSec"] == 1200 and lim["bound"] == 0 and lim["boundBy"] is None
    e.lens.kamis[ABSENT]["cooldownSec"] = 300
    s = _tick(e, book)
    assert _preview_reads(e) == [] and s["cooldownSkipped"] == []
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["cooldownSec"] == 300 and lim["bound"] == BOUND


def test_t2_the_ticks_vitals_count_includes_the_standing_read_of_every_pass(make_keeper):
    """`attackerVitalsCalls` is the `kami <index>` reads the tick made: the
    standing read is one of them — on EVERY pass the kami is absent (its
    cooldown is read per pass), and none in the world without the key."""
    e = _env(make_keeper)
    book = _book(e)
    for _pass in range(3):
        s = _tick(e, book)
        assert _kami_reads(e) == [ABSENT] and s["attackerVitalsCalls"] == 1
        assert "attackerVitalsOverCap" not in s and "attackerVitalsUnread" not in s
    c = _env(make_keeper, standing=())
    s = _tick(c, _book(c))
    assert _kami_reads(c) == [] and s["attackerVitalsCalls"] == 0


def test_t2_two_books_on_one_node_share_the_read_and_only_the_book_that_made_it_counts_it(make_keeper):
    """One `kami` read per pass and node: the second book of the pass is
    served the first one's row and its tick counts no read."""
    e = _env(make_keeper)
    first, second = _start(e, KAMIS[:1], **LAZY), _start(e, KAMIS[1:], **LAZY)
    e.clock["now"] += 30.0
    e.lens.requests.clear()
    assert sorted(e.keeper.tick_due(now=e.clock["now"])) == sorted([first.id, second.id])
    assert _kami_reads(e) == [ABSENT]
    counts = [_rows(e, b, "tick")[-1]["summary"]["attackerVitalsCalls"] for b in (first, second)]
    assert sorted(counts) == [0, 1]
    assert all(_rows(e, b, "tick")[-1]["summary"]["standingKamisAbsent"] == [ABSENT] for b in (first, second))


def test_t2_the_standing_read_and_the_attackers_reads_share_one_cap(make_keeper):
    """`attackerVitalsPerTick` is one budget per pass and node: the read of
    the absent standing kami takes a unit of it, so with a cap of 1 a
    present attacker's read is over the cap; with room for both, both are
    read and both are counted."""
    e = _env(make_keeper, standing=(SECOND, ABSENT))
    _add_occupant(e.lens, SECOND, OWNER, hp=(100, 100), level=56)
    _serve_kami(e.lens, SECOND)
    book = _book(e, attackerVitalsPerTick=1)
    s = _tick(e, book)  # SECOND's cooldown comes with its preview answer: the one read is the absent kami's
    assert _kami_reads(e) == [ABSENT] and sorted(_preview_reads(e)) == sorted([SECOND, ABSENT]) and s["attackerVitalsCalls"] == 1
    s = _tick(e, book)  # no preview answer this pass: SECOND needs a read and the budget is spent
    assert _kami_reads(e) == [ABSENT] and s["attackerVitalsCalls"] == 1 and s["attackerVitalsOverCap"] == [SECOND]
    wide = _env(make_keeper, standing=(SECOND, ABSENT))
    _add_occupant(wide.lens, SECOND, OWNER, hp=(100, 100), level=56)
    _serve_kami(wide.lens, SECOND)
    book = _book(wide, attackerVitalsPerTick=2)
    _tick(wide, book)
    s = _tick(wide, book)
    assert sorted(_kami_reads(wide)) == sorted([SECOND, ABSENT]) and s["attackerVitalsCalls"] == 2 and "attackerVitalsOverCap" not in s


def test_t2_the_kami_read_is_counted_and_is_one_of_the_answers_the_tick_rests_on(make_keeper):
    """Against the same world without the key the first pass makes exactly
    two reads more (the `kami` read and the preview), and the observation's
    basis is taken over the `kami` answer too: the lowest reconciled mark
    and the highest block of the answers (`Observation.latest_basis`)."""
    c = _env(make_keeper, standing=())
    plain = _tick(c, _book(c))
    e = _env(make_keeper)
    book = _book(e)
    s = _tick(e, book)
    assert _kami_reads(e) == [ABSENT] and _preview_reads(e) == [ABSENT]
    assert s["lensCalls"] == plain["lensCalls"] + 2
    served = e.lens.answer

    def answer(req: dict[str, Any]) -> dict[str, Any]:
        resp = served(req)
        if req.get("query") == "kami" and resp.get("ok"):
            resp["meta"]["blockNumber"] += 3
            resp["meta"]["reconciledThrough"] -= 7
        return resp

    e.lens.answer = answer  # type: ignore[method-assign]
    strat: Auto = e.keeper.strategies[book.id]
    e.keeper.census.begin_pass()  # a new pass: the read is made again
    e.lens.requests.clear()
    obs = strat.observe(e.keeper.lens)
    assert _kami_reads(e) == [ABSENT]
    assert obs.basis.lensBlock == e.lens.block + 3 and obs.basis.lensReconciledThrough == e.lens.reconciled_through() - 7


def test_t3_the_status_names_the_all_zero_standing_kamis_next_refresh_within_the_hour(make_keeper):
    """The book's only entry is an absent standing kami whose every line is
    0: `nextPreviewDueAt` is its fetch time + `previewRefreshSec`, never the
    86400-s no-threshold interval."""
    e = _env(make_keeper)
    _preview_threshold(e.lens, ABSENT, KAMIS[1], 0)
    book = _book(e, previewRefreshSec=3600)
    _tick(e, book)
    ents = e.keeper.previews.entries(NODE, CAROL)
    assert list(ents) == [ABSENT] and ents[ABSENT].max_clamped() == 0
    fetched = ents[ABSENT].fetched_at
    assert _status_row(e, book)["strategy"]["nextPreviewDueAt"] == _iso(fetched + 3600)
    assert _iso(fetched + 3600) != _iso(fetched + 86400)


def test_t2_an_index_the_lens_does_not_know_holds_every_kami_of_the_book_on_every_tick(make_keeper):
    """A listed index the lens cannot read is still an attacker of the
    node — an UNKNOWN one. The book holds its kamis (no start, no rest on
    the bound, no decision) on every tick until the index is read or leaves
    the list; the read and the preview are tried again each pass."""
    e = _env(make_keeper, standing=(UNKNOWN,))
    assert UNKNOWN not in e.lens.kamis
    book = _book(e)
    for _pass in range(3):
        s = _tick(e, book)
        assert _kami_reads(e) == [UNKNOWN] and _preview_reads(e) == [UNKNOWN]
        assert s["standingKamisAbsent"] == [UNKNOWN] and s["previewsRefused"] == [UNKNOWN] and s["attackerVitalsUnread"] == [UNKNOWN]
        assert s["previewCalls"] == 0 and s["attackerVitalsCalls"] == 1
        assert s["kamis"]["held"] == len(KAMIS) and s["acted"] is False and s["decisions"] == []
        _strat, lim, row = _rows_of(e, book)
        assert lim["unknown"] == [UNKNOWN] and lim["bound"] == 0 and row[UNKNOWN]["class"] is OwnerClass.STANDING
    assert _decisions(e, book) == []
    # the floor is not a line of the unknown attacker: it still rests
    _set_party(e.lens, KAMIS[1], hp=(20, 270))
    s = _tick(e, book)
    rests = _decisions(e, book, "rest")
    assert s["decisions"] == ["rest"] and [r["parameters"]["kamis"] for r in rests] == [[KAMIS[1]]]
    assert rests[0]["parameters"]["detail"][0]["boundBy"] is None and rests[0]["parameters"]["detail"][0]["restFloor"] == 27


# ------------------------------------------- what the lab's round 2 asked for

THIRD = 900004  # a third standing kami, put ON the node by the test that uses it


def _absent(e: Env, kami: int, cooldown: int = 0) -> None:
    """One more standing kami the mirror knows, resting on no node, with a
    line against the test kami — what `_env` does for `ABSENT`."""
    e.lens.set_kami(kami, state="RESTING", node=None, hp=(100, 100), account=OWNER)
    e.lens.kamis[kami]["level"] = 56
    e.lens.kamis[kami]["cooldownSec"] = cooldown
    _preview_threshold(e.lens, kami, 68017, THR)


def test_t2_the_ticks_vitals_count_is_its_own_nodes_and_never_another_nodes_reads(make_keeper):
    """The pass's counters are per node: reads made on ANOTHER node earlier
    in the same pass are not in this tick's `attackerVitalsCalls`, and the
    standing read takes its unit of this node's counter only."""
    e = _env(make_keeper)
    book = _book(e)
    _tick(e, book)
    strat: Auto = e.keeper.strategies[book.id]
    other = NODE + 1
    e.keeper.census.begin_pass()
    e.keeper.census.vitals_calls[other] = 4  # another node's book read four attackers before this one's turn
    e.lens.requests.clear()
    obs = strat.observe(e.keeper.lens)
    assert _kami_reads(e) == [ABSENT] and obs.notes["attackerVitalsCalls"] == 1
    assert e.keeper.census.vitals_calls == {other: 4, NODE: 1}


def test_t2_two_absent_standing_kamis_are_each_read_each_counted_and_each_take_a_unit_of_the_cap(make_keeper):
    """Two absent standing kamis in one book: both are read on every pass,
    both are synthetic occupants classed standing, the tick counts 2 — and
    each read takes its OWN unit of `attackerVitalsPerTick`, so with a cap
    of 2 a present attacker's read on a pass without a preview answer is
    over the cap."""
    e = _env(make_keeper, standing=(SECOND, ABSENT))
    _absent(e, SECOND)
    book = _book(e)
    for _pass in range(3):
        s = _tick(e, book)
        assert sorted(_kami_reads(e)) == sorted([SECOND, ABSENT]) and s["attackerVitalsCalls"] == 2
        assert s["standingKamisAbsent"] == sorted([SECOND, ABSENT]) and s["boundKamisBy"] == {"standing/standing": 2}
        assert "attackerVitalsOverCap" not in s and "attackerVitalsUnread" not in s
    _strat, lim, row = _rows_of(e, book)
    assert all(row[k]["class"] is OwnerClass.STANDING and row[k]["hostileBy"] == "standing" for k in (SECOND, ABSENT)) and lim["bound"] == BOUND
    capped = _env(make_keeper, standing=(SECOND, ABSENT, THIRD))
    _absent(capped, SECOND)
    _add_occupant(capped.lens, THIRD, OWNER, hp=(100, 100), level=56)
    _serve_kami(capped.lens, THIRD)
    book = _book(capped, attackerVitalsPerTick=2)
    s = _tick(capped, book)  # THIRD's cooldown comes with its preview answer: the two reads are the absent kamis'
    assert sorted(_kami_reads(capped)) == sorted([SECOND, ABSENT]) and s["attackerVitalsCalls"] == 2
    s = _tick(capped, book)  # no preview answer this pass: THIRD needs a read and both units are spent
    assert sorted(_kami_reads(capped)) == sorted([SECOND, ABSENT]) and s["attackerVitalsCalls"] == 2 and s["attackerVitalsOverCap"] == [THIRD]


def test_t2_under_the_schema_defaults_a_refused_kami_read_still_holds_the_book_until_the_preview_and_then_binds(make_keeper):
    """A book started with the schema defaults (`previewPolicy all`,
    `nodeCensus vitals`): the absent standing kami is previewed IN ITS TURN
    — on the recorded node that is the fifth pass, after the node's 45
    present attackers in index order — and the book holds its kamis (no
    start, no rest on the bound, no decision) until then. With the `kami`
    read refused the synthetic row's cooldown is 0: the kami is never
    skipped, and once its preview lands it binds and the book rests."""
    e = _env(make_keeper)
    _refuse_kami_read(e, ABSENT)
    book = _start(e, KAMIS)
    for _pass in range(4):
        s = _tick(e, book)
        assert _kami_reads(e) == [ABSENT] and len(_preview_reads(e)) == 10 and ABSENT not in _preview_reads(e)
        assert s["standingKamisAbsent"] == [ABSENT] and s["cooldownSkipped"] == [] and s["attackerVitalsCalls"] == 1
        assert s["kamis"]["held"] == len(KAMIS) and s["acted"] is False and s["decisions"] == []
        _strat, lim, row = _rows_of(e, book)
        assert lim["unknown"] == [ABSENT] and row[ABSENT]["cooldownSec"] == 0 and row[ABSENT]["class"] is OwnerClass.STANDING
    assert _decisions(e, book) == []
    s = _tick(e, book)  # the fifth pass: its turn
    assert ABSENT in _preview_reads(e) and s["previewsPending"] == 0 and s["cooldownSkipped"] == []
    assert s["boundKamisBy"] == {"standing/standing": 2} and s["kamis"]["held"] == 0 and s["decisions"] == ["rest"]
    rest = _decisions(e, book, "rest")[0]["parameters"]
    assert rest["kamis"] == [KAMIS[1]] and rest["detail"][0]["bound"] == BOUND and rest["detail"][0]["boundBy"]["attackerKami"] == ABSENT
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["owner"] is None and row[ABSENT]["cooldownSec"] == 0 and lim["unknown"] == [] and lim["bound"] == BOUND
    # the world without the key, same defaults: nobody holds on the same four passes
    c = _env(make_keeper, standing=())
    plain = _start(c, KAMIS)
    assert all(_tick(c, plain)["kamis"]["held"] == 0 for _pass in range(4))


def test_t2_under_the_schema_defaults_the_cooldown_the_kami_read_serves_decides_whether_the_absent_kami_binds(make_keeper):
    """Under the default census the absent kami's cooldown is read off the
    synthetic row itself (there is no separate vitals read to overlay):
    above `cooldownHorizonSec` the kami is skipped for the tick, below it
    binds."""
    e = _env(make_keeper, cooldown=1200)
    book = _start(e, KAMIS, cooldownHorizonSec=600, previewsPerTick=50)
    s = _tick(e, book)  # every attacker of the node previewed in one pass
    assert ABSENT in _preview_reads(e) and s["previewsPending"] == 0
    s = _tick(e, book)
    assert _preview_reads(e) == [] and _kami_reads(e) == [ABSENT]
    assert s["cooldownSkipped"] == [ABSENT] and s["boundKamisBy"] != {"standing/standing": 2}
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["cooldownSec"] == 1200 and (lim["boundBy"] or {}).get("attackerKami") != ABSENT
    e.lens.kamis[ABSENT]["cooldownSec"] = 300
    s = _tick(e, book)
    assert s["cooldownSkipped"] == [] and s["boundKamisBy"] == {"standing/standing": 2}
    _strat, lim, row = _rows_of(e, book)
    assert row[ABSENT]["cooldownSec"] == 300 and lim["bound"] == BOUND and lim["boundBy"]["attackerKami"] == ABSENT
