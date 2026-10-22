"""Kill rows dated by the CHAIN's own block timestamp, not by the block
clock's model of the chain's rate.

The block clock extrapolates at its whole-span rate for a target time
before its first sample, and the chain's rate moves within a day (2.2-2.7
s/block), so a window measured in blocks can end hours early: an attacker
whose kill was dated by extrapolation stops binding while it is still
inside the window in real time.

Here: `Chain.block_timestamp`, the scanner's per-pass header reads and
their cap, a kill 47.45 h back through `OwnerLedger` (the dated row binds
where the undated one is forgiven), the ts tables' raise rule, `prune`,
the `owners.json` round trip at an unchanged `schemaVersion`, the battles
seed's own date, and the ledger row + `killScan.tsUnread` through the core.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fakechain import FakeChain, cast_log, liquidate_log, liquidate_tx
from keeper import kills as kills_mod
from keeper.blockclock import BlockClock
from keeper.chain import Chain, RpcUnavailable, harvest_entity_id, kami_entity_id
from keeper.kills import BLOCKS_PER_HOUR, TS_READS_PER_PASS, KillScanner, OccupantRef
from keeper.owners import OWNERS_SCHEMA_VERSION, OwnerLedger
from test_phase2a import CAROL, NODE, T, _add_occupant, _rows, _seed, _start
from test_phase2b import BATTLES, _battles_for
from test_phase2g import LAZY, _book, _env, _tick
from test_phase2t import HEAD, _clock

H = 3600.0
# 38 h of samples at 2.61 s/block, a kill 47.45 h
# back that the extrapolated floor dated at 48.07 h
RATE_2T_B = 2.61
INCIDENT_BLOCK = HEAD - 66_300


def _kill(block: int, tx: str, attacker: int | None = 2481, attacker_kami: int | None = 40106, victim_account: int = CAROL, node: int = NODE, block_ts: int | None = None, log_index: int = 0) -> dict[str, Any]:
    return {"block": block, "txHash": tx, "logIndex": log_index, "node": node, "victimKami": 5034, "victimAccount": victim_account, "attackerKami": attacker_kami, "attackerAccount": attacker, "attribution": "occupant", "blockTs": block_ts}


def _led(tmp_path: Path, hours: float = 38.0, rate: float = RATE_2T_B) -> OwnerLedger:
    """A ledger on a block clock with `hours` of samples at `rate` ending at
    (T, HEAD)."""
    return OwnerLedger(tmp_path, block_clock=_clock(BlockClock(tmp_path, lambda: T), hours, rate=rate))


# ------------------------------------------------- (1) the chain read


class _Eth:
    def __init__(self, answer: Any):
        self.answer, self.calls = answer, []

    def get_block(self, block: Any, full_transactions: Any) -> Any:
        self.calls.append((block, full_transactions))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class _W3:
    def __init__(self, eth: _Eth):
        self.eth = eth
        self.provider = object()


def _chain(answer: Any) -> tuple[Chain, _Eth]:
    eth = _Eth(answer)
    return Chain("", "0x" + "22" * 20, 1, web3=_W3(eth)), eth


def test_1_block_timestamp_is_the_header_and_any_failure_is_rpc_unavailable():
    chain, eth = _chain({"number": 12581714, "timestamp": 1790000000})
    assert chain.block_timestamp(12581714) == 1790000000
    assert eth.calls == [(12581714, False)]  # the header only, never the bodies
    bad, _e = _chain(ValueError("BlockNotFound"))  # a block the node does not have
    with pytest.raises(RpcUnavailable):
        bad.block_timestamp(1)


# --------------------------------------- (2) one header read per block per pass


def _scan_world(head: int = 1000) -> tuple[FakeChain, dict[int, OccupantRef]]:
    ch = FakeChain(head=head)
    occ = {kami_entity_id(k): OccupantRef(NODE, k, CAROL) for k in (5946, 7834, 7835)}
    ch.kami_index[kami_entity_id(42823)] = 42823
    return ch, occ


def _scanner(ch: FakeChain) -> KillScanner:
    return KillScanner(ch, 40, lookup_index=ch.kami_index_of, lookup_account=lambda idx: 3878, lookup_account_entity=lambda entity: 997, emitter=lambda: ch.emitter)


def _three_kills_and_a_cast(ch: FakeChain) -> None:
    """Two kills in block 990, one in 991, and a KAMI_CAST in 991 too."""
    ch.logs = [liquidate_log(990, "0x01", kami_entity_id(5946), 0), liquidate_log(990, "0x02", kami_entity_id(7834), 1), liquidate_log(991, "0x03", kami_entity_id(7835), 0), cast_log(991, "0x04", 0xabc, kami_entity_id(5946), 21202, NODE)]
    for tx, victim in (("0x01", 5946), ("0x02", 7834), ("0x03", 7835)):
        ch.txs[tx] = liquidate_tx(tx, harvest_entity_id(victim), kami_entity_id(42823))


def test_2_every_row_carries_its_blocks_stamp_at_one_header_read_per_block(monkeypatch):
    ch, occ = _scan_world()
    _three_kills_and_a_cast(ch)
    ch.block_ts = {990: 1790000000, 991: 1790000003}
    res = _scanner(ch).scan(989, occ, 48, our_accounts={CAROL})
    assert [k["blockTs"] for k in res.kills] == [1790000000, 1790000000, 1790000003]
    assert [c["blockTs"] for c in res.casts] == [1790000003]  # the cast rides the cached header of its block
    assert ch.block_ts_calls == 2 and res.ts_unread == 0  # one read per DISTINCT block
    # the node does not answer the headers: every row is recorded UNDATED and
    # counted once per distinct block, and the kills land all the same
    ch2, occ2 = _scan_world()
    _three_kills_and_a_cast(ch2)
    ch2.fail_block_ts = True
    res2 = _scanner(ch2).scan(989, occ2, 48, our_accounts={CAROL})
    assert [k["blockTs"] for k in res2.kills] == [None, None, None] and res2.casts[0]["blockTs"] is None
    assert res2.ts_unread == 2 and len(res2.kills) == 1 + 2 and res2.logCalls == 1
    assert [k["victimKami"] for k in res2.kills] == [5946, 7834, 7835]
    # the per-pass cap: beyond it a row is recorded undated, and the eth_getLogs
    # budget is untouched by header reads
    monkeypatch.setattr(kills_mod, "TS_READS_PER_PASS", 1)
    ch3, occ3 = _scan_world()
    _three_kills_and_a_cast(ch3)
    ch3.block_ts = {990: 1790000000, 991: 1790000003}
    res3 = _scanner(ch3).scan(989, occ3, 48, our_accounts={CAROL})
    assert [k["blockTs"] for k in res3.kills] == [1790000000, 1790000000, None]
    assert ch3.block_ts_calls == 1 and res3.ts_unread == 1 and res3.logCalls == 1
    assert TS_READS_PER_PASS == 50  # the shipped budget


# ------------------------------------- (3) a kill 47.45 h back binds


def test_3_a_47_45_hour_kill_binds_by_its_timestamp_where_the_extrapolated_floor_forgave_it(tmp_path):
    dated = _led(tmp_path)
    floor = dated.block_clock.floor(48, HEAD)
    assert floor > INCIDENT_BLOCK  # the clock has forgiven it: extrapolation at 2.61 s/block calls the block 48.07 h old
    assert dated.record_kill(_kill(INCIDENT_BLOCK, "0xdrift", block_ts=int(T - 47.45 * H))) is True
    assert dated.killed_here_in_window(2481, NODE, 48, HEAD) is True
    assert dated.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    assert dated.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is True
    # the SAME row with no stamp (a row written without a block timestamp) is forgiven
    # 1 h 28 min early by the clock's floor
    undated = _led(tmp_path / "undated")
    assert undated.record_kill(_kill(INCIDENT_BLOCK, "0xdrift")) is True
    assert undated.killed_here_in_window(2481, NODE, 48, HEAD) is False
    assert undated.killed_ours_in_window(2481, CAROL, 48, HEAD) is False
    assert undated.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is False
    # 48.1 real hours IS outside the window, whatever the block says
    old = _led(tmp_path / "old")
    assert old.record_kill(_kill(HEAD - 10, "0xold", block_ts=int(T - 48.1 * H))) is True
    assert old.killed_here_in_window(2481, NODE, 48, HEAD) is False and old.killed_ours_in_window(2481, CAROL, 48, HEAD) is False
    assert old.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is False
    # a dated row is judged with NO head at all (the head is not consulted)
    assert dated.killed_here_in_window(2481, NODE, 48, None) is True
    assert old.killed_here_in_window(2481, NODE, 48, None) is False


# --------------------------------------------- (4) the raise rule of the stamps


def test_4_a_later_row_raises_every_ts_entry_and_an_earlier_one_never_lowers_it(tmp_path):
    led = _led(tmp_path)
    early, late = int(T - 30 * H), int(T - 2 * H)
    # both blocks sit far BELOW the window's floor: the stamps are what bind
    assert led.record_kill(_kill(HEAD - 200_000, "0xa", block_ts=early)) is True
    assert led.record_kill(_kill(HEAD - 190_000, "0xb", block_ts=late, log_index=1)) is True
    row = led.hostile_kamis[40106]
    assert led.killed_accounts_ts == {2481: {CAROL: late}} and row["lastTs"] == late
    assert row["nodesTs"] == {NODE: late} and row["victimAccountsTs"] == {CAROL: late}
    # an EARLIER row recorded afterwards lowers nothing
    assert led.record_kill(_kill(HEAD - 210_000, "0xc", block_ts=early, log_index=2)) is True
    assert led.killed_accounts_ts == {2481: {CAROL: late}} and led.hostile_kamis[40106]["lastTs"] == late
    assert row["nodesTs"] == {NODE: late} and row["victimAccountsTs"] == {CAROL: late}
    # and the reads judge by the stamp, not by the block (every block is outside)
    assert led.block_clock.floor(48, HEAD) > HEAD - 190_000
    assert led.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    assert led.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is True
    assert led.killed_ours_in_window(2481, CAROL, 1, HEAD) is False  # 2 h old against a 1-h window
    assert led.kami_hostile_in_window(40106, NODE, CAROL, 1, HEAD) is False


# ----------------------------------------------------------------- (5) prune


def test_5_prune_drops_a_dated_row_by_the_clock_and_keeps_an_undated_one_as_today(tmp_path):
    led = _led(tmp_path)
    # a row the block floor would KEEP (it is 10 blocks below the head) whose
    # stamp is 100 h old: dropped
    assert led.record_kill(_kill(HEAD - 10, "0xstale", attacker=1743, attacker_kami=11111, block_ts=int(T - 100 * H))) is True
    # a row the block floor would DROP (200,000 blocks back) stamped an hour
    # ago: kept
    assert led.record_kill(_kill(HEAD - 200_000, "0xfresh", block_ts=int(T - 1 * H), log_index=1)) is True
    assert led.prune(72, HEAD) == 1 + 1 + 1  # the kill row, its killedAccounts pair and its kami row (dropped whole by lastTs)
    assert [r["txHash"] for r in led.kills[NODE][2481]] == ["0xfresh"] and 1743 not in led.kills[NODE]
    assert led.killed_accounts == {2481: {CAROL: HEAD - 200_000}} and led.killed_accounts_ts == {2481: {CAROL: int(T - 1 * H)}}
    assert 11111 not in led.hostile_kamis and led.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    # with NO head the dated rows are still judged and the undated ones
    # are all kept
    assert led.record_kill(_kill(HEAD - 5, "0xstale2", attacker=1743, attacker_kami=11111, block_ts=int(T - 100 * H), log_index=2)) is True
    assert led.prune(72, None) >= 1 and 1743 not in led.kills.get(NODE, {})
    # the undated ledger prunes by block alone (test_owners.py's
    # scenario, block for block)
    plain = OwnerLedger(tmp_path / "plain")
    assert plain.record_kill(_kill(100, "0x01")) is True
    assert plain.record_kill(_kill(50_000, "0x02", log_index=1)) is True
    assert plain.prune(24 + 24, 50_100) == 0
    assert plain.prune(1, 50_100) == 1 and [r["txHash"] for r in plain.kills[NODE][2481]] == ["0x02"]
    assert plain.killed_accounts == {2481: {CAROL: 50_000}} and plain.killed_accounts_ts == {}
    assert plain.prune(1, None) == 0  # no head, no stamps: nothing is dropped


# ------------------------------------------------- (6) the persisted document


def test_6_the_new_keys_round_trip_and_a_pre_2t_b_document_still_loads(tmp_path):
    assert OWNERS_SCHEMA_VERSION == "0.2.0"  # the keys are ADDITIVE: a reader without them must keep LOADING the file
    led = _led(tmp_path)
    ts = int(T - 3 * H)
    assert led.record_kill(_kill(HEAD - 1_000, "0xk", block_ts=ts)) is True
    assert led.record_cast({"block": HEAD - 900, "txHash": "0xc", "logIndex": 0, "node": NODE, "targetKami": 263, "targetAccount": CAROL, "casterAccount": 997, "item": 21202, "blockTs": ts + 10}) is True
    assert led.save() is True
    doc = json.loads((tmp_path / "owners.json").read_text())
    assert doc["schemaVersion"] == "0.2.0"
    o = doc["owners"]
    assert o["kills"][str(NODE)]["2481"][0]["blockTs"] == ts and o["casts"][str(NODE)]["997"][0]["blockTs"] == ts + 10
    assert o["killedAccountsTs"] == {"2481": {str(CAROL): ts}, "997": {str(CAROL): ts + 10}}
    assert o["hostileKamis"]["40106"]["lastTs"] == ts and o["hostileKamis"]["40106"]["nodesTs"] == {str(NODE): ts} and o["hostileKamis"]["40106"]["victimAccountsTs"] == {str(CAROL): ts}
    again = _led(tmp_path)
    assert again.load() == "restored"
    assert again.killed_accounts_ts == {2481: {CAROL: ts}, 997: {CAROL: ts + 10}}
    assert again.hostile_kamis[40106]["lastTs"] == ts and again.hostile_kamis[40106]["nodesTs"] == {NODE: ts} and again.hostile_kamis[40106]["victimAccountsTs"] == {CAROL: ts}
    assert again.killed_here_in_window(2481, NODE, 48, HEAD) is True and again.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is True
    # a document with no ts keys anywhere, the same schemaVersion, loads and
    # is judged by its blocks alone
    old_doc = {"schemaVersion": "0.2.0", "owners": {"lastScannedBlock": 50_100, "killedAccounts": {"2481": {str(CAROL): 50_000}}, "hostileKamis": {"40106": {"lastBlock": 50_000, "node": NODE, "victimAccount": CAROL, "ownerAtKill": 2481, "nodes": {str(NODE): 50_000}, "victimAccounts": {str(CAROL): 50_000}}}}}
    (tmp_path / "old" / "owners.json").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "old" / "owners.json").write_text(json.dumps(old_doc), encoding="utf-8")
    old = OwnerLedger(tmp_path / "old")  # no clock either: block arithmetic only
    assert old.load() == "restored" and old.killed_accounts == {2481: {CAROL: 50_000}} and old.killed_accounts_ts == {}
    row = old.hostile_kamis[40106]
    assert row["lastTs"] is None and row["nodesTs"] == {} and row["victimAccountsTs"] == {}
    assert old.killed_ours_in_window(2481, CAROL, 48, 50_100) is True  # the block floor
    assert old.killed_ours_in_window(2481, CAROL, 48, 50_000 + 48 * BLOCKS_PER_HOUR + 10) is False


# ---------------------------------------------------------- (7) the battles seed


def test_7_the_battles_seed_dates_a_row_that_carries_a_timestamp(make_keeper):
    e = make_keeper(cap=6)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    _seed(e.lens)
    e.grant()
    e.books = []  # type: ignore[attr-defined]
    head = e.lens.block
    deaths = [r for r in BATTLES["kills"] if r["isDeath"]]
    dated, plain = deaths[0], deaths[1]
    ts = int(T - 2 * H)
    e.lens.battles[263] = _battles_for(263, [dict(dated, blockNumber=head - 100, timestamp=ts)])
    _start(e, [263])
    o = e.keeper.owners
    owner, killer = dated["account"]["index"], dated["killer"]["index"]
    assert o.killed_accounts_ts == {owner: {CAROL: ts}} and o.hostile_kamis[killer]["lastTs"] == ts
    assert o.hostile_kamis[killer]["victimAccountsTs"] == {CAROL: ts} and o.killed_ours_in_window(owner, CAROL, 48, head) is True
    # a row with no `timestamp` at all seeds no stamp: the block dates it
    no_ts = {k: v for k, v in dict(plain, blockNumber=head - 50).items() if k != "timestamp"}
    e.lens.battles[508] = _battles_for(508, [no_ts])
    _start(e, [508])
    other = plain["account"]["index"]
    assert o.killed_accounts[other] == {CAROL: head - 50} and other not in o.killed_accounts_ts
    assert o.killed_ours_in_window(other, CAROL, 48, head) is True  # the block floor


# ------------------------------------ (8) the ledger row and the tick's note


def test_8_a_scan_pass_dates_the_kill_row_and_ts_unread_is_carried_not_compared(make_keeper, tmp_path):
    chain = FakeChain(head=HEAD)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, 5946, CAROL)
    _add_occupant(e.lens, 7834, CAROL)
    e.keeper.owners.set_last_scanned(chain.head - 1)
    book = _book(e, [263, 68017], **LAZY)
    _tick(e)  # the census this pass takes is what the next pass resolves against
    ticks_before = len(_rows(e, book, "tick"))
    # a kill nobody can be attributed to (no transaction for the log): no owner
    # class moves, so the tick note is the only thing that changes
    chain.head += 1
    chain.logs = [liquidate_log(chain.head, "0x01", kami_entity_id(5946))]
    chain.block_ts[chain.head] = 1790000000
    _tick(e)
    row = _rows(e, book, "kill")[-1]["kill"]
    assert row["victimKami"] == 5946 and row["blockTs"] == 1790000000  # dated by the chain
    note = e.keeper.context.scan["note"]
    assert note["tsUnread"] == 0 and note["kills"] == 1 and note["unattributed"] == 1
    assert len(_rows(e, book, "tick")) == ticks_before + 1  # kills 0 -> 1 wrote the row
    rows_after = len(_rows(e, book, "tick"))
    # the same pass again with the header unread: `tsUnread` moves, every
    # COMPARED key of the note is what it was, and no tick row is written
    chain.head += 1
    chain.fail_block_ts = True
    chain.logs = [liquidate_log(chain.head, "0x02", kami_entity_id(7834))]
    _tick(e)
    note2 = e.keeper.context.scan["note"]
    assert note2["tsUnread"] == 1 and note2["kills"] == 1 and note2["unattributed"] == 1
    assert _rows(e, book, "kill")[-1]["kill"]["blockTs"] is None  # undated: the block floor dates it
    assert len(_rows(e, book, "tick")) == rows_after


# ------------------- (9) a stamp always describes its block's row


def test_9_an_undated_later_kill_clears_the_stamp_and_an_older_dated_one_never_writes_it(tmp_path):
    """The ts entry ALWAYS describes the row that holds the block entry. A
    stale stamp beside a raised block would forgive early."""
    stale, recent = int(T - 100 * H), int(T - 2 * H)
    # (a) a DATED row, then a LATER UNDATED one (the header read failed or the
    # budget was spent): the stamp is cleared and the pair falls back to the
    # block floor, which keeps the newer block
    led = _led(tmp_path)
    assert led.record_kill(_kill(HEAD - 2_000, "0xdated", block_ts=stale)) is True
    assert led.killed_accounts_ts == {2481: {CAROL: stale}}
    assert led.killed_ours_in_window(2481, CAROL, 48, HEAD) is False  # 100 h old by its own date
    assert led.record_kill(_kill(HEAD - 1_000, "0xundated", log_index=1)) is True
    assert led.killed_accounts == {2481: {CAROL: HEAD - 1_000}} and led.killed_accounts_ts == {}
    row = led.hostile_kamis[40106]
    assert row["lastBlock"] == HEAD - 1_000 and row["lastTs"] is None and row["nodesTs"] == {} and row["victimAccountsTs"] == {}
    assert led.block_clock.floor(48, HEAD) < HEAD - 1_000  # the floor keeps it
    assert led.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    assert led.kami_hostile_in_window(40106, NODE, CAROL, 48, HEAD) is True
    # prune drops the stale ROW (it carries its own 100-h stamp) and KEEPS the
    # pair, which is judged by its block now that the stamp is cleared
    assert led.prune(72, HEAD) == 1 and [r["txHash"] for r in led.kills[NODE][2481]] == ["0xundated"]
    assert led.killed_accounts == {2481: {CAROL: HEAD - 1_000}} and led.killed_ours_in_window(2481, CAROL, 48, HEAD) is True
    # (b) an UNDATED latest, then an OLDER DATED row afterwards (an overlap
    # re-read, or a seed): no stamp is written and the block stays the newer one
    mirror = _led(tmp_path / "mirror")
    assert mirror.record_kill(_kill(HEAD - 1_000, "0xlatest")) is True
    assert mirror.record_kill(_kill(HEAD - 5_000, "0xolder", block_ts=recent, log_index=1)) is True
    assert mirror.killed_accounts == {2481: {CAROL: HEAD - 1_000}} and mirror.killed_accounts_ts == {}
    mrow = mirror.hostile_kamis[40106]
    assert mrow["lastBlock"] == HEAD - 1_000 and mrow["lastTs"] is None
    assert mrow["nodes"] == {NODE: HEAD - 1_000} and mrow["nodesTs"] == {} and mrow["victimAccountsTs"] == {}
    # (c) a LATER dated row after an undated one does set the stamp
    assert mirror.record_kill(_kill(HEAD - 500, "0xnewer", block_ts=recent, log_index=2)) is True
    assert mirror.killed_accounts_ts == {2481: {CAROL: recent}}
    assert mirror.hostile_kamis[40106]["lastTs"] == recent and mirror.hostile_kamis[40106]["nodesTs"] == {NODE: recent}
    assert mirror.hostile_kamis[40106]["victimAccountsTs"] == {CAROL: recent}
    assert mirror.killed_ours_in_window(2481, CAROL, 48, HEAD) is True and mirror.killed_ours_in_window(2481, CAROL, 1, HEAD) is False
    # and the document never carries an empty owner map after a clear
    assert led.save() is True and json.loads((tmp_path / "owners.json").read_text())["owners"]["killedAccountsTs"] == {}
