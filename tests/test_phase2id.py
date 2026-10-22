"""The START join window:

  a. `startJoinWindowMin` 0 (the default) is the key-off behaviour — the ready
     kami starts alone, no `startJoined`, `kamis.joined` 0;
  b. window 10: the kami 10 projected minutes from `resumeHpPct` joins the
     ready kami's start, the one 30 minutes away and the one with no regen
     rate this tick do not;
  c. only the FREE places of the last chunk — ten ready take no joiner at
     all, four ready take the nearest six (4 + 6 = 10, one transaction);
  d. never a joiner: a kami in start backoff, a kami whose bound is
     UNKNOWN, a kami at or below its bound plus an hour of drain, a kami on
     the chain's action cooldown; and a READY kami is a start, not a joiner;
  e. with the start-batch hold: nothing leaves while the hold holds, so nothing
     joins; the tick the hold releases, the joiner rides with it and
     `startWaiting` never lists it;
  f. a start that did not land re-offers the READY members only; the joiner
     keeps its plan, is not backed off, and joins the next start;
  g. the stale-lens and chain-fallback paths add no joiner;
  h. `describe` states the key, and its default is 0.

The book is the four RESTING kamis of `tests/test_phase2c.py`
(RESTING_FOUR, 250 / 240 / 230 / 250 HP) at `resumeHpPct` 100 and
`startBatchWaitSec` 0 unless a test says otherwise. Every attacker is
synthesized at threshold 0 (`previews[NODE] = {}`), so every bound is 0 and
nothing is `unknown`. A RESTING kami's regen is the party row's
`hpRatePerHr`, and a kami's distance from full is given in HP (`_away`), so
the projection `_mins(below, rate)` is exact on each kami's OWN total (the
fixture's four are 250 / 240 / 230 / 250, not all 250).
"""

from __future__ import annotations

from typing import Any

from keeper.books import schema_defaults
from keeper.describe import describe_type
from keeper.kills import BLOCKS_PER_HOUR
from keeper.signer import DryRunSigner
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import CAROL, NODE, T, _decisions, _last_tick, _party_row, _preview_threshold, _rows, _seed, _set_party, _start, _tick_all
from test_phase2c import RESTING_FOUR

A, B, C, D = RESTING_FOUR
# twelve of the fixture's forty RESTING kamis, in party order, for the
# chunk tests (c): the four above plus eight more
BIG = [1941, 2065, 5034, 5146, 7420, 16245, 18992, 20369, 21052, 24454, 25133, 27850]


# ------------------------------------------------------------ the fixture


def _total(e, kami: int) -> int:
    return int(_party_row(e.lens, kami)["hp"]["total"])


def _regen(e, kami: int, rate: float | None) -> None:
    row = _party_row(e.lens, kami)
    if rate is None:
        row.pop("hpRatePerHr", None)  # the keeper reads no regen rate at all
    else:
        row["hpRatePerHr"] = f"+{float(rate):.2f}"


def _full(e, kami: int, rate: float | None = 30.0) -> None:
    """RESTING at `resumeHpPct` 100: READY, a start and never a joiner."""
    t = _total(e, kami)
    _set_party(e.lens, kami, hp=(t, t))
    _regen(e, kami, rate)


def _away(e, kami: int, below: int, rate: float | None = 30.0) -> None:
    """RESTING `below` HP under full, regenerating at `rate` HP/h — so its
    projected minutes to `resumeHpPct` 100 are `_mins(below, rate)`."""
    t = _total(e, kami)
    _set_party(e.lens, kami, hp=(t - int(below), t))
    _regen(e, kami, rate)


def _mins(below: int, rate: float = 30.0) -> float:
    """The projected minutes the keeper will report for `_away(below, rate)`."""
    return round(below / rate * 60.0, 1)


def _join_env(make_keeper, world: dict[int, Any], kamis: list[int] | None = None, refuse_previews: bool = False, own_on_node: bool = True, **over: Any):
    """A lab dry-run keeper on the recorded fixtures with ONE auto book over
    `kamis` (RESTING_FOUR by default), each RESTING as `world` says
    (kami -> `("full", rate)` | `(HP below full, rate)`). No recorded preview:
    every attacker is synthesized at threshold 0, so every bound is 0."""
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    e.lens.previews[NODE] = {}
    if refuse_previews:
        e.lens.synth_default = None  # no recorded and no synthesized preview: every read is refused
    if not own_on_node:
        # with nothing of the account on the node the ONE probe start is
        # granted, so a start can leave while every bound is unknown
        e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] != CAROL]
    e.grant()
    for kami, (where, rate) in world.items():
        if where == "full":
            _full(e, kami, rate)
        else:
            _away(e, kami, int(where), rate)
    book = _start(e, list(kamis or RESTING_FOUR), previewsPerTick=50, resumeHpPct=100, **over)
    e.books = [book]  # type: ignore[attr-defined]
    return e, book


def _record_kill(e, book, owner: int = 806, attacker: int = 360) -> None:
    """`owner` killed one of the account's kamis on the node an hour ago, so
    its attackers BIND. Without a kill nobody whose threshold is 0 or
    unknown binds at all."""
    head = e.keeper.strategies[book.id].observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill({"block": head - BLOCKS_PER_HOUR, "txHash": "0x01", "logIndex": 0, "node": NODE, "victimKami": 5946, "victimAccount": CAROL, "attackerKami": attacker, "attackerAccount": owner, "attribution": "occupant"})


def _starts(e, book) -> list[dict[str, Any]]:
    return [d["parameters"] for d in _decisions(e, book, "start")]


def _start_txs(e, book) -> list[dict[str, Any]]:
    return [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["system"] == "system.harvest.start"]


# ------------------------------------------------------------ a. the default is off


def test_window_zero_is_todays_behaviour_only_the_ready_kami_starts(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (15, 30.0), D: (30, 30.0)})
    assert book.config["startJoinWindowMin"] == 0 and book.config["startBatchWaitSec"] == 0
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A] and st[0]["batchSize"] == 1
    assert [m["kami"] for m in st[0]["detail"]] == [A]
    assert all(k not in st[0]["detail"][0] for k in ("startJoined", "projectedMinutes", "regenPerHr"))
    assert st[0]["detail"][0]["readySinceSec"] == 0
    s = _last_tick(e, book)
    assert "startJoined" not in s and s["kamis"]["joined"] == 0
    # the auto tick row shape, plus the join count
    assert set(s["kamis"]) == {"harvesting", "resting", "dead", "held", "backoff", "elsewhere", "waiting", "pulled", "joined", "cooldown"}
    assert s["kamis"]["resting"] == 4 and s["kamis"]["harvesting"] == 0 and s["kamis"]["held"] == 0
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE}
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None} for k in (B, C, D))


# ------------------------------------------------------------ b. the join


def test_a_kami_ten_minutes_from_full_joins_the_ready_kamis_start(make_keeper):
    # B 10.0 min from full at 30 HP/h joins; C 30 min away stays; D has no rate
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (15, 30.0), D: (5, None)}, startJoinWindowMin=10)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A, B] and st[0]["batchSize"] == 2 and st[0]["node"] == NODE
    ready, joiner = st[0]["detail"]
    assert ready["kami"] == A and all(k not in ready for k in ("startJoined", "projectedMinutes", "regenPerHr"))
    assert joiner == {"kami": B, "hp": 235, "hpTotal": 240, "bound": 0, "restFloor": 24, "boundBy": None, "drainPerHr": 0.0, "resumeHpPct": 100, "readySinceSec": 0, "startJoined": True, "projectedMinutes": 10.0, "regenPerHr": 30.0}
    s = _last_tick(e, book)
    assert s["startJoined"] == [{"kami": B, "projectedMinutes": 10.0}] and s["kamis"]["joined"] == 1
    assert s["kamis"]["resting"] == 4  # a joiner is still RESTING at read time
    assert [(t["fn"], t["count"]) for t in _start_txs(e, book)] == [("executeBatched", 2)]
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (A, B))
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None} for k in (C, D))
    # the status count rides the same dict
    assert e.keeper.handle("status", {"book": book.id})["data"]["books"][0]["strategy"]["kamis"]["joined"] == 1


# ------------------------------------------------------------ c. the free places of the last chunk only


def test_ten_ready_kamis_fill_the_chunk_and_take_no_joiner(make_keeper):
    ready, cand = BIG[:10], BIG[10]
    world = {k: ("full", 30.0) for k in ready}
    world[cand] = (2, 24.0)  # 2 HP at 24 HP/h = 5.0 projected minutes, well inside the window
    e, book = _join_env(make_keeper, world, kamis=ready + [cand], startJoinWindowMin=30)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == ready and st[0]["batchSize"] == 10
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 0 and "startJoined" not in s
    assert [(t["fn"], t["count"]) for t in _start_txs(e, book)] == [("executeBatched", 10)]
    assert book.plan[str(cand)] == {"state": RESTING, "node": None}


def test_four_ready_kamis_take_the_nearest_six_of_eight_candidates(make_keeper):
    ready, cands = BIG[:4], BIG[4:]
    world = {k: ("full", 30.0) for k in ready}
    for i, k in enumerate(cands):  # 1..8 HP below full at 30 HP/h = 2, 4, 6, 8, 10, 12, 14, 16 projected minutes
        world[k] = (i + 1, 30.0)
    e, book = _join_env(make_keeper, world, kamis=BIG, startJoinWindowMin=30)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == ready + cands[:6] and st[0]["batchSize"] == 10
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 6
    assert s["startJoined"] == [{"kami": k, "projectedMinutes": _mins(i + 1)} for i, k in enumerate(cands[:6])]
    assert [(t["fn"], t["count"]) for t in _start_txs(e, book)] == [("executeBatched", 10)]  # one transaction, never two
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None} for k in cands[6:])
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in ready + cands[:6])


# ------------------------------------------------------------ d. never a joiner


def test_a_kami_in_start_backoff_is_never_a_joiner(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}, startJoinWindowMin=10)
    e.keeper.runtime[book.id]["backoff"] = {str(B): {"until": e.clock["now"] + 3600, "kind": "start", "reason": "reverted on chain", "since": e.clock["now"]}}
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A]
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 0 and "startJoined" not in s and s["backoffKamis"] == [B]


def test_a_kami_whose_bound_is_unknown_is_never_a_joiner(make_keeper):
    """The held pattern — every preview refused plus a recorded kill, so the
    owner binds and not one of its thresholds was ever served. An
    unknown bound holds the READY kami too (a start, unlike a rest, has no
    floor to fall back on), so nothing is signed this tick and nothing
    joins. The control leg is the SAME world with the previews served: A
    starts and B joins, so the unknown bound is what held them."""
    world = {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}
    e, book = _join_env(make_keeper, world, refuse_previews=True, startJoinWindowMin=10)
    _record_kill(e, book)  # the owner binds, and not one of its thresholds was ever served
    _tick_all(e)
    s = _last_tick(e, book)
    assert _starts(e, book) == [] and s["kamis"]["joined"] == 0 and "startJoined" not in s
    assert s["kamis"]["held"] == 1 and s["held"][0] == {"kami": A, "reason": "bound unknown: attacker not yet previewed", "attackers": s["held"][0]["attackers"]}
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None} for k in RESTING_FOUR)
    # the control: the previews are served, so nothing is unknown
    e2, book2 = _join_env(make_keeper, world, startJoinWindowMin=10)
    _record_kill(e2, book2)
    _tick_all(e2)
    st2 = _starts(e2, book2)
    assert len(st2) == 1 and st2[0]["kamis"] == [A, B] and st2[0]["detail"][1]["startJoined"] is True
    assert _last_tick(e2, book2)["kamis"]["held"] == 0


def _bound_b(e, book) -> None:
    """A killed-here bound of 228 on B (240 HP): the attacker is served 216
    against an occupant (240 HP), so the ratio stand-in prices a kami with
    no row of its own at ratio 0.9 x its own max HP — 216 on B, + 5 % x 240.
    A (250 HP) is bound at 238 and is still READY at 250."""
    _preview_threshold(e.lens, 360, 263, 216)
    _record_kill(e, book)


def test_a_kami_at_or_below_its_bound_plus_an_hour_of_drain_is_never_a_joiner(make_keeper):
    """B is bound at 228 and the keeper holds a 30 HP/h drain rate for it, so
    the ready rule's `need` is 258 — above B's 235 HP. Without that rate the
    `need` is 228 and B joins, so the drain is what held it."""
    world = {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}
    e, book = _join_env(make_keeper, world, startJoinWindowMin=10, safetyMarginPct=5)
    _bound_b(e, book)
    e.keeper.runtime[book.id]["drainRate"] = {str(B): 30.0}
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A] and st[0]["detail"][0]["bound"] == 238
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 0 and "startJoined" not in s
    # the same world WITHOUT the drain rate: B joins, so the `need` is what held it
    e2, book2 = _join_env(make_keeper, world, startJoinWindowMin=10, safetyMarginPct=5)
    _bound_b(e2, book2)
    _tick_all(e2)
    st2 = _starts(e2, book2)
    assert st2[0]["kamis"] == [A, B] and st2[0]["detail"][1]["bound"] == 228
    assert st2[0]["detail"][1]["startJoined"] is True and st2[0]["detail"][1]["drainPerHr"] == 0.0


def test_a_kami_on_the_chains_action_cooldown_is_never_a_joiner(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}, startJoinWindowMin=10)
    _party_row(e.lens, B)["cooldownSec"] = 60
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A]
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 0 and "startJoined" not in s
    assert s["kamis"]["cooldown"] == 1 and s["cooldownDeferred"] == [{"kami": B, "action": "start", "cooldownSec": 60}]


def test_a_ready_kami_is_a_start_not_a_joiner(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: ("full", 30.0), C: (30, 30.0), D: (30, 30.0)}, startJoinWindowMin=10)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A, B]
    assert all("startJoined" not in m for m in st[0]["detail"])
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 0 and "startJoined" not in s


# ------------------------------------------------------------ e. with the start-batch hold


def test_nothing_joins_while_the_hold_holds_and_the_joiner_rides_the_release(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: ("full", 30.0), D: ("full", 30.0)}, startJoinWindowMin=10, startBatchWaitSec=300, startBatchMin=4)
    _tick_all(e)  # t: three ready, under the minimum -> nothing leaves, so nothing joins
    t = e.clock["now"]
    s = _last_tick(e, book)
    assert _starts(e, book) == [] and s["kamis"]["waiting"] == 3 and s["kamis"]["joined"] == 0
    assert [w["kami"] for w in s["startWaiting"]] == [A, C, D] and "startJoined" not in s
    while e.clock["now"] < t + 270:
        _tick_all(e)
        assert _starts(e, book) == [] and _last_tick(e, book)["kamis"]["joined"] == 0
        assert B not in [w["kami"] for w in _last_tick(e, book)["startWaiting"]]
    _tick_all(e)  # t+300: the hold releases the three and B joins them
    assert e.clock["now"] == t + 300
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A, C, D, B] and st[0]["batchSize"] == 4
    assert [m["readySinceSec"] for m in st[0]["detail"]] == [300, 300, 300, 0]
    s = _last_tick(e, book)
    assert s["kamis"]["joined"] == 1 and s["startJoined"] == [{"kami": B, "projectedMinutes": 10.0}] and "startWaiting" not in s
    assert [(t_["fn"], t_["count"]) for t_ in _start_txs(e, book)] == [("executeBatched", 4)]


# ------------------------------------------------------------ f. the batch fallback


def test_a_start_that_did_not_land_re_offers_the_ready_member_only(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}, startJoinWindowMin=10)

    class RaceStarts(DryRunSigner):
        """Every `harvest start` is lost to a nonce race: the batch does not
        land (the core re-offers the members as singles) and the single does
        not either — a race is no verdict on the kami, so nothing backs off."""

        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system == "system.harvest.start":
                    r.status, r.reason = "not_sent", "nonce race: account sequence mismatch, expected 9, got 8"
            return recs

    race = RaceStarts()
    race._grants = e.keeper.signer._grants
    ok = e.keeper.signer
    e.keeper.signer = race
    _tick_all(e)
    tx = _start_txs(e, book)
    # the batch of 2, then ONE fallback single: the READY kami. The joiner was not ready.
    assert [(t["count"], t.get("batchFallback", False), t["kami"]) for t in tx] == [(2, False, None), (1, True, A)]
    assert _last_tick(e, book)["batchFallbackTx"] == 1
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None} for k in (A, B))
    assert e.keeper.runtime[book.id].get("backoff", {}) == {} and _rows(e, book, "deviation") == []
    # next tick: A is still ready and B still inside the window -> the same pair
    e.keeper.signer = ok
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 2 and st[1]["kamis"] == [A, B] and st[1]["detail"][1]["startJoined"] is True
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (A, B))


# ------------------------------------------------------------ g. the stale and chain paths


def test_a_stale_lens_starts_nothing_and_joins_nobody(make_keeper):
    e, book = _join_env(make_keeper, {A: ("full", 30.0), B: (5, 30.0), C: (30, 30.0), D: (30, 30.0)}, startJoinWindowMin=10)
    e.lens.mode = "stale"
    e.lens.lag = 100
    s = e.keeper.tick_book(book, now=e.clock["now"] + 30)
    assert any(f.startswith("lens-stale:") for f in s["degraded"]) and s["acted"] is False
    assert _starts(e, book) == [] and "startJoined" not in s
    assert s["underStaleIdle"] == "REST book: no stop, start or collect on a stale read"


def test_the_chain_fallback_starts_nothing_and_joins_nobody(make_keeper, tmp_path):
    from test_chainhp import FakeHpReader
    from test_phase2f import _env as _chain_env

    # one kami at the floor (a chain-backed rest), one RESTING 5 minutes from full:
    # the chain reading carries no regen rate and the fallback never starts
    reader = FakeHpReader({263: ("HARVESTING", 24, 240, NODE), 508: ("RESTING", 218, 220, None)})
    e, book = _chain_env(make_keeper, tmp_path, reader, [263, 508], chainFallbackAfterSec=0, startJoinWindowMin=10, resumeHpPct=100)
    e.lens.mode = "not-ready"
    s = e.keeper.tick_book(book, now=T + 30)
    assert s["underChain"] is True and s["chainRested"] == [263] and "startJoined" not in s
    assert [d["decision"]["kind"] for d in _rows(e, book, "decision")] == ["rest"]  # never a start under the fallback
    assert [r.system for r in e.keeper.signer.recorded] == ["system.harvest.stop"]


# ------------------------------------------------------------ h. describe and the default


def test_describe_states_the_key_and_the_default_is_off(make_keeper):
    assert schema_defaults("auto")["startJoinWindowMin"] == 0
    text = describe_type("auto", "fleet")["description"]
    line = next(line for line in text.splitlines() if line.startswith("- startJoinWindowMin "))
    assert line.startswith("- startJoinWindowMin (integer, min):") and line.endswith("[0..30; default 0]")
    assert "resumeHpPct" in line
