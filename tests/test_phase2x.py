"""Cooldown-aware start / rest:

  1. a RESTING kami the lens reads on the chain's cooldown is not offered a
      start, is counted `kamis.cooldown`, rides `cooldownDeferred[]`, and is
      neither backed off nor queued for the start batch;
  2. the cooldown ends -> the kami is offered afresh, `readySinceSec` 0,
      and the note is gone;
  3. a HARVESTING kami at its limit on cooldown is not offered a stop this
      tick (no `rest` backoff, no deviation); the next tick stops it;
  4. a joiner on cooldown is never pulled into a stop pack;
  5. a `kami on cooldown` refusal that reaches the chain all the same waits
      min(`startBackoffSec`, 300) = 300 s, any other reason still 3600 s;
  6. with `cooldownSec` 0 everywhere a start-batch scenario decides
      exactly as without the rule, and the only added key is the constant 0.

The books are the four kamis of `tests/test_phase2c.py` (RESTING_FOUR, 250 /
240 / 230 / 250 HP) on the recorded fixtures: `_batch_env` for the RESTING
paths (every attacker previewed, `resumeHpPct` 100) and `_pack_env` of
`tests/test_phase2ib.py` for the HARVESTING ones (no recorded preview, every
threshold synthesized 0, so a limit is the 10 % rest floor: 25 / 24 / 23 /
25 HP). `cooldownSec` rides the kami's own row of the `party --full` answer,
where `keeper/strategies/base.py` reads it onto `KamiView.cooldown_sec`.
"""

from __future__ import annotations

from typing import Any

from keeper.signer import DryRunSigner
from keeper.strategies.auto import COOLDOWN_REFUSAL_BACKOFF_SEC
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import NODE, _apply_plans_to_world, _last_tick, _party_row, _rows, _tick_all
from test_phase2c import RESTING_FOUR, _batch_env, _full, _starts
from test_phase2ib import A, B, C, D, _pack_env, _rests


def _cooldown(e, kami: int, sec: int) -> None:
    """The kami's `party --full` row carries the chain's action cooldown."""
    _party_row(e.lens, kami)["cooldownSec"] = int(sec)


def _counts(e, book) -> dict[str, Any]:
    return _last_tick(e, book)["kamis"]


# ------------------------------------------------------------ 1 / 2: the start side


def test_a_resting_kami_on_cooldown_is_not_offered_a_start_and_is_not_queued(make_keeper):
    """An arrival inside the 1-h purchase cooldown. The chain
    would refuse the start pre-send, so the book does not offer it — and it
    is NOT a backoff, so nothing is held for an hour after the cooldown."""
    e, book = _batch_env(make_keeper, startBatchWaitSec=300, startBatchMin=4)
    _full(e, A)
    _cooldown(e, A, 3000)
    _tick_all(e)
    s = _last_tick(e, book)
    assert _starts(e, book) == []
    assert s["kamis"]["cooldown"] == 1 and s["kamis"]["waiting"] == 0 and s["kamis"]["resting"] == 4
    assert s["cooldownDeferred"] == [{"kami": A, "action": "start", "cooldownSec": 3000}]
    assert "startWaiting" not in s and s.get("decisions") in (None, [])
    assert _rows(e, book, "deviation") == [] and e.keeper.runtime[book.id].get("backoff", {}) == {}
    assert e.keeper.runtime[book.id].get("startQueue", {}) == {}  # not ready: it never enters the start queue
    assert book.plan[str(A)] == {"state": RESTING, "node": None}


def test_when_the_cooldown_ends_the_kami_is_offered_afresh_and_the_note_is_gone(make_keeper):
    e, book = _batch_env(make_keeper)  # the schema default: startBatchWaitSec 0
    _full(e, A)
    _cooldown(e, A, 3000)
    _tick_all(e)
    assert _starts(e, book) == [] and _counts(e, book)["cooldown"] == 1
    _cooldown(e, A, 0)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A] and st[0]["batchSize"] == 1
    assert st[0]["detail"][0]["readySinceSec"] == 0 and st[0]["detail"][0]["resumeHpPct"] == 100
    s = _last_tick(e, book)
    assert s["kamis"]["cooldown"] == 0 and "cooldownDeferred" not in s
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE}


# ------------------------------------------------------------ 3. the rest side


def test_a_harvesting_kami_on_cooldown_is_not_offered_a_stop_this_tick(make_keeper):
    """A kami inside the 180-s action cooldown of its own start that reaches
    its limit: the chain would refuse the stop, and a refused single costs it
    a 3600-s `rest` backoff — an hour harvesting under a hostile line. The
    stop is deferred instead and offered as soon as the cooldown clears."""
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (100, 30), C: (100, 30), D: (100, 30)})
    _cooldown(e, A, 120)
    _tick_all(e)
    s = _last_tick(e, book)
    assert _rests(e, book) == []
    assert s["kamis"]["cooldown"] == 1 and s["kamis"]["harvesting"] == 4 and s["kamis"]["pulled"] == 0
    assert s["cooldownDeferred"] == [{"kami": A, "action": "rest", "cooldownSec": 120, "hp": 25, "limit": 25}]
    assert _rows(e, book, "deviation") == [] and e.keeper.runtime[book.id].get("backoff", {}) == {}
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE}
    _cooldown(e, A, 0)
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A] and r[0]["batchSize"] == 1
    s = _last_tick(e, book)
    assert s["kamis"]["cooldown"] == 0 and "cooldownDeferred" not in s
    assert book.plan[str(A)] == {"state": RESTING, "node": None, "pending": True}


# ------------------------------------------------------------ 4. never a pack joiner


def test_a_joiner_on_cooldown_is_never_pulled_into_the_stop_pack(make_keeper):
    """B and C are both 10 projected minutes from their own limit; C is on
    cooldown, so only B rides the anchor's stop. C is ABOVE its limit, so it
    is not a deferred decision either — the pack filter simply passes it
    over, and it joins the next pack once the cooldown has cleared."""
    # B: (29 - 24) / 30 x 60 = 10.0 min; C: (28 - 23) / 30 x 60 = 10.0 min; D: 150 min
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (29, 30), C: (28, 30), D: (100, 30)}, stopJoinWindowMin=10, stopPackMax=12)
    _cooldown(e, C, 60)
    _tick_all(e)
    r = _rests(e, book)
    assert len(r) == 1 and r[0]["kamis"] == [A, B] and r[0]["batchSize"] == 2
    assert r[0]["detail"][1]["pulled"] is True and r[0]["detail"][1]["projectedMinutes"] == 10.0
    s = _last_tick(e, book)
    assert s["stopPulled"] == [{"kami": B, "projectedMinutes": 10.0}] and s["kamis"]["pulled"] == 1
    assert s["kamis"]["cooldown"] == 0 and "cooldownDeferred" not in s  # C is above its limit: nothing was deferred
    assert book.plan[str(C)] == {"state": HARVESTING, "node": NODE}


# ------------------------------------------------------------ 5. the refusal that still reaches the chain


def test_a_kami_on_cooldown_refusal_waits_five_minutes_and_any_other_reason_still_waits_the_hour(make_keeper):
    """The lens normally makes this refusal impossible; when the lens and
    the chain disagree, the wait is one re-offer, not the configured hour."""
    e, book = _batch_env(make_keeper)
    assert book.config["startBackoffSec"] == 3600 and COOLDOWN_REFUSAL_BACKOFF_SEC == 300
    _full(e, A)
    said = {"reason": "revert: kami on cooldown: Reverted [code -32000]"}

    class Refuse(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system == "system.harvest.start":
                    r.status, r.precheck, r.reason = "not_sent", "reverted", said["reason"]
            return recs

    signer = Refuse()
    signer._grants = e.keeper.signer._grants
    e.keeper.signer = signer
    _tick_all(e)
    t = e.clock["now"]
    dev = [r["deviation"] for r in _rows(e, book, "deviation")]
    assert len(dev) == 1 and dev[0]["kami"] == A and dev[0]["action"] == "start"
    assert dev[0]["backoffSec"] == 300 and dev[0]["until"] - t == 300 and dev[0]["reason"] == said["reason"]
    bo = e.keeper.runtime[book.id]["backoff"][str(A)]
    assert bo["kind"] == "start" and bo["until"] - t == 300 and bo["reason"] == said["reason"]
    # the wait elapses -> the kami is offered again after five minutes, not an
    # hour; this time the chain refuses it for another reason -> 3600 s
    said["reason"] = "revert: something else"
    _tick_all(e, dt=300)
    assert e.clock["now"] == t + 300
    dev = [r["deviation"] for r in _rows(e, book, "deviation")]
    assert len(dev) == 2 and dev[1]["kami"] == A
    assert dev[1]["backoffSec"] == 3600 and dev[1]["until"] - e.clock["now"] == 3600
    assert e.keeper.runtime[book.id]["backoff"][str(A)]["until"] - e.clock["now"] == 3600


# ------------------------------------------------------------ 6. zero cooldown changes nothing


def test_a_book_with_no_cooldown_anywhere_decides_exactly_as_it_did(make_keeper):
    """`tests/test_phase2c.py::test_wait_zero_is_todays_behaviour_a_ready_kami_starts_on_its_tick`
    re-run: the same decisions, the same rows, and the `kamis` counts with
    the one added key at its constant 0."""
    e, book = _batch_env(make_keeper)  # the schema defaults: startBatchWaitSec 0, startBatchMin 4
    assert book.config["startBatchWaitSec"] == 0 and book.config["stopJoinWindowMin"] == 0
    assert all(_party_row(e.lens, k).get("cooldownSec", 0) == 0 for k in RESTING_FOUR)
    _full(e, A)
    _tick_all(e)
    st = _starts(e, book)
    assert len(st) == 1 and st[0]["kamis"] == [A] and st[0]["batchSize"] == 1 and st[0]["detail"][0]["readySinceSec"] == 0
    s = _last_tick(e, book)
    assert s["kamis"] == {"harvesting": 0, "resting": 4, "dead": 0, "held": 0, "backoff": 0, "elsewhere": 0, "waiting": 0, "pulled": 0, "joined": 0, "cooldown": 0}  # `joined` is part of the counts
    assert "cooldownDeferred" not in s and "startWaiting" not in s and s["decisions"] == ["start"]
    assert "startQueue" not in e.keeper.runtime[book.id]
    _apply_plans_to_world(e)
    _full(e, B)
    _tick_all(e)
    assert _starts(e, book)[-1]["kamis"] == [B]  # one single per ready kami, as before
    assert _counts(e, book)["cooldown"] == 0 and "cooldownDeferred" not in _last_tick(e, book)
