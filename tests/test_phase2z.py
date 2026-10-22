"""A NODE-TRANSIENT refusal is not a verdict on the kami:

  1. a single `rest` whose pre-check PASSED and whose BROADCAST the node
     refused with a readiness race waits 60 s, not the configured hour,
     and the kami is rested on the first tick after the wait;
  2. a start refused with no batch answer for its nonce — the same 60 s;
  3. a start refused by a transport failure — the same 60 s;
  4. the verdicts are unchanged — a revert text 3600 s, `kami on cooldown`
     300 s, a tx that landed and reverted 3600 s, and none of the three
     rows carries `transient`;
  5. a `nonce race:` refusal is still neither a deviation nor a backoff;
  6. `refusal_is_verdict` on the seven texts the node is known to answer.

The books are the four kamis of `tests/test_phase2c.py` (RESTING_FOUR, 250 /
240 / 230 / 250 HP) on the recorded fixtures: `_batch_env` for the start side
(`resumeHpPct` 100, so `_full` makes exactly one kami ready) and `_pack_env`
of `tests/test_phase2ib.py` for the rest side, where no preview is recorded
and every threshold is synthesized 0, so a limit is the 10 % rest floor
(25 / 24 / 23 / 25 HP).
"""

from __future__ import annotations

import types
from typing import Any

from keeper.signer import DryRunSigner
from keeper.strategies.auto import COOLDOWN_REFUSAL_BACKOFF_SEC, TRANSIENT_REFUSAL_BACKOFF_SEC, refusal_is_verdict
from keeper.strategies.base import HARVESTING, RESTING
from test_phase2a import NODE, _last_tick, _rows, _tick_all
from test_phase2c import _batch_env, _full, _starts
from test_phase2ib import A, B, C, D, _pack_env, _rests

# the node's own text, verbatim as a ledger row records it: the pre-check
# passed and the broadcast was refused because the node had not committed
# the block it was asked to simulate against — nothing about the kami
READINESS = "jsonrpc readiness error: failed to load state at height 12985162; historical version not ready: 12985162: invalid height (latest height: 12985162): invalid request [code 5]"
NO_ANSWER = "no response for nonce 7 in batch of 1"
TRANSPORT = "transport failed: TransportError"


def _refuse(e, system: str, said: dict[Any, tuple[str, str, str]]) -> None:
    """Put a refusing dry-run signer behind the core: every record of
    `system` whose `kami` (None = a batched intent, `"*"` = any) is a key of
    `said` comes back in that `(status, precheck, reason)` shape. `said` is
    read on every send, so a test clears it to let the action land."""

    class Refuse(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system != system:
                    continue
                say = said.get(r.kami, said.get("*"))
                if say is not None:
                    r.status, r.precheck, r.reason = say
            return recs

    signer = Refuse()
    signer._grants = e.keeper.signer._grants
    e.keeper.signer = signer


def _devs(e, book) -> list[dict[str, Any]]:
    return [r["deviation"] for r in _rows(e, book, "deviation")]


# ------------------------------------------------------------ 1. a transient rest refusal


def test_a_rest_refused_by_a_node_readiness_race_waits_sixty_seconds_and_is_offered_again(make_keeper):
    """A refusal the node owned must not cost the kami the full
    `startBackoffSec` under a present attacker's line: the pre-check PASSED,
    so the refusal was never a verdict on the kami."""
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (100, 30), C: (100, 30), D: (100, 30)})
    assert book.config["startBackoffSec"] == 3600 and TRANSIENT_REFUSAL_BACKOFF_SEC == 60
    said: dict[Any, tuple[str, str, str]] = {"*": ("not_sent", "ok", READINESS)}
    _refuse(e, "system.harvest.stop", said)

    _tick_all(e)
    t = e.clock["now"]
    dev = _devs(e, book)
    assert len(dev) == 1
    assert dev[0] == {"kami": A, "action": "rest", "reason": READINESS[:160], "backoffSec": 60, "until": float(t + 60), "transient": True}
    bo = e.keeper.runtime[book.id]["backoff"][str(A)]
    assert bo["kind"] == "rest" and bo["until"] - t == 60 and bo["reason"] == READINESS[:160]
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE}  # the stop did not land
    assert _rests(e, book) and len(_rests(e, book)) == 1
    # the counts of a tick come from `assess`, which ran BEFORE the refusal,
    # so the refusal tick itself still reads 0;
    # the kami is counted from the next tick on
    assert _last_tick(e, book)["kamis"]["backoff"] == 0

    # half the wait on: still in backoff, counted, not offered
    _tick_all(e, dt=30)
    assert len(_rests(e, book)) == 1 and len(_devs(e, book)) == 1
    assert _last_tick(e, book)["kamis"]["backoff"] == 1

    # the wait elapses and the node answers normally again: the rest is
    # offered afresh on that tick, not at the end of the hour
    said.clear()
    _tick_all(e, dt=30)
    assert e.clock["now"] == t + 60
    r = _rests(e, book)
    assert len(r) == 2 and r[1]["kamis"] == [A] and r[1]["batchSize"] == 1
    assert book.plan[str(A)] == {"state": RESTING, "node": None, "pending": True}
    assert _last_tick(e, book)["kamis"]["backoff"] == 0
    assert len(_devs(e, book)) == 1  # the landed rest wrote no new row


# ------------------------------------------------------------ 2 / 3. the start side


def test_a_start_refused_with_no_batch_answer_waits_sixty_seconds_and_is_offered_again(make_keeper):
    """The broadcast carried the step and the node answered for no nonce:
    on the start side an hour of backoff costs harvest time, and it is the
    same non-verdict."""
    e, book = _batch_env(make_keeper)
    _full(e, A)
    said: dict[Any, tuple[str, str, str]] = {"*": ("not_sent", "ok", NO_ANSWER)}
    _refuse(e, "system.harvest.start", said)

    _tick_all(e)
    t = e.clock["now"]
    dev = _devs(e, book)
    assert len(dev) == 1 and dev[0]["kami"] == A and dev[0]["action"] == "start"
    assert dev[0]["backoffSec"] == 60 and dev[0]["until"] - t == 60 and dev[0]["transient"] is True
    assert dev[0]["reason"] == NO_ANSWER
    assert e.keeper.runtime[book.id]["backoff"][str(A)]["until"] - t == 60
    assert _starts(e, book)[-1]["kamis"] == [A] and book.plan[str(A)] == {"state": RESTING, "node": None}

    said.clear()
    _tick_all(e, dt=60)
    assert e.clock["now"] == t + 60
    st = _starts(e, book)
    assert len(st) == 2 and st[1]["kamis"] == [A] and st[1]["batchSize"] == 1
    assert book.plan[str(A)] == {"state": HARVESTING, "node": NODE}


def test_a_start_refused_by_a_transport_failure_waits_sixty_seconds(make_keeper):
    e, book = _batch_env(make_keeper)
    _full(e, A)
    _refuse(e, "system.harvest.start", {"*": ("not_sent", "ok", TRANSPORT)})
    _tick_all(e)
    assert _devs(e, book) == [{"kami": A, "action": "start", "reason": TRANSPORT, "backoffSec": 60, "until": float(e.clock["now"] + 60), "transient": True}]


# ------------------------------------------------------------ 4. a verdict is unchanged


def test_every_refusal_that_is_a_verdict_waits_exactly_as_it_did(make_keeper):
    """Three kamis at their rest floor go out as one batched stop; the core
    re-offers each as a single and the node refuses each one
    its own way. A pre-check revert, a `kami on cooldown` revert and a tx
    that landed and reverted: 3600 / 300 / 3600, and no `transient` key."""
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (24, 30), C: (23, 30), D: (100, 30)})
    assert book.config["startBackoffSec"] == 3600 and COOLDOWN_REFUSAL_BACKOFF_SEC == 300
    _refuse(e, "system.harvest.stop", {
        None: ("not_sent", "reverted", "revert: the batch did not land"),  # the batch, so singles are re-offered
        A: ("not_sent", "reverted", "revert: something else"),
        B: ("not_sent", "reverted", "revert: kami on cooldown: Reverted [code -32000]"),
        C: ("reverted", "ok", ""),  # landed on chain and reverted
    })

    _tick_all(e)
    t = e.clock["now"]
    by_kami = {d["kami"]: d for d in _devs(e, book)}
    assert sorted(by_kami) == sorted([A, B, C]) and len(_devs(e, book)) == 3
    assert by_kami[A]["backoffSec"] == 3600 and by_kami[A]["until"] - t == 3600 and "transient" not in by_kami[A]
    assert by_kami[B]["backoffSec"] == 300 and by_kami[B]["until"] - t == 300 and "transient" not in by_kami[B]
    assert by_kami[C]["backoffSec"] == 3600 and by_kami[C]["reason"] == "reverted on chain" and "transient" not in by_kami[C]
    rt = e.keeper.runtime[book.id]["backoff"]
    assert [rt[str(k)]["until"] - t for k in (A, B, C)] == [3600.0, 300.0, 3600.0]


# ------------------------------------------------------------ 5. a nonce race is untouched


def test_a_nonce_race_is_still_neither_a_deviation_nor_a_backoff(make_keeper):
    e, book = _pack_env(make_keeper, {A: (25, 30), B: (100, 30), C: (100, 30), D: (100, 30)})
    _refuse(e, "system.harvest.stop", {"*": ("not_sent", "ok", "nonce race: nonce 7 taken by 0xabc")})
    _tick_all(e)
    assert _devs(e, book) == [] and e.keeper.runtime[book.id].get("backoff", {}) == {}


# ------------------------------------------------------------ 6. the predicate itself


def test_refusal_is_verdict_on_every_text_that_has_been_observed():
    def v(precheck: str, status: str, reason: str) -> bool:
        return refusal_is_verdict(types.SimpleNamespace(precheck=precheck, status=status), reason)

    # the chain's verdict on the kami
    assert v("reverted", "not_sent", "") is True  # refused pre-send
    assert v("ok", "reverted", "") is True  # landed and reverted
    assert v("ok", "not_sent", "revert: x") is True
    assert v("ok", "not_sent", "execution reverted [code 3]") is True
    # the node's own problem: the pre-check passed and the broadcast was refused
    assert v("ok", "not_sent", READINESS) is False
    assert v("ok", "not_sent", "not offered: stop requested before this chunk") is False
    assert v("ok", "not_sent", "node refused nonce 3 with no error message: {…}") is False
