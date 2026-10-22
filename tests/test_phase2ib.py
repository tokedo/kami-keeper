"""The stop PACK:

  a. `stopJoinWindowMin` 0 (the default) — one stop of
     the anchor alone, no `stopPulled`, no `pulled` key, `kamis.pulled` 0;
  b. window 10: the kami 10 projected minutes from its OWN limit joins the
     anchor's stop, the one 30 minutes away and the one with no drain rate
     this tick do not;
  c. `stopPackMax` caps the pack — 2 takes the nearest joiner only, 1 takes
     none (the anchors already fill it);
  d. anchors beyond the cap still go out this tick and chunk at 12;
  e. never a joiner: a kami in rest backoff, a kami whose bound is UNKNOWN,
     and the projection is from max(bound, restFloor), not the floor alone;
  f. a pack that did not land re-offers its ANCHORS as singles; the joiner
     keeps its plan, is not backed off, and joins the next pack;
  g. the stale-lens and chain-fallback paths add no joiner;
  h. `describe` states both keys, and their defaults are 0 / 12.

The book is the four kamis of `tests/test_phase2c.py` (RESTING_FOUR), put
HARVESTING on the node before the book is declared so the plan adopts the
world. Every attacker is synthesized at threshold 0 (`previews[NODE] = {}`),
so a kami's limit is its rest floor: 10 % of 250 / 240 / 230 / 250 =
25 / 24 / 23 / 25 HP.
"""

from __future__ import annotations

from typing import Any

from keeper.books import schema_defaults
from keeper.describe import describe_type
from keeper.kills import BLOCKS_PER_HOUR
from keeper.signer import DryRunSigner
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import CAROL, NODE, T, _add_occupant, _decisions, _last_tick, _party_row, _preview_threshold, _rows, _seed, _set_party, _start, _tick_all
from test_phase2c import RESTING_FOUR, TOTALS

# the four kamis hold 250 / 240 / 230 / 250 HP; restFloorPct 10, so
# the limits are 25 / 24 / 23 / 25 HP
A, B, C, D = RESTING_FOUR


def _harvest(e, kami: int, hp: int, rate: float | None) -> None:
    """`kami` HARVESTING on the node at `hp`, draining `rate` HP/h (None =
    the party row carries no `hpRatePerHr`: the keeper reads no rate)."""
    total = TOTALS[kami]
    _set_party(e.lens, kami, state=HARVESTING, node=NODE, hp=(hp, total))
    row = _party_row(e.lens, kami)
    if rate is None:
        row.pop("hpRatePerHr", None)
    else:
        row["hpRatePerHr"] = f"-{float(rate):.2f}"
    if not any(h["kami"]["index"] == kami for h in e.lens.nodes[NODE]["harvests"]):
        _add_occupant(e.lens, kami, CAROL, hp=(hp, total), level=row["level"])


def _pack_env(make_keeper, world: dict[int, tuple[int, float | None]], refuse_previews: bool = False, **over: Any):
    """A lab dry-run keeper on the recorded fixtures with ONE auto REST book
    over the four kamis, each HARVESTING on the node as `world` says
    (kami -> (hp, rate)). No recorded preview: every attacker is
    synthesized at threshold 0, so every bound is 0."""
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    e.lens.previews[NODE] = {}
    if refuse_previews:
        e.lens.synth_default = None  # no recorded and no synthesized preview: every read is refused
    e.grant()
    for kami, (hp, rate) in world.items():
        _harvest(e, kami, hp, rate)
    book = _start(e, RESTING_FOUR, previewsPerTick=50, **over)
    e.books = [book]  # type: ignore[attr-defined]
    return e, book


def _record_kill(e, book, owner: int = 806, attacker: int = 360) -> None:
    """`owner` killed one of ours on the node an hour ago, so its attackers
    BIND (a killed-here class). Without a kill nobody
    whose threshold is 0 or unknown binds at all, so nothing is ever
    `unknown` and nothing is held."""
    head = e.keeper.strategies[book.id].observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill({"block": head - BLOCKS_PER_HOUR, "txHash": "0x01", "logIndex": 0, "node": NODE, "victimKami": 5946, "victimAccount": CAROL, "attackerKami": attacker, "attackerAccount": owner, "attribution": "occupant"})


def _rests(e, book) -> list[dict[str, Any]]:
    return [d["parameters"] for d in _decisions(e, book, "rest")]


def _stop_txs(e, book) -> list[dict[str, Any]]:
    return [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["system"] == "system.harvest.stop"]


# ------------------------------------------------------------ a. the default


def test_window_zero_is_todays_behaviour_only_the_anchor_is_rested(make_keeper):
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (30, 30), C: (60, 30), D: (100, 30)})
    assert book.config["stopJoinWindowMin"] == 0 and book.config["stopPackMax"] == 12
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A] and r[0]["batchSize"] == 1
    assert [m["kami"] for m in r[0]["detail"]] == [A] and "pulled" not in r[0]["detail"][0] and "projectedMinutes" not in r[0]["detail"][0]
    s = _last_tick(e, book)
    assert "stopPulled" not in s and s["kamis"]["pulled"] == 0
    # the tick row shape, plus the pack count
    assert set(s["kamis"]) == {"harvesting", "resting", "dead", "held", "backoff", "elsewhere", "waiting", "pulled", "joined", "cooldown"}  # with the `cooldown` and `joined` counts
    assert s["kamis"]["harvesting"] == 4 and s["kamis"]["resting"] == 0 and s["minMarginHp"] == 25
    assert book.plan[str(A)] == {"state": RESTING, "node": None, "pending": True}
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (B, C, D))


# ------------------------------------------------------------ b. the pack


def test_a_kami_ten_minutes_from_its_own_limit_joins_the_anchors_stop(make_keeper):
    # B: (29 - 24) / 30 x 60 = 10.0 min; C: (38 - 23) / 30 x 60 = 30 min; D has no rate
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (38, 30), D: (27, None)}, stopJoinWindowMin=10)
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A, B] and r[0]["batchSize"] == 2
    anchor, joiner = r[0]["detail"]
    assert "pulled" not in anchor and "projectedMinutes" not in anchor and anchor["kami"] == A
    assert joiner == {"kami": B, "hp": 29, "hpTotal": 240, "bound": 0, "restFloor": 24, "boundBy": None, "drainPerHr": 30.0, "pulled": True, "projectedMinutes": 10.0}
    s = _last_tick(e, book)
    assert s["stopPulled"] == [{"kami": B, "projectedMinutes": 10.0}] and s["kamis"]["pulled"] == 1
    assert s["kamis"]["harvesting"] == 4  # a joiner is still harvesting at read time
    tx = _stop_txs(e, book)
    assert [(t["fn"], t["count"]) for t in tx] == [("executeBatchedAllowFailure", 2)]
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True} for k in (A, B))
    assert all(book.plan[str(k)] == {"state": HARVESTING, "node": NODE} for k in (C, D))


# ------------------------------------------------------------ c. the pack cap


def test_the_pack_cap_takes_the_nearest_joiners_and_no_more(make_keeper):
    # B 10 min, C (33 - 23) / 30 x 60 = 20 min, D (35 - 25) / 24 x 60 = 25 min
    world = {A: (25, 30), B: (29, 30), C: (33, 30), D: (35, 24)}
    e, book = _pack_env(make_keeper, world, stopJoinWindowMin=30, stopPackMax=2)
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A, B] and r[0]["batchSize"] == 2
    assert _last_tick(e, book)["stopPulled"] == [{"kami": B, "projectedMinutes": 10.0}]
    # cap 1: the anchor alone already fills the pack, so nothing is projected
    e2, book2 = _pack_env(make_keeper, world, stopJoinWindowMin=30, stopPackMax=1)
    _tick_all(e2)
    r2 = _rests(e2, book2)
    assert len(r2) == 1 and r2[0]["kamis"] == [A] and _last_tick(e2, book2)["kamis"]["pulled"] == 0
    assert "stopPulled" not in _last_tick(e2, book2)


# ------------------------------------------------------------ d. anchors beyond the cap


def test_anchors_beyond_the_pack_cap_still_go_out_and_chunk_at_the_stop_cap(make_keeper):
    # three anchors at their floors and D 10 min away: no joiner, one tx of 3
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (24, 30), C: (23, 30), D: (30, 30)}, stopJoinWindowMin=30, stopPackMax=2)
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A, B, C] and r[0]["batchSize"] == 3
    assert all("pulled" not in m for m in r[0]["detail"])
    s = _last_tick(e, book)
    assert s["kamis"]["pulled"] == 0 and "stopPulled" not in s
    assert [(t["fn"], t["count"]) for t in _stop_txs(e, book)] == [("executeBatchedAllowFailure", 3)]
    assert book.plan[str(D)] == {"state": HARVESTING, "node": NODE}


# ------------------------------------------------------------ e. never a joiner


def test_a_kami_in_rest_backoff_is_never_a_joiner(make_keeper):
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (60, 30), D: (100, 30)}, stopJoinWindowMin=10)
    e.keeper.runtime[book.id]["backoff"] = {str(B): {"until": e.clock["now"] + 3600, "kind": "rest", "reason": "reverted on chain", "since": e.clock["now"]}}
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A]
    s = _last_tick(e, book)
    assert s["kamis"]["pulled"] == 0 and "stopPulled" not in s and s["backoffKamis"] == [B]


def test_a_kami_whose_bound_is_unknown_is_never_a_joiner(make_keeper):
    # refused before the book is declared: `start` observes too, and an entry
    # served once at threshold 0 would not be unknown
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (60, 30), D: (100, 30)}, refuse_previews=True, stopJoinWindowMin=10)
    _record_kill(e, book)  # the owner binds, and not one of its thresholds was ever served
    _tick_all(e)
    s = _last_tick(e, book)
    assert s["kamis"]["held"] == 4 and s["held"][0]["reason"] == "bound unknown: attacker not yet previewed"
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A]  # the floor still rests the anchor
    assert s["kamis"]["pulled"] == 0 and "stopPulled" not in s


def test_the_projection_is_to_max_bound_and_floor_not_to_the_floor_alone(make_keeper):
    """B is bound at 100 (a killed-here attacker's 88 + 5 % x 240 = 100), so
    at 110 HP and 30 HP/h it is 20 minutes from its OWN limit — inside a
    30-minute window, outside a 10-minute one. Against the floor (24) it
    would read 172 minutes and never join."""
    for window, joins in ((10, False), (30, True)):
        e, book = _pack_env(make_keeper, {A: (25, 30), B: (110, 30), C: (60, 30), D: (100, 30)}, stopJoinWindowMin=window, safetyMarginPct=5)
        _preview_threshold(e.lens, 360, B, 88)
        _record_kill(e, book)
        _tick_all(e)
        r = _rests(e, book)
        assert len(r) == 1 and r[0]["kamis"] == ([A, B] if joins else [A]), window
        if joins:
            m = r[0]["detail"][1]
            assert m["bound"] == 100 and m["restFloor"] == 24 and m["projectedMinutes"] == 20.0 and m["pulled"] is True
            assert _last_tick(e, book)["stopPulled"] == [{"kami": B, "projectedMinutes": 20.0}]


# ------------------------------------------------------------ f. the batch fallback


def test_a_pack_that_did_not_land_re_offers_its_anchors_only_and_the_joiner_waits(make_keeper):
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (60, 30), D: (100, 30)}, stopJoinWindowMin=10)

    class RaceStops(DryRunSigner):
        """Every `harvest stop` is lost to a nonce race: the batch does not
        land (the core re-offers the members as singles) and the single does
        not either — a race is no verdict on the kami, so nothing backs off."""

        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system == "system.harvest.stop":
                    r.status, r.reason = "not_sent", "nonce race: account sequence mismatch, expected 9, got 8"
            return recs

    race = RaceStops()
    race._grants = e.keeper.signer._grants
    ok = e.keeper.signer
    e.keeper.signer = race
    _tick_all(e)
    tx = _stop_txs(e, book)
    # the pack of 2, then ONE fallback single: the anchor. The joiner was not due.
    assert [(t["count"], t.get("batchFallback", False), t["kami"]) for t in tx] == [(2, False, None), (1, True, A)]
    assert _last_tick(e, book)["batchFallbackTx"] == 1
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE} and book.plan[str(B)] == {"state": HARVESTING, "node": NODE}
    assert e.keeper.runtime[book.id].get("backoff", {}) == {} and _rows(e, book, "deviation") == []
    # next tick: the anchor is still at its limit and the joiner still inside the window
    e.keeper.signer = ok
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 2 and r[1]["kamis"] == [A, B] and r[1]["detail"][1]["pulled"] is True
    assert all(book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True} for k in (A, B))


# ------------------------------------------------------------ g. the stale and chain paths


def test_a_stale_lens_rests_nothing_and_pulls_nobody(make_keeper):
    # 5 projected minutes from its limit (26.5 -> 24 at 30 HP/h), and it stays
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (27, 30), C: (60, 30), D: (100, 30)}, stopJoinWindowMin=10)
    e.lens.mode = "stale"
    e.lens.lag = 100
    s = e.keeper.tick_book(book, now=e.clock["now"] + 30)
    assert any(f.startswith("lens-stale:") for f in s["degraded"]) and s["acted"] is False
    assert _rests(e, book) == [] and "stopPulled" not in s
    assert s["underStaleIdle"] == "REST book: no stop, start or collect on a stale read"


def test_the_chain_fallback_rests_the_floor_and_pulls_nobody(make_keeper, tmp_path):
    from test_chainhp import FakeHpReader
    from test_phase2f import _env as _chain_env

    # one kami at the floor (24 of 240), one below it, one far above: no drain rate
    # is read from the chain at all, so no kami can be projected there
    reader = FakeHpReader({263: ("HARVESTING", 24, 240, NODE), 508: ("HARVESTING", 12, 220, NODE), 699: ("HARVESTING", 100, 220, NODE)})
    e, book = _chain_env(make_keeper, tmp_path, reader, [263, 508, 699], chainFallbackAfterSec=0, stopJoinWindowMin=10)
    e.lens.mode = "not-ready"
    s = e.keeper.tick_book(book, now=T + 30)
    assert s["underChain"] is True and s["chainRested"] == [263, 508] and "stopPulled" not in s
    d = _rows(e, book, "decision")[-1]["decision"]["parameters"]
    assert d["kamis"] == [263, 508] and all("pulled" not in m for m in d["detail"])


# ------------------------------------------------------------ h. describe and the defaults


def test_describe_states_both_keys_and_the_defaults_are_off(make_keeper):
    d = schema_defaults("auto")
    assert d["stopJoinWindowMin"] == 0 and d["stopPackMax"] == 12
    text = describe_type("auto", "fleet")["description"]
    window = next(line for line in text.splitlines() if line.startswith("- stopJoinWindowMin "))
    pack = next(line for line in text.splitlines() if line.startswith("- stopPackMax "))
    assert window.startswith("- stopJoinWindowMin (integer, min):") and window.endswith("[0..60; default 0]")
    assert pack.startswith("- stopPackMax (integer, kamis):") and pack.endswith("[1..12; default 12]")
    assert "stopPackMax" in window and "stopJoinWindowMin" in pack  # each names the other
