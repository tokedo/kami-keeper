"""An UNATTRIBUTED kill is re-attributed on a later pass.

The gap: the kill scan reads a liquidate log and `chain.get_transaction`
answers FALSY — an RPC replica can serve the log before it serves the
transaction — so the row goes to `owners.unattributed[node]` with
`attackerAccount: null`. Without a retry nothing reads it again: the next
pass's overlap re-read asks `OwnerLedger.known` first, which claims an
unattributed row exactly like an attributed one, so the row is skipped
before any transaction read, the owner never enters
`kills[node][owner]`, and no book binds against it for its
`hostileWindowHours`.

The rule: a kill row the scan could not attribute is re-read from the
chain on a later pass, and when the transaction resolves the owner is
learned with the kill's OWN date — exactly as if the first pass had
attributed it. A row that stays unreadable is left as it is.

No config key, no schema change: the two numbers are module constants of
`keeper/kills.py` and `tests/test_defaults_lock.py` is untouched.
"""

from __future__ import annotations

from typing import Any

from conftest import Env
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.kills import REATTRIBUTE_HEAD_BLOCKS, REATTRIBUTE_MAX_CALLS_PER_PASS, KillScanner, OccupantRef
from keeper.owners import OwnerLedger
from test_phase2a import NODE, T, _add_occupant, _apply_plans_to_world, _last_tick, _tick_all
from test_phase2q import ATTACKER, ATTACKER_ACCOUNT, FOREIGN, FOREIGN_ACCOUNT, OURS, _env, _kills, _tx_reads

TX = "0xaa"


def _dead_write_only(chain: FakeChain, block: int, victim: int, tx: str, log_index: int = 0) -> None:
    """The shape of the miss: the DEAD write is served, the transaction is
    NOT (yet)."""
    chain.logs.append(liquidate_log(block, tx, kami_entity_id(victim), log_index))


def _serve_tx(chain: FakeChain, tx: str, victim: int, killer: int, form: str = "typed") -> None:
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(victim), kami_entity_id(killer), form)


def _unattributed(make_keeper, tmp_path, sub: str, block: int = 1000, ts: int | None = None, logs: int = 1) -> tuple[Env, FakeChain]:
    """A keeper whose last pass recorded `logs` unattributed kill row(s) of
    the node at `block`: the DEAD write is served, no transaction is."""
    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain, sub=sub)
    _add_occupant(e.lens, FOREIGN, FOREIGN_ACCOUNT, hp=(30, 200))
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    for _ in range(3):  # the census reaches the scanner's table (it is the PREVIOUS pass's)
        _tick_all(e)
        _apply_plans_to_world(e)
    if ts is not None:
        chain.block_ts[block] = int(ts)
    for i in range(logs):
        _dead_write_only(chain, block, FOREIGN, f"{TX}{i:02x}", i)
    _tick_all(e)
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["kills"] == logs and note["unattributed"] == logs and note["reattributed"] == 0
    assert len(e.keeper.owners.unattributed[NODE]) == logs
    assert e.keeper.owners.kills.get(NODE, {}).get(ATTACKER_ACCOUNT) is None
    return e, chain


def _attacker_rows(e: Env) -> list[dict[str, Any]]:
    return [k for k in _kills(e) if k.get("attackerAccount") is not None]


def _lines(e: Env) -> list[str]:
    return [l["line"] for l in e.keeper.logs]


# ------------------------------------------------------- 1. the bind is recovered


def test_the_bind_is_recovered_on_the_next_pass_with_a_row_a_flag_and_a_line(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb1")
    owners = e.keeper.owners
    head = chain.head
    # pass 1: the owner's class did not move — that is the whole gap
    assert not owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, head)
    first = _kills(e)
    assert len(first) == 1 and first[0]["attackerAccount"] is None and first[0]["attribution"] == "no transaction for the log"
    assert "reattributed" not in first[0]
    # pass 2: the replica serves the transaction
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER)
    chain.head += 3
    _tick_all(e)
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["reattributed"] == 1 and note["kills"] == 0 and note["unattributed"] == 0
    # the ledger: a SECOND row, the attacker named, the earlier row untouched
    kills = _kills(e)
    assert len(kills) == 2 and kills[0] == first[0]
    k = kills[1]
    assert (k["victimKami"], k["victimAccount"], k["attackerKami"], k["attackerAccount"]) == (FOREIGN, FOREIGN_ACCOUNT, ATTACKER, ATTACKER_ACCOUNT)
    assert k["attribution"] == "occupant" and k["block"] == 1000 and k["reattributed"] is True
    assert k["ownerClassAfter"] == "killed-here-48h"
    # the owner ledger: one row under the attacker, nothing unattributed
    assert len(owners.kills[NODE][ATTACKER_ACCOUNT]) == 1 and NODE not in owners.unattributed
    assert owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, chain.head)
    assert owners.kami_hostile_in_window(ATTACKER, NODE, FOREIGN_ACCOUNT, 48, chain.head)
    # the daemon line: the suffix, and the summary line
    assert any("; re-attributed 3 block(s) after the log" in l for l in _lines(e))
    assert any("1 re-attributed" in l for l in _lines(e))


def test_the_tick_change_key_writes_a_row_for_a_re_attribution():
    from keeper.core import KILLSCAN_KEY_COMPARED

    assert "reattributed" in KILLSCAN_KEY_COMPARED


# ------------------------------------- 2. the kill's own date, not the retry's


def test_the_promoted_row_carries_the_kills_own_date_so_the_window_starts_at_the_kill(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb2", ts=int(T))
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER)
    chain.head += 3
    _tick_all(e)
    owners = e.keeper.owners
    row = owners.kills[NODE][ATTACKER_ACCOUNT][0]
    # the stored stamp, not the moment of the retry (which is T + ~150 s)
    assert row["blockTs"] == int(T) and row["block"] == 1000
    assert owners.killed_accounts_ts[ATTACKER_ACCOUNT][FOREIGN_ACCOUNT] == int(T)
    assert owners.hostile_kamis[ATTACKER]["lastTs"] == int(T)
    assert owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, chain.head)
    # 48 h after the KILL the owner is out of the window again; had the row
    # been dated by the re-attribution it would still be inside it
    e.clock["now"] = T + 48 * 3600 + 60
    assert not owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, chain.head)
    assert not owners.kami_hostile_in_window(ATTACKER, NODE, FOREIGN_ACCOUNT, 48, chain.head)


# ----------------------------------------------------------- 3. outside the window


def test_a_row_further_behind_the_head_than_the_window_is_never_retried(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb3")
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER)
    chain.head = 1000 + REATTRIBUTE_HEAD_BLOCKS + 1
    reads = _tx_reads(chain)
    _tick_all(e)
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["reattributed"] == 0
    # the overlap re-read still skips the row through `known`, before any
    # transaction read — and the retry does not spend one either
    assert _tx_reads(chain) == reads
    assert len(e.keeper.owners.unattributed[NODE]) == 1
    assert e.keeper.owners.kills.get(NODE, {}).get(ATTACKER_ACCOUNT) is None
    assert len(_kills(e)) == 1
    # one block closer and the same row resolves
    chain.head = 1000 + REATTRIBUTE_HEAD_BLOCKS
    _tick_all(e)
    assert _last_tick(e, e.books[0])["killScan"]["reattributed"] == 1
    assert _tx_reads(chain) == reads + 1


# ------------------------------------------------------------------ 4. the budget


def test_the_retry_spends_at_most_the_budget_of_transaction_reads_a_pass(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb4", logs=6)
    assert 6 > REATTRIBUTE_MAX_CALLS_PER_PASS
    for i in range(6):
        _serve_tx(chain, f"{TX}{i:02x}", FOREIGN, ATTACKER)
    owners = e.keeper.owners
    reads = _tx_reads(chain)
    chain.head += 3
    _tick_all(e)
    assert _tx_reads(chain) - reads == REATTRIBUTE_MAX_CALLS_PER_PASS
    assert _last_tick(e, e.books[0])["killScan"]["reattributed"] == REATTRIBUTE_MAX_CALLS_PER_PASS
    assert len(owners.kills[NODE][ATTACKER_ACCOUNT]) == REATTRIBUTE_MAX_CALLS_PER_PASS
    assert len(owners.unattributed[NODE]) == 6 - REATTRIBUTE_MAX_CALLS_PER_PASS
    # the rest on the next pass
    reads = _tx_reads(chain)
    chain.head += 3
    _tick_all(e)
    assert _tx_reads(chain) - reads == 6 - REATTRIBUTE_MAX_CALLS_PER_PASS
    assert _last_tick(e, e.books[0])["killScan"]["reattributed"] == 6 - REATTRIBUTE_MAX_CALLS_PER_PASS
    assert len(owners.kills[NODE][ATTACKER_ACCOUNT]) == 6 and NODE not in owners.unattributed
    assert len(_attacker_rows(e)) == 6
    # and the pass after that spends nothing
    reads = _tx_reads(chain)
    chain.head += 3
    _tick_all(e)
    assert _tx_reads(chain) == reads and _last_tick(e, e.books[0])["killScan"]["reattributed"] == 0


# --------------------------------------------------- 5. an unreadable retry


def test_an_unreadable_retry_changes_nothing_and_is_offered_again_next_pass(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb5")  # the transaction is never served
    owners = e.keeper.owners
    for i in range(3):
        reads = _tx_reads(chain)
        chain.head += 3
        _tick_all(e)
        note = _last_tick(e, e.books[0])["killScan"]
        assert note["reattributed"] == 0 and note["kills"] == 0, (i, note)
        assert note["overlapSkipped"] == 1 and note["deadWrites"] == 0  # `known` still claims it
        assert _tx_reads(chain) - reads == 1  # exactly one retry a pass, never a second row
        assert len(owners.unattributed[NODE]) == 1
        assert owners.kills.get(NODE, {}).get(ATTACKER_ACCOUNT) is None
        assert not owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, chain.head)
        assert len(_kills(e)) == 1 and _attacker_rows(e) == []
    assert not any("re-attributed" in l for l in _lines(e))
    # and when it finally reads, the same row resolves
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER)
    chain.head += 3
    _tick_all(e)
    assert _last_tick(e, e.books[0])["killScan"]["reattributed"] == 1 and len(_attacker_rows(e)) == 1


# -------------------------------------------------------- 6. no double promotion


def test_a_resolved_row_is_promoted_once(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb6")
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER)
    for _ in range(3):  # three passes that could all resolve it
        chain.head += 3
        _tick_all(e)
    owners = e.keeper.owners
    assert len(owners.kills[NODE][ATTACKER_ACCOUNT]) == 1 and owners.unattributed == {}
    assert len(_attacker_rows(e)) == 1
    assert sum(1 for l in _lines(e) if "block(s) after the log" in l) == 1
    assert _last_tick(e, e.books[0])["killScan"]["reattributed"] == 0


def test_promote_refuses_a_log_the_attackers_bucket_already_holds_and_still_drops_the_row(tmp_path):
    led = OwnerLedger(tmp_path / "qb6b")
    row = {"node": NODE, "block": 1000, "txHash": TX, "logIndex": 0, "victimKami": FOREIGN, "victimAccount": FOREIGN_ACCOUNT, "attackerKami": None, "attackerAccount": None, "attribution": "no transaction for the log", "blockTs": int(T)}
    assert led.record_kill(dict(row))
    assert led.record_kill(dict(row, attackerKami=ATTACKER, attackerAccount=ATTACKER_ACCOUNT, attribution="occupant"))
    assert len(led.unattributed[NODE]) == 1 and len(led.kills[NODE][ATTACKER_ACCOUNT]) == 1
    # the same log under the same attacker: nothing is appended, no block is
    # raised a second time, and the unattributed row is gone — it IS attributed
    assert led.promote_unattributed(NODE, TX, 0, ATTACKER, ATTACKER_ACCOUNT, "occupant") is None
    assert len(led.kills[NODE][ATTACKER_ACCOUNT]) == 1 and led.unattributed == {}
    # and a node that holds no such row answers None without touching anything
    assert led.promote_unattributed(NODE, TX, 0, ATTACKER, ATTACKER_ACCOUNT, "occupant") is None
    assert led.promote_unattributed(99, TX, 0, ATTACKER, ATTACKER_ACCOUNT, "occupant") is None
    assert 99 not in led.kills


def test_unattributed_rows_is_a_pure_read_oldest_block_first(tmp_path):
    led = OwnerLedger(tmp_path / "qb6c")
    base = {"victimKami": FOREIGN, "victimAccount": FOREIGN_ACCOUNT, "attackerKami": None, "attackerAccount": None, "attribution": "no transaction for the log", "blockTs": None}
    for node, block, idx, tx in ((NODE, 1002, 0, "0x3"), (NODE, 1000, 1, "0x2"), (83, 1000, 0, "0x1")):
        assert led.record_kill(dict(base, node=node, block=block, logIndex=idx, txHash=tx))
    led._dirty = False
    assert [(n, r["txHash"]) for n, r in led.unattributed_rows()] == [(83, "0x1"), (NODE, "0x2"), (NODE, "0x3")]
    assert led._dirty is False


# ---------------------------------- 7. the selector / unresolved-account outcomes


def _occupants() -> dict[int, OccupantRef]:
    return {kami_entity_id(FOREIGN): OccupantRef(NODE, FOREIGN, FOREIGN_ACCOUNT), kami_entity_id(ATTACKER): OccupantRef(NODE, ATTACKER, ATTACKER_ACCOUNT)}


def test_reattribute_answers_none_for_every_outcome_that_names_no_account():
    ch = FakeChain(head=1200)
    vh = harvest_entity_id(FOREIGN)
    ch.txs["0x01"] = liquidate_tx("0x01", vh, kami_entity_id(ATTACKER), "other")   # not a liquidate selector
    ch.txs["0x02"] = liquidate_tx("0x02", vh, kami_entity_id(998), "typed")        # killer unresolvable
    ch.txs["0x03"] = liquidate_tx("0x03", vh, kami_entity_id(999), "typed")        # index resolves, account does not
    ch.txs["0x04"] = liquidate_tx("0x04", vh, kami_entity_id(ATTACKER), "typed")   # the one that resolves
    ch.kami_index[kami_entity_id(999)] = 999
    sc = KillScanner(ch, 40, lookup_account=lambda idx: None)
    occ = _occupants()

    def row(tx: str | None) -> dict[str, Any]:
        return {"block": 1100, "txHash": tx, "logIndex": 0, "victimKami": FOREIGN, "victimAccount": FOREIGN_ACCOUNT, "attackerKami": None, "attackerAccount": None, "attribution": "no transaction for the log", "blockTs": None}

    assert sc.reattribute(row(None), occ) is None          # a row with no hash is never read
    assert sc.reattribute(row(""), occ) is None
    assert ch.calls == []
    assert sc.reattribute(row("0x01"), occ) is None         # selector not a liquidate
    assert sc.reattribute(row("0x02"), occ) is None         # index unresolved
    assert sc.reattribute(row("0x03"), occ) is None         # account unresolved
    assert sc.reattribute(row("0xff"), occ) is None         # no transaction for the log
    assert sc.reattribute(row("0x04"), occ) == (ATTACKER, ATTACKER_ACCOUNT, "occupant")
    # an occupant whose own account index is unknown names no account either
    blind = {kami_entity_id(ATTACKER): OccupantRef(NODE, ATTACKER, None)}
    assert sc.reattribute(row("0x04"), blind) is None
    # the chain fallback, with an account this time
    assert KillScanner(ch, 40, lookup_account=lambda idx: 4444).reattribute(row("0x03"), occ) == (999, 4444, "chain fallback")
    # RpcUnavailable is masked as None, and every attempt is counted
    from keeper.kills import ScanResult

    res = ScanResult(None, None, None, None)
    assert sc.reattribute(row("0x04"), occ, res) == (ATTACKER, ATTACKER_ACCOUNT, "occupant")
    ch.fail_reads = True
    assert sc.reattribute(row("0x04"), occ, res) is None
    assert res.tx_lookups == 2
    ch.fail_reads = False
    # the row itself is never mutated, and no block header is ever read
    r = row("0x04")
    sc.reattribute(r, occ)
    assert r == row("0x04") and ch.block_ts_calls == 0


def test_a_selector_that_is_not_a_liquidate_leaves_the_row_unattributed_through_the_core(make_keeper, tmp_path):
    e, chain = _unattributed(make_keeper, tmp_path, "qb7")
    _serve_tx(chain, f"{TX}00", FOREIGN, ATTACKER, form="other")
    reads = _tx_reads(chain)
    chain.head += 3
    _tick_all(e)
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["reattributed"] == 0 and _tx_reads(chain) - reads == 1
    assert len(e.keeper.owners.unattributed[NODE]) == 1
    assert e.keeper.owners.kills.get(NODE, {}).get(ATTACKER_ACCOUNT) is None
    assert len(_kills(e)) == 1


# --------------------------------------------- 8. the regression: a fresh kill


def test_an_attributed_fresh_kill_is_byte_identical_to_before_2qb(make_keeper, tmp_path):
    """The extraction of `_kill_rows` changes nothing for a kill the scan
    attributes on its own pass: the same ledger row keys, no `reattributed`
    key, and the same daemon line."""
    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain, sub="qb8")
    _add_occupant(e.lens, FOREIGN, FOREIGN_ACCOUNT, hp=(30, 200))
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    for _ in range(3):
        _tick_all(e)
        _apply_plans_to_world(e)
    _dead_write_only(chain, 1000, FOREIGN, TX)
    _serve_tx(chain, TX, FOREIGN, ATTACKER)  # the log AND the transaction, as usual
    _tick_all(e)
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["kills"] == 1 and note["unattributed"] == 0 and note["reattributed"] == 0
    kills = _kills(e)
    assert len(kills) == 1
    k = kills[0]
    assert sorted(k) == sorted(["event", "victimKami", "victimAccount", "attackerKami", "attackerAccount", "block", "txHash", "ownerClassAfter", "learned", "attribution", "blockTs"])
    assert "reattributed" not in k
    assert (k["attackerKami"], k["attackerAccount"], k["attribution"]) == (ATTACKER, ATTACKER_ACCOUNT, "occupant")
    assert k["ownerClassAfter"] == "killed-here-48h"
    line = f"kill on node {NODE}: kami {FOREIGN} (account {FOREIGN_ACCOUNT}) by kami {ATTACKER} of account {ATTACKER_ACCOUNT} at block 1000; owner class now killed-here-48h"
    assert line in _lines(e)
    assert not any("re-attributed" in l for l in _lines(e))
    # and the summary line is the plain one, with no suffix
    assert any(l.startswith("kill scan: ") and l.endswith("1 kill(s), 0 cast(s), 0 unattributed") for l in _lines(e))


def test_the_resolver_extraction_keeps_the_scanners_own_attribution_verbatim():
    """`_attacker_from_tx` is `_attribute`'s old body: the same five
    attribution strings, the same triple."""
    ch = FakeChain(head=120)
    vh = harvest_entity_id(FOREIGN)
    ch.logs = [liquidate_log(101, "0x01", kami_entity_id(FOREIGN), 0), liquidate_log(102, "0x02", kami_entity_id(FOREIGN), 0), liquidate_log(103, "0x03", kami_entity_id(FOREIGN), 0), liquidate_log(104, "0x04", kami_entity_id(FOREIGN), 0), liquidate_log(105, "0x05", kami_entity_id(FOREIGN), 0)]
    ch.txs["0x01"] = liquidate_tx("0x01", vh, kami_entity_id(ATTACKER), "bytes")
    ch.txs["0x02"] = liquidate_tx("0x02", vh, kami_entity_id(ATTACKER), "other")
    ch.txs["0x03"] = liquidate_tx("0x03", vh, kami_entity_id(999), "typed")
    ch.txs["0x04"] = liquidate_tx("0x04", vh, kami_entity_id(998), "typed")
    ch.kami_index[kami_entity_id(999)] = 999
    res = KillScanner(ch, 40, lookup_account=lambda idx: 4444 if idx == 999 else None).scan(100, _occupants(), 24)
    by = {k["txHash"]: k for k in res.kills}
    assert (by["0x01"]["attackerAccount"], by["0x01"]["attribution"]) == (ATTACKER_ACCOUNT, "occupant")
    assert (by["0x02"]["attackerKami"], by["0x02"]["attackerAccount"], by["0x02"]["attribution"]) == (None, None, "selector not a liquidate")
    assert (by["0x03"]["attackerKami"], by["0x03"]["attackerAccount"], by["0x03"]["attribution"]) == (999, 4444, "chain fallback")
    assert (by["0x04"]["attackerKami"], by["0x04"]["attackerAccount"], by["0x04"]["attribution"]) == (None, None, "killer not an occupant; index unresolved")
    assert (by["0x05"]["attackerAccount"], by["0x05"]["attribution"]) == (None, "no transaction for the log")
    assert res.tx_lookups == 5
    # the fifth string, on the branch the scan keeps for itself
    acct_unresolved = KillScanner(ch, 40, lookup_account=lambda idx: None).scan(102, _occupants(), 24)
    assert next(k for k in acct_unresolved.kills if k["txHash"] == "0x03")["attribution"] == "killer resolved by index; account unresolved"


def test_the_two_numbers_are_module_constants_and_no_config_key_moved():
    """The two numbers are module constants of
    keeper/kills.py, in no book schema and in no defaults lock."""
    from keeper.books import schema_defaults
    from keeper.owners import OWNERS_SCHEMA_VERSION
    from keeper.protocol import STRATEGY_TYPES

    assert (REATTRIBUTE_HEAD_BLOCKS, REATTRIBUTE_MAX_CALLS_PER_PASS) == (400, 4)
    for st in STRATEGY_TYPES:
        keys = set(schema_defaults(st))
        assert not any("eattribut" in k for k in keys), (st, sorted(keys))
    assert OWNERS_SCHEMA_VERSION == "0.2.0"  # the owner ledger's schema does not move


# ------------------- 9. a re-attributed kill of the book's own kami is never MEASURED


def test_a_re_attributed_kill_of_ours_recovers_the_bind_and_is_never_measured(make_keeper, tmp_path):
    """`learn_kill` prices the killer's line off the victim's view from the
    PREVIOUS pass. By the time a retry
    resolves, that view is the kami read DEAD — and if it had been revived
    AND restarted it would be a post-revive HP that was never the HP at the
    kill, which would LIFT the owner's lines off a wrong number.
    `resumeHpPct` 100 makes the second case unreachable; the guard keeps it
    unreachable under any setting. So the retry recovers the BIND and the row says
    `skipped: re-attributed` instead of blaming a missing view."""
    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain, sub="qb9")
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    for _ in range(3):
        _tick_all(e)
        _apply_plans_to_world(e)
    strategy = e.keeper.strategies[e.books[0].id]
    calls: list[Any] = []
    real = strategy.learn_kill
    strategy.learn_kill = lambda kill, now: (calls.append(kill), real(kill, now))[1]  # type: ignore[method-assign]
    # the kill of one of the book's own kamis, log served, transaction not
    _dead_write_only(chain, 1000, OURS, TX)
    _tick_all(e)
    first = _kills(e)
    assert len(first) == 1 and first[0]["victimKami"] == OURS and first[0]["attackerAccount"] is None
    assert first[0]["learned"] == {"skipped": "unattributed"} and calls, "the fresh path DOES call learn_kill"
    # the replica serves the transaction; the retry resolves
    _serve_tx(chain, TX, OURS, ATTACKER)
    chain.head += 5
    calls.clear()
    _tick_all(e)
    k = _attacker_rows(e)
    assert len(k) == 1 and k[0]["attackerAccount"] == ATTACKER_ACCOUNT and k[0]["reattributed"] is True
    assert k[0]["ownerClassAfter"] == "killed-here-48h"
    # the BIND is recovered — that is what the retry is for
    owners = e.keeper.owners
    assert owners.killed_here_in_window(ATTACKER_ACCOUNT, NODE, 48, chain.head)
    # and the measurement is NOT taken: learn_kill was never called again
    assert calls == [], "a re-attributed kill must not be measured"
    assert k[0]["learned"] == {"skipped": "re-attributed"}
    assert any("; measured: skipped (re-attributed); re-attributed 5 block(s) after the log" in l for l in _lines(e))
