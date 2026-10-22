"""keeper/blockclock.py: the hostility window is WALL-CLOCK time, not
blocks at an assumed 2.5 s. At a chain rate of 2.21 s/block a 48-h window
counted in 2.5-s blocks is ~42 real hours: a kill 44.7 h old would be
forgiven too early.

Here: the clock's own arithmetic (interpolation, extrapolation, the
default rate below an hour of samples, the `observe` guards, pruning and
persistence), the live case through `OwnerLedger` (a 44.7-h kill binds
with the clock and does not with the block arithmetic), the `KillScanner`
first-boot window, the battles seed placing a timestamp-only row, the
status block, and the sample source: one per kill-scan pass.
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import Env
from fakechain import FakeChain
from keeper import blockclock as bcmod
from keeper.blockclock import BLOCKCLOCK_FILE, BlockClock
from keeper.kills import BLOCK_SECONDS, BLOCKS_PER_HOUR, KillScanner
from keeper.owners import OwnerLedger
from test_phase2a import CAROL, NODE, T, _seed, _start
from test_phase2b import BATTLES, _battles_for
from test_phase2g import LAZY, _book, _env, _tick

RATE = 2.21  # a chain running faster than the assumed 2.5 s/block
HEAD = 12_278_391


def _clock(bc: BlockClock, hours: float, rate: float = RATE, end_ts: float = T, end_block: int = HEAD, step: float = 60.0) -> BlockClock:
    """Fill `bc` with one sample every `step` seconds over `hours` of chain
    running at `rate` s/block, ending at (`end_ts`, `end_block`)."""
    n = int(hours * 3600 / step)
    start_ts = end_ts - n * step
    start_block = end_block - int(round(n * step / rate))
    for i in range(n + 1):
        assert bc.observe(start_block + int(round(i * step / rate)), start_ts + i * step) is True
    assert bc.samples[-1] == (end_ts, end_block)
    return bc


def _fresh(tmp_path: Path, hours: float = 50.0, **over) -> BlockClock:
    return _clock(BlockClock(tmp_path, lambda: T), hours, **over)


# ---------------------------------------------------------------- (a) the rate


def test_a_interpolation_answers_the_block_the_chain_was_at_48_wall_clock_hours_ago(tmp_path):
    bc = _fresh(tmp_path, 50.0)
    assert bc.measured() is True and abs(bc.rate() - RATE) < 0.01 and bc.span_sec() == 50 * 3600
    # the sampled block nearest now - 48 h, within a block of the answer
    target = T - 48 * 3600
    nearest = min(bc.samples, key=lambda s: abs(s[0] - target))[1]
    floor = bc.floor(48, HEAD)
    assert abs(floor - nearest) <= 1
    # and it is NOT what 2.5 s/block says: 48 h x (1/2.21 - 1/2.5) blocks lower
    expected_gap = 48 * 3600 * (1 / RATE - 1 / BLOCK_SECONDS)
    assert abs((HEAD - 48 * BLOCKS_PER_HOUR) - floor - expected_gap) <= 2
    assert floor < HEAD - 48 * BLOCKS_PER_HOUR  # a 2.5 s window is SHORTER than 48 real hours here
    # the window in real seconds is 48 h to the sample spacing
    assert abs((HEAD - floor) * bc.rate() - 48 * 3600) <= 120


# ------------------------------------------------- (b) too few / too short


def test_b_under_two_samples_is_todays_arithmetic_and_a_short_span_is_the_default_rate(tmp_path):
    bc = BlockClock(tmp_path, lambda: T)
    assert bc.block_at(T - 1000) is None and bc.rate() == BLOCK_SECONDS and bc.measured() is False
    assert bc.floor(48, HEAD) == HEAD - 48 * BLOCKS_PER_HOUR  # exactly what the 2.5-s block arithmetic answers
    assert bc.floor(48, None) == 0
    assert bc.observe(HEAD, T) is True
    assert bc.block_at(T - 1000) is None and bc.floor(48, HEAD) == HEAD - 48 * BLOCKS_PER_HOUR  # one sample is not a rate
    # two samples 30 min apart: the span is under an hour, so the rate is the
    # FALLBACK — but interpolation inside the span is still the measurement
    bc2 = BlockClock(tmp_path, lambda: T)
    assert bc2.observe(HEAD - 800, T - 1800) is True and bc2.observe(HEAD, T) is True
    assert bc2.measured() is False and bc2.rate() == BLOCK_SECONDS
    assert bc2.block_at(T - 900) == HEAD - 400 and bc2.block_at(T - 1800) == HEAD - 800 and bc2.block_at(T) == HEAD
    assert bc2.floor(1, HEAD) == (HEAD - 800) - BLOCKS_PER_HOUR // 2  # 1 h ago is 30 min before the first sample: extrapolated from it at 2.5 s
    assert bc2.describe() == {"samples": 2, "spanSec": 1800.0, "blockSeconds": BLOCK_SECONDS, "measured": False, "restore": "missing"}


# ------------------------------------------------------ (c) extrapolation


def test_c_outside_the_span_extrapolates_at_the_measured_rate(tmp_path):
    bc = BlockClock(tmp_path, lambda: T)
    _clock(bc, 2.0, rate=2.0)  # 2 h of samples at exactly 2 s/block
    assert bc.measured() is True and bc.rate() == 2.0
    first_ts, first_block = bc.samples[0]
    assert bc.block_at(first_ts - 100) == first_block - 50
    assert bc.block_at(first_ts - 7200) == first_block - 3600
    assert bc.block_at(T + 100) == HEAD + 50
    assert bc.block_at(first_ts - first_block * 2.0 - 1000) == 0  # clamped, never negative


# ------------------------------------------- (d) observe, pruning, the file


def test_d_observe_guards_pruning_and_the_round_trip(tmp_path, monkeypatch):
    bc = BlockClock(tmp_path, lambda: T)
    assert bc.observe(1000, T) is True  # the first sample always appends
    assert bc.observe(999, T + 120) is False  # below the last block
    assert bc.observe(1000, T + 120) is False  # not ABOVE the last block
    assert bc.observe(True, T + 120) is False and bc.observe(1001.0, T + 120) is False and bc.observe("1001", T + 120) is False  # type: ignore[arg-type]
    assert bc.observe(-1, T + 120) is False
    assert bc.observe(1100, T + 59) is False  # 59 s after the last: too close
    assert bc.observe(1100, T + 60) is True and bc.samples == [(T, 1000), (T + 60, 1100)]
    # ts defaults to the injected clock
    bc.clock = lambda: T + 600
    assert bc.observe(1200) is True and bc.samples[-1] == (T + 600, 1200)
    # age pruning: a sample 9 days on drops everything older than 8 days
    assert bc.observe(9999, T + 9 * 86400) is True
    assert bc.samples == [(T + 9 * 86400, 9999)]
    # count pruning
    monkeypatch.setattr(bcmod, "MAX_SAMPLES", 3)
    for i in range(1, 5):
        assert bc.observe(9999 + i, T + 9 * 86400 + i * 60) is True
    assert [b for _ts, b in bc.samples] == [10001, 10002, 10003]  # the cap holds at every append
    # the file: written once per change, restored as it was
    assert bc.save() is True and bc.save() is False
    doc = json.loads((tmp_path / BLOCKCLOCK_FILE).read_text())
    assert doc["schemaVersion"] == "0.1.0" and doc["samples"][-1] == [T + 9 * 86400 + 240, 10003]
    again = BlockClock(tmp_path, lambda: T)
    assert again.load() == "restored" and again.samples == bc.samples and again.describe()["restore"] == "restored"
    # an unreadable document: an empty clock that says so, never an exception
    (tmp_path / BLOCKCLOCK_FILE).write_text("{not json", encoding="utf-8")
    broken = BlockClock(tmp_path, lambda: T)
    assert broken.load() == "unreadable: JSONDecodeError" and broken.samples == []
    assert broken.block_at(T) is None and broken.floor(48, HEAD) == HEAD - 48 * BLOCKS_PER_HOUR
    missing = BlockClock(tmp_path / "nope", lambda: T)
    assert missing.load() == "missing" and missing.save() is False


# ------------------------------------------- (e) the live case: 44.7 hours


def _kill(block: int, tx: str, attacker: int = 2481, victim_account: int = CAROL, node: int = NODE) -> dict:
    return {"block": block, "txHash": tx, "logIndex": 0, "node": node, "victimKami": 65297, "victimAccount": victim_account, "attackerKami": 40106, "attackerAccount": attacker, "attribution": "occupant"}


def test_e_a_44_7_hour_old_kill_binds_with_the_clock_and_not_with_the_block_arithmetic(tmp_path):
    bc = _fresh(tmp_path, 50.0)
    drift = bc.block_at(T - 44.7 * 3600)  # a strike 44.7 real hours ago
    stale = bc.block_at(T - 48.5 * 3600)  # outside 48 real hours
    assert drift is not None and stale is not None and HEAD - drift > 48 * BLOCKS_PER_HOUR  # the block arithmetic has forgiven it
    led = OwnerLedger(tmp_path, block_clock=bc)
    assert led.record_kill(_kill(drift, "0xdrift")) is True
    assert led.killed_here_in_window(2481, NODE, 48, HEAD) is True
    assert led.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    assert led.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is True
    assert led.hostile_owners(NODE, 48, HEAD) == {2481} and led.hostile_kamis_for(NODE, CAROL, 48, HEAD) == {40106}
    # the same rows read with the 2.5 s arithmetic: forgiven 40 minutes early
    blind = OwnerLedger(tmp_path)
    assert blind.record_kill(_kill(drift, "0xdrift")) is True
    assert blind.killed_here_in_window(2481, NODE, 48, HEAD) is False
    assert blind.killed_ours_in_window(2481, CAROL, 48, HEAD) is False
    assert blind.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is False
    # 48.5 real hours IS outside the window, with the clock
    old = OwnerLedger(tmp_path, block_clock=bc)
    assert old.record_kill(_kill(stale, "0xold")) is True
    assert old.killed_here_in_window(2481, NODE, 48, HEAD) is False and old.killed_ours_in_window(2481, CAROL, 48, HEAD) is False
    # prune keeps the window plus a day in real hours
    ancient = bc.block_at(T - 73 * 3600)
    assert led.record_kill(_kill(ancient, "0xancient", attacker=1743)) is True
    assert led.prune(72, HEAD) >= 1
    assert led.killed_here_in_window(2481, NODE, 48, HEAD) is True and 1743 not in led.kills.get(NODE, {})
    assert led.prune(72, None) == 0  # no head: nothing is dropped


# -------------------------------------------- (f) the first-boot scan window


def test_f_the_first_boot_scan_window_is_wall_clock(tmp_path):
    bc = _fresh(tmp_path, 50.0)
    ch = FakeChain(head=HEAD)
    res = KillScanner(ch, 40, block_clock=bc).scan(None, {}, 48)
    assert res.from_block == bc.floor(48, HEAD)
    assert res.from_block != HEAD - 48 * BLOCKS_PER_HOUR and res.from_block < HEAD - 48 * BLOCKS_PER_HOUR
    # no clock: byte-identical to the 2.5-s block window
    plain = KillScanner(ch, 40).scan(None, {}, 48)
    assert plain.from_block == HEAD - 48 * BLOCKS_PER_HOUR
    empty = KillScanner(ch, 40, block_clock=BlockClock(tmp_path / "nope", lambda: T)).scan(None, {}, 48)
    assert empty.from_block == plain.from_block


# --------------------------------------------- (g) the battles seed placement


def test_g_a_timestamp_only_battles_row_is_placed_by_the_block_clock(make_keeper):
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    _seed(e.lens)
    e.grant()
    e.books = []  # type: ignore[attr-defined]
    lens_block = e.lens.block
    _clock(e.keeper.block_clock, 50.0, end_block=lens_block)
    death = [r for r in BATTLES["kills"] if r["isDeath"]][0]
    ts = T - 10 * 3600
    e.lens.battles[263] = _battles_for(263, [dict(death, blockNumber=None, timestamp=ts)])
    book = _start(e, [263])
    seed = [r for r in e.keeper.handle("ledger", {"book": book.id, "kind": "lifecycle"})["data"]["rows"] if r["event"] == "started"][0]["seedFromBattles"]
    assert seed == {"status": "seeded", "kamisRead": 1, "deaths": 1, "ownersAdded": 1, "pending": 0}
    placed = e.keeper.owners.killed_accounts[death["account"]["index"]][CAROL]
    assert placed == e.keeper.block_clock.block_at(ts)
    assert placed != lens_block - int(10 * 3600 / BLOCK_SECONDS)  # not the 2.5 s placement
    assert e.keeper.owners.killed_ours_in_window(death["account"]["index"], CAROL, 48, lens_block) is True


# ------------------------------------------------------------- (h) status


def test_h_status_carries_the_block_clock(env: Env):
    bc = env.keeper.handle("status")["data"]["blockClock"]
    assert bc == {"samples": 0, "spanSec": 0.0, "blockSeconds": BLOCK_SECONDS, "measured": False, "restore": "missing"}
    _clock(env.keeper.block_clock, 50.0)
    bc = env.keeper.handle("status")["data"]["blockClock"]
    assert bc["samples"] == 3001 and bc["measured"] is True and abs(bc["blockSeconds"] - RATE) < 0.01 and bc["spanSec"] == 180000.0


# ------------------------------------- (i) one sample per kill-scan pass


def test_i_the_kill_scan_feeds_the_clock_once_a_pass(make_keeper, tmp_path):
    chain = FakeChain(head=HEAD)
    e = _env(make_keeper, tmp_path, chain)
    e.keeper.owners.set_last_scanned(chain.head - 1)
    _book(e, [263, 68017], **LAZY)
    _tick(e)
    now1 = e.clock["now"]
    assert e.keeper.block_clock.samples == [(now1, HEAD)]
    assert json.loads((e.data_dir / BLOCKCLOCK_FILE).read_text())["samples"] == [[now1, HEAD]]
    # a second pass 30 s later: the head moved, the sample spacing has not
    chain.head += 100
    _tick(e, dt=30.0)
    assert e.keeper.block_clock.samples == [(now1, HEAD)]
    # 70 s after the first: a sample
    chain.head += 100
    _tick(e, dt=40.0)
    assert e.keeper.block_clock.samples == [(now1, HEAD), (now1 + 70.0, HEAD + 200)]
    assert len(json.loads((e.data_dir / BLOCKCLOCK_FILE).read_text())["samples"]) == 2


def test_i_no_chain_measures_nothing(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)  # the lab profile without a chain
    _book(e, [263, 68017], **LAZY)
    _tick(e)
    assert e.keeper.chain is None and e.keeper.block_clock.samples == []
    assert not (e.data_dir / BLOCKCLOCK_FILE).exists()
