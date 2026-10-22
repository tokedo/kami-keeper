"""keeper/owners.py: the owner ledger's record /
window / killed-ours-ever / prune / persistence rules."""

from __future__ import annotations

import json

from keeper.kills import BLOCKS_PER_HOUR
from keeper.owners import OwnerLedger


def _kill(block: int, tx: str, attacker: int | None, victim_account: int = 2849, node: int = 79, log_index: int = 0) -> dict:
    return {"block": block, "txHash": tx, "logIndex": log_index, "node": node, "victimKami": 5946, "victimAccount": victim_account, "attackerKami": 42823 if attacker else None, "attackerAccount": attacker, "attribution": "occupant" if attacker else "selector not a liquidate"}


def test_record_window_and_killed_ours_ever(tmp_path):
    led = OwnerLedger(tmp_path)
    assert led.load() == "missing"
    assert led.record_kill(_kill(1000, "0x01", 3878)) is True
    assert led.record_kill(_kill(1000, "0x01", 3878)) is False  # same tx + log index: not twice
    assert led.record_kill(_kill(1000, "0x01", 3878, log_index=1)) is True
    head = 1000 + 10 * BLOCKS_PER_HOUR
    assert led.killed_here_in_window(3878, 79, 24, head) is True
    assert led.killed_here_in_window(3878, 79, 6, head) is False  # 10 h ago, 6 h window
    assert led.killed_here_in_window(3878, 62, 24, head) is False  # another node
    assert led.killed_here_in_window(3878, 79, 24, None) is True  # no head: every recorded kill counts
    assert led.killed_ours_in_window(3878, 2849, 24, head) is True and led.killed_ours_in_window(3878, 2307, 24, head) is False  # the windowed read (no permanent class)
    assert led.killed_accounts == {3878: {2849: 1000}}
    assert led.hostile_owners(79, 24, head) == {3878} and led.hostile_owners(79, 6, head) == set()
    # an unattributed kill is recorded, listed on the node, and classes nobody
    assert led.record_kill(_kill(1001, "0x02", None)) is True
    assert [r["txHash"] for r in led.kills_on(79)] == ["0x01", "0x01", "0x02"]
    assert led.kills_on(79)[-1]["attackerAccount"] is None
    assert led.hostile_owners(79, 24, head) == {3878}


def test_casts_count_like_kills():
    led = OwnerLedger("/nonexistent")
    assert led.record_cast({"block": 5, "txHash": "0xc", "logIndex": 0, "node": 79, "targetKami": 263, "targetAccount": 2849, "casterAccount": 555, "item": 21202}) is True
    assert led.record_cast({"block": 5, "txHash": "0xc", "logIndex": 0, "node": 79, "targetKami": 263, "targetAccount": 2849, "casterAccount": 555, "item": 21202}) is False
    assert led.record_cast({"block": 5, "txHash": "0xd", "logIndex": 0, "node": 79, "targetKami": 263, "targetAccount": 2849, "casterAccount": None, "item": 21202}) is False
    assert led.killed_here_in_window(555, 79, 24, 100) and led.killed_ours_in_window(555, 2849, 24, 100)


def test_prune_keeps_killed_accounts_and_persistence_round_trips(tmp_path):
    led = OwnerLedger(tmp_path)
    led.record_kill(_kill(100, "0x01", 3878))
    led.record_kill(_kill(50_000, "0x02", 3878))
    led.record_kill(_kill(60, "0x03", None))
    led.set_last_scanned(50_100)
    assert led.save() is True and led.save() is False  # write on change only
    head = 50_100
    dropped = led.prune(24 + 24, head)  # keep 48 h = 69,120 blocks: nothing below 0 here... move head
    assert dropped == 0
    dropped = led.prune(1, 50_100)  # keep 1 h = 1,440 blocks: the kills at 100 and 60 go
    assert dropped == 2 and [r["txHash"] for r in led.kills[79][3878]] == ["0x02"]
    assert led.killed_accounts == {3878: {2849: 50_000}}  # the LAST kill block, and pruned like any row (it is inside the hour kept here)
    assert led.unattributed == {}
    led.save()
    again = OwnerLedger(tmp_path)
    assert again.load() == "restored" and again.last_scanned_block == 50_100 and again.killed_accounts == {3878: {2849: 50_000}}
    assert [r["txHash"] for r in again.kills_on(79)] == ["0x02"]
    doc = json.loads((tmp_path / "owners.json").read_text())
    assert doc["owners"]["lastScannedBlock"] == 50_100 and "schemaVersion" in doc
    (tmp_path / "owners.json").write_text("{not json")
    broken = OwnerLedger(tmp_path)
    assert broken.load().startswith("unreadable") and broken.last_scanned_block is None
