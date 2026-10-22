"""A served line is a lens number WITH vitals; an attacker-side blocker is
not a line — on the recorded node previews and end to end through an
`auto` book:

  1. the degraded shape of a lens flap (no vitals, ATTACKER_STARVING,
     threshold 0) never replaces a line: every row is
     CARRIED from the entry it would have replaced;
  2. a row WITH vitals under an attacker-side reason and a non-positive
     threshold is invalid too WHEN THE ATTACKER'S OWN ROW LACKS VITALS,
     while a THRESHOLD_ZERO row with vitals stays a valid 0 (the
     lens says this attacker cannot kill that kami);
  3. a degraded entry is due again after `previewRetrySec`, never at
     `previewRefreshSec` or REFRESH_NO_THRESHOLD_SEC;
  4. `invalid_rows` and the carried marks survive the round trip;
  5. end to end: a flap during a refresh leaves the book's bound where it
     was, counts `previewInvalidRows` on the tick, and still rests a kami
     that reads below the carried line.

Clause 2 is narrow — a non-positive threshold under an `ATTACKER_*` reason is the lens's normal
answer for a weak attacker read on its cooldown, so it is a LINE as long
as the attacker's own row carries vitals:

  a. the weak-attacker-on-cooldown answer is stored as served and ages at
     `previewRefreshSec` (86400 when every row is <= 0), not at 300 s;
  b. the same target rows with the attacker's own row stripped of vitals
     are refused (`attacker row without vitals`), carried, retried at 300 s;
  c. an attacker with NO row in the answer counts as no vitals.
"""

from __future__ import annotations

import json
from typing import Any

from keeper.previews import REFRESH_NO_THRESHOLD_SEC, PreviewCache, PreviewEntry, valid_row
from keeper.strategies.base import node_occupant
from test_phase2a import CAROL, NODE, _decisions, _last_tick, _preview_threshold, _set_party
from test_phase2g import _book, _env, _preview_reads, _tick
from test_phase2h import _bounds
from test_previews import ATT157, OURS, _foreign, _lens

DEGRADED = {"eligible": False, "reason": "ATTACKER_STARVING", "threshold": 0, "margin": 0, "spoils": 0, "salvage": 0, "recoil": 0}


def _row(fake, attacker: int, kami: int) -> dict[str, Any]:
    return next(h for h in fake.previews[79][attacker]["harvests"] if h["kami"]["index"] == kami)


def _own_att157(fake) -> None:
    """A private copy of the second attacker's recorded preview: the tests below
    rewrite its rows in place and the module-level fixture object is shared
    with every other test that reads it."""
    fake.previews[79][448] = json.loads(json.dumps(ATT157))


# ------------------------------------------------- 1. the flap's shape


def test_degraded_rows_never_replace_a_line(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 1000.0)
    foreign = _foreign()
    first, _changed, _ans = cache.fetch(lens, 79, 448, foreign[448], OURS, 1000.0)
    lines = {k: first.clamped(k) for k in first.rows}
    assert len(lines) == 107 and first.max_clamped() == 10 and first.invalid_rows == {} and first.degraded_read is False
    ratio = first.max_ratio()
    assert ratio > 0
    # the same attacker answers in the flap's shape: no vitals, ATTACKER_STARVING, threshold 0
    fake.set_degraded_preview(79, 448, True)
    ans = lens.node(79, attacker=448, with_vitals=True, full=True)
    served = [h for h in ans.data["harvests"] if h["account"]["index"] == OURS]
    assert len(served) == 107 and all("vitals" not in h and h["liquidation"] == DEGRADED for h in served)
    second, changed, _a = cache.fetch(lens, 79, 448, foreign[448], OURS, 2000.0)
    # every line is the one the flap would have zeroed, carried from the old entry
    assert {k: second.clamped(k) for k in second.rows} == lines
    assert all(r["carriedFrom"] == 1000.0 for r in second.rows.values())
    assert set(second.invalid_rows) == set(lines) and all(v.startswith("no vitals:") for v in second.invalid_rows.values())
    assert second.invalid_rows[263] == "no vitals: vitals"
    assert changed is False and second.degraded_read is True
    assert second.max_clamped() == 10 and second.max_ratio() == ratio and second.stand_in(240) == first.stand_in(240)
    fake.stop()


# ------------------------------------------- 2. attacker-side vs THRESHOLD_ZERO


def test_attacker_side_negative_with_vitals_is_invalid_but_threshold_zero_is_a_line(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    _own_att157(fake)
    # the attacker's OWN row carries no vitals either — the flap's
    # shape, in which an attacker-side non-positive threshold is not a line.
    # (With the attacker's vitals present the same row IS a line: the unit
    # assertions at the end of this test and test_2sa_a below.)
    _row(fake, 448, 448).pop("vitals")
    cache = PreviewCache(tmp_path, lambda: 10.0)
    foreign = _foreign()
    _row(fake, 448, 508)["liquidation"].update({"threshold": 120, "reason": "TARGET_HP_ABOVE_THRESHOLD"})
    first, _c, _a = cache.fetch(lens, 79, 448, foreign[448], OURS, 10.0)
    assert first.clamped(508) == 120 and first.clamped(699) == 0
    # a row with vitals, threshold -16, ATTACKER_COOLDOWN — the attacker's own gate, not the target kami's line
    _row(fake, 448, 508)["liquidation"].update({"threshold": -16, "reason": "ATTACKER_COOLDOWN"})
    # a row with vitals, threshold 0, THRESHOLD_ZERO — the lens says this attacker cannot kill it
    _row(fake, 448, 699)["liquidation"].update({"threshold": 0, "reason": "THRESHOLD_ZERO"})
    second, _c2, _a2 = cache.fetch(lens, 79, 448, foreign[448], OURS, 20.0)
    assert second.clamped(508) == 120 and second.rows[508]["carriedFrom"] == 10.0 and second.rows[508]["threshold"] == 120
    assert second.invalid_rows[508] == "attacker-side ATTACKER_COOLDOWN with threshold -16; attacker row without vitals"
    assert second.clamped(699) == 0 and 699 not in second.invalid_rows and "carriedFrom" not in second.rows[699]
    assert set(second.invalid_rows) == {508} and second.degraded_read is True
    # the rule itself, on the rows the lens serves
    vit = {"hp": {"current": 100, "total": 250, "percent": 40}, "level": 36, "cooldownSec": 0, "musuAccrued": 0}
    base = {"id": "0x1", "kami": {"id": "0x2", "index": 11653}, "account": {"index": OURS}, "vitals": vit}
    assert valid_row(node_occupant(dict(base, liquidation={"threshold": -16, "reason": "ATTACKER_COOLDOWN"})), False) == "attacker-side ATTACKER_COOLDOWN with threshold -16; attacker row without vitals"
    assert valid_row(node_occupant(dict(base, liquidation={"threshold": -16, "reason": "ATTACKER_COOLDOWN"})), True) is None  # the attacker's own vitals are there — a line as served
    assert valid_row(node_occupant(dict(base, liquidation={"threshold": 0, "reason": "THRESHOLD_ZERO"})), False) is None
    assert valid_row(node_occupant(dict(base, liquidation={"threshold": 145, "reason": "ATTACKER_STARVING"})), False) is None  # positive: a line whatever the reason
    no_vitals = {"id": "0x1", "kami": {"id": "0x2", "index": 11653}, "account": {"index": OURS}, "liquidation": dict(DEGRADED)}
    assert valid_row(node_occupant(no_vitals), True) == "no vitals: vitals"  # clause 1 stands whatever the attacker served
    fake.stop()


# ------------------------------------------------------ 3. due again at retry


def test_degraded_entry_is_due_after_retry_sec(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    occ = _foreign()[360]
    only = {360: occ}
    ents = cache.entries(79, OURS)
    ents[360] = PreviewEntry(79, 360, 0.0, 1, occ.level, occ.account_index, {263: {"threshold": 100, "level": 38, "hpTotal": 240, "carriedFrom": 0.0}}, 0, None, {263: "no vitals: vitals"})
    assert cache.due(79, OURS, only, {263: 38}, 3600, 299.0) == []
    assert cache.due(79, OURS, only, {263: 38}, 3600, 300.0) == [(360, "degraded read: 1 invalid rows; retry")]
    assert cache.next_due_at(79, OURS, 3600, {360}) == 300.0
    assert cache.due(79, OURS, only, {263: 38}, 3600, 299.0, retry_sec=120) == [(360, "degraded read: 1 invalid rows; retry")]
    assert cache.next_due_at(79, OURS, 3600, {360}, retry_sec=120) == 120.0
    # the same entry read cleanly: every row THRESHOLD_ZERO WITH vitals is a
    # valid 0, so it waits the no-threshold interval
    ents[360] = PreviewEntry(79, 360, 0.0, 1, occ.level, occ.account_index, {263: {"threshold": -18, "level": 38, "reason": "THRESHOLD_ZERO", "hpTotal": 240}}, 0, None)
    assert cache.due(79, OURS, only, {263: 38}, 3600, 3600.0) == [] and cache.due(79, OURS, only, {263: 38}, 3600, 300.0) == []
    assert cache.due(79, OURS, only, {263: 38}, 3600, float(REFRESH_NO_THRESHOLD_SEC)) == [(360, f"older than {REFRESH_NO_THRESHOLD_SEC} s")]
    assert cache.next_due_at(79, OURS, 3600, {360}) == float(REFRESH_NO_THRESHOLD_SEC)


# ------------------------------------------------------------ 4. persistence


def test_round_trip_keeps_invalid_rows_and_carried_marks(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 5.0)
    cache.fetch(lens, 79, 448, _foreign()[448], OURS, 5.0)
    fake.set_degraded_preview(79, None, True)  # every attacker on the node
    cache.fetch(lens, 79, 448, _foreign()[448], OURS, 6.0)
    assert cache.save(79, OURS) is True
    again = PreviewCache(tmp_path, lambda: 7.0)
    e = again.entries(79, OURS)[448]
    assert e.degraded_read is True and len(e.invalid_rows) == 107
    assert e.invalid_rows[263] == "no vitals: vitals" and all(isinstance(k, int) for k in e.invalid_rows)
    assert e.rows[263]["carriedFrom"] == 5.0 and e.clamped(263) == 0 and e.max_clamped() == 10
    # and it is due again at the retry interval, not at 86400
    assert again.next_due_at(79, OURS, 3600, {448}) == 6.0 + 300
    fake.stop()


# ---------------------------------------------------------- 5. end to end


def test_book_bound_survives_a_degraded_preview(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    # one hostile attacker on the node, serving 100 against the book's kami
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] == CAROL or h["kami"]["index"] == 360]
    _preview_threshold(e.lens, 360, 263, 100)
    book = _book(e, [263])
    _tick(e)
    s = _last_tick(e, book)
    assert _preview_reads(e) == [360] and s["previewCalls"] == 1 and s["previewInvalidRows"] == 0
    _strat, _obs, lim, _c, _a = _bounds(e, book)
    bound = lim[263]["bound"]
    assert bound == 105 and lim[263]["boundBy"]["threshold"] == 100  # the served line + the 2 % passive margin of 240
    entry = e.keeper.previews.entries(NODE, CAROL)[360]
    first_at = entry.fetched_at
    # the lens flaps while the refresh is due, and the kami reads one HP under its line
    e.lens.set_degraded_preview(NODE, 360, True)
    _set_party(e.lens, 263, hp=(bound - 1, 240))
    _tick(e, 3601.0)
    s = _last_tick(e, book)
    assert s["previewCalls"] == 1 and s["previewInvalidRows"] == 107
    _strat, _obs, lim2, _c, _a = _bounds(e, book)
    assert lim2[263]["bound"] == bound  # UNCHANGED: the flap's zeros never became the line
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "rest")] == [[263]]
    ent = e.keeper.previews.entries(NODE, CAROL)[360]
    assert ent.fetched_at > first_at and ent.degraded_read is True and ent.clamped(263) == 100 and ent.rows[263]["carriedFrom"] == first_at
    # the flap passes: the entry is refreshed at the retry interval, not 86400
    e.lens.set_degraded_preview(NODE, 360, False)
    _tick(e, 300.0)
    ent2 = e.keeper.previews.entries(NODE, CAROL)[360]
    assert ent2.fetched_at == ent.fetched_at + 300.0 and ent2.invalid_rows == {} and ent2.degraded_read is False
    assert "carriedFrom" not in ent2.rows[263] and ent2.clamped(263) == 100
    assert _last_tick(e, book)["previewInvalidRows"] == 0


# ---------------------------- a weak attacker on cooldown serves lines


def test_2sa_a_non_positive_threshold_with_vitals_on_both_sides_is_a_line(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    _own_att157(fake)
    assert "vitals" in _row(fake, 448, 448)  # the attacker's own row is complete: the lens is healthy
    # a weak attacker read inside its cooldown answers
    # every pair it cannot kill with an attacker-side reason and the same
    # threshold it serves as THRESHOLD_ZERO once the cooldown is over
    _row(fake, 448, 508)["liquidation"].update({"threshold": -8, "reason": "ATTACKER_COOLDOWN"})
    _row(fake, 448, 699)["liquidation"].update({"threshold": 0, "reason": "ATTACKER_STARVING"})
    cache = PreviewCache(tmp_path, lambda: 100.0)
    foreign = _foreign()
    only = {448: foreign[448]}
    e, _c, _a = cache.fetch(lens, 79, 448, foreign[448], OURS, 100.0)
    assert e.invalid_rows == {} and e.degraded_read is False and len(e.rows) == 107
    assert e.rows[508] == {"threshold": -8, "level": 39, "reason": "ATTACKER_COOLDOWN", "hpTotal": 220}
    assert e.rows[699] == {"threshold": 0, "level": 38, "reason": "ATTACKER_STARVING", "hpTotal": 220}
    assert e.clamped(508) == 0 and e.clamped(699) == 0 and e.max_clamped([508, 699]) == 0
    assert e.max_ratio([508, 699]) == 0.0 and e.stand_in(240, [508, 699]) == (0, True)
    # an entry with positive rows elsewhere ages at `previewRefreshSec`
    assert cache.due(79, OURS, only, {}, 3600, 400.0) == []  # NOT the 300 s retry
    assert cache.due(79, OURS, only, {}, 3600, 3700.0) == [(448, "older than 3600 s")]
    assert cache.next_due_at(79, OURS, 3600, {448}) == 3700.0
    # every row <= 0 with vitals on both sides: the no-threshold interval,
    # exactly as for any all-zero entry
    for h in fake.previews[79][448]["harvests"]:
        if h["account"]["index"] == OURS:
            h["liquidation"].update({"threshold": -8, "reason": "ATTACKER_COOLDOWN"})
    e2, _c2, _a2 = cache.fetch(lens, 79, 448, foreign[448], OURS, 200.0)
    assert e2.invalid_rows == {} and len(e2.rows) == 107 and e2.max_clamped() == 0 and e2.max_ratio() == 0.0
    assert cache.due(79, OURS, only, {}, 3600, 500.0) == [] and cache.due(79, OURS, only, {}, 3600, 3800.0) == []
    assert cache.next_due_at(79, OURS, 3600, {448}) == 200.0 + REFRESH_NO_THRESHOLD_SEC
    fake.stop()


def test_2sa_b_the_same_rows_are_refused_when_the_attacker_row_has_no_vitals(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    _own_att157(fake)
    cache = PreviewCache(tmp_path, lambda: 100.0)
    foreign = _foreign()
    only = {448: foreign[448]}
    first, _c, _a = cache.fetch(lens, 79, 448, foreign[448], OURS, 100.0)
    assert first.rows[508]["threshold"] == -18 and first.rows[699]["threshold"] == -17 and first.invalid_rows == {}
    # the flap takes the attacker's vitals with everyone else's
    _row(fake, 448, 448).pop("vitals")
    _row(fake, 448, 508)["liquidation"].update({"threshold": -8, "reason": "ATTACKER_COOLDOWN"})
    _row(fake, 448, 699)["liquidation"].update({"threshold": 0, "reason": "ATTACKER_STARVING"})
    second, changed, _a2 = cache.fetch(lens, 79, 448, foreign[448], OURS, 200.0)
    assert set(second.invalid_rows) == {508, 699} and second.degraded_read is True and changed is False
    assert second.invalid_rows[508] == "attacker-side ATTACKER_COOLDOWN with threshold -8; attacker row without vitals"
    assert second.invalid_rows[699] == "attacker-side ATTACKER_STARVING with threshold 0; attacker row without vitals"
    assert second.rows[508] == {"threshold": -18, "level": 39, "reason": "THRESHOLD_ZERO", "hpTotal": 220, "carriedFrom": 100.0}
    assert second.rows[699]["threshold"] == -17 and second.rows[699]["carriedFrom"] == 100.0
    assert cache.due(79, OURS, only, {}, 3600, 499.0) == []
    assert cache.due(79, OURS, only, {}, 3600, 500.0) == [(448, "degraded read: 2 invalid rows; retry")]
    assert cache.next_due_at(79, OURS, 3600, {448}) == 500.0
    fake.stop()


def test_2sa_c_an_attacker_with_no_row_in_the_answer_counts_as_no_vitals(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    _own_att157(fake)
    cache = PreviewCache(tmp_path, lambda: 100.0)
    foreign = _foreign()
    only = {448: foreign[448]}
    cache.fetch(lens, 79, 448, foreign[448], OURS, 100.0)
    doc = fake.previews[79][448]
    doc["harvests"] = [h for h in doc["harvests"] if h["kami"]["index"] != 448]  # the attacker is not harvesting on the node
    _row(fake, 448, 508)["liquidation"].update({"threshold": -8, "reason": "ATTACKER_COOLDOWN"})
    second, _c2, _a2 = cache.fetch(lens, 79, 448, foreign[448], OURS, 200.0)
    assert set(second.invalid_rows) == {508} and second.degraded_read is True
    assert second.invalid_rows[508] == "attacker-side ATTACKER_COOLDOWN with threshold -8; attacker row without vitals"
    assert second.rows[508]["threshold"] == -18 and second.rows[508]["carriedFrom"] == 100.0
    assert cache.due(79, OURS, only, {}, 3600, 500.0) == [(448, "degraded read: 1 invalid rows; retry")]
    fake.stop()
