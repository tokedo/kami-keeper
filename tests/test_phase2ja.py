"""Collect de-sync on `harvestAndFeed`:

  1. schema: `collectEveryMinSec` / `collectEveryMaxSec` (108000 / 144000)
     are the default window; `collectEvery` has no default and, set ALONE,
     pins the period (min = max) — a stored config without the window reads the same
     way; with a window key it is ignored; min above max is refused;
  2. the draw: uniform in [min, max], seeded from the book id + the draw
     counter, at the start of the collect clock and after every collect /
     start that LANDED; the deadline (`runtime.nextCollectAt`) survives a
     restart and a collect that did not land; a runtime without a deadline
     draws once from `lastCollectAt`;
  3. `expectedTicksBetweenActs` is the MAX of the window; status carries
     `nextCollectAt` / `collectIntervalSec`; the `collect` decision carries
     `drawnIntervalSec`.
"""

from __future__ import annotations

import json
import random
from types import SimpleNamespace

import pytest

from conftest import HAF_CONFIG
from keeper.books import Book, BookError, apply_defaults, collect_window, validate_config
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.describe import describe_type
from keeper.lens import LensClient
from keeper.protocol import Basis
from keeper.runtime import iso, runtime_path
from keeper.strategies.base import HARVESTING, KamiView, Observation
from keeper.strategies.harvest_and_feed import HarvestAndFeed

LO, HI = 108000, 144000
# `start` stamps nextTickAt with the wall clock, so a synthetic `now` for a
# tick PASS (tick_due) must lie beyond it; a direct tick_book takes any now
T = 2_000_000_000.0


def _start(e, **over):
    s = e.start_haf(**over)
    assert s["ok"], s
    return e.keeper.store.get(s["data"]["book"]["id"])


def _rows(k, book, kind=None):
    rows = k.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def _restart(e, cap: int = 4) -> Keeper:
    """A second keeper process on the SAME data dir (SPEC P8 restart)."""
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": cap, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    return k


def _expected(book_id: str, n: int, lo: int = LO, hi: int = HI) -> int:
    """Draw n of a book: the pinned generator (book id + counter)."""
    return random.Random(f"{book_id}:{n}").randint(lo, hi)


def _unit(book_id: str = "haf-unit", **over) -> HarvestAndFeed:
    cfg = validate_config("harvestAndFeed", dict(HAF_CONFIG, **over), "lab")
    return HarvestAndFeed(Book(id=book_id, account="lab", strategyType="harvestAndFeed", config=cfg))


def _obs(hp: int = 150) -> Observation:
    view = KamiView(index=1001, state=HARVESTING, hp_current=hp, hp_total=200, node_index=62, musu_accrued=777, cooldown_sec=0, level=10, account_index=4242)
    return Observation(basis=Basis(), kamis={1001: view}, inventory={11301: 5})


# ----------------------------------------------------------------- 1: the schema


def test_the_default_is_the_window_and_a_collect_every_set_alone_pins_it():
    full = validate_config("harvestAndFeed", dict(HAF_CONFIG), "lab")
    assert full["collectEveryMinSec"] == LO and full["collectEveryMaxSec"] == HI and "collectEvery" not in full
    assert collect_window(full) == (LO, HI)
    # `collectEvery` alone: the window defaults are NOT materialised, the stored config pins the period
    pinned = validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEvery=172800), "lab")
    assert pinned["collectEvery"] == 172800 and "collectEveryMinSec" not in pinned and "collectEveryMaxSec" not in pinned
    assert collect_window(pinned) == (172800, 172800)
    # a COMPLETE stored config without the window keys (every other default materialised) reads the same, and re-applying defaults adds nothing
    stored = dict(pinned)
    assert apply_defaults("harvestAndFeed", stored, "lab") == stored and collect_window(stored) == (172800, 172800)
    # a config that sets a window key ignores `collectEvery`; the side it lacks is defaulted into the stored config
    both = validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEvery=172800, collectEveryMinSec=3600, collectEveryMaxSec=7200), "lab")
    assert collect_window(both) == (3600, 7200)
    one = validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEvery=172800, collectEveryMinSec=3600), "lab")
    assert one["collectEveryMaxSec"] == HI and collect_window(one) == (3600, HI)
    assert collect_window(validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEveryMaxSec=HI + 1), "lab")) == (LO, HI + 1)


def test_min_above_max_is_refused(env):
    for over in ({"collectEveryMinSec": 7200, "collectEveryMaxSec": 3600}, {"collectEveryMinSec": HI + 1}, {"collectEveryMaxSec": LO - 1}, {"collectEvery": 60, "collectEveryMinSec": HI + 1}):
        with pytest.raises(BookError, match="collectEveryMinSec: .* is above collectEveryMaxSec"):
            validate_config("harvestAndFeed", dict(HAF_CONFIG, **over), "lab")
    assert collect_window(validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEveryMinSec=3600, collectEveryMaxSec=3600), "lab")) == (3600, 3600)  # equal is a pinned window, not a refusal
    with pytest.raises(BookError):
        validate_config("harvestAndFeed", dict(HAF_CONFIG, collectEveryMinSec=59), "lab")
    # through the socket it is the existing config refusal, and nothing is saved
    env.grant()
    r = env.start_haf(collectEveryMinSec=7200, collectEveryMaxSec=3600)
    assert not r["ok"] and r["error"]["code"] == "BAD_ARGS" and "collectEveryMinSec" in r["error"]["message"]
    assert env.keeper.store.all() == []


def test_describe_serves_the_window_and_the_withheld_default():
    d = describe_type("harvestAndFeed", "lab")
    assert d["defaults"]["collectEveryMinSec"] == LO and d["defaults"]["collectEveryMaxSec"] == HI and "collectEvery" not in d["defaults"]
    line = [l for l in d["description"].splitlines() if l.startswith("- collectEveryMinSec ")][0]
    assert "default 108000" in line and "default not applied when collectEvery is set and none of collectEveryMinSec, collectEveryMaxSec" in line
    assert "default" not in [l for l in d["description"].splitlines() if l.startswith("- collectEvery ")][0].split("[")[-1]


# ------------------------------------------------------------------- 2: the draw


def test_a_draw_lies_inside_the_window_and_follows_the_book_seed():
    s = _unit("haf-a")
    rt: dict = {}
    seen = []
    now = 0.0
    for n in range(200):
        s._draw_collect(rt, now)
        assert LO <= rt["collectIntervalSec"] <= HI and rt["nextCollectAt"] == now + rt["collectIntervalSec"] and rt["collectDraws"] == n + 1
        seen.append(rt["collectIntervalSec"])
        now = rt["nextCollectAt"]
    assert seen == [_expected("haf-a", n) for n in range(200)]  # the sequence is the seed's, in every process
    assert len(set(seen)) > 150 and min(seen) < LO + 3600 and max(seen) > HI - 3600  # spread over the window, not a constant
    # a narrow window: both ends are reachable, nothing outside
    narrow = _unit("haf-n", collectEveryMinSec=60, collectEveryMaxSec=62)
    got = set()
    for _ in range(100):
        narrow._draw_collect(rt, 0.0)
        got.add(rt["collectIntervalSec"])
    assert got == {60, 61, 62}


def test_two_books_draw_different_sequences():
    a, b = _unit("haf-a"), _unit("haf-b")
    ra: dict = {}
    rb: dict = {}
    sa, sb = [], []
    for _ in range(20):
        a._draw_collect(ra, 0.0)
        b._draw_collect(rb, 0.0)
        sa.append(ra["collectIntervalSec"])
        sb.append(rb["collectIntervalSec"])
    assert sa != sb and sum(1 for x, y in zip(sa, sb) if x == y) <= 1
    # the same book id in another process: the same sequence (no wall-clock seed)
    again: dict = {}
    twin = _unit("haf-a")
    assert [twin._draw_collect(again, 0.0) or again["collectIntervalSec"] for _ in range(20)] == sa


def test_three_books_started_in_one_tick_spread_their_collects(env):
    env.grant()
    books = []
    for kami in (1001, 1002, 1003):
        env.lens.set_kami(kami, state="HARVESTING", node=62, hp=(150, 200), account=4242)
        books.append(_start(env, kamiIndex=kami))
    env.keeper.tick_due(now=T)
    rt = env.keeper.runtime
    due = [rt[b.id]["nextCollectAt"] for b in books]
    assert due == [T + _expected(b.id, 0) for b in books] and len(set(due)) == 3
    assert all(T + LO <= d <= T + HI for d in due)
    # status: the ISO deadline and the draw it came from
    for b in books:
        st = env.keeper.handle("status", {"book": b.id})["data"]["books"][0]
        assert st["strategy"] == {"nextCollectAt": iso(rt[b.id]["nextCollectAt"]), "collectIntervalSec": _expected(b.id, 0)}
        assert st["expectedTicksBetweenActs"] == HI // 30  # the MAX of the window over tickSeconds
    # each book collects at its own time: the earliest one alone, never re-synchronised with the others
    first = min(books, key=lambda b: rt[b.id]["nextCollectAt"])
    t1 = rt[first.id]["nextCollectAt"]
    env.keeper.tick_due(now=t1 - 30)  # one tick period earlier (a pass ticks a book once per tickSeconds)
    assert not any(r.system == "system.harvest.collect" for r in env.keeper.signer.recorded)
    env.keeper.tick_due(now=t1)
    recs = [r for r in env.keeper.signer.recorded if r.system == "system.harvest.collect"]
    assert [(r.kami, r.count) for r in recs] == [(first.config["kamiIndex"], 1)]
    dec = _rows(env.keeper, first, "decision")[-1]["decision"]
    assert dec["kind"] == "collect" and dec["parameters"]["drawnIntervalSec"] == _expected(first.id, 0) and dec["parameters"]["sinceLastCollectSec"] == _expected(first.id, 0)
    # landed: redrawn from the landing tick with draw 1; the others keep draw 0
    assert rt[first.id]["lastCollectAt"] == t1 and rt[first.id]["collectDraws"] == 2 and rt[first.id]["nextCollectAt"] == t1 + _expected(first.id, 1)
    assert all(rt[b.id]["collectDraws"] == 1 and rt[b.id]["nextCollectAt"] == T + _expected(b.id, 0) for b in books if b is not first)


def test_a_status_before_the_collect_clock_starts_serves_nulls(env):
    env.grant()
    book = _start(env)
    st = env.keeper.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["strategy"] == {"nextCollectAt": None, "collectIntervalSec": None}


def test_the_deadline_survives_a_restart_with_no_redraw(env):
    env.grant()
    book = _start(env)
    env.keeper.tick_book(book, now=0.0)
    draw = _expected(book.id, 0)
    doc = json.loads(runtime_path(env.data_dir / "books" / book.id).read_text())["runtime"]
    assert doc["nextCollectAt"] == float(draw) and doc["collectIntervalSec"] == draw and doc["collectDraws"] == 1 and doc["lastCollectAt"] == 0.0
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    assert {k: k2.runtime[book.id][k] for k in ("nextCollectAt", "collectIntervalSec", "collectDraws")} == {"nextCollectAt": float(draw), "collectIntervalSec": draw, "collectDraws": 1}
    s = k2.tick_book(b2, now=draw - 30.0)
    assert "collect" not in s["decisions"] and k2.runtime[book.id]["collectDraws"] == 1 and k2.runtime[book.id]["nextCollectAt"] == float(draw)  # the restart drew nothing
    st = k2.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]
    assert st == {"nextCollectAt": iso(float(draw)), "collectIntervalSec": draw}
    s = k2.tick_book(b2, now=float(draw))
    assert "collect" in s["decisions"] and k2.runtime[book.id]["collectDraws"] == 2
    assert k2.runtime[book.id]["nextCollectAt"] == draw + _expected(book.id, 1)
    k2.stop_engine()
    # a restart while the collect is DUE (past the deadline): still due, still the same deadline until it lands
    k3 = _restart(env)
    due = draw + _expected(book.id, 1)
    assert k3.runtime[book.id]["nextCollectAt"] == due
    assert "collect" in k3.tick_book(k3.store.get(book.id), now=due + 5000.0)["decisions"]
    k3.stop_engine()


class _Unlanded:
    """A DryRunSigner subclass factory: every collect comes back in the
    given un-landed shape."""

    SHAPES = {"not_sent": ("not_sent", "reverted"), "reverted": ("reverted", "ok"), "unconfirmed": ("unconfirmed", "ok")}

    @staticmethod
    def make(shape: str | None):
        from keeper.signer import DryRunSigner

        class S(DryRunSigner):
            def _send(self, grant, intents):
                recs = super()._send(grant, intents)
                for r in recs:
                    if shape is not None and r.system == "system.harvest.collect":
                        r.status, r.precheck = _Unlanded.SHAPES[shape]
                        r.reason = "Harvest: kami starving [code 3]"
                return recs

        return S()


@pytest.mark.parametrize("shape", ["not_sent", "reverted", "unconfirmed"])
def test_an_unlanded_collect_keeps_its_deadline_and_stays_due_and_a_landed_one_redraws(env, shape):
    env.grant()
    book = _start(env)
    ok = env.keeper.signer
    bad = _Unlanded.make(shape)
    bad._grants = ok._grants
    env.keeper.tick_book(book, now=0.0)
    rt = env.keeper.runtime[book.id]
    draw = _expected(book.id, 0)
    env.keeper.signer = bad
    for i in range(3):  # refused three ticks running: due every time, one deadline, one draw
        s = env.keeper.tick_book(book, now=draw + 30.0 * i)
        assert s["decisions"] == ["collect"] and s["collectPooled"] == 1
        assert rt["nextCollectAt"] == float(draw) and rt["collectIntervalSec"] == draw and rt["collectDraws"] == 1 and rt["lastCollectAt"] == 0.0
    assert json.loads(runtime_path(env.data_dir / "books" / book.id).read_text())["runtime"]["nextCollectAt"] == float(draw)
    assert [d["decision"]["parameters"]["drawnIntervalSec"] for d in _rows(env.keeper, book, "decision")] == [draw] * 3
    env.keeper.signer = ok
    landed_at = draw + 90.0
    assert env.keeper.tick_book(book, now=landed_at)["decisions"] == ["collect"]
    assert rt["lastCollectAt"] == landed_at and rt["collectDraws"] == 2 and rt["collectIntervalSec"] == _expected(book.id, 1)
    assert rt["nextCollectAt"] == landed_at + _expected(book.id, 1)  # from the landing tick, not from the old deadline
    assert "collect" not in env.keeper.tick_book(book, now=rt["nextCollectAt"] - 1)["decisions"]
    assert "collect" in env.keeper.tick_book(book, now=rt["nextCollectAt"])["decisions"]


@pytest.mark.parametrize("status,precheck,landed", [("success", "ok", True), ("dry_run", "ok", True), ("dry_run", "reverted", False), ("not_sent", "reverted", False), ("not_sent", None, False), ("reverted", "ok", False), ("unconfirmed", "ok", False)])
def test_on_records_redraws_only_on_a_landed_collect_or_start(status, precheck, landed):
    for kind in ("collect", "start"):
        s = _unit("haf-r")
        rt: dict = {}
        decisions = s.assess(_obs(), 1000.0, rt)
        assert decisions == [] and rt["nextCollectAt"] == 1000.0 + _expected("haf-r", 0)
        before = dict(rt)
        d = SimpleNamespace(kind=kind, intents=[object()], parameters={"kami": 1001})
        s.on_records([d], [SimpleNamespace(status=status, precheck=precheck)], 5000.0, rt)
        if landed:
            assert rt["lastCollectAt"] == 5000.0 and rt["collectDraws"] == 2 and rt["nextCollectAt"] == 5000.0 + _expected("haf-r", 1) and rt["collectIntervalSec"] == _expected("haf-r", 1)
        else:
            assert rt == before


def test_a_landed_start_draws_the_first_deadline_from_the_start(env):
    env.grant()
    env.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))
    book = _start(env)
    assert env.keeper.tick_book(book, now=600.0)["decisions"] == ["start"]
    rt = env.keeper.runtime[book.id]
    assert rt["lastCollectAt"] == 600.0 and rt["nextCollectAt"] == 600.0 + _expected(book.id, 0) and rt["collectDraws"] == 1


# ---------------------------------------------------- 3: legacy runtimes and configs


def test_a_legacy_config_collects_at_exactly_collect_every_as_before(env):
    env.grant()
    book = _start(env, collectEvery=172800)
    assert "collectEveryMinSec" not in book.config and "collectEveryMaxSec" not in book.config
    assert env.keeper.handle("status", {"book": book.id})["data"]["books"][0]["expectedTicksBetweenActs"] == 172800 // 30
    env.keeper.tick_book(book, now=0.0)
    rt = env.keeper.runtime[book.id]
    assert rt["nextCollectAt"] == 172800.0 and rt["collectIntervalSec"] == 172800
    assert "collect" not in env.keeper.tick_book(book, now=172799.0)["decisions"]
    assert "collect" in env.keeper.tick_book(book, now=172800.0)["decisions"]
    dec = _rows(env.keeper, book, "decision")[-1]["decision"]["parameters"]
    assert dec["sinceLastCollectSec"] == 172800 and dec["drawnIntervalSec"] == 172800
    # and the next one exactly 172800 s after the landing tick, cycle after cycle
    for n in range(2, 5):
        assert rt["nextCollectAt"] == 172800.0 * n
        assert "collect" not in env.keeper.tick_book(book, now=172800.0 * n - 1)["decisions"]
        assert "collect" in env.keeper.tick_book(book, now=172800.0 * n)["decisions"]


def _strip_deadline(env, book) -> None:
    """Rewrite runtime.json in the older shape: the collect
    clock, no deadline, no draw, no counter."""
    p = runtime_path(env.data_dir / "books" / book.id)
    doc = json.loads(p.read_text())
    for k in ("nextCollectAt", "collectIntervalSec", "collectDraws"):
        doc["runtime"].pop(k, None)
    assert doc["runtime"]["lastCollectAt"] == 0.0
    p.write_text(json.dumps(doc))


def test_a_legacy_runtime_without_a_deadline_draws_once_from_last_collect_at(env):
    env.grant()
    book = _start(env)
    env.keeper.tick_book(book, now=0.0)
    env.keeper.stop_engine()
    _strip_deadline(env, book)
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    assert "nextCollectAt" not in k2.runtime[book.id] and k2.runtime[book.id]["lastCollectAt"] == 0.0
    draw = _expected(book.id, 0)
    s = k2.tick_book(b2, now=50000.0)  # mid-window: the draw counts from lastCollectAt (0), not from this tick
    assert "collect" not in s["decisions"] and k2.runtime[book.id]["nextCollectAt"] == float(draw) and k2.runtime[book.id]["collectDraws"] == 1
    k2.tick_book(b2, now=50030.0)
    assert k2.runtime[book.id]["collectDraws"] == 1  # once
    assert "collect" in k2.tick_book(b2, now=float(draw))["decisions"]
    k2.stop_engine()


def test_a_legacy_runtime_whose_drawn_deadline_is_already_past_collects_now(env):
    """A legacy runtime whose `lastCollectAt` is so old that lastCollectAt
    + any draw of 30-40 h is already behind the clock: collected now."""
    env.grant()
    book = _start(env)
    env.keeper.tick_book(book, now=0.0)
    env.keeper.stop_engine()
    _strip_deadline(env, book)
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    s = k2.tick_book(b2, now=200000.0)
    assert s["decisions"] == ["collect"]
    rt = k2.runtime[book.id]
    assert rt["lastCollectAt"] == 200000.0 and rt["collectDraws"] == 2 and rt["nextCollectAt"] == 200000.0 + _expected(book.id, 1)
    dec = _rows(k2, b2, "decision")[-1]["decision"]["parameters"]
    assert dec["drawnIntervalSec"] == _expected(book.id, 0) and dec["sinceLastCollectSec"] == 200000
    k2.stop_engine()


def test_a_legacy_config_with_a_legacy_runtime_keeps_its_48_h_due_time(env):
    """A book stored with `collectEvery: 172800` and a runtime without a
    deadline keeps its 48-h due time."""
    env.grant()
    book = _start(env, collectEvery=172800)
    env.keeper.tick_book(book, now=0.0)
    env.keeper.stop_engine()
    _strip_deadline(env, book)
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    assert "collect" not in k2.tick_book(b2, now=172799.0)["decisions"] and k2.runtime[book.id]["nextCollectAt"] == 172800.0
    assert "collect" in k2.tick_book(b2, now=172800.0)["decisions"]
    k2.stop_engine()
