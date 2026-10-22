"""Two ways a kill scan can miss kills: the occupant snapshot is newer
than the blocks it resolves, and a head block's logs are served late.

  1. the census the scan resolves DEAD writes against is read at a LENS
     block INSIDE the range the next pass scans, so a kami killed between
     the previous scan head and that census is already gone from the node
     when the census is taken — `ignored`, never attributed;
  2. `eth_getLogs` at the head block can answer before the node's log
     index has caught up with its own `eth_blockNumber` (no DEAD write
     served for a block that holds one), and the block would be marked
     scanned for ever.

What closes both: an occupant MEMORY of the last `occupantMemoryBlocks` plus
every running node-watching book's OWN kamis, and an overlap re-read of
`killScanOverlapBlocks` behind the cursor whose already-recorded rows are
skipped by `OwnerLedger.known` before any transaction read.

The scanner-level tests of both live in `tests/test_kills.py`; these run
them through the core on the recorded fixtures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from conftest import Env
from fakechain import FakeChain, liquidate_log, liquidate_tx
from keeper.books import BookError, validate_config
from keeper.chain import harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from test_phase2a import CAROL, FRIENDS, NODE, T, _add_occupant, _apply_plans_to_world, _last_tick, _rows, _seed, _set_party, _start, _tick_all

FOREIGN = 5946       # a foreign occupant of the node, under FOREIGN_ACCOUNT here
ATTACKER = 42823     # the killer, an occupant of the node (another account)
OURS = 263            # a book kami, HARVESTING in the fixtures (171 / 240)
OURS_RESTING = 508   # the book's second kami, parked RESTING below resumeHpPct
FOREIGN_ACCOUNT = 4444
ATTACKER_ACCOUNT = 3878


def _env(make_keeper, tmp_path, chain: FakeChain, sub: str = "kk2q", prepare: Any = None, **over: Any) -> Env:
    """A lab keeper with a chain and ONE two-kami `auto` book on the node
    (the kill scan runs once per tick PASS, so every tick here is
    `tick_due`). `friends` excludes both foreign accounts: a listed owner
    is never previewed, and these tests are about the scan, not the class."""
    e = make_keeper(cap=4)
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / sub), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    clock = {"now": T}
    k.clock = lambda: clock["now"]
    k.boot()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(cfg.dataDir)
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    if prepare is not None:
        prepare(e)  # the world as it must read BEFORE the book adopts a plan
    assert k.handle("grant", {"account": "lab"})["ok"]
    friends = [f for f in FRIENDS if f not in (ATTACKER_ACCOUNT, FOREIGN_ACCOUNT)]
    e.books = [_start(e, [OURS, OURS_RESTING], friends=friends, **over)]  # type: ignore[attr-defined]
    return e


def _liquidate(chain: FakeChain, block: int, victim: int, killer: int, tx: str) -> None:
    chain.logs.append(liquidate_log(block, tx, kami_entity_id(victim), 0))
    chain.txs[tx] = liquidate_tx(tx, harvest_entity_id(victim), kami_entity_id(killer), "typed")


def _kills(e: Env) -> list[dict[str, Any]]:
    return [r["kill"] for r in _rows(e, e.books[0], "kill")]


def _tx_reads(chain: FakeChain) -> int:
    return sum(1 for c in chain.calls if c[0] == "eth_getTransactionByHash")


# -------------------------------------- 4. the census newer than the scan


def _memory_scenario(make_keeper, tmp_path, sub: str, **over: Any) -> tuple[Env, FakeChain]:
    """The foreign occupant W is listed by a census,
    then killed in a block ABOVE the scan head — so the NEXT census (taken
    before that block is ever scanned) no longer lists it, and only the
    memory can resolve its DEAD write."""
    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain, sub=sub, **over)
    _add_occupant(e.lens, FOREIGN, FOREIGN_ACCOUNT, hp=(30, 200))
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    for _ in range(3):  # the census reaches the scanner's table (it is the PREVIOUS pass's)
        _tick_all(e)
        _apply_plans_to_world(e)
    assert _last_tick(e, e.books[0])["killScan"]["kills"] == 0
    # W is liquidated at block 1005 — above the head, so no pass has scanned
    # it — and is gone from the node by the time the next census is read
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != FOREIGN]
    _liquidate(chain, 1005, FOREIGN, ATTACKER, "0xaa")
    _tick_all(e)  # the census of this pass no longer lists W; the block is still unscanned
    _apply_plans_to_world(e)
    chain.head = 1020
    _tick_all(e)  # now the block is scanned, and W is not on the node any more
    return e, chain


def test_a_foreign_occupant_killed_between_the_scan_head_and_the_census_is_attributed_from_memory(make_keeper, tmp_path):
    e, _chain = _memory_scenario(make_keeper, tmp_path, "mem120")
    kills = _kills(e)
    assert len(kills) == 1
    k = kills[0]
    assert (k["victimKami"], k["victimAccount"], k["attackerKami"], k["attackerAccount"]) == (FOREIGN, FOREIGN_ACCOUNT, ATTACKER, ATTACKER_ACCOUNT)
    assert k["attribution"] == "occupant" and k["block"] == 1005
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["kills"] == 1 and note["ignored"] == 0 and note["remembered"] >= 1


def test_with_occupant_memory_zero_the_same_kill_is_ignored_as_before_2q(make_keeper, tmp_path):
    e, _chain = _memory_scenario(make_keeper, tmp_path, "mem0", occupantMemoryBlocks=0)
    note = _last_tick(e, e.books[0])["killScan"]
    assert _kills(e) == [] and note["kills"] == 0 and note["ignored"] == 1 and note["remembered"] == 0


# ------------------------------------------- 5. the book's own kami, never listed


def test_an_own_kami_killed_before_any_census_listed_it_is_still_a_kill_row(make_keeper, tmp_path):
    """The book's own kamis are resolvable from the BOOKS, so a restart —
    which empties the occupant memory — does not lose a kill of ours."""
    def park(env: Env) -> None:
        # V is RESTING below resumeHpPct before the book adopts: it is never
        # deployed, so no census of the node ever lists it
        _set_party(env.lens, OURS_RESTING, state="RESTING", node=None, hp=(50, 220))
        env.lens.nodes[NODE]["harvests"] = [h for h in env.lens.nodes[NODE]["harvests"] if h["kami"]["index"] != OURS_RESTING]

    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain, prepare=park)
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    _tick_all(e)
    _apply_plans_to_world(e)
    assert e.books[0].plan[str(OURS_RESTING)]["state"] == "RESTING"
    # V dies without ever having been on the node: no census can name it
    _set_party(e.lens, OURS_RESTING, state="DEAD", node=None)
    _liquidate(chain, 1003, OURS_RESTING, ATTACKER, "0xbb")
    # a restart between the passes: the occupant memory starts empty
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "requestTimeoutSec": 5}, env={})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k2.clock = lambda: e.clock["now"]
    k2.boot()
    e.keeper.stop_engine()
    e.keeper = k2
    e.books = [k2.store.get(e.books[0].id)]  # type: ignore[attr-defined]
    assert k2.handle("grant", {"account": "lab"})["ok"]
    assert k2._occupant_memory == {}
    _tick_all(e)  # the death is observed; block 1003 is still above the head
    assert [r["death"]["outcome"] for r in _rows(e, e.books[0], "death")][0] == "observed"
    assert _kills(e) == []
    chain.head = 1010
    _tick_all(e)
    kills = _kills(e)
    assert len(kills) == 1 and kills[0]["victimKami"] == OURS_RESTING
    assert kills[0]["victimAccount"] == CAROL and kills[0]["attackerKami"] == ATTACKER and kills[0]["attribution"] == "occupant"
    note = _last_tick(e, e.books[0])["killScan"]
    assert note["kills"] == 1 and note["own"] >= 1 and note["ignored"] == 0
    # the death / revive path is untouched by the kill row
    assert [r["death"]["outcome"] for r in _rows(e, e.books[0], "death")][0] == "observed"


# ------------------------------------------------ 6. the overlap re-reads


def test_the_overlap_writes_no_second_row_and_makes_no_second_tx_read(make_keeper, tmp_path):
    chain = FakeChain(head=1000)
    e = _env(make_keeper, tmp_path, chain)
    _add_occupant(e.lens, FOREIGN, FOREIGN_ACCOUNT, hp=(30, 200))
    _add_occupant(e.lens, ATTACKER, ATTACKER_ACCOUNT, hp=(250, 300), level=45)
    e.keeper.owners.set_last_scanned(995)
    for _ in range(3):
        _tick_all(e)
        _apply_plans_to_world(e)
    _liquidate(chain, 1000, FOREIGN, ATTACKER, "0xcc")
    chain.head = 1000
    _tick_all(e)
    assert len(_kills(e)) == 1 and _last_tick(e, e.books[0])["killScan"]["kills"] == 1
    reads = _tx_reads(chain)
    # three passes inside the 12-block re-read: the row is seen again every
    # pass and skipped before the transaction read
    for i in range(3):
        chain.head += 3
        _tick_all(e)
        note = _last_tick(e, e.books[0])["killScan"]
        assert note["overlapSkipped"] == 1, (i, note)
        assert note["kills"] == 0 and note["deadWrites"] == 0 and note["ignored"] == 0
        assert len(_kills(e)) == 1
        assert _tx_reads(chain) == reads


# ------------------------------------------------------- 7. the two keys


def test_defaults_and_ranges():
    auto = validate_config("auto", {"kamiIndices": [263], "nodeIndex": NODE, "accountIndex": CAROL}, "lab")
    assert (auto["killScanOverlapBlocks"], auto["occupantMemoryBlocks"]) == (12, 120)
    ladder = validate_config("harvestAndFeed", {"kamiIndex": 263, "nodeIndex": NODE, "accountIndex": CAROL, "foodItem": 11304, "foodLadder": [11304]}, "lab")
    assert (ladder["killScanOverlapBlocks"], ladder["occupantMemoryBlocks"]) == (12, 120)
    for key, bad in (("killScanOverlapBlocks", 201), ("occupantMemoryBlocks", -1)):
        with pytest.raises(BookError) as err:
            validate_config("auto", {"kamiIndices": [263], "nodeIndex": NODE, "accountIndex": CAROL, key: bad}, "lab")
        assert f"config invalid at {key}" in str(err.value)
    # a configured 0 is a real 0, never the default
    zero = validate_config("auto", {"kamiIndices": [263], "nodeIndex": NODE, "accountIndex": CAROL, "killScanOverlapBlocks": 0, "occupantMemoryBlocks": 0}, "lab")
    assert (zero["killScanOverlapBlocks"], zero["occupantMemoryBlocks"]) == (0, 0)
