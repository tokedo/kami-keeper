"""The start batch cap (10).

The harness figure 31 is derived from MAX_TX_GAS, a per-TRANSACTION lane
limit. The node's pre-check (`eth_call` with the transaction's gasLimit)
refuses batched starts well below that lane: starts of 19 (gasLimit
19,350,000) and 23 (23,150,000) have landed, 25 (25,050,000) and 28
(27,900,000, `Reverted [code -32000]`, `not_sent` -> 28 singles) were
refused. The cap is a batch size known to land, the same measured rule as
stop 12 and collect 12.
"""

from __future__ import annotations

import pytest

from keeper.signer import BATCH_CAPS, Intent, WhitelistRefused, check_whitelist, gas_for
from test_phase2a import _decisions
from test_phase2g import LAZY, _book, _env, _scale_world, _tick
from test_phase2k import _rest_owned

CFG = {"foodItem": 11301, "reviveItems": [21301]}


def test_start_cap_is_the_largest_landed_batch():
    assert BATCH_CAPS["system.harvest.start"] == 10
    # the gasLimit at the cap: base 1,300,000 + 950,000 x 10
    assert gas_for("system.harvest.start", 10) == 10_800_000
    # a batch that has LANDED (one sample)
    assert gas_for("system.harvest.start", 23) == 23_150_000
    # the next batch up is the one that reverted at the pre-check
    assert gas_for("system.harvest.start", 25) == 25_050_000
    # stop and collect keep their own measured caps
    assert BATCH_CAPS["system.harvest.stop"] == 12 and BATCH_CAPS["system.harvest.collect"] == 12


def test_a_start_batch_above_the_cap_is_refused_pre_sign():
    with pytest.raises(WhitelistRefused) as err:
        check_whitelist(Intent("system.harvest.start", "lab", None, {"count": 11}), CFG)
    assert "exceed the batch cap 10" in str(err.value)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.harvest.start", "lab", None, {"kamiIndices": list(range(11))}), CFG)
    # 10 is signable
    check_whitelist(Intent("system.harvest.start", "lab", None, {"count": 10}), CFG)
    check_whitelist(Intent("system.harvest.start", "lab", None, {"kamiIndices": list(range(10))}), CFG)


def test_447_ready_starts_chunk_into_forty_five_batches(make_keeper, tmp_path):
    """A 447-kami book: 447 ready kamis deploy in ONE tick as 20
    batched starts of at most 10 — the burst that reverted as one 28."""
    e = _env(make_keeper, tmp_path)
    ours, _theirs = _scale_world(e.lens, owned=447)
    _rest_owned(e.lens, ours)
    book = _book(e, ours, friends=[], maxKamisPerBook=450, startBatchWaitSec=300, startBatchMin=4, **LAZY)
    _tick(e)
    starts = _decisions(e, book, "start")
    cap = int(BATCH_CAPS["system.harvest.start"])
    assert cap == 10 and len(starts) == 45
    assert [len(d["parameters"]["kamis"]) for d in starts] == [10] * 44 + [7]
    assert sorted(k for d in starts for k in d["parameters"]["kamis"]) == sorted(ours)
