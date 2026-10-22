"""Two preview holes of the read policy, on the recorded fixtures:

  A. a preview fetched while NONE of the account's kamis was on the node
     has no rows, binds nothing and holds every start; the entry is due
     again as soon as one of the account's kamis is there, and one probe
     start per book per tick is allowed to put one there;
  B. a preview the lens refuses never refuses the tick — under a census
     read this pass as under a reused one.
"""

from __future__ import annotations

from typing import Any

from keeper.previews import REFRESH_NO_THRESHOLD_SEC, PreviewCache, PreviewEntry
from keeper.strategies.auto import Auto
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import CAROL, NODE, T, _add_occupant, _apply_plans_to_world, _decisions, _preview_threshold, _set_party
from test_phase2g import H, LAZY, _book, _env, _node_reads, _preview_reads, _tick_one
from test_phase2h import _kill
from test_previews import _foreign


# ------------------------------------------------------------ helpers


def _held_kamis(summary: dict[str, Any]) -> list[int]:
    return sorted(h["kami"] for h in summary.get("held") or [])


def _empty_the_node_of_the_account(e) -> None:
    """Nothing of the account on the node: every kami of the account
    RESTING at full HP, no own row in the census — so a preview of any
    attacker is served against foreign occupants only and carries no row
    of the account."""
    for r in list(e.lens.party[CAROL]["kamis"]):
        _set_party(e.lens, r["index"], state=RESTING, node=None, hp=(r["hp"]["total"], r["hp"]["total"]))
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] != CAROL]


def _deadlock(make_keeper, tmp_path, killers: tuple[int, ...] = (360,), **over: Any):
    """The deadlock world: the account is off the node, `killers` are
    killed-here attackers present on it, and no recorded preview stands in
    (every preview is synthesized from the node rows of the moment)."""
    e = _env(make_keeper, tmp_path)
    _empty_the_node_of_the_account(e)
    e.lens.previews[NODE] = {}
    book = _book(e, [263, 68017], **dict(LAZY, **over))
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    for n, a in enumerate(killers):
        e.keeper.owners.record_kill(_kill(head - H, f"0x0{n}", 4444, a, node=62))  # the kami killed one of the account's kamis elsewhere
    return e, book


# ------------------------------------------------------------ A. a preview with no rows


def test_a_preview_with_no_rows_lets_exactly_one_kami_of_the_book_out(make_keeper, tmp_path):
    e, book = _deadlock(make_keeper, tmp_path)
    s = _tick_one(e, book)
    assert _preview_reads(e) == [360] and e.keeper.previews.entries(NODE, CAROL)[360].rows == {}  # the lens previews occupants: nothing of the account is one
    assert s["ownOnNode"] is False and s["probeStart"] == 263
    starts = _decisions(e, book, "start")
    assert len(starts) == 1 and starts[0]["parameters"]["kamis"] == [263]
    assert starts[0]["parameters"]["detail"][0]["probe"] is True
    assert _held_kamis(s) == [68017] and s["held"][0]["reason"] == "bound unknown: attacker not yet previewed" and s["kamis"]["held"] == 1


def test_no_probe_while_a_kami_of_the_account_is_on_the_node(make_keeper, tmp_path):
    """Same world, but a kami of the account in another book harvests on
    the node, and the one fetch of the pass goes to the arrival ahead of
    the empty entry in the queue."""
    e, book = _deadlock(make_keeper, tmp_path, killers=(360, 448), previewsPerTick=1)
    _set_party(e.lens, 66655, state=HARVESTING, node=NODE)
    _add_occupant(e.lens, 66655, CAROL)
    e.keeper.previews.entries(NODE, CAROL)[360] = PreviewEntry(NODE, 360, T, 1, 38, 806, {}, 0, None)
    s = _tick_one(e, book)
    assert _preview_reads(e) == [448] and e.keeper.previews.entries(NODE, CAROL)[360].rows == {}  # the second attacker arrives, the first waits: still no rows this tick
    assert s["ownOnNode"] is True and "probeStart" not in s
    assert _held_kamis(s) == [263, 68017] and _decisions(e, book, "start") == []


def test_no_probe_while_one_unknown_attacker_was_never_fetched(make_keeper, tmp_path):
    """An attacker with NO entry at all is unknown for a reason a probe
    start does not answer: it is fetched next pass."""
    e, book = _deadlock(make_keeper, tmp_path, killers=(360, 448), previewsPerTick=1)
    s = _tick_one(e, book)
    assert _preview_reads(e) == [360] and s["previewsPending"] == 1
    assert s["ownOnNode"] is False and "probeStart" not in s
    assert _held_kamis(s) == [263, 68017] and _decisions(e, book, "start") == []


def test_the_probe_start_makes_the_entry_due_again_and_the_others_take_the_bound_by_proxy(make_keeper, tmp_path):
    e, book = _deadlock(make_keeper, tmp_path)
    _preview_threshold(e.lens, 360, 263, 165)
    assert _tick_one(e, book)["probeStart"] == 263
    _apply_plans_to_world(e)  # the probe kami harvests on the node from the next read on
    s = _tick_one(e, book)
    assert _preview_reads(e) == [360] and e.keeper.previews.entries(NODE, CAROL)[360].rows[263]["threshold"] == 165
    assert s["ownOnNode"] is True and "probeStart" not in s and "held" not in s
    starts = _decisions(e, book, "start")
    assert len(starts) == 2 and starts[1]["parameters"]["kamis"] == [68017] and "probe" not in starts[1]["parameters"]["detail"][0]
    strat: Auto = e.keeper.strategies[book.id]
    obs = strat.observe(e.keeper.lens)
    lim, _c, _a, _s = strat.bounds(obs, {k: v for k, v in obs.kamis.items() if not v.missing})
    # the second kami has no row of its own (it was resting when the preview
    # was fetched). The stand-in is the highest served RATIO over the account's
    # occupants — 165 against the probe kami, whose max HP in this world is 240
    # (rested to full by `_empty_the_node_of_the_account`) = 0.6875 — times the
    # second kami's own 270: ceil(185.625) = 186, + 5 % x 270 = 13.5 -> 199.5
    # -> 200. The ABSOLUTE line would be 165 + 13.5 = 179, which prices the
    # kami on a smaller kami's line.
    assert lim[68017]["unknown"] == [] and lim[68017]["unknownNoRows"] == [] and lim[68017]["bound"] == 200
    assert lim[68017]["boundBy"]["thresholdFrom"] == "max served ratio over the account's occupants x this kami's max HP (no row for this kami)"


def test_due_puts_a_no_rows_entry_after_the_arrivals_and_only_with_a_kami_on_the_node(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign = _foreign()
    ents = cache.entries(79, CAROL)
    for a, occ in foreign.items():
        ents[a] = PreviewEntry(79, a, 0.0, 1, occ.level, occ.account_index, {263: {"threshold": 5 if a == 360 else -3, "level": 38}}, 0, None)
    arrival = next(a for a in sorted(foreign) if a not in (360, 448))
    del ents[arrival]  # never fetched
    ents[448].rows = {}  # fetched while nothing of the account was on the node
    foreign[360].level = 39  # a level change
    assert cache.due(79, CAROL, foreign, {263: 38}, 3600, 100.0, own_on_node=True) == [(arrival, "arrival"), (448, "no rows for the account; a kami is on the node now"), (360, "attacker level 38 -> 39")]
    # with nothing of the account on the node an empty entry binds nothing,
    # so it ages on the no-threshold interval
    today = [(arrival, "arrival"), (360, "attacker level 38 -> 39")]
    assert cache.due(79, CAROL, foreign, {263: 38}, 3600, 100.0) == today == cache.due(79, CAROL, foreign, {263: 38}, 3600, 100.0, own_on_node=False)
    assert (448, f"older than {REFRESH_NO_THRESHOLD_SEC} s") in cache.due(79, CAROL, foreign, {263: 38}, 3600, float(REFRESH_NO_THRESHOLD_SEC))


# ------------------------------------------------------------ B. a refused preview never refuses the tick


def test_a_refused_preview_never_refuses_a_tick_whose_census_was_read_this_pass(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    e.lens.synth_default = None  # the lens answers a preview only for a recorded attacker
    book = _book(e, [263, 68017], nodeCensusEverySec=120, **LAZY)
    e.keeper.owners.record_kill(_kill(e.lens.block - H, "0x01", 4444, 3215, node=62))
    s = _tick_one(e, book)  # no census is held yet: this pass reads one of its own
    assert len(_node_reads(e)) == 1 and s["censusAgeSec"] == 0  # the census of THIS pass
    assert s.get("reason") is None and s["previewsRefused"] == [3215] and s["previewCalls"] == 0
    assert _held_kamis(s) == [263, 68017]  # what is unknown stays unknown: held at the floor
    assert e.keeper.census.held(NODE, "plain") is None  # and the census is forced
    s2 = _tick_one(e, book)
    assert len(_node_reads(e)) == 1 and s2["censusAgeSec"] == 0 and s2.get("reason") is None  # read again although the interval is 120 s
