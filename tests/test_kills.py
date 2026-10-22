"""keeper/kills.py against the recorded liquidate
receipt (tests/fixtures/receipts/liquidate_12143977.json): the DEAD write
shape, the calldata shape (both selectors), the KAMI_CAST decode, and the
scanner's catch-up / attribution / failure paths on a fake chain."""

from __future__ import annotations

import json
from pathlib import Path

from fakechain import FakeChain, cast_log, liquidate_log, liquidate_tx
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.kills import BLOCKS_PER_HOUR, EXECUTE_BYTES_SELECTOR, LIQUIDATE_SELECTOR, STATE_COMPONENT_TOPIC, KillScanner, OccupantRef, cast_events, dead_writes, decode_liquidate_calldata, decode_state_write

RECEIPT = json.loads((Path(__file__).with_name("fixtures") / "receipts" / "liquidate_12143977.json").read_text())
VICTIM, KILLER = 5946, 42823


def _fixture_logs() -> list[dict]:
    return [{"topics": ["0x" + t for t in l["topics"]], "data": "0x" + l["data"], "blockNumber": RECEIPT["block"], "transactionHash": "0x" + RECEIPT["tx_hash"], "logIndex": i, "address": l["address"]} for i, l in enumerate(RECEIPT["logs"])]


def test_the_recorded_liquidate_writes_dead_on_the_victims_kami_entity_and_its_calldata_names_both_parties():
    # victimHarvID == keccak("harvest", keccak("kami.id", VICTIM)); killerID == keccak("kami.id", KILLER)
    assert RECEIPT["input"][:8] == LIQUIDATE_SELECTOR[2:]
    assert decode_liquidate_calldata("0x" + RECEIPT["input"]) == (harvest_entity_id(VICTIM), kami_entity_id(KILLER))
    writes = dead_writes(_fixture_logs())
    assert len(writes) == 1
    w = writes[0]
    assert w.entity == kami_entity_id(VICTIM) == int(RECEIPT["victim_entity_dead"], 16)
    assert w.block == 12143977 and w.tx_hash == "0x" + RECEIPT["tx_hash"] and w.log_index == 29
    # the same block's INACTIVE write on the harvest entity is not a death
    state_logs = [l for l in _fixture_logs() if l["topics"][1] == STATE_COMPONENT_TOPIC]
    assert [decode_state_write(l["data"]) for l in state_logs] == ["DEAD", "INACTIVE"]
    assert decode_state_write("0x") is None and decode_state_write("0x" + "00" * 40) is None


def test_calldata_forms():
    vh, kid = harvest_entity_id(VICTIM), kami_entity_id(KILLER)
    assert decode_liquidate_calldata(liquidate_tx("0xaa", vh, kid, "typed")["input"]) == (vh, kid)
    assert decode_liquidate_calldata(liquidate_tx("0xaa", vh, kid, "bytes")["input"]) == (vh, kid)
    assert liquidate_tx("0xaa", vh, kid, "bytes")["input"].startswith(EXECUTE_BYTES_SELECTOR)
    assert decode_liquidate_calldata(liquidate_tx("0xaa", vh, kid, "other")["input"]) is None
    assert decode_liquidate_calldata("0x91") is None and decode_liquidate_calldata(LIQUIDATE_SELECTOR + "00") is None


def test_kami_cast_decodes_per_the_guard_reference():
    acc = int("0xa2A2a2A2A2A2A2a2a2a2a2A2A2A2A2a2A2A2a2a2", 16)
    ev = cast_events([cast_log(500, "0xcc", acc, kami_entity_id(263), 21202, 79, log_index=3)])
    assert len(ev) == 1 and ev[0].account_entity == acc and ev[0].target_kami_entity == kami_entity_id(263) and ev[0].item_index == 21202 and ev[0].node_index == 79 and ev[0].block == 500 and ev[0].log_index == 3
    bad = cast_log(500, "0xcc", acc, kami_entity_id(263), 21202, 79)
    bad["data"] = "0x1234"
    assert cast_events([bad]) == []


def _occupants() -> dict[int, OccupantRef]:
    return {kami_entity_id(VICTIM): OccupantRef(79, VICTIM, 2849), kami_entity_id(KILLER): OccupantRef(79, KILLER, 3878), kami_entity_id(263): OccupantRef(79, 263, 2849)}


def test_scanner_attributes_the_fixture_kill_through_the_occupants_and_the_calldata():
    ch = FakeChain(head=12143991)
    ch.logs = _fixture_logs()
    ch.txs["0x" + RECEIPT["tx_hash"]] = {"hash": "0x" + RECEIPT["tx_hash"], "input": "0x" + RECEIPT["input"], "from": RECEIPT["from"], "to": RECEIPT["to"], "blockNumber": RECEIPT["block"]}
    res = KillScanner(ch, 40).scan(12143971, _occupants(), 24)
    assert res.rpc_error is None and res.logCalls == 1 and res.last_scanned == 12143991 and res.gap == 0
    assert res.dead_writes == 1 and res.ignored == 0 and len(res.kills) == 1
    k = res.kills[0]
    assert k["victimKami"] == VICTIM and k["victimAccount"] == 2849 and k["attackerKami"] == KILLER and k["attackerAccount"] == 3878 and k["attribution"] == "occupant"
    assert k["block"] == 12143977 and k["txHash"] == "0x" + RECEIPT["tx_hash"] and k["node"] == 79
    # the same fixture with the DEAD entity unknown: ignored, no tx lookup
    res2 = KillScanner(ch, 40).scan(12143971, {kami_entity_id(263): OccupantRef(79, 263, 2849)}, 24)
    assert res2.dead_writes == 1 and res2.ignored == 1 and res2.kills == [] and res2.tx_lookups == 0


def test_scanner_attribution_paths_bytes_form_unknown_selector_and_the_chain_fallback():
    ch = FakeChain(head=120)
    vh, kid = harvest_entity_id(VICTIM), kami_entity_id(KILLER)
    ch.logs = [liquidate_log(101, "0x01", kami_entity_id(VICTIM), 0), liquidate_log(102, "0x02", kami_entity_id(VICTIM), 0), liquidate_log(103, "0x03", kami_entity_id(VICTIM), 0), liquidate_log(104, "0x04", kami_entity_id(VICTIM), 0), liquidate_log(105, "0x05", kami_entity_id(VICTIM), 0)]
    ch.txs["0x01"] = liquidate_tx("0x01", vh, kid, "bytes")  # execute(bytes), killer an occupant
    ch.txs["0x02"] = liquidate_tx("0x02", vh, kid, "other")  # unknown selector
    ch.txs["0x03"] = liquidate_tx("0x03", vh, kami_entity_id(999), "typed")  # killer not an occupant, chain resolves
    ch.txs["0x04"] = liquidate_tx("0x04", vh, kami_entity_id(998), "typed")  # killer unresolvable
    # 0x05: no transaction for the log
    ch.kami_index[kami_entity_id(999)] = 999
    occ = {kami_entity_id(VICTIM): OccupantRef(79, VICTIM, 2849), kid: OccupantRef(79, KILLER, 3878)}
    res = KillScanner(ch, 40, lookup_account=lambda idx: 4444 if idx == 999 else None).scan(100, occ, 24)
    by = {k["txHash"]: k for k in res.kills}
    assert by["0x01"]["attackerAccount"] == 3878 and by["0x01"]["attribution"] == "occupant"
    assert by["0x02"]["attackerKami"] is None and by["0x02"]["attackerAccount"] is None and by["0x02"]["attribution"] == "selector not a liquidate"
    assert by["0x03"]["attackerKami"] == 999 and by["0x03"]["attackerAccount"] == 4444 and by["0x03"]["attribution"] == "chain fallback"
    assert by["0x04"]["attackerKami"] is None and by["0x04"]["attackerAccount"] is None and "unresolved" in by["0x04"]["attribution"]
    assert by["0x05"]["attackerAccount"] is None and by["0x05"]["attribution"] == "no transaction for the log"
    assert res.tx_lookups == 5


def test_scanner_catches_up_in_capped_calls_and_reports_the_remainder_as_a_gap():
    ch = FakeChain(head=1000)
    res = KillScanner(ch, 40, max_calls=3).scan(0, {}, 24)
    spans = [(c[1][0], c[1][1]) for c in ch.calls if c[0] == "eth_getLogs" and c[1][2] is None]
    assert spans == [(1, 40), (41, 80), (81, 120)] and res.logCalls == 3 and res.last_scanned == 120 and res.gap == 880
    res2 = KillScanner(ch, 40, max_calls=100).scan(res.last_scanned, {}, 24)
    assert res2.last_scanned == 1000 and res2.gap == 0 and res2.logCalls == 22
    assert KillScanner(ch, 40).scan(1000, {}, 24).logCalls == 0  # nothing new: no call


def test_first_boot_starts_at_head_minus_the_window():
    ch = FakeChain(head=100_000)
    res = KillScanner(ch, 40, max_calls=1).scan(None, {}, 24)
    assert res.from_block == 100_000 - 24 * BLOCKS_PER_HOUR and BLOCKS_PER_HOUR == 1440
    assert res.gap == 100_000 - res.last_scanned


def test_rpc_failures_are_reported_never_raised():
    ch = FakeChain(head=100)
    ch.fail_reads = True
    res = KillScanner(ch, 40).scan(50, {}, 24)
    assert res.rpc_error and res.head is None and res.last_scanned == 50 and res.kills == []
    ch.fail_reads = False
    ch.logs = [liquidate_log(60, "0x01", kami_entity_id(VICTIM), 0)]
    calls = {"n": 0}

    def flaky(tx_hash):
        calls["n"] += 1
        from keeper.chain import RpcUnavailable

        raise RpcUnavailable("eth_getTransactionByHash: Timeout")

    ch.get_transaction = flaky  # type: ignore[method-assign]
    res = KillScanner(ch, 40).scan(50, {kami_entity_id(VICTIM): OccupantRef(79, VICTIM, 2849)}, 24)
    assert len(res.kills) == 1 and res.kills[0]["attackerAccount"] is None and res.kills[0]["attribution"] == "tx read unavailable"


# ------------------------------------------------- the overlap re-read


def test_overlap_rescans_behind_the_cursor_and_known_rows_skip_the_tx_read():
    ch = FakeChain(head=196)
    vh, kid = harvest_entity_id(VICTIM), kami_entity_id(KILLER)
    ch.logs = [liquidate_log(190, "0x01", kami_entity_id(VICTIM), 0), liquidate_log(195, "0x02", kami_entity_id(VICTIM), 0)]
    ch.txs["0x01"] = liquidate_tx("0x01", vh, kid, "typed")
    ch.txs["0x02"] = liquidate_tx("0x02", vh, kid, "typed")
    occ = _occupants()
    first = KillScanner(ch, 24).scan(150, occ, 24)
    assert len(first.kills) == 2 and first.last_scanned == 196 and first.overlap_skipped == 0
    # the next pass re-reads 12 blocks behind the cursor; the ledger already holds 0x01
    ch.head = 200
    res = KillScanner(ch, 24).scan(196, occ, 24, overlap=12, known=lambda h, i: h == "0x01")
    assert res.from_block == 185 and res.last_scanned == 200
    assert res.overlap_skipped == 1 and res.dead_writes == 1 and res.tx_lookups == 1
    assert [k["txHash"] for k in res.kills] == ["0x02"]
    # without the overlap the pass starts at the cursor + 1 and sees nothing
    plain = KillScanner(ch, 24).scan(196, occ, 24)
    assert plain.from_block == 197 and plain.kills == [] and plain.dead_writes == 0 and plain.overlap_skipped == 0
    # the start is never negative
    assert KillScanner(ch, 24).scan(5, occ, 24, overlap=12).from_block == 0


def test_a_dead_write_the_node_served_late_is_caught_by_the_overlap():
    # a pass whose log read misses a DEAD write the node's log index has not
    # caught up with yet would mark the block scanned for good
    ch = FakeChain(head=100)
    ch.log_lag = 1
    vh, kid = harvest_entity_id(VICTIM), kami_entity_id(KILLER)
    ch.logs = [liquidate_log(100, "0x01", kami_entity_id(VICTIM), 0)]
    ch.txs["0x01"] = liquidate_tx("0x01", vh, kid, "typed")
    occ = _occupants()
    late = KillScanner(ch, 24).scan(90, occ, 24, overlap=12)
    assert late.kills == [] and late.last_scanned == 100 and late.dead_writes == 0
    ch.head = 101
    caught = KillScanner(ch, 24).scan(100, occ, 24, overlap=12)
    assert caught.from_block == 89 and len(caught.kills) == 1 and caught.kills[0]["txHash"] == "0x01"
    # the miss, kept reachable: with no overlap the block is never looked at again
    assert KillScanner(ch, 24).scan(100, occ, 24, overlap=0).kills == []


def test_known_skips_casts_too():
    acc = int("0xa2A2a2A2A2A2A2a2a2a2a2A2A2A2A2a2A2A2a2a2", 16)
    ch = FakeChain(head=120)
    ch.logs = [cast_log(110, "0xcc", acc, kami_entity_id(263), 21202, 79, log_index=3)]
    occ = _occupants()
    sc = KillScanner(ch, 40, lookup_account_entity=lambda e: 4444, emitter=lambda: FakeChain.emitter)
    seen = sc.scan(100, occ, 24, our_accounts={2849})
    assert len(seen.casts) == 1 and seen.overlap_skipped == 0
    again = sc.scan(100, occ, 24, our_accounts={2849}, overlap=12, known=lambda h, i: (h, i) == ("0xcc", 3))
    assert again.casts == [] and again.overlap_skipped == 1 and again.casts_on_others == 0
