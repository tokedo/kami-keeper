"""A stand-in threshold scales with the kami's own max HP, on the
recorded node fixtures. The served line is a RATIO of the victim's max HP
(the chain: `threshold = raw x victimMaxHP / precision`), so taking the
ABSOLUTE row of the largest occupant would price a smaller resting kami
at 94 % of its own HP and hold it indefinitely — it could never start, so
it would never become an occupant and never earn a row of its own:

  A. a 240-HP kami under a 226 row of a 320-HP kami is bound at 182, not
     238, and starts;
  B. a larger kami is never priced LOWER than the absolute row;
  C. rows that carry no `hpTotal`: by itself the entry can only serve the
     absolute maximum (through the book it scales on the book's own
     figure);
  D. the owner stand-in scales the same way;
  E. `fetch()` records the served max HP of every row.

An ABSENT but remembered attacker's entry is never refreshed (`due()`
iterates the PRESENT occupants), so a legacy entry would stand in at the
ABSOLUTE maximum for the whole window — a remembered attacker whose line
on a 320-HP kami is 209 would hold every 200-HP kami of the book. A
legacy row's denominator is therefore the BOOK's own max HP for that kami:

  F. `hp_totals` scales a legacy row (209 / 320 x 200 -> 131);
  G. with neither source the absolute maximum still stands in;
  H. through the book: the remembered legacy entry binds the row-less
     200-HP kami at 141, not 219, and it starts.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from fakelens import FakeLens
from keeper.lens import LensClient
from keeper.previews import PreviewCache, PreviewEntry
from keeper.strategies.base import RESTING
from test_phase2a import CAROL, NODE, _decisions, _preview_threshold, _set_party
from test_phase2g import H, LAZY, _book, _env, _preview_reads, _serve_kami, _tick_one
from test_phase2gb import _held_kamis
from test_phase2h import _bounds, _kill
from test_previews import ATT92, NODE79, OURS

PROXY1 = "max served ratio over the account's occupants x this kami's max HP (no row for this kami)"
PROXY1_PRE_2O = "max over the account's occupants (no row for this kami)"
PROXY_OWNER = "max served ratio over the owner's previewed attackers x this kami's max HP (this attacker not yet previewed)"
PROXY_OWNER_PRE_2O = "max over the owner's previewed attackers (this attacker not yet previewed)"


# ------------------------------------------------------------ helpers


def _preview_rows(e, attacker: int) -> list[dict[str, Any]]:
    return e.lens.previews[NODE][attacker]["harvests"]


def _preview_hp(e, attacker: int, kami: int, hp: tuple[int, int]) -> None:
    """The max HP the preview of `attacker` SERVES for `kami` — the
    denominator of the served ratio."""
    row = next(h for h in _preview_rows(e, attacker) if h["kami"]["index"] == kami)
    row["vitals"]["hp"] = {"current": hp[0], "total": hp[1], "percent": round(100 * hp[0] / hp[1])}


def _drop_preview_row(e, attacker: int, kami: int) -> None:
    e.lens.previews[NODE][attacker]["harvests"] = [h for h in _preview_rows(e, attacker) if h["kami"]["index"] != kami]


def _rest_off_node(e, kami: int, hp: tuple[int, int]) -> None:
    """`kami` RESTING at `hp`, off the node — the preview serves rows for
    OCCUPANTS only, so it earns none."""
    _set_party(e.lens, kami, state=RESTING, node=None, hp=hp)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


def _strip_hp_totals(entry: PreviewEntry) -> PreviewEntry:
    """The same entry in the legacy shape: no `hpTotal` on any row."""
    d = copy.deepcopy(entry.to_dict())
    d["rows"] = {k: {kk: vv for kk, vv in r.items() if kk != "hpTotal"} for k, r in d["rows"].items()}
    return PreviewEntry.from_dict(json.loads(json.dumps(d)))


def _world(make_keeper, tmp_path, victim_hp: tuple[int, int] = (240, 240), **over: Any):
    """An owner keeps ONE kami on the node — an attacker that killed here —
    serving 226 against one of the book's kamis, a 320-HP occupant (70.6 %);
    another book kami rests at full HP off the node, so the attacker's
    preview has no row for it."""
    e = _env(make_keeper, tmp_path)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] != 806 or h["kami"]["index"] == 360]
    _set_party(e.lens, 263, hp=(320, 320))
    _preview_threshold(e.lens, 360, 263, 226)
    _preview_hp(e, 360, 263, (320, 320))
    _rest_off_node(e, 68017, victim_hp)
    _drop_preview_row(e, 360, 68017)
    book = _book(e, [263, 68017], **{**LAZY, **over})
    head = e.keeper.strategies[book.id].observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 806, 360, node=NODE))
    return e, book


# ------------------------------------------------------------ A. the ratio stand-in


def test_a_a_kami_without_a_row_is_priced_on_the_served_ratio_and_starts(make_keeper, tmp_path):
    e, book = _world(make_keeper, tmp_path)
    s = _tick_one(e, book)
    assert _preview_reads(e) == [360] and e.keeper.previews.entries(NODE, CAROL)[360].rows[263] == {"threshold": 226, "level": 38, "reason": "THRESHOLD_ZERO", "hpTotal": 320}  # the fixture's own `reason`, untouched by the threshold override
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    # ceil(226 / 320 x 240) = ceil(169.5) = 170, + 5 % x 240 = 12 -> 182.
    # The ABSOLUTE stand-in would be 226 — the occupant's row, a 320-HP
    # kami — so 226 + 12 = 238 and `need = 238 + drain >= 240`: held with
    # "bound above resume HP" indefinitely, since a kami that never starts
    # never becomes an occupant and never earns a row of its own.
    assert lim[68017]["bound"] == 182
    assert lim[68017]["boundBy"]["thresholdFrom"] == PROXY1
    assert lim[68017]["boundBy"]["threshold"] == 170 and lim[68017]["boundBy"]["attackerKami"] == 360
    assert lim[68017]["unknown"] == [] and lim[68017]["proxied"] == []
    assert "held" not in s and _held_kamis(s) == []
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "start")] == [[68017]]
    # the occupant keeps its own served row, untouched by any stand-in
    assert lim[263]["bound"] == 242 and lim[263]["boundBy"]["threshold"] == 226 and "thresholdFrom" not in lim[263]["boundBy"]  # 226 + 5 % x 320


# ------------------------------------------------------------ B. a larger kami


def test_b_a_larger_kami_is_never_priced_lower_than_before_2o(make_keeper, tmp_path):
    e, book = _world(make_keeper, tmp_path, victim_hp=(340, 340))
    _tick_one(e, book)
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    # ceil(226 / 320 x 340) = ceil(240.125) = 241, + 5 % x 340 = 17 -> 258;
    # the absolute row would give the same kami 226 + 17 = 243
    assert lim[68017]["bound"] == 258 and lim[68017]["boundBy"]["thresholdFrom"] == PROXY1


# ------------------------------------------------------------ C. legacy rows (no hpTotal)


def test_c_legacy_rows_without_hp_totals_keep_the_absolute_stand_in(make_keeper, tmp_path):
    e, book = _world(make_keeper, tmp_path)
    _tick_one(e, book)
    entry = e.keeper.previews.entries(NODE, CAROL)[360]
    assert entry.stand_in(240) == (170, True) and round(entry.max_ratio(), 5) == 0.70625
    legacy = _strip_hp_totals(entry)
    assert legacy.max_ratio() == 0.0 and legacy.max_ratio([263]) == 0.0
    assert legacy.stand_in(240) == (226, False) and legacy.stand_in(340) == (226, False)
    # and through the book: `bounds()` hands the entry the book's OWN max
    # HP per kami, so the occupant's legacy row has a denominator after all
    # (320, from the party read) and the stand-in is the same scaled
    # 170 + 12 = 182 as with a fetch that recorded `hpTotal`. The absolute
    # maximum stands in only where NEITHER source knows a counted row's max
    # HP — the unit assertions above and test G
    e.keeper.previews.entries(NODE, CAROL)[360] = legacy
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    assert lim[68017]["bound"] == 182 and lim[68017]["boundBy"]["thresholdFrom"] == PROXY1  # not 238 / PROXY1_PRE_2O (the absolute stand-in)


# ------------------------------------------------------------ D. the owner stand-in


def _two_attacker_world(make_keeper, tmp_path):
    """The owner keeps two kamis on the node: one previewed, serving 226
    against a 320-HP occupant of the book, no row for the resting book
    kami, and parked on a cooldown above the horizon so it cannot bind; and
    a second whose own preview the lens always refuses."""
    e = _env(make_keeper, tmp_path)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] != 806 or h["kami"]["index"] in (360, 1136)]
    _set_party(e.lens, 263, hp=(320, 320))
    _preview_threshold(e.lens, 360, 263, 226)
    _preview_hp(e, 360, 263, (320, 320))
    _rest_off_node(e, 68017, (240, 240))
    _drop_preview_row(e, 360, 68017)
    e.lens.previews[NODE][360]["attacker"]["cooldownSec"] = 900  # > cooldownHorizonSec 600
    e.lens.synth_default = None  # the second attacker has no recorded preview: its read is always refused
    _serve_kami(e.lens, 1136)
    book = _book(e, [263, 68017], **LAZY)
    head = e.keeper.strategies[book.id].observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 806, 360, node=NODE))
    return e, book


def test_d_the_owners_stand_in_for_an_unread_attacker_scales_too(make_keeper, tmp_path):
    e, book = _two_attacker_world(make_keeper, tmp_path)
    s = _tick_one(e, book)
    assert _preview_reads(e) == [360, 1136] and s["previewsRefused"] == [1136] and s["cooldownSkipped"] == [360]
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    # the previewed attacker cannot bind (cooldown), so both kamis are bound
    # by the unpreviewed one, standing on its OWNER's highest served RATIO
    # (226 / 320 from the previewed row) x its own max HP: ceil(169.5) = 170, + 12
    assert lim[68017]["proxied"] == [1136] and lim[68017]["unknown"] == [] and lim[68017]["unknownNoRows"] == []
    assert lim[68017]["bound"] == 182
    assert lim[68017]["boundBy"] == {"attackerKami": 1136, "ownerClass": "killed-here-48h", "threshold": 170, "present": True, "hostileBy": "owner", "thresholdFrom": PROXY_OWNER}
    # legacy rows (no `hpTotal`): the owner's maximum RATIO is measured with
    # the book's own max HP for the occupant (320), so the owner stand-in
    # scales exactly as above: 226 / 320 x 240 -> 170 + 12 = 182 (not the
    # owner's ABSOLUTE 226 + 12 = 238)
    e.keeper.previews.entries(NODE, CAROL)[360] = _strip_hp_totals(e.keeper.previews.entries(NODE, CAROL)[360])
    strat, obs, lim, _cls, _att = _bounds(e, book)
    assert lim[68017]["bound"] == 182 and lim[68017]["boundBy"]["threshold"] == 170
    assert lim[68017]["boundBy"]["thresholdFrom"] == PROXY_OWNER
    # and the absolute wording where NEITHER source knows a counted row's max
    # HP: a view without the occupant has no figure for its legacy row, so
    # the owner's ABSOLUTE 226 stands in (+ 5 % x 240 = 238)
    only, _cls, _att, _sk = strat.bounds(obs, {68017: obs.kamis[68017]})
    assert only[68017]["bound"] == 238 and only[68017]["boundBy"]["threshold"] == 226
    assert only[68017]["boundBy"]["thresholdFrom"] == PROXY_OWNER_PRE_2O


# ------------------------------------------------------------ E. fetch records the served max HP


def test_e_fetch_records_the_served_max_hp_of_every_row(sock_dir, tmp_path):
    fake = FakeLens(str(sock_dir / "p2o.sock")).start()
    fake.nodes[79] = copy.deepcopy(NODE79)
    fake.previews[79] = {360: copy.deepcopy(ATT92)}
    lens = LensClient(fake.socket_path, 5)
    blind = next(h for h in fake.previews[79][360]["harvests"] if h["account"]["index"] == OURS and h["kami"]["index"] != 263)
    blind["vitals"]["hp"]["total"] = 0  # a row whose vitals serve no max HP (vitals must be present; a total of 0 keeps the legacy shape)
    cache = PreviewCache(tmp_path, lambda: 1000.0)
    entry, _changed, _ans = cache.fetch(lens, 79, 360, None, OURS, 1000.0)
    served = {h["kami"]["index"]: (h["vitals"]["hp"] or {}).get("total") for h in ATT92["harvests"] if h["account"]["index"] == OURS}
    assert len(entry.rows) == 107 and entry.rows[263]["hpTotal"] == 240 == served[263]
    assert all(entry.rows[k]["hpTotal"] == served[k] for k in entry.rows if k != blind["kami"]["index"])
    assert "hpTotal" not in entry.rows[blind["kami"]["index"]]  # legacy-shaped by construction
    # the key rides to_dict / from_dict with the rest of the row
    assert PreviewEntry.from_dict(json.loads(json.dumps(entry.to_dict()))).rows[263]["hpTotal"] == 240


# ------------------------------------------------------------ F-H. the book's own max HP is a denominator too


def _legacy_world(make_keeper, tmp_path, threshold: int = 209, victim_hp: tuple[int, int] = (200, 200)):
    """An attacker is ABSENT and remembered (it killed elsewhere, its owner is
    killed-here), and its entry is legacy-shaped, so no row carries
    `hpTotal`. `due()` iterates the PRESENT occupants, so the entry is never
    refreshed inside the window — without the book's own denominator the
    absolute line (on a 320-HP kami) would stand in indefinitely. Here: the
    remembered attacker, one served row of `threshold` against a
    book kami (320 max HP), and another book kami resting at `victim_hp`
    with no row of its own."""
    e, book = _world(make_keeper, tmp_path, victim_hp=(victim_hp[1] // 2, victim_hp[1]), resumeHpPct=100)
    _preview_threshold(e.lens, 360, 263, threshold)
    first = _tick_one(e, book)  # the row-less kami is at half HP: it waits, so no plan of its own
    ents = e.keeper.previews.entries(NODE, CAROL)
    ents[360] = _strip_hp_totals(ents[360])  # the entry in the legacy shape (no `hpTotal`)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != 360]
    _set_party(e.lens, 68017, hp=victim_hp)  # rested to full: startable unless a stand-in binds it
    s = _tick_one(e, book)
    assert s["remembered"] == [360] and _preview_reads(e) == []  # an absent entry is never refetched
    entry = e.keeper.previews.entries(NODE, CAROL)[360]
    assert entry.remembered and entry.rows[263]["threshold"] == threshold and all("hpTotal" not in r for r in entry.rows.values())
    return e, book, entry, first, s


def test_f_a_legacy_row_is_scaled_by_the_books_own_max_hp(make_keeper, tmp_path):
    _e, _book, entry, _first, _s = _legacy_world(make_keeper, tmp_path)
    # no row carries `hpTotal`, so by itself the entry can only serve the
    # absolute maximum — the remembered attacker's line on a 320-HP kami
    assert entry.max_ratio() == 0.0 and entry.stand_in(200) == (209, False)
    assert entry.stand_in(200, hp_totals={68017: 200}) == (209, False)  # a kami WITHOUT a row is not a denominator
    # with the book's own figure for the occupant the row counts: 209 / 320
    assert round(entry.max_ratio(hp_totals={263: 320}), 6) == 0.653125
    assert entry.stand_in(200, hp_totals={263: 320}) == (131, True)  # ceil(0.653125 x 200) = ceil(130.625)
    assert entry.stand_in(320, hp_totals={263: 320}) == (209, True)  # the row's own kami: its own line, never lower
    assert entry.stand_in(200, [263], {263: 320}) == (131, True) and entry.stand_in(200, [68017], {263: 320}) == (0, False)
    # a row's OWN `hpTotal` wins over `hp_totals` (a served figure beats ours)
    entry.rows[263]["hpTotal"] = 320
    assert entry.stand_in(200, hp_totals={263: 640}) == (131, True)


def test_g_with_neither_source_the_absolute_maximum_still_stands_in(make_keeper, tmp_path):
    _e, _book, entry, _first, _s = _legacy_world(make_keeper, tmp_path)
    for hp_totals in (None, {}, {263: 0}, {263: None}, {68017: 200}):  # type: ignore[arg-type]
        assert entry.stand_in(200, hp_totals=hp_totals) == (209, False)
        assert entry.max_ratio(hp_totals=hp_totals) == 0.0


def test_h_a_remembered_legacy_entry_no_longer_holds_the_small_kamis(make_keeper, tmp_path):
    e, book, _entry, first, s = _legacy_world(make_keeper, tmp_path)
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    # 209 / 320 = 0.653125 -> ceil(0.653125 x 200) = 131, + 5 % x 200 = 10
    # -> 141. The absolute 209 + 10 = 219 would exceed the kami's own 200
    # max HP, so `need = 219 + drain >= 200` would hold it with "bound above
    # resume HP" indefinitely
    assert lim[68017]["bound"] == 141
    assert lim[68017]["boundBy"]["threshold"] == 131 and lim[68017]["boundBy"]["attackerKami"] == 360 and lim[68017]["boundBy"]["present"] is False
    assert lim[68017]["boundBy"]["thresholdFrom"] == PROXY1  # the SCALED wording, not the absolute text
    assert lim[68017]["unknown"] == [] and lim[68017]["proxied"] == []
    assert _held_kamis(first) == [] and _held_kamis(s) == []
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "start")] == [[68017]]
    # the occupant keeps its own served row, whatever the denominator of the ratio
    assert lim[263]["bound"] == 225 and "thresholdFrom" not in lim[263]["boundBy"]  # 209 + 5 % x 320
    # the absolute stand-in survives where NEITHER source knows a counted
    # row's max HP: a view without the occupant has no figure for its legacy
    # row, so the absolute 209 stands in again (+ 5 % x 200 = 219)
    strat, obs, _l, _c, _a = _bounds(e, book)
    only, _cls, _att, _sk = strat.bounds(obs, {68017: obs.kamis[68017]})
    assert only[68017]["bound"] == 219 and only[68017]["boundBy"]["thresholdFrom"] == PROXY1_PRE_2O
