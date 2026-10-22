"""The measured dip cap. The world of `tests/test_phase2jb2.py` (one
kami at 330 HP, the ladder `[11313, 11304, 11312, 11303, 11301]`) with a
WATCHLIST file beside it: `<dataDir>/watchlist.json`, written by the
operator's own tooling and only ever read here.

  1. a `flat` book reads no watchlist and decides as a ladder book with
     no dip cap mode, with a list and its answers on the lens;
  2. two listed kamis, thresholds 80 and 120 -> the cap is 137, not the
     flat 198: the apple is back;
  3. a 215 line raises the cap ABOVE the flat one and sticks are out;
  4. a kami outside the window, and one under `friends`, do not count;
  5. a stale list, and a missing one, leave the flat cap;
  6. a listed kami not yet read leaves the flat cap; the per-pass cap is
     shared by the books on the node and the refresh waits
     `watchRefreshSec`;
  7. nobody active -> `dipMeasuredMinPct`;
  8. no row for this kami -> the maximum over the account's rows; no rows
     at all -> flat;
  9. a refused watch read never refuses the tick;
 10. `watch/<node>.json` round trip — and the keeper never writes
     `watchlist.json`;
 11. the config refusal;
 12. scrub over describe / status / logs / ledger.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
from typing import Any

import pytest

from conftest import HAF_CONFIG, Env
from keeper.books import BookError, validate_config
from keeper.describe import describe_type
from keeper.scrub import collect_strings, forbidden_in
from keeper.watchlist import WatchCache, Watchlist
from test_phase2jb2 import (
    ACCT,
    APPLE,
    GUM,
    HEAL,
    HONEYDEW,
    KAMI,
    NODE,
    STICKS,
    T,
    _decisions,
    _env,
    _harvest,
    _last_tick,
    _ladder_config,
    _node_doc,
    _requests,
    _rows,
    _start,
    _tick,
    _world,
)

KILLER, KILLER2 = 9001, 9002
OTHER_ACCOUNT = 8001
DAY = 86400.0


def _iso(sec: float) -> str:
    return _dt.datetime.fromtimestamp(sec, _dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_writes = {"n": 0}


def _watchlist(e: Env, killers: list[tuple[int, int | None, float]], updated_at: float = T) -> Path:
    """Write the watchlist file: (kami, account, last kill at) rows. The mtime
    is moved by hand because the keeper re-reads on it and two writes of one
    test land inside the filesystem's timestamp granularity."""
    doc = {"updatedAt": _iso(updated_at), "killers": [{"kami": k, "account": a, "lastKillAt": _iso(at)} for k, a, at in killers]}
    p = e.data_dir / "watchlist.json"
    p.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    _writes["n"] += 1
    os.utime(p, (T + _writes["n"], T + _writes["n"]))
    return p


def _served(e: Env, killer: int, thresholds: dict[int, int], ours: dict[int, tuple[int, int]] | None = None) -> None:
    """What the lens answers for `node <node> <killer> --with-vitals --full`.
    The killer is NOT on the node — that is the whole point of the read —
    so the answer is recorded rather than synthesized from the node rows.
    `_world` resets the recorded answers, so this is called after it."""
    if ours is None:
        ours = {k: (330, 330) for k in thresholds} or {KAMI: (330, 330)}
    rows = []
    for k, hp in ours.items():
        row = _harvest(k, ACCT, hp, level=12)
        thr = thresholds.get(k)
        if thr is not None:
            row["liquidation"] = {"eligible": hp[0] < thr, "reason": None if hp[0] < thr else "TARGET_HP_ABOVE_THRESHOLD", "threshold": thr, "margin": hp[0] - thr, "spoils": 0, "salvage": 0, "recoil": 0}
        rows.append(row)
    doc = _node_doc(rows)
    doc["attacker"] = {"id": str(killer), "index": killer, "name": f"kami-{killer}", "cooldownSec": 0, "cooldownUntil": 0, "blocked": None}
    e.lens.previews.setdefault(NODE, {})[killer] = doc


def _measured_config(**over: Any) -> dict[str, Any]:
    return _ladder_config(**dict({"dipCapMode": "measured"}, **over))


def _watch_reads(e: Env) -> list[str]:
    """The `node <node> <killer>` reads of the last tick (the census carries no
    second positional argument, so it is not one of these)."""
    return [r for r in _requests(e) if r.startswith(f"node {NODE} ") and not r.startswith(f"node {NODE} --")]


# ------------------------------------------------- 1. a flat book is untouched


def test_a_flat_book_never_reads_the_watchlist(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    _served(e, KILLER, {KAMI: 300})
    book = _start(e)  # no dipCapMode at all: a plain ladder book
    s = _tick(e, book)
    assert (s["ladder"]["item"], s["ladder"]["need"], s["ladder"]["dipFrom"]) == (STICKS, 198, "flat")
    assert _watch_reads(e) == [] and [k for k in s if k.startswith("watch")] == []
    assert not (e.data_dir / "watch").exists()
    # and an explicit `flat` reads no differently
    book2 = _start(e, kamiIndex=1002, dipCapMode="flat")
    _world(e, ours={KAMI: (330, 330), 1002: (330, 330)})
    _served(e, KILLER, {1002: 300})
    s2 = _tick(e, book2)
    assert s2["ladder"]["dipFrom"] == "flat" and _watch_reads(e) == []


# ------------------------------------------------------- 2-3. the measured cap


def test_the_highest_active_line_plus_the_margin_is_the_cap(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600), (KILLER2, OTHER_ACCOUNT, T - 2 * DAY)])
    _served(e, KILLER, {KAMI: 80})
    _served(e, KILLER2, {KAMI: 120})
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    # 120 (the higher line) + ceil(5 % x 330) = 120 + 17 = 137; the flat cap
    # would be 198 and the apple's no-waste floor is 180
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (137, "measured")
    assert (s["ladder"]["item"], s["ladder"]["heal"], s["ladder"]["floor"]) == (APPLE, HEAL[APPLE], 180)
    assert (s["watchKillers"], s["watchCalls"], s["watchPending"]) == (2, 2, 0)
    # they are the LAST reads of the tick — everything a flat book reads
    # comes first, in the order it always did
    assert _requests(e) == ["status", "kami 1001", f"inventory {ACCT}", f"node {NODE} --with-vitals --full", f"node {NODE} {KILLER} --with-vitals --full", f"node {NODE} {KILLER2} --with-vitals --full"]


def test_a_high_line_raises_the_cap_above_the_flat_one(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    _served(e, KILLER, {KAMI: 215})
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    # 215 + 17 = 232, ABOVE the flat 198: the sticks floor 230 no longer
    # clears it and Honeydew (255) is the largest rung that does
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (232, "measured")
    assert (s["ladder"]["item"], s["ladder"]["floor"]) == (HONEYDEW, 255)


# ------------------------------------------------- 4, 7. who counts as active


def test_a_kami_outside_the_window_or_under_friends_does_not_count(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 8 * DAY), (KILLER2, 7777, T - 3600)])
    _served(e, KILLER, {KAMI: 300})
    _served(e, KILLER2, {KAMI: 300})
    book = _start(e, dipCapMode="measured", friends=[7777])
    s = _tick(e, book)
    # the 8-day-old kill is outside dipWindowDays 7 and the other kami is
    # listed: nobody is active, so the cap is dipMeasuredMinPct (30 % = 99)
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (99, "measured: no active killer")
    assert (s["watchKillers"], s["watchCalls"]) == (0, 0) and _watch_reads(e) == []
    assert s["ladder"]["item"] == APPLE  # the apple's 180 clears 99
    # the older one comes back inside a wider window
    book2 = _start(e, kamiIndex=1002, dipCapMode="measured", dipWindowDays=30, friends=[7777])
    _world(e, ours={KAMI: (330, 330), 1002: (330, 330)})
    _served(e, KILLER, {1002: 300})
    s2 = _tick(e, book2)
    assert s2["watchKillers"] == 1 and s2["ladder"]["dipFrom"] == "measured" and s2["ladder"]["need"] == 317  # 300 + 17


def test_no_active_kami_ends_on_the_measured_minimum(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [])
    book = _start(e, dipCapMode="measured", dipMeasuredMinPct=50)
    s = _tick(e, book)
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (165, "measured: no active killer")
    assert s["ladder"]["item"] == APPLE and s["ladder"]["floor"] == 180


# --------------------------------------------- 5. a stale or missing list


def test_a_stale_or_missing_list_leaves_the_flat_cap(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)], updated_at=T - 2 * DAY)
    _served(e, KILLER, {KAMI: 80})
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    # the file is 2 days old against watchlistMaxAgeSec 86400: flat, and the
    # tick says which. The reads still happen (the list is read, not trusted)
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (198, "flat: watchlist stale")
    assert s["ladder"]["item"] == STICKS
    # a longer tolerance takes the same file as fresh
    book2 = _start(e, kamiIndex=1002, dipCapMode="measured", watchlistMaxAgeSec=3 * 86400)
    _world(e, ours={KAMI: (330, 330), 1002: (330, 330)})
    _served(e, KILLER, {1002: 80})
    s2 = _tick(e, book2)
    # 80 + 17 = 97 is under dipMeasuredMinPct (30 % x 330 = 99), which is the
    # lowest a measured cap ever goes
    assert (s2["ladder"]["need"], s2["ladder"]["dipFrom"]) == (99, "measured")
    # no file at all is no answer either
    (e.data_dir / "watchlist.json").unlink()
    _world(e, ours={KAMI: (330, 330), 1002: (330, 330)})
    s3 = _tick(e, book2)
    assert (s3["ladder"]["need"], s3["ladder"]["dipFrom"]) == (198, "flat: watchlist stale")
    assert s3["watchKillers"] == 0


# ------------------------------- 6. what is read, when, and how often


def test_a_kami_not_read_yet_holds_the_flat_cap_and_the_pass_cap_is_shared(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (330, 330), 1002: (330, 330)})
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600), (KILLER2, OTHER_ACCOUNT, T - 3600)])
    _served(e, KILLER, {KAMI: 80, 1002: 80})
    _served(e, KILLER2, {KAMI: 120, 1002: 120})
    book = _start(e, dipCapMode="measured", watchPreviewsPerTick=1)
    book2 = _start(e, kamiIndex=1002, dipCapMode="measured", watchPreviewsPerTick=1)
    e.clock["now"] += 30
    e.lens.requests.clear()
    assert len(e.keeper.tick_due(now=e.clock["now"])) == 2
    # ONE read for the pass, shared by the two books on the node — and with
    # one of the two listed kamis unread neither book may measure
    assert _watch_reads(e) == [f"node {NODE} {KILLER} --with-vitals --full"]
    for b in (book, book2):
        lad = _last_tick(e, b)["ladder"]
        assert (lad["need"], lad["dipFrom"], lad["item"]) == (198, "flat: watchlist not read yet", STICKS)
    assert _last_tick(e, book)["watchPending"] == 1
    e.clock["now"] += 30
    e.lens.requests.clear()
    e.keeper.tick_due(now=e.clock["now"])
    assert _watch_reads(e) == [f"node {NODE} {KILLER2} --with-vitals --full"]
    for b in (book, book2):
        lad = _last_tick(e, b)["ladder"]
        # the cap is measured now (120 + 17); the rung is still sticks — the
        # way UP the ladder is one rung per tierUpCooldownSec, as ever
        assert (lad["need"], lad["dipFrom"], lad["item"], lad["reason"]) == (137, "measured", STICKS, "held at the last rung")
    # nothing is read again inside watchRefreshSec, and everything after it
    e.clock["now"] += 30
    e.lens.requests.clear()
    e.keeper.tick_due(now=e.clock["now"])
    assert _watch_reads(e) == [] and _last_tick(e, book)["watchCalls"] == 0
    e.clock["now"] += 3600
    e.lens.requests.clear()
    e.keeper.tick_due(now=e.clock["now"])
    assert len(_watch_reads(e)) == 1  # the cap is still 1 a pass
    lad = _last_tick(e, book)["ladder"]
    assert (lad["item"], lad["reason"], lad["dipFrom"]) == (APPLE, "one rung up", "measured")


# ------------------------------------------- 8. the rows a read comes back with


def test_a_kami_without_a_row_takes_the_maximum_over_the_accounts_rows(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={1002: (330, 330)})  # the book's kami is RESTING: it has no row
    e.lens.set_kami(KAMI, state="RESTING", node=None, hp=(330, 330), account=ACCT)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    _served(e, KILLER, {1002: 215}, ours={1002: (330, 330)})
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    # no row for the book's kami, so the line this kami serves against the account's
    # occupants stands in: 215 + 17 = 232, and the start is not held
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (232, "measured")
    assert s["decisions"] == ["start"]


def test_a_read_with_no_rows_at_all_leaves_the_flat_cap(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    _served(e, KILLER, {}, ours={})  # the account had nothing on the node
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (198, "flat: no row for this kami")
    assert s["watchCalls"] == 1 and s["ladder"]["item"] == STICKS


# --------------------------------------------------- 9. a refused read


def test_a_refused_watch_read_never_refuses_the_tick(make_keeper):
    e = _env(make_keeper)
    _world(e, ours={KAMI: (230, 330)})
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    # nothing recorded for the killer and it is not on the node: NOT_FOUND
    book = _start(e, dipCapMode="measured")
    s = _tick(e, book)
    assert s["acted"] is True and s["decisions"] == ["feed"]
    assert (s["ladder"]["need"], s["ladder"]["dipFrom"]) == (198, "flat: watchlist not read yet")
    assert (s["watchKillers"], s["watchCalls"], s["watchPending"]) == (1, 0, 0)
    assert _decisions(e, book, "feed")[-1]["parameters"]["item"] == STICKS
    assert not (e.data_dir / "watch" / f"{NODE}.json").exists()  # nothing stored
    # and the read is tried again next tick, since nothing was stored
    _world(e, ours={KAMI: (330, 330)})
    _served(e, KILLER, {KAMI: 80})
    s2 = _tick(e, book)
    assert (s2["watchCalls"], s2["ladder"]["dipFrom"], s2["ladder"]["need"]) == (1, "measured", 99)


# ----------------------------------- 10. the store, and the file we never write


def test_the_watch_store_round_trips_and_the_watchlist_is_never_written(make_keeper):
    e = _env(make_keeper)
    _world(e)
    path = _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600), (KILLER2, OTHER_ACCOUNT, T - 3600)])
    before = path.read_text(encoding="utf-8")
    mtime = path.stat().st_mtime
    _served(e, KILLER, {KAMI: 80})
    _served(e, KILLER2, {KAMI: 120})
    book = _start(e, dipCapMode="measured")
    assert _tick(e, book)["ladder"]["need"] == 137
    stored = json.loads((e.data_dir / "watch" / f"{NODE}.json").read_text(encoding="utf-8"))
    assert stored["node"] == NODE and set(stored["entries"]) == {str(KILLER), str(KILLER2)}
    assert stored["entries"][str(KILLER2)]["rows"] == {str(KAMI): 120}
    # a second cache on the same directory reads it back
    cache = WatchCache(e.data_dir, lambda: T)
    assert cache.entries(NODE)[KILLER]["rows"] == {KAMI: 80}
    # the watchlist file is untouched, and a kami that drops off the list is dropped
    assert path.read_text(encoding="utf-8") == before and path.stat().st_mtime == mtime
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600)])
    _world(e)
    _served(e, KILLER, {KAMI: 80})
    assert _tick(e, book)["ladder"]["need"] == 99  # 80 + 17, lifted to dipMeasuredMinPct
    assert set(json.loads((e.data_dir / "watch" / f"{NODE}.json").read_text(encoding="utf-8"))["entries"]) == {str(KILLER)}
    assert sorted(p.name for p in e.data_dir.glob("watchlist*")) == ["watchlist.json"]


def test_a_missing_or_unreadable_watchlist_is_an_empty_list(tmp_path):
    w = Watchlist(tmp_path, lambda: T)
    w.load()
    assert w.killers == [] and w.updated_at is None and w.fresh(86400, T) is False
    assert w.active(7, set(), T) == []
    p = tmp_path / "watchlist.json"
    p.write_text("{not json", encoding="utf-8")
    os.utime(p, (T + 1, T + 1))
    w.load()
    assert w.killers == [] and w.updated_at is None
    p.write_text(json.dumps({"updatedAt": _iso(T), "killers": [{"kami": 5, "account": None, "lastKillAt": _iso(T - 60)}, {"account": 1}, {"kami": 6, "account": 4242, "lastKillAt": _iso(T - 60)}, {"kami": 7, "account": None}]}), encoding="utf-8")
    os.utime(p, (T + 2, T + 2))
    w.load()
    # an entry with no account is kept; one under a listed account and one
    # without a last kill are not
    assert w.fresh(86400, T) and w.active(7, {4242}, T) == [5]
    assert w.active(7, set(), T) == [5, 6]


# ------------------------------------------------------ 11. the config refusal


def test_measured_without_the_ladder_is_refused():
    with pytest.raises(BookError) as err:
        validate_config("harvestAndFeed", dict(HAF_CONFIG, dipCapMode="measured"), "lab")
    assert "config invalid at dipCapMode" in str(err.value) and not forbidden_in([str(err.value)])
    # with the ladder it is taken, and the five other keys have defaults
    full = validate_config("harvestAndFeed", _measured_config(), "lab")
    assert (full["dipCapMode"], full["dipWindowDays"], full["dipMeasuredMinPct"]) == ("measured", 7, 30)
    assert (full["watchRefreshSec"], full["watchPreviewsPerTick"], full["watchlistMaxAgeSec"]) == (3600, 5, 86400)
    # a static book carries the defaulted keys and stays `flat`
    static = validate_config("harvestAndFeed", dict(HAF_CONFIG), "lab")
    assert static["dipCapMode"] == "flat"
    with pytest.raises(BookError):
        validate_config("harvestAndFeed", _measured_config(dipCapMode="lowest"), "lab")
    with pytest.raises(BookError):
        validate_config("harvestAndFeed", _measured_config(dipWindowDays=0), "lab")


def test_describe_names_the_new_keys_and_stays_clean():
    d = describe_type("harvestAndFeed", "lab")
    assert set(d["defaults"]) >= {"dipCapMode", "dipWindowDays", "dipMeasuredMinPct", "watchRefreshSec", "watchPreviewsPerTick", "watchlistMaxAgeSec"}
    assert 'default "flat"' in [line for line in d["description"].splitlines() if line.startswith("- dipCapMode ")][0]
    assert not forbidden_in(d)


# ------------------------------------------------------------- 12. the scrub


def test_every_new_surface_is_clean(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600), (KILLER2, OTHER_ACCOUNT, T - 9 * DAY)])
    _served(e, KILLER, {KAMI: 215})
    book = _start(e, dipCapMode="measured")
    _tick(e, book)  # measured
    _world(e, ours={KAMI: (229, 330)})
    _tick(e, book)  # a feed sized by the measured cap
    (e.data_dir / "watchlist.json").unlink()
    _world(e)
    _tick(e, book)  # and back to the flat cap
    frames = [e.keeper.handle("describe", {"type": "harvestAndFeed"}), e.keeper.handle("status"), e.keeper.handle("books", {"account": "lab"}), e.keeper.handle("logs"), e.keeper.handle("ledger", {"book": book.id})]
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    ticks = [r["summary"] for r in _rows(e, book, "tick") if "ladder" in r["summary"]]
    assert {t["ladder"]["dipFrom"] for t in ticks} == {"measured", "flat: watchlist stale"}
    assert all(set(t["ladder"]) == {"item", "heal", "floor", "need", "boundBy", "reason", "dipFrom"} for t in ticks)


def test_the_status_lens_block_names_the_list(make_keeper):
    e = _env(make_keeper)
    _world(e)
    _watchlist(e, [(KILLER, OTHER_ACCOUNT, T - 3600), (KILLER2, OTHER_ACCOUNT, T - 3600)], updated_at=T - 60)
    _served(e, KILLER, {KAMI: 80})
    _served(e, KILLER2, {KAMI: 120})
    book = _start(e, dipCapMode="measured")
    lens = e.keeper.handle("status")["data"]["lens"]
    assert lens["watchlist"] == {"updatedAt": None, "killers": 0}  # not read yet
    _tick(e, book)
    lens = e.keeper.handle("status")["data"]["lens"]
    assert lens["watchlist"] == {"updatedAt": _iso(T - 60), "killers": 2}
    # the book's own status keys are the plain ladder ones, unmoved
    st = e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert set(st) == {"nextCollectAt", "collectIntervalSec", "ladderItem", "ladderFloorHp", "foodStock", "foodLow"}
    assert (st["ladderItem"], st["ladderFloorHp"]) == (APPLE, 180) and str(GUM) in st["foodStock"]
