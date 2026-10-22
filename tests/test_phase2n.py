"""An unknown attacker never LOWERS a known bound, on the recorded node
fixture (one owner with 22 attackers):

  A. the burst — a killed-here owner's 22 present attackers become
     arrivals, ten previews land under the per-tick cap, twelve are still
     unread: the kami the ten previews already bound rests on THAT tick,
     and nothing is held;
  B. the stand-in — an attacker whose own preview has not landed takes
     its owner's served maximum over the book's kamis, is listed
     `proxied` (never `unknown`), binds like a known threshold and names
     itself on `boundBy.thresholdFrom`;
  C. an owner with NO preview at all is unchanged: unknown, held, and a
     rest only at the floor;
  D. the resting side — a start is offered while the unread attackers are
     proxied, and still held while one is truly unknown;
  E. the status command's lens timeout.
"""

from __future__ import annotations

from typing import Any

from keeper.core import STATUS_LENS_TIMEOUT_SEC
from keeper.strategies.auto import Auto
from test_phase2a import CAROL, NODE, _decisions, _last_tick, _preview_threshold, _set_party
from test_phase2g import H, LAZY, OWNER_913, _book, _env, _held_kamis, _preview_reads, _serve_kami, _tick
from test_phase2h import _bounds, _kill


def _head(e, book) -> int:
    strat: Auto = e.keeper.strategies[book.id]
    return strat.observe(e.keeper.lens).basis.lensBlock


def _rest_a_kami(e, kami: int, hp: tuple[int, int]) -> None:
    """`kami` RESTING at `hp`, off the node (the resting side of a book)."""
    _set_party(e.lens, kami, state="RESTING", node=None, hp=hp)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != kami]


def _all_previews_refused(e) -> None:
    """No recorded preview and no synthesized one: every preview read is
    refused, so a killed-here attacker stays truly UNREAD."""
    e.lens.previews[NODE] = {}
    e.lens.synth_default = None


# ------------------------------------------------------------ A. the burst


def test_a_known_bound_survives_an_unpreviewed_arrival(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _preview_threshold(e.lens, 360, 263, 165)  # the first kami reads 171 / 240: 165 + 5 % x 240 = 177
    for k in OWNER_913:
        _serve_kami(e.lens, k)  # the cooldown of the attackers no preview of the pass answers for
    book = _book(e, [263, 68017], **LAZY)
    e.keeper.owners.record_kill(_kill(_head(e, book) - H, "0x01", 806, 360, node=NODE))
    _tick(e)
    s = _last_tick(e, book)
    # the owner's 22 present attackers are arrivals: ten fetched (previewsPerTick), twelve unread
    assert sorted(_preview_reads(e)) == sorted(OWNER_913)[:10] and s["previewCalls"] == 10 and s["previewsPending"] == 12
    # the twelve are `proxied`, not `unknown` — nothing is held, and the
    # first kami (HP 171, bound 177 from the attacker's preview) rests on
    # THIS tick, not after the twelve previews land.
    assert "held" not in s and s["kamis"]["held"] == 0
    rests = _decisions(e, book, "rest")
    assert [d["parameters"]["kamis"] for d in rests] == [[263]]
    assert rests[0]["parameters"]["detail"][0]["bound"] == 177
    assert rests[0]["parameters"]["detail"][0]["boundBy"]["attackerKami"] == 360 and "thresholdFrom" not in rests[0]["parameters"]["detail"][0]["boundBy"]
    # the second kami (268 / 270) is bound by the stand-in too, and keeps
    # harvesting. The stand-in is the owner's highest served RATIO — 165
    # against the first kami, a 240-HP occupant, = 0.6875 — times the second
    # kami's own 270: ceil(185.625) = 186, + 5 % x 270 = 13.5 -> 199.5 -> 200
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    assert lim[68017]["bound"] == 200 and lim[68017]["unknown"] == [] and sorted(lim[68017]["proxied"]) == sorted(OWNER_913)[10:]


# ------------------------------------------------------------ B. the stand-in


def _two_attacker_world(make_keeper, tmp_path):
    """The owner keeps two kamis on the node: one previewed, serving 165
    against the first book kami and NO row for the second, parked on a
    cooldown above the horizon so it cannot bind; and one whose own preview
    is refused."""
    e = _env(make_keeper, tmp_path)
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] != 806 or h["kami"]["index"] in (360, 1136)]
    _preview_threshold(e.lens, 360, 263, 165)
    e.lens.previews[NODE][360]["harvests"] = [h for h in e.lens.previews[NODE][360]["harvests"] if h["kami"]["index"] != 68017]
    e.lens.previews[NODE][360]["attacker"]["cooldownSec"] = 900  # > cooldownHorizonSec 600
    e.lens.synth_default = None  # the second attacker has no recorded preview: its read is refused, for ever
    _serve_kami(e.lens, 1136)
    return e


def test_the_owners_served_maximum_stands_in_for_an_unread_attacker(make_keeper, tmp_path):
    e = _two_attacker_world(make_keeper, tmp_path)
    book = _book(e, [263, 68017], **LAZY)
    e.keeper.owners.record_kill(_kill(_head(e, book) - H, "0x01", 806, 360, node=NODE))
    _tick(e)
    s = _last_tick(e, book)
    assert _preview_reads(e) == [360, 1136] and s["previewsRefused"] == [1136] and s["cooldownSkipped"] == [360]
    strat, _obs, lim, _cls, _att = _bounds(e, book)
    # the previewed attacker cannot bind (cooldown), so the bound of both
    # kamis is the unread one's — an attacker nobody previewed, standing on
    # its OWNER's served line (165, from the previewed row against the first
    # kami), never on a computed number. That line is a RATIO, 165 / 240 =
    # 0.6875, scaled by each kami's own max HP
    assert lim[68017]["proxied"] == [1136] and lim[68017]["unknown"] == [] and lim[68017]["unknownNoRows"] == []
    assert lim[68017]["bound"] == 200  # ceil(0.6875 x 270) = 186, + 5 % x 270 = 13.5 -> 199.5
    assert lim[68017]["boundBy"] == {"attackerKami": 1136, "ownerClass": "killed-here-48h", "threshold": 186, "present": True, "hostileBy": "owner", "thresholdFrom": "max served ratio over the owner's previewed attackers x this kami's max HP (this attacker not yet previewed)"}
    assert lim[263]["bound"] == 177 and lim[263]["proxied"] == [1136] and lim[263]["unknown"] == []  # ceil(0.6875 x 240) = 165, + 5 % x 240 = 12 — its own size, so unchanged
    # an unread attacker that binds holds nothing: no `held` row, and the first kami rests
    assert "held" not in s and [d["parameters"]["kamis"] for d in _decisions(e, book, "rest")] == [[263]]
    assert strat.preview_policy == "killed-here"


# ------------------------------------------------------------ C. no preview at all


def test_an_owner_with_no_preview_at_all_stays_unknown_and_rests_at_the_floor(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _all_previews_refused(e)
    book = _book(e, [263, 68017], **LAZY)
    e.keeper.owners.record_kill(_kill(_head(e, book) - H, "0x01", 806, 360, node=NODE))
    _tick(e)
    s = _last_tick(e, book)
    assert s["previewsRefused"] == sorted(OWNER_913)[:10]  # nothing was ever served for the owner
    _strat, _obs, lim, _cls, _att = _bounds(e, book)
    # max over the owner's previewed attackers is 0: no stand-in, unknown as before
    assert lim[263]["bound"] == 0 and lim[263]["boundBy"] is None and lim[263]["proxied"] == [] and lim[263]["unknown"] == sorted(OWNER_913)
    assert _held_kamis(s) == [263, 68017] and s["held"][0]["reason"] == "bound unknown: attacker not yet previewed"
    assert s["held"][0]["bound"] == 0 and s["held"][0]["boundBy"] is None
    assert _decisions(e, book, "rest") == []  # the first kami at 171 / 240 is far above the floor (24)
    # the floor is all that is left, and it still rests
    _set_party(e.lens, 263, hp=(24, 240))
    _tick(e)
    assert [d["parameters"]["kamis"] for d in _decisions(e, book, "rest")] == [[263]]
    assert _last_tick(e, book)["held"][0]["kami"] == 263


# ------------------------------------------------------------ D. the resting side


def test_a_resting_kami_starts_while_its_unread_attackers_are_proxied(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _preview_threshold(e.lens, 360, 263, 165)
    for k in OWNER_913:
        _serve_kami(e.lens, k)
    _rest_a_kami(e, 66655, (220, 220))  # full HP, bound well under 220 even with an hour of drain
    book = _book(e, [263, 66655, 68017], **LAZY)
    e.keeper.owners.record_kill(_kill(_head(e, book) - H, "0x01", 806, 360, node=NODE))
    _tick(e)
    s = _last_tick(e, book)
    assert s["previewsPending"] == 12 and "held" not in s
    starts = _decisions(e, book, "start")
    assert [d["parameters"]["kamis"] for d in starts] == [[66655]]
    # the stand-in scales DOWN with a smaller kami too — this one is 220 max
    # HP, so ceil(165 / 240 x 220) = ceil(151.25) = 152, + 5 % x 220 = 11 -> 163
    assert starts[0]["parameters"]["detail"][0]["bound"] == 163


def test_a_resting_kami_is_still_held_while_an_attacker_is_truly_unknown(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    _all_previews_refused(e)
    _rest_a_kami(e, 66655, (220, 220))
    book = _book(e, [263, 66655, 68017], **LAZY)
    e.keeper.owners.record_kill(_kill(_head(e, book) - H, "0x01", 806, 360, node=NODE))
    _tick(e)
    s = _last_tick(e, book)
    assert 66655 in _held_kamis(s) and _decisions(e, book, "start") == []
    held = next(h for h in s["held"] if h["kami"] == 66655)
    assert held["reason"] == "bound unknown: attacker not yet previewed"


# ------------------------------------------------------------ E. the status read


def test_the_status_commands_lens_timeout_is_six_seconds():
    # a 2 s status read can time out under a preview burst and report
    # `lens-unavailable` while every book's own 30 s client reads fine
    assert STATUS_LENS_TIMEOUT_SEC == 6.0
