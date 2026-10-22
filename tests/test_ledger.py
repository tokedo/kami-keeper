"""SPEC P5 — row schema for every kind; death rows; dry-run marking;
every dry-run 'tx' has a row; --since."""

from __future__ import annotations

import pytest
from jsonschema import ValidationError

from keeper.ledger import DEATH_OUTCOMES, LIFECYCLE_EVENTS, ROW_KINDS, Ledger, validate_row
from keeper.protocol import Basis
from keeper.scrub import ScrubError


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "b1", "b1", "lab")


def test_every_kind_writes_a_schema_valid_row(ledger):
    b = Basis(lensBlock=100, lensReconciledThrough=98)
    ledger.tick(b, {"acted": False})
    ledger.decision(b, "feed", {"hp": 40, "threshold": 62, "margin": 22, "class": "threshold-no-kill", "window": "24h"})
    ledger.tx(b, {"txHash": "0x" + "ab" * 32, "nonce": 5, "block": 101, "status": "success", "gasUsed": 1, "effectiveGasPrice": 2500000, "kami": 1, "system": "system.kami.use.item", "musuCollected": None})
    ledger.death(b, 1, 100, "revive-skipped: no-item")
    ledger.lifecycle(b, "started", reason=None)
    # the chain kill scan's row and a refused action's backoff
    ledger.kill(b, {"victimKami": 5946, "victimAccount": 806, "attackerKami": 42823, "attackerAccount": 3878, "block": 12143977, "txHash": "0x" + "cd" * 32, "ownerClassAfter": "killed-here-24h"})
    ledger.deviation(b, {"kami": 263, "action": "start", "reason": "Harvest: kami not in room [code 3]", "backoffSec": 3600, "until": 1.0})
    rows = list(ledger.rows())
    assert [r["kind"] for r in rows] == list(ROW_KINDS)
    assert rows[5]["kill"]["event"] == "liquidate" and rows[6]["deviation"]["kami"] == 263
    for r in rows:
        assert r["schemaVersion"] and r["lensBlock"] == 100 and r["lensReconciledThrough"] == 98 and r["degraded"] == [] and r["dryRun"] is False
    assert set(LIFECYCLE_EVENTS) == {"started", "stopped_by", "paused", "resumed", "external-change", "readopted", "parking", "failed"}  # the `parking` kind
    assert set(DEATH_OUTCOMES) >= {"revived", "revive-skipped: no-item"}


def test_invalid_rows_are_refused(ledger):
    b = Basis()
    with pytest.raises(ValidationError):
        ledger.lifecycle(b, "vanished")
    with pytest.raises(ValidationError):
        ledger.death(b, 1, None, "maybe")
    with pytest.raises(ScrubError):
        ledger.decision(b, "rest", {"note": "a predator is near"})
    with pytest.raises(ValidationError):
        validate_row({"kind": "tick"})
    assert not ledger.path.exists() or ledger.path.read_text() == ""


def test_since_and_kind_filters(ledger):
    for blk in (10, 20, 30):
        ledger.tick(Basis(lensBlock=blk, lensReconciledThrough=blk), {"n": blk})
    ledger.lifecycle(Basis(), "paused")
    assert [r["lensBlock"] for r in ledger.since(20)] == [20, 30]
    assert len(ledger.since(None, "lifecycle")) == 1
    assert len(ledger.since()) == 4


def test_death_row_precedes_any_revive_and_dry_run_rows_are_marked(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    env.lens.set_kami(1001, hp=(40, 200))
    env.keeper.tick_book(book)  # feed (dry run)
    env.lens.set_kami(1001, state="DEAD", node=None)
    env.keeper.tick_book(book)
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    kinds = [r["kind"] for r in rows]
    assert "death" in kinds
    d = rows[kinds.index("death")]
    assert d["death"] == {"kami": 1001, "deadBlock": None, "outcome": "revive-skipped: disabled", "txHash": None}
    txs = [r for r in rows if r["kind"] == "tx"]
    assert txs and all(r["dryRun"] is True and r["tx"]["status"] == "dry_run" for r in txs)
    # every recorded dry-run send has a row
    assert len(txs) == len(env.keeper.signer.recorded)
    assert book.state == "paused"


def test_tick_rows_are_written_on_change_and_at_the_heartbeat(env):
    """A `tick` row when the summary changes, and at
    least every tickHeartbeat ticks; identical quiet ticks write nothing."""
    env.grant()
    bid = env.start_haf(tickHeartbeat=5)["data"]["book"]["id"]
    book = env.keeper.store.get(bid)

    def ticks():
        return [r for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tick"]

    env.keeper.tick_book(book)  # first assessment: a row
    assert len(ticks()) == 1
    for _ in range(4):
        env.keeper.tick_book(book)  # unchanged: no row
    assert len(ticks()) == 1
    env.keeper.tick_book(book)  # 5 ticks after the last row: heartbeat
    assert len(ticks()) == 2
    env.lens.set_kami(1001, hp=(40, 200))
    env.keeper.tick_book(book)  # feed: the summary changed
    assert len(ticks()) == 3 and ticks()[-1]["summary"]["acted"] is True
