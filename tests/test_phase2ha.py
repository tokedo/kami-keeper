"""Standing owners:

  1. the definition — STANDING between HOSTILE and HARMLESS, the fifth
     rendered name, `hostile_by` -> `standing`, the severity order;
  2. the keeper-level key `standingOwners` — file, env, ints, duplicates,
     the one error naming the key, no profile gate, the describe frame;
  3. through the book — a standing owner's present attacker is previewed on
     the first pass under `previewPolicy killed-here` with an EMPTY owner
     ledger, renders `standing`, binds with `safetyMarginPct` (not
     `passiveMarginPct`) and rests the kami; a non-standing owner of the
     same shape stays `unpreviewed`;
  4. departure and return — remembered for `hostileWindowHours` from the
     DEPARTURE, then expired, then bound again the tick it is seen, no kill;
  5. precedence — a kill by a standing owner renders `killed-here-<window>`
     and counts there; past the window it is `standing` again, still binding;
  6. `listed` wins over the standing list;
  7. restart — the list is CONFIGURATION: the persisted book is resumed
     (same id) and classes the owner from the config of the keeper that
     reads it, not from anything on disk;
  8. scrub — every formatted surface of 3-5 and the boot log line.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from conftest import Env
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.kills import BLOCKS_PER_HOUR
from keeper.lens import LensClient
from keeper.owner_class import OwnerClass, classify, hostile_by, is_observation_name, render, worst
from keeper.scrub import collect_strings, forbidden_in
from keeper.strategies.auto import Auto
from test_phase2a import CAROL, FRIENDS, NODE, T, _add_occupant, _config, _decisions, _preview_threshold, _rows, _seed, _start
from test_phase2g import LAZY, _preview_reads, _serve_kami
from test_phase2h import _kill

H = BLOCKS_PER_HOUR
OTHER_NODE = 62
# the recorded node holds three foreign owners (22, 20 and 3 kamis).
# STANDING is the owner of the smallest pack, the one test_phase2h also
# uses as an attacker owner.
STANDING = 2481
STANDING_KAMIS = [601, 2865, 4016]
THR = 260  # against the test kami (268 / 270): 260 + 5 % x 270 = 274 rests it, 260 + 2 % x 270 = 266 does not
BOUND = 274
KAMIS = [66655, 68017]


# ------------------------------------------------------------ helpers


def _env(make_keeper, standing: tuple[int, ...] = (STANDING,), cap: int = 8) -> Env:
    """A lab keeper on the seeded recorded world with NO book yet (a cold
    node), the clock at T, and `standingOwners` from the ENVIRONMENT."""
    env = {"KAMI_KEEPER_STANDING_OWNERS": ",".join(str(a) for a in standing)} if standing else {}
    e = make_keeper(cap=cap, env=env)
    assert e.keeper.cfg.standingOwners == list(standing)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    for k in STANDING_KAMIS:
        _serve_kami(e.lens, k)  # the plain census serves no cooldown: one `kami` read each
    _preview_threshold(e.lens, 601, 68017, THR)
    assert e.keeper.handle("grant", {"account": "lab"})["ok"]
    e.books = []  # type: ignore[attr-defined]
    return e


def _book(e: Env, kamis: list[int] | None = None, **over: Any):
    b = _start(e, kamis or KAMIS, **dict(LAZY, **over))
    e.books.append(b)
    return b


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    e.lens.requests.clear()
    return e.keeper.tick_book(book, now=e.clock["now"])


def _rows_of(e: Env, book, kami: int = 68017) -> tuple[Auto, dict[str, Any], dict[str, Any]]:
    """(the strategy, the limit of `kami`, the attacker row of the standing owner's first kami)."""
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    lim, _classes, attackers, _s = strat.bounds(obs, {k: v for k, v in obs.kamis.items() if not v.missing})
    return strat, lim[kami], {a["kami"]: a for a in attackers}


def _leave(e: Env, kami: int) -> None:
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


# ------------------------------------------------------- 1. the definition


def test_t1_standing_sits_between_hostile_and_harmless_and_renders_into_the_closed_set():
    base = dict(listed=False, max_threshold=0, owner_hostile_in_window=False, kami_hostile_in_window=False)
    # the definition rows, in severity order
    assert classify(**dict(base, listed=True, standing=True)) is OwnerClass.FRIEND  # listed wins over the standing list
    assert classify(**dict(base, owner_hostile_in_window=True, standing=True)) is OwnerClass.HOSTILE
    assert classify(**dict(base, kami_hostile_in_window=True, standing=True)) is OwnerClass.HOSTILE
    assert classify(**dict(base, standing=True)) is OwnerClass.STANDING  # threshold 0 and no kill: still binds
    assert classify(**dict(base, max_threshold=7, standing=True)) is OwnerClass.STANDING
    assert classify(**base) is OwnerClass.HARMLESS and classify(**dict(base, max_threshold=7)) is OwnerClass.ARMED_PASSIVE
    # the fifth rendered name
    assert render(OwnerClass.STANDING, 48) == "standing" and is_observation_name("standing")
    # what made it bind
    assert hostile_by(owner_hostile_in_window=False, kami_hostile_in_window=False, standing=True) == "standing"
    assert hostile_by(owner_hostile_in_window=True, kami_hostile_in_window=False, standing=True) == "owner"
    assert hostile_by(owner_hostile_in_window=True, kami_hostile_in_window=True, standing=True) == "both"
    assert hostile_by(owner_hostile_in_window=False, kami_hostile_in_window=True, standing=True) == "kami"
    assert hostile_by(owner_hostile_in_window=False, kami_hostile_in_window=False) is None
    # severity: a kill outranks the list, the list outranks a threshold
    assert worst([OwnerClass.STANDING, OwnerClass.HOSTILE]) is OwnerClass.HOSTILE
    assert worst([OwnerClass.STANDING, OwnerClass.ARMED_PASSIVE]) is OwnerClass.STANDING
    assert worst([OwnerClass.STANDING, OwnerClass.FRIEND, OwnerClass.HARMLESS]) is OwnerClass.STANDING


# ------------------------------------------------------- 2. the config key


def test_t2_standing_owners_loads_as_account_indices_from_file_and_env(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text("standingOwners: [1743, 1202]\nmaxBooksPerAccount: 2\n", encoding="utf-8")
    cfg = load_config(flags={}, env={}, config_file=str(f))
    assert cfg.standingOwners == [1743, 1202] and all(type(a) is int for a in cfg.standingOwners)
    assert cfg.sources["standingOwners"] == "file" and cfg.errors == []
    # the same list from the environment, as a comma string
    env = load_config(flags={"maxBooksPerAccount": 2}, env={"KAMI_KEEPER_STANDING_OWNERS": "1743,1202"})
    assert env.standingOwners == [1743, 1202] and env.sources["standingOwners"] == "env"
    # absent is the default and means "no standing owner"
    none = load_config(flags={"maxBooksPerAccount": 2}, env={})
    assert none.standingOwners == [] and none.sources["standingOwners"] == "default" and none.errors == []
    # duplicates collapse, order kept
    assert load_config(flags={"maxBooksPerAccount": 2, "standingOwners": [1202, 1743, 1202]}, env={}).standingOwners == [1202, 1743]


def test_t2_a_bad_entry_is_one_error_naming_the_key_and_there_is_no_profile_gate():
    for bad in ([1743, "x"], [-1], [1743, -1, "x"]):
        c = load_config(flags={"maxBooksPerAccount": 2, "standingOwners": bad}, env={})
        assert [e for e in c.errors if e.startswith("standingOwners")] == ["standingOwners: expected account indices (non-negative integers)"]
        assert c.standingOwners == [1743] if 1743 in bad else c.standingOwners == []
    # no registry row: the key is allowed under `lab` and `fleet` alike
    for profile in ("lab", "fleet"):
        c = load_config(flags={"profile": profile, "maxBooksPerAccount": 2, "standingOwners": [1743]}, env={})
        assert c.standingOwners == [1743] and c.errors == []


def test_t2_the_describe_frame_echoes_ints_and_scrubs_clean():
    cfg = load_config(flags={"maxBooksPerAccount": 2, "standingOwners": [1743, 1202]}, env={})
    d = cfg.as_dict()
    assert d["standingOwners"] == [1743, 1202] and all(type(a) is int for a in d["standingOwners"])
    assert forbidden_in(d) == [] and collect_strings(d)


# ----------------------------------------------- 3. through the book


def test_t3_a_standing_owner_is_previewed_classed_standing_and_binds_at_the_safety_margin(make_keeper):
    e = _env(make_keeper)
    book = _book(e)
    # the owner ledger is EMPTY for this owner: nothing killed, nothing is remembered
    assert e.keeper.owners.killed_accounts == {} and e.keeper.owners.hostile_kamis == {}
    s = _tick(e, book)
    # the standing owner's three present attackers are fetched on the FIRST
    # pass; the two other owners (22 and 20 kamis) have the same shape and no kill, so
    # under `previewPolicy killed-here` they are never read
    assert sorted(_preview_reads(e)) == sorted(STANDING_KAMIS) and s["previewCalls"] == 3
    assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 0, "standing": 1, "unpreviewed": 2}
    assert s["boundKamisBy"] == {"standing/standing": 2} and s["hostileKamis"] == 0  # `<rendered class>/<hostileBy>`; both kamis are bound by a standing attacker
    assert set(e.keeper.previews.entries(NODE, CAROL)) == set(STANDING_KAMIS)
    # the class, what made it bind, and the SAFETY margin (not the passive one)
    strat, lim, row = _rows_of(e, book)
    assert row[601]["class"] is OwnerClass.STANDING and row[601]["hostileBy"] == "standing"
    assert render(row[601]["class"], strat.window_hours) == "standing" and strat.render_owner_class(STANDING) == "standing"
    assert lim["bound"] == BOUND  # 260 + 5 % x 270; passiveMarginPct 2 would be 266
    assert lim["boundBy"] == {"attackerKami": 601, "ownerClass": "standing", "threshold": THR, "present": True, "hostileBy": "standing"}
    # and the book rests the kami at that bound (268 <= 274)
    rests = _decisions(e, book, "rest")
    assert [r["parameters"]["kamis"] for r in rests] == [[68017]]
    assert rests[0]["parameters"]["detail"][0]["bound"] == BOUND and rests[0]["parameters"]["detail"][0]["boundBy"]["ownerClass"] == "standing"


def test_t3_the_same_world_without_the_standing_list_previews_nobody_and_binds_nothing(make_keeper):
    """The control: the key is the only difference."""
    e = _env(make_keeper, standing=())
    book = _book(e)
    s = _tick(e, book)
    assert _preview_reads(e) == [] and s["previewCalls"] == 0
    assert s["owners"] == {"listed": 0, "no-threshold": 0, "threshold-no-kill": 0, "killed-here": 0, "standing": 0, "unpreviewed": 3}
    _strat, lim, row = _rows_of(e, book)
    assert row[601]["class"] is OwnerClass.HARMLESS and row[601]["hostileBy"] is None
    assert lim["bound"] == 0 and lim["boundBy"] is None and _decisions(e, book, "rest") == []


# ------------------------------------------- 4. departure and return


def test_t4_a_departed_standing_attacker_is_remembered_for_the_window_then_expires_then_binds_again(make_keeper):
    e = _env(make_keeper)
    book = _book(e)
    _tick(e, book)
    assert _rows_of(e, book)[1]["bound"] == BOUND
    # it leaves: remembered, and it keeps binding while it is
    _leave(e, 601)
    s = _tick(e, book)
    assert s["previewsRemembered"] == [601]
    _strat, lim, row = _rows_of(e, book)
    assert row[601]["present"] is False and row[601]["class"] is OwnerClass.STANDING
    assert lim["bound"] == BOUND and lim["boundBy"]["present"] is False
    # 47 h of absence: still remembered, still binding (the window runs from the DEPARTURE)
    s = _tick(e, book, dt=47 * 3600.0)
    assert "previewsExpired" not in s and _rows_of(e, book)[1]["bound"] == BOUND
    # past hostileWindowHours of absence: expired, and nothing binds any more
    rests_before = len(_decisions(e, book, "rest"))  # the fixture never applies a stop, so the kami is re-offered while the departed attacker binds
    s = _tick(e, book, dt=2 * 3600.0)
    assert s["previewsExpired"] == [601] and 601 not in e.keeper.previews.entries(NODE, CAROL)
    _strat, lim, row = _rows_of(e, book)
    # the departed attacker is gone from the attacker table and binds nothing. Its two present
    # siblings are standing too and serve threshold 0 against the kami, so what
    # is left is the safety margin alone (5 % x 270 = 14) — below the floor
    assert 601 not in row and lim["boundBy"]["attackerKami"] in (2865, 4016) and lim["bound"] == 14
    assert len(_decisions(e, book, "rest")) == rests_before  # no rest once the bound is 14 and the kami reads 268
    # it comes back: bound again on the next pass, with no kill anywhere
    _add_occupant(e.lens, 601, STANDING, hp=(200, 240), level=30)
    _preview_threshold(e.lens, 601, 68017, THR)
    _serve_kami(e.lens, 601)
    s = _tick(e, book)
    assert _preview_reads(e) == [601] and e.keeper.owners.killed_accounts == {}
    _strat, lim, row = _rows_of(e, book)
    assert row[601]["class"] is OwnerClass.STANDING and row[601]["hostileBy"] == "standing" and lim["bound"] == BOUND


# ------------------------------------------------------- 5. precedence


def test_t5_a_kill_by_a_standing_owner_renders_killed_here_and_the_window_hands_it_back(make_keeper):
    e = _env(make_keeper)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))  # the victim: the account's, outside the book
    book = _book(e)
    _tick(e, book)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    # the standing owner's first kami liquidates one of the account's kamis on this node
    e.keeper.owners.record_kill(_kill(head - H, "0xaa", STANDING, 601, node=NODE, victim=5946))
    s = _tick(e, book)
    _strat, lim, row = _rows_of(e, book)
    assert row[601]["class"] is OwnerClass.HOSTILE and row[601]["hostileBy"] == "both"  # the killer kami: owner AND kami
    assert row[2865]["class"] is OwnerClass.HOSTILE and row[2865]["hostileBy"] == "owner"  # a sibling: the owner killed
    assert lim["boundBy"]["ownerClass"] == "killed-here-48h" and lim["boundBy"]["hostileBy"] == "both" and lim["bound"] == BOUND
    # the owner is counted under killed-here, NOT under standing
    assert s["owners"]["killed-here"] == 1 and s["owners"]["standing"] == 0
    assert _strat.render_owner_class(STANDING, head) == "killed-here-48h"
    # 49 h on the kill is forgiven — and the standing list hands the owner
    # straight back to `standing`, still binding at the safety margin
    e.lens.advance(49 * H)
    s = _tick(e, book)
    _strat, lim, row = _rows_of(e, book)
    assert row[601]["class"] is OwnerClass.STANDING and row[601]["hostileBy"] == "standing"
    assert lim["boundBy"]["ownerClass"] == "standing" and lim["bound"] == BOUND
    assert s["owners"]["killed-here"] == 0 and s["owners"]["standing"] == 1


# ---------------------------------------------------- 6. `listed` wins


def test_t6_a_listed_owner_on_the_standing_list_is_listed_and_binds_nothing(make_keeper):
    e = _env(make_keeper)
    book = _book(e, friends=FRIENDS + [STANDING])
    s = _tick(e, book)
    assert _preview_reads(e) == []  # a listed owner is never previewed
    _strat, lim, row = _rows_of(e, book)
    assert row == {} or all(a["owner"] != STANDING for a in row.values())  # a listed owner is not a foreign occupant
    assert lim["bound"] == 0 and lim["boundBy"] is None and _decisions(e, book, "rest") == []
    assert s["owners"]["standing"] == 0 and s["owners"]["listed"] == 1
    assert _strat.render_owner_class(STANDING) == "listed"


# ------------------------------------------------------------ 7. restart


def _restart(e: Env, standing: tuple[int, ...]) -> Keeper:
    """A second keeper on the SAME dataDir, with its own standing list."""
    cfg = load_config(
        flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 8, "requestTimeoutSec": 5},
        env={"KAMI_KEEPER_STANDING_OWNERS": ",".join(str(a) for a in standing)} if standing else {},
    )
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k.clock = lambda: e.clock["now"]
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    e.keeper.stop_engine()
    e.keeper = k
    return k


def test_t7_the_standing_list_is_configuration_and_nothing_of_it_is_persisted(make_keeper):
    e = _env(make_keeper)
    book = _book(e)
    _tick(e, book)
    assert _rows_of(e, book)[1]["bound"] == BOUND
    # a second keeper with the list set RESUMES the persisted book (same id,
    # not re-declared) and its first tick classes the owner `standing`
    k2 = _restart(e, (STANDING,))
    b2 = k2.store.get(book.id)
    assert b2.id == book.id and b2.state == "running"
    s = _tick(e, b2)
    assert s["owners"]["standing"] == 1
    _strat, lim, row = _rows_of(e, b2)
    assert row[601]["class"] is OwnerClass.STANDING and lim["bound"] == BOUND and lim["boundBy"]["hostileBy"] == "standing"
    # a third keeper with the list EMPTY resumes the SAME book and the owner
    # is classed by its threshold alone again: the previews on disk survived,
    # nothing of the list did
    k3 = _restart(e, ())
    b3 = k3.store.get(book.id)
    assert b3.id == book.id
    s = _tick(e, b3)
    _strat, lim, row = _rows_of(e, b3)
    assert row[601]["class"] is OwnerClass.ARMED_PASSIVE and row[601]["hostileBy"] is None
    assert s["owners"]["standing"] == 0 and s["owners"]["threshold-no-kill"] == 1
    # passiveOwnersBind is false: a threshold-no-kill owner binds nothing
    assert lim["bound"] == 0 and lim["boundBy"] is None


# ------------------------------------------------------------ 8. scrub


def test_t8_every_formatted_surface_of_the_standing_class_is_clean(make_keeper):
    e = _env(make_keeper)
    _add_occupant(e.lens, 5946, CAROL, hp=(30, 200))
    book = _book(e)
    _tick(e, book)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0xaa", STANDING, 601, node=NODE, victim=5946))
    _tick(e, book)
    e.lens.advance(49 * H)
    _tick(e, book)
    _leave(e, 601)
    _tick(e, book)
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
    # the boot log line of a keeper that HAS a standing list
    lines = [ln["line"] for ln in _restart(e, (STANDING, 1743)).handle("logs")["data"]["lines"]]
    assert "standing owners: [1743, 2481]" in lines
    assert forbidden_in({"lines": lines}) == []
    # every rendered class the run produced is in the closed set
    names = {row["boundBy"]["ownerClass"] for d in _decisions(e, book) for row in (d.get("parameters") or {}).get("detail") or [] if row.get("boundBy")}
    assert names and names <= {"standing", "killed-here-48h"} and all(is_observation_name(n) for n in names)
