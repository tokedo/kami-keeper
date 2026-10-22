"""One `auto` book per account: `maxKamisPerBook` above the default 50.
The chain's limits are per TRANSACTION and independent of book size:

  a. config — 150 kamis validate when the book names `maxKamisPerBook`
     150; without the key the existing refusal stands (the default is
     still 50); 1001 is refused by the schema (the ceiling is 1000:
     450 and 660 kamis each validate as ONE book);
  b. scale — the node-read scale world (1,750 foreign occupants, 2 killed-here
     attackers, `killed-here` + `plain`) with ONE book of 150 owned
     kamis: one observe + assess under 1.0 s on a settled pass, the same
     reads as at 50;
  c. the reaction to a strike — 150 kamis at or under their limit in one
     tick rest in ONE tick, chunked at BATCH_CAPS stop (12): 13 intents;
  d. the start queue — 150 kamis ready in one tick go out in ONE tick,
     chunked at BATCH_CAPS start (10): 15 intents.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from keeper.books import BookError, validate_config
from keeper.scrub import forbidden_in
from keeper.signer import BATCH_CAPS
from keeper.strategies.auto import Auto
from keeper.strategies.base import RESTING
from test_phase2a import CAROL, NODE, _decisions
from test_phase2g import H, LAZY, _book, _env, _kami_reads, _preview_reads, _scale_world, _serve_kami, _tick
from test_phase2h import _kill

BOOK_150 = list(range(100_000, 100_150))


# ------------------------------------------------------------ a. config


def test_a_book_of_150_kamis_validates_when_it_names_maxkamisperbook():
    base = {"kamiIndices": list(BOOK_150), "nodeIndex": NODE, "accountIndex": CAROL}
    full = validate_config("auto", dict(base, maxKamisPerBook=150), "fleet")
    assert full["maxKamisPerBook"] == 150 and len(full["kamiIndices"]) == 150
    # the default did not move: a 150-kami book that does not name the key
    # is refused exactly as before
    with pytest.raises(BookError) as err:
        validate_config("auto", dict(base), "fleet")
    assert str(err.value) == "config invalid at kamiIndices: 150 kamis exceed maxKamisPerBook 50"
    assert validate_config("auto", {**base, "kamiIndices": BOOK_150[:50]}, "fleet")["maxKamisPerBook"] == 50
    # and 1000 is the ceiling
    assert validate_config("auto", dict(base, maxKamisPerBook=1000), "fleet")["maxKamisPerBook"] == 1000
    with pytest.raises(BookError) as over:
        validate_config("auto", dict(base, maxKamisPerBook=1001), "fleet")
    assert "config invalid at maxKamisPerBook" in str(over.value) and "1000" in str(over.value)
    # 450 distinct kamis are ONE book under a 450 bound
    book_450 = [i + off for off in (0, 1_000, 2_000) for i in BOOK_150]
    assert len(set(book_450)) == 450 and min(book_450) > 0
    full_450 = validate_config(
        "auto", dict(base, kamiIndices=book_450, maxKamisPerBook=450), "fleet"
    )
    assert full_450["maxKamisPerBook"] == 450 and len(full_450["kamiIndices"]) == 450
    # 660 distinct kamis are ONE book under a 700 bound
    book_660 = [i + off for off in (0, 1_000, 2_000, 3_000, 4_000) for i in BOOK_150][:660]
    assert len(set(book_660)) == 660 and min(book_660) > 0
    full_660 = validate_config(
        "auto", dict(base, kamiIndices=book_660, maxKamisPerBook=700), "fleet"
    )
    assert full_660["maxKamisPerBook"] == 700 and len(full_660["kamiIndices"]) == 660
    assert not forbidden_in([str(err.value), str(over.value)])


# ------------------------------------------------------------ b. scale


def test_scale_one_book_of_150_owned_kamis_on_1750_foreign_occupants(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    ours, theirs = _scale_world(e.lens, owned=150)
    by_owner, by_kami = theirs[7], theirs[1234]  # as in the node-read scale test: one killed-here by its owner, one by the kami itself
    book = _book(e, ours, friends=[], maxKamisPerBook=150, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 99_999, by_kami, node=62))
    e.keeper.owners.record_kill(_kill(head - H, "0x02", 99_998, by_owner, node=62))
    e.lens.synth_thresholds.update({(a, ours[0]): 190 for a in (by_owner, by_kami)})
    for a in (by_owner, by_kami):
        _serve_kami(e.lens, a)
    from keeper import census as census_module

    census_module.entity_id.cache_clear()  # the warming pass pays the entity-id keccaks of a cold process
    timings = []
    for n in range(2):
        e.lens.requests.clear()
        e.clock["now"] += 30
        e.keeper.census.begin_pass()
        e.keeper.previews.begin_pass()
        t0 = time.perf_counter()
        obs = strat.observe(e.keeper.lens)
        rt: dict[str, Any] = {}
        decisions = strat.assess(obs, e.clock["now"], rt)
        timings.append(time.perf_counter() - t0)
        s = rt["summaryExtra"]
        world = [r for r in e.lens.requests if not (r["query"] == "node" and len([a for a in r["args"] if not a.startswith("--")]) == 2) and r["query"] != "kami"]
        # the reads are per ACCOUNT and per NODE: three of them, whatever the book holds
        assert [r["query"] for r in world] == ["party", "node", "inventory"] and s["foreignOccupants"] == 1750 and s["attackers"] == 1750
        assert s["kamis"]["harvesting"] == 150 and s["previewsPending"] == 0 and "held" not in s
        if n == 0:
            assert sorted(_preview_reads(e)) == sorted([by_owner, by_kami]) and _kami_reads(e) == [] and s["previewCalls"] == 2
        else:
            # a settled pass: party + node + inventory + one `kami` read per killed-here attacker
            assert _preview_reads(e) == [] and sorted(_kami_reads(e)) == sorted([by_owner, by_kami]) and s["lensCalls"] == 5
        assert s["boundKamis"] == 150 and [d.kind for d in decisions] == ["rest"] and decisions[0].parameters["kamis"] == [ours[0]]  # 190 + 5 % x 240 = 202 >= 200
    assert max(timings) < 1.0, timings
    print(f"\n2k scale: observe + assess over 1,750 foreign / 150 owned / 2 killed-here: {timings[0]:.3f} s warming, {timings[1]:.3f} s settled")


# ------------------------------------------------------------ c. one reaction


def test_150_kamis_at_their_limit_all_rest_in_one_tick(make_keeper, tmp_path):
    """One book = ONE reaction: every kami the bound covers rests on the
    tick that reads it, chunked at the chain's per-transaction cap."""
    e = _env(make_keeper, tmp_path)
    ours, theirs = _scale_world(e.lens, owned=150)
    attacker = theirs[1234]
    book = _book(e, ours, friends=[], maxKamisPerBook=200, **LAZY)
    strat: Auto = e.keeper.strategies[book.id]
    head = strat.observe(e.keeper.lens).basis.lensBlock
    e.keeper.owners.record_kill(_kill(head - H, "0x01", 99_999, attacker, node=62))
    _serve_kami(e.lens, attacker)
    e.lens.synth_thresholds.update({(attacker, k): 190 for k in ours})  # 190 + 5 % x 240 = 202 >= the 200 every kami reads
    _tick(e)
    rests = _decisions(e, book, "rest")
    cap = int(BATCH_CAPS["system.harvest.stop"])
    assert cap == 12 and len(rests) == 13
    assert [len(d["parameters"]["kamis"]) for d in rests] == [12] * 12 + [6]
    assert sorted(k for d in rests for k in d["parameters"]["kamis"]) == sorted(ours)  # none left for a later tick
    assert all(d["parameters"]["batchSize"] == len(d["parameters"]["kamis"]) for d in rests)


# ------------------------------------------------------------ d. one start queue


def _rest_owned(fake, ours: list[int]) -> None:
    """Every owned kami RESTING at full HP and off the node: the book the
    tick before it deploys."""
    owned = set(ours)
    for r in fake.party[CAROL]["kamis"]:
        if r["index"] in owned:
            r["state"] = RESTING
            r["hp"] = {"current": 240, "total": 240, "percent": 100}
            r.pop("node", None)
    fake.nodes[NODE]["harvests"] = [h for h in fake.nodes[NODE]["harvests"] if h["kami"]["index"] not in owned]


def test_150_ready_kamis_go_out_as_one_start_queue(make_keeper, tmp_path):
    """One book = ONE start queue: 150 ready kamis deploy on one tick,
    chunked at the chain's per-transaction cap."""
    e = _env(make_keeper, tmp_path)
    ours, _theirs = _scale_world(e.lens, owned=150)
    _rest_owned(e.lens, ours)
    book = _book(e, ours, friends=[], maxKamisPerBook=200, startBatchWaitSec=300, startBatchMin=4, **LAZY)
    _tick(e)
    starts = _decisions(e, book, "start")
    cap = int(BATCH_CAPS["system.harvest.start"])
    assert cap == 10 and len(starts) == 15
    assert [len(d["parameters"]["kamis"]) for d in starts] == [10] * 15
    assert sorted(k for d in starts for k in d["parameters"]["kamis"]) == sorted(ours)
    assert all(d["parameters"]["node"] == NODE for d in starts)
