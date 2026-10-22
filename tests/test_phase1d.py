"""Persistence and collect decoding:

  1. per-book runtime persisted to runtime.json and restored on resume:
     a restart mid-window keeps the collect due time; the `resumed` row
     states what was restored; missing / unreadable falls back and says so;
  2. `musuCollected` decoded from the collect receipt's harvest-entity
     drain (the recorded Yominet receipt; a synthetic batched shape);
     `null` + `decodeError` when it cannot be read;
  3. collects of one account due in the same tick pass go out as ONE
     batched tx (chunked at the collect cap), one `tx` row per book with the shared hash and
     `batchSize`; feeds are never batched;
  4. the keeper-level status staleness gate follows the running books;
  5. deploy.sh runs its status check from /srv/keeper/app.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, HAF_CONFIG
from evm_stubs import BatchingTesterProvider, deploy_world
from keeper.chain import Chain, harvest_entity_id, kami_entity_id
from keeper.config import load as load_config
from keeper.core import Keeper, _iso
from keeper.ledger import validate_row
from keeper.lens import LensClient
from keeper.protocol import Basis
from keeper.receipts import COMPONENT_VALUE_SET_TOPIC0, VALUE_COMPONENT_ID, component_writes, decode_harvest_drains, decode_musu_collected, drained
from keeper.runtime import PERSISTED_KEYS, load_runtime, restore_into, runtime_path, save_runtime, snapshot

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = Path(__file__).with_name("fixtures") / "receipts"
# `start` stamps nextTickAt with the wall clock, so a synthetic `now` for a
# tick PASS (tick_due) must lie beyond it; a direct tick_book takes any now
T = 2_000_000_000.0

# tests/fixtures/receipts/README.md: the recorded liquidation at 32677500
VICTIM_HARVEST = 689272690007724837243479850868931864664682577897694345337885744847683293322
KILLER_HARVEST = 38986004861930616840179368661838477756132336890865011080109669343625179232305
KILLER_KAMI_ENTITY = 23795525015705015365938580453147150190015236266987467513986926056567962803026
KILLER_KAMI = 42823


def _start(e, **over):
    s = e.start_haf(**over)
    assert s["ok"], s
    return e.keeper.store.get(s["data"]["book"]["id"])


def _rows(k, book, kind=None):
    rows = k.handle("ledger", {"book": book.id})["data"]["rows"]
    return [r for r in rows if kind is None or r["kind"] == kind]


def _restart(e, cap: int = 4) -> Keeper:
    """A second keeper process on the SAME data dir (SPEC P8 restart)."""
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": cap, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    return k


# ------------------------------------------------------- 2: the receipt decoder


def _log(entity: int, value: int, component: int = VALUE_COMPONENT_ID, extra_words: int = 0) -> dict:
    """One ComponentValueSet log in the recorded receipts' hex shape."""
    payload = (32).to_bytes(32, "big") + (32 + 32 * extra_words).to_bytes(32, "big") + value.to_bytes(32, "big") + b"\x00" * (32 * extra_words)
    return {"topics": ["0x" + COMPONENT_VALUE_SET_TOPIC0.hex(), "0x" + component.to_bytes(32, "big").hex(), "0x" + (0x23f8).to_bytes(32, "big").hex(), "0x" + entity.to_bytes(32, "big").hex()], "data": "0x" + payload.hex()}


def test_the_topic_and_component_id_are_the_harness_constants_and_the_entity_ids_match_the_recorded_receipt():
    # kami-harness 3.7.0 server.py L9056 _STORE_SET_RECORD_EVENT; L9337 _VALUE_COMPONENT_ID
    assert COMPONENT_VALUE_SET_TOPIC0.hex() == "6ac31c38682e0128240cf68316d7ae751020d8f74c614e2a30278afcec8a6073"
    assert VALUE_COMPONENT_ID == int.from_bytes(Web3.keccak(text="component.value"), "big")
    # the fixture index names the killer's kami and harvest entities: the keeper's derivations agree with the chain
    assert kami_entity_id(KILLER_KAMI) == KILLER_KAMI_ENTITY
    assert harvest_entity_id(KILLER_KAMI) == KILLER_HARVEST


def test_the_recorded_yominet_receipt_yields_the_oracle_amount_for_the_drained_harvest_entity():
    doc = json.loads((RECEIPTS / "yominet_32677500.json").read_text())
    assert doc["block"] == 32677500 and doc["status"] == 1 and len(doc["logs"]) == 21
    writes = component_writes(doc["logs"])
    assert writes[VICTIM_HARVEST] == [1798, 0]  # sync, then drain — the collect / stop shape
    assert writes[KILLER_HARVEST] == [0, 1191]  # spoils added, never drained
    out = decode_harvest_drains(doc["logs"], [VICTIM_HARVEST, KILLER_HARVEST, 12345])
    assert out[VICTIM_HARVEST] == (1798, None)  # == the amount the recorded receipt's source states for this collect
    amount, err = out[KILLER_HARVEST]
    assert amount is None and "did not end in a drain" in err and "1191" in err
    assert out[12345] == (None, "no component.value write to the harvest entity in this receipt")
    # the same drain through the kami path: the killer's harvest entity is not a collect
    by_kami = decode_musu_collected(doc["logs"], [KILLER_KAMI])
    assert by_kami[KILLER_KAMI]["musuCollected"] is None and "drain" in by_kami[KILLER_KAMI]["decodeError"]


def test_a_batched_collect_shape_decodes_per_kami_and_never_guesses():
    logs = [
        _log(harvest_entity_id(1001), 500), _log(harvest_entity_id(1001), 0),  # drained 500
        _log(harvest_entity_id(1002), 0), _log(harvest_entity_id(1002), 700),  # written up, not drained
        _log(harvest_entity_id(1003), 0),  # only a zero write
        _log(harvest_entity_id(1004), 9, extra_words=1),  # a longer payload: skipped, never misread
        _log(harvest_entity_id(1004), 0),
        _log(harvest_entity_id(1005), 42, component=12345), _log(harvest_entity_id(1005), 0, component=12345),  # another component
        _log(harvest_entity_id(1006), 0), _log(harvest_entity_id(1006), 300), _log(harvest_entity_id(1006), 0),  # [0, N, 0]: drained N
        {"topics": ["0x" + COMPONENT_VALUE_SET_TOPIC0.hex()], "data": "0x"},  # too few topics
    ]
    out = decode_musu_collected(logs, [1001, 1002, 1003, 1004, 1005, 1006, 1007])
    assert out[1001] == {"musuCollected": 500, "decodeError": None}
    assert out[1002]["musuCollected"] is None and "did not end in a drain (last write 700)" in out[1002]["decodeError"]
    assert out[1003]["musuCollected"] is None and "only zero" in out[1003]["decodeError"]
    assert out[1004]["musuCollected"] is None and "only zero" in out[1004]["decodeError"]
    assert out[1005]["musuCollected"] is None and "no component.value write" in out[1005]["decodeError"]
    assert out[1006] == {"musuCollected": 300, "decodeError": None}
    assert out[1007]["musuCollected"] is None
    assert decode_musu_collected([], [1001])[1001] == {"musuCollected": None, "decodeError": "the receipt carried no logs"}
    assert decode_musu_collected(None, [1001])[1001] == {"musuCollected": None, "decodeError": "no receipt logs"}
    assert drained([]) == (None, "no component.value write to the harvest entity in this receipt")
    # bytes topics (a web3 receipt) decode the same as hex strings
    raw = [{"topics": [bytes.fromhex(t[2:]) for t in l["topics"]], "data": bytes.fromhex(l["data"][2:])} for l in logs[:2]]
    assert decode_musu_collected(raw, [1001])[1001]["musuCollected"] == 500


def test_a_tx_row_with_musu_collected_batch_size_and_decode_error_is_schema_valid(tmp_path):
    from keeper.ledger import Ledger

    led = Ledger(tmp_path / "b", "b", "lab")
    b = Basis(lensBlock=1, lensReconciledThrough=1)
    led.tx(b, {"txHash": "0x" + "ab" * 32, "nonce": 1, "block": 2, "status": "success", "gasUsed": 3, "effectiveGasPrice": 4, "kami": 1001, "system": "system.harvest.collect", "musuCollected": 500, "batchSize": 3})
    led.tx(b, {"txHash": "0x" + "ab" * 32, "nonce": 1, "block": 2, "status": "success", "gasUsed": 3, "effectiveGasPrice": 4, "kami": 1002, "system": "system.harvest.collect", "musuCollected": None, "decodeError": "no component.value write to the harvest entity in this receipt", "batchSize": 3})
    rows = list(led.rows())
    assert rows[0]["tx"]["musuCollected"] == 500 and rows[0]["tx"]["batchSize"] == 3 and rows[1]["tx"]["musuCollected"] is None and rows[1]["tx"]["decodeError"]
    for r in rows:
        validate_row(r)


# ------------------------------------------------------- 1: runtime persistence


def test_restart_mid_window_keeps_the_collect_due_time(env):
    env.grant()
    book = _start(env, collectEvery=1000)
    env.keeper.tick_book(book, now=0.0)  # HARVESTING on the node: the collect clock starts at t=0
    p = runtime_path(env.data_dir / "books" / book.id)
    doc = json.loads(p.read_text())
    assert doc["bookId"] == book.id and doc["runtime"]["lastCollectAt"] == 0.0 and doc["schemaVersion"] and not p.with_name("runtime.json.tmp").exists()
    # the process restarts 600 s into the 1000 s window
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    resumed = [r for r in _rows(k2, b2, "lifecycle") if r["event"] == "resumed"][-1]
    assert resumed["reason"] == "keeper-restart" and resumed["runtime"]["restore"] == "restored" and resumed["runtime"]["lastCollectAt"] == "1970-01-01T00:00:00.000Z"
    assert k2.runtime[book.id]["lastCollectAt"] == 0.0
    s = k2.tick_book(b2, now=600.0)
    assert "collect" not in s["decisions"] and k2.runtime[book.id]["lastCollectAt"] == 0.0  # the restored clock, not the restart time
    s = k2.tick_book(b2, now=1000.0)
    assert "collect" in s["decisions"] and s["collectPooled"] == 1
    tx = [r for r in _rows(k2, b2, "tx")][-1]["tx"]
    assert tx["system"] == "system.harvest.collect" and tx["kami"] == 1001 and tx["batchSize"] == 1 and tx["status"] == "dry_run" and tx["musuCollected"] is None and "decodeError" not in tx
    dec = [r for r in _rows(k2, b2, "decision")][-1]["decision"]
    assert dec["kind"] == "collect" and dec["parameters"]["sinceLastCollectSec"] == 1000
    k2.stop_engine()


def test_a_missing_or_unreadable_runtime_file_falls_back_to_the_next_tick_and_says_so(env):
    env.grant()
    book = _start(env, collectEvery=1000)
    env.keeper.tick_book(book, now=0.0)
    p = runtime_path(env.data_dir / "books" / book.id)
    p.unlink()
    k2 = _restart(env)
    resumed = [r for r in _rows(k2, k2.store.get(book.id), "lifecycle") if r["event"] == "resumed"][-1]
    assert resumed["runtime"] == {"restore": "missing", "lastCollectAt": None, "lastFeedAt": None, "lastActAt": None}
    s = k2.tick_book(k2.store.get(book.id), now=600.0)
    assert "collect" not in s["decisions"] and k2.runtime[book.id]["lastCollectAt"] == 600.0  # without a runtime file: the clock restarts
    assert "collect" not in k2.tick_book(k2.store.get(book.id), now=1000.0)["decisions"]
    assert json.loads(p.read_text())["runtime"]["lastCollectAt"] == 600.0  # and is persisted again
    k2.stop_engine()
    p.write_text("{not json")
    k3 = _restart(env)
    resumed = [r for r in _rows(k3, k3.store.get(book.id), "lifecycle") if r["event"] == "resumed"][-1]
    assert resumed["runtime"]["restore"] == "unreadable: JSONDecodeError"
    assert any("runtime.json unreadable" in l["line"] for l in k3.logs)
    k3.stop_engine()


def test_the_feed_guard_survives_a_restart_so_a_restart_never_double_feeds(env):
    env.grant()
    book = _start(env)
    env.lens.set_kami(1001, hp=(40, 200))
    assert env.keeper.tick_book(book, now=1000.0)["decisions"] == ["feed"]
    doc = json.loads(runtime_path(env.data_dir / "books" / book.id).read_text())
    assert doc["runtime"]["feedGuard"]["1001"]["lastFeedHp"] == 40 and doc["runtime"]["lastFeedAt"] == 1000.0 and doc["runtime"]["lastActAt"] == 1000.0
    k2 = _restart(env)
    b2 = k2.store.get(book.id)
    s = k2.tick_book(b2, now=1030.0)  # the lens still reads the pre-feed HP
    assert s["acted"] is False and s["feedWithheld"]["lastFeedHp"] == 40 and k2.signer.recorded == []
    assert k2.tick_book(b2, now=1300.0)["decisions"] == ["feed"]  # the cooldown, as before
    st = k2.handle("status", {"book": book.id})["data"]["books"][0]
    assert st["lastActAt"] == "1970-01-01T00:21:40.000Z"  # restored, so status names the last act after a restart
    k2.stop_engine()


def test_runtime_round_trip_covers_every_persisted_key_and_writes_only_on_change(tmp_path):
    rt = {"lastCollectAt": 1.5, "nextCollectAt": 120001.5, "collectIntervalSec": 120000, "collectDraws": 1, "lastFeedAt": 2.0, "feedGuard": {"7": {"lastFeedAt": 2.0, "lastFeedHp": 40, "expectedHpAfter": 65, "item": 11301, "heal": 25}}, "crafted": {"29": 3}, "parked": {"23": {"retryAt": 99.0, "reason": "insufficient inputs", "parkedAt": 9.0}}, "staminaMin": {"29": 73}, "backoff": {"263": {"until": 9.0, "kind": "start", "reason": "x", "since": 1.0}}, "drainRate": {"263": 12.5}, "startQueue": {"44": 3.5}, "ladder": {"item": 11304, "heal": 100, "steppedAt": 7.0}, "chainRests": {"outageSince": 5.0, "kamis": [263]}, "contradictionTicks": 2, "contradiction": "kami 7: planned HARVESTING on node 62, observed RESTING on node None", "matchingSinceBlock": 5, "lastActAt": 2.0, "lastActBlock": 12566155, "lastTxHash": "0x" + "cd" * 32, "nextTickAt": 3.0, "ticksSinceAct": 4, "withheld": {"x": 1}, "lastSummary": {"acted": False}, "degraded": ["lens-stale:5"]}
    snap = snapshot(rt)
    assert set(snap) == set(PERSISTED_KEYS)
    assert save_runtime(tmp_path, "b1", rt) is True
    assert save_runtime(tmp_path, "b1", rt) is False  # unchanged: no write
    rt["crafted"]["29"] = 4
    assert save_runtime(tmp_path, "b1", rt) is True
    loaded, status = load_runtime(tmp_path, "b1")
    assert status == "restored" and loaded == snapshot(rt) and loaded["crafted"] == {"29": 4} and loaded["parked"]["23"]["retryAt"] == 99.0
    fresh = {"ticksSinceAct": 0}
    restore_into(fresh, loaded)
    assert fresh["lastCollectAt"] == 1.5 and save_runtime(tmp_path, "b1", fresh) is False  # restored state is the persisted state
    assert load_runtime(tmp_path / "none", "b2")[1] == "missing"
    (tmp_path / "runtime.json").write_text(json.dumps({"bookId": "other", "runtime": {}}))
    assert load_runtime(tmp_path, "b1")[1] == "unreadable: bookId mismatch (other)"
    (tmp_path / "runtime.json").write_text("[]")
    assert load_runtime(tmp_path, "b1")[1] == "unreadable: not a runtime document"


# ------------------------------------------------------- 3: batched collects


def _three_books(e, cap_ok=True):
    e.grant()
    books = []
    for k in (1001, 1002, 1003):
        e.lens.set_kami(k, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        books.append(_start(e, kamiIndex=k, collectEvery=1000))
    return books


def test_collects_due_in_the_same_tick_pass_go_out_as_one_batched_tx_and_feeds_do_not(env):
    books = _three_books(env)
    assert env.keeper.tick_due(now=T) == [b.id for b in books]  # clocks start
    env.lens.set_kami(1002, hp=(40, 200))  # one kami under the floor: a feed, never batched
    env.keeper.tick_due(now=T + 1000)
    recs = env.keeper.signer.recorded
    collects = [r for r in recs if r.system == "system.harvest.collect"]
    feeds = [r for r in recs if r.system == "system.kami.use.item"]
    assert len(collects) == 1 and len(feeds) == 1
    c = collects[0]
    assert c.params == {"kamiIndices": [1001, 1002, 1003], "count": 3} and c.fn == "executeBatched" and c.count == 3 and c.kami is None and c.batchSize == 3
    assert feeds[0].kami == 1002 and feeds[0].fn == "executeTyped"
    assert recs.index(feeds[0]) < recs.index(c)  # the feed went in the book's own burst; the collect at the end of the pass
    for b in books:
        tx = [r["tx"] for r in _rows(env.keeper, b, "tx") if r["tx"]["system"] == "system.harvest.collect"]
        assert len(tx) == 1 and tx[0]["kami"] == b.config["kamiIndex"] and tx[0]["batchSize"] == 3 and tx[0]["bookId"] == b.id and tx[0]["count"] == 3
        tick = [r for r in _rows(env.keeper, b, "tick")][-1]["summary"]
        assert tick["collectPooled"] == 1 and "collect" in tick["decisions"] and tick["acted"] is True
        assert env.keeper.runtime[b.id]["ticksSinceAct"] == 0 and env.keeper.runtime[b.id]["lastActAt"] == T + 1000
    st = env.keeper.handle("books", {"account": "lab"})["data"]["books"]
    assert all(b["lastActAt"] == _iso(T + 1000) for b in st)
    # a second pass inside the window pools nothing
    env.keeper.tick_due(now=T + 1030)
    assert len([r for r in env.keeper.signer.recorded if r.system == "system.harvest.collect"]) == 1


def test_a_direct_tick_book_sends_its_collect_alone_and_a_stopped_book_leaves_the_pool(env):
    b1, b2, b3 = _three_books(env)
    env.keeper.tick_due(now=T)
    s = env.keeper.tick_book(b1, now=T + 1000)  # no pass open: the collect goes out at once, alone
    assert s["collectPooled"] == 1
    c = env.keeper.signer.recorded[-1]
    assert c.system == "system.harvest.collect" and c.kami == 1001 and c.params == {"count": 1} and c.fn == "executeTyped" and c.batchSize == 1
    assert [r["tx"]["batchSize"] for r in _rows(env.keeper, b1, "tx")] == [1]
    # a pass in which a pooled book is stopped before the flush: its collect is not sent
    env.keeper._pooling = True
    env.keeper.tick_book(b2, now=T + 1000)
    env.keeper.tick_book(b3, now=T + 1000)
    assert env.keeper.handle("stop", {"book": b3.id})["ok"]
    env.keeper._pooling = False
    env.keeper._flush_collects()
    c = env.keeper.signer.recorded[-1]
    assert c.system == "system.harvest.collect" and c.kami == 1002 and c.batchSize == 1
    assert _rows(env.keeper, b3, "tx") == [] and any("collect not sent: the book is stopped" in l["line"] for l in env.keeper.logs if l["book"] == b3.id)


def test_more_than_the_cap_is_chunked_at_twelve(make_keeper):
    e = make_keeper(cap=20)
    e.grant()
    kamis = list(range(2001, 2019))  # 18 books
    for k in kamis:
        e.lens.set_kami(k, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        _start(e, kamiIndex=k, collectEvery=1000)
    e.keeper.tick_due(now=T)
    e.keeper.tick_due(now=T + 1000)
    collects = [r for r in e.keeper.signer.recorded if r.system == "system.harvest.collect"]
    assert [c.count for c in collects] == [12, 6] and collects[0].params["kamiIndices"] == kamis[:12] and collects[1].params["kamiIndices"] == kamis[12:]
    assert [c.batchSize for c in collects] == [12, 6]
    assert collects[0].gasLimit == 1_600_000 + 12 * 1_700_000


def test_a_batched_collect_on_the_tester_chain_shares_one_hash_and_reports_the_decode_honestly(make_keeper, tmp_path):
    """LiveSigner end to end: two books, one collect tx; both rows carry
    the same hash and `batchSize: 2`; the tester's sink has no code, so
    the receipt has no logs and each row says `musuCollected: null` with
    the reason — never a plausible number."""
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "armed"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "armed": True, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    k.signer.gas_price = {"maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 0}
    k.signer.receipt_timeout_sec, k.signer.receipt_poll_sec = 5, 0.01
    assert k.handle("grant", {"account": "lab"})["ok"]
    books = []
    for kami in (1001, 1002):
        e.lens.set_kami(kami, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(HAF_CONFIG, kamiIndex=kami, collectEvery=1000)})
        assert s["ok"], s
        books.append(k.store.get(s["data"]["book"]["id"]))
    k.tick_due(now=T)
    k.tick_due(now=T + 1000)
    staged = [[r["tx"]["stage"] for r in _rows(k, b, "tx")] for b in books]
    assert staged == [["broadcast", "receipt"], ["broadcast", "receipt"]]  # one broadcast row and one receipt row per book, the shared hash on each
    rows = [[r["tx"] for r in _rows(k, b, "tx") if r["tx"]["stage"] == "receipt"] for b in books]
    assert [len(r) for r in rows] == [1, 1]
    a, b = rows[0][0], rows[1][0]
    assert a["txHash"] == b["txHash"] and a["status"] == b["status"] == "success" and a["batchSize"] == b["batchSize"] == 2 and a["nonce"] == b["nonce"] == 0
    assert a["kami"] == 1001 and b["kami"] == 1002 and a["fn"] == "executeBatched"
    assert a["musuCollected"] is None and a["decodeError"] == "the receipt carried no logs" and b["decodeError"] == a["decodeError"]
    assert w3.eth.get_transaction_receipt(a["txHash"]).status == 1 and len(k.signer.sent) == 1 and k.signer.sent[0].logs == []
    assert k.runtime[books[0].id]["lastTxHash"] == a["txHash"] == k.runtime[books[1].id]["lastTxHash"]
    q = k.handle("stop", {"all": True, "account": "lab"})["data"]["quiescence"]
    assert q["quiescent"] is True and q["lastTxHash"] == a["txHash"]
    k.stop_engine()


def test_a_collect_decoded_from_receipt_logs_lands_on_the_row(env):
    """The decode path through the core: a signer whose record carries the
    recorded receipt's logs for the book's own harvest entity."""
    from keeper.signer import DryRunSigner

    class WithLogs(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                if r.system == "system.harvest.collect":
                    r.status = "success"
                    r.txHash = "0x" + "ee" * 32
                    r.logs = [_log(harvest_entity_id(1001), 1834), _log(harvest_entity_id(1001), 0), _log(harvest_entity_id(1002), 0), _log(harvest_entity_id(1002), 9)]
            return recs

    b1, b2, b3 = _three_books(env)
    signer = WithLogs()
    signer._grants = env.keeper.signer._grants
    env.keeper.signer = signer
    env.keeper.tick_due(now=T)
    env.keeper.tick_due(now=T + 1000)
    t1 = _rows(env.keeper, b1, "tx")[-1]["tx"]
    t2 = _rows(env.keeper, b2, "tx")[-1]["tx"]
    t3 = _rows(env.keeper, b3, "tx")[-1]["tx"]
    assert t1["musuCollected"] == 1834 and "decodeError" not in t1 and t1["txHash"] == "0x" + "ee" * 32 and t1["batchSize"] == 3
    assert t2["musuCollected"] is None and "did not end in a drain (last write 9)" in t2["decodeError"]
    assert t3["musuCollected"] is None and t3["decodeError"] == "no component.value write to the harvest entity in this receipt"
    assert any("musu 1834" in l["line"] for l in env.keeper.logs if l["book"] == b1.id)


# ------------------------------------------------------- 4: status staleness


def test_status_staleness_gate_follows_the_running_books_or_ninety(env):
    env.lens.mode = "stale"
    env.lens.lag = 50  # staleness reads lag + 3 (headBlockNumber = block + 3)
    st = env.keeper.handle("status")["data"]
    assert st["degraded"] == [] and st["lens"]["stalenessBlocks"] == 53 and st["lens"]["maxStalenessBlocks"] == 90  # no books: 90, not the old 20
    env.lens.mode = "live"
    env.grant()
    tight = _start(env, maxStalenessBlocks=30)
    env.lens.mode = "stale"
    st = env.keeper.handle("status")["data"]
    assert st["degraded"] == ["lens-stale:53"] and st["lens"]["maxStalenessBlocks"] == 30
    env.lens.mode = "live"
    env.lens.set_kami(1002, state="HARVESTING", node=62, hp=(120, 200), account=4242)
    loose = _start(env, kamiIndex=1002, maxStalenessBlocks=120)
    env.lens.mode = "stale"
    st = env.keeper.handle("status")["data"]
    assert st["degraded"] == [] and st["lens"]["maxStalenessBlocks"] == 120  # the loosest running bound
    assert env.keeper.handle("stop", {"book": loose.id})["ok"]
    assert env.keeper.handle("status")["data"]["lens"]["maxStalenessBlocks"] == 30
    assert env.keeper.handle("pause", {"book": tight.id})["ok"]
    assert env.keeper.handle("status")["data"]["lens"]["maxStalenessBlocks"] == 90  # paused books do not count


# ------------------------------------------------------- 5: deploy.sh


def test_deploy_script_runs_the_status_check_from_the_app_dir():
    text = (ROOT / "deploy" / "deploy.sh").read_text()
    check = [l for l in text.splitlines() if "-m keeper status" in l]
    assert len(check) == 1
    assert "PYTHONPATH=/srv/keeper/app" in check[0]
    before = text[: text.index(check[0])]
    assert before.rstrip().splitlines()[-1].strip() == "cd /srv/keeper/app"


# ------------------------------------------------------- clocks and the batch fallback


class _RefusingSigner:
    """A DryRunSigner subclass factory: collects whose kami set meets
    `refuse(kamis)` come back not_sent with a pre-check revert."""

    @staticmethod
    def make(refuse):
        from keeper.signer import DryRunSigner

        class S(DryRunSigner):
            def _send(self, grant, intents):
                recs = super()._send(grant, intents)
                for i, r in zip(intents, recs):
                    if r.system == "system.harvest.collect" and refuse(i.kamis):
                        r.status, r.precheck, r.reason = "not_sent", "reverted", "Harvest: kami starving [code 3]"
                return recs

        return S()


def test_a_batch_that_fails_its_precheck_is_re_offered_as_singles_and_the_clock_moves_only_for_landed(env):
    b1, b2, b3 = _three_books(env)
    signer = _RefusingSigner.make(lambda ks: 1002 in ks)  # the batch (holds 1002) and that kami's single are refused
    signer._grants = env.keeper.signer._grants
    env.keeper.signer = signer
    env.keeper.tick_due(now=T)
    env.keeper.tick_due(now=T + 1000)
    recs = [r for r in signer.recorded if r.system == "system.harvest.collect"]
    assert [(r.count, r.status, r.batchSize) for r in recs] == [(3, "not_sent", 3), (1, "dry_run", 1), (1, "not_sent", 1), (1, "dry_run", 1)]
    assert [r.kami for r in recs[1:]] == [1001, 1002, 1003] and recs[0].params["kamiIndices"] == [1001, 1002, 1003]
    rows = {b.id: [r["tx"] for r in _rows(env.keeper, b, "tx") if r["tx"]["system"] == "system.harvest.collect"] for b in (b1, b2, b3)}
    for b in (b1, b2, b3):
        batch, single = rows[b.id]
        assert batch["status"] == "not_sent" and batch["batchSize"] == 3 and batch["precheck"] == "reverted" and "starving" in batch["reason"] and "batchFallback" not in batch
        assert single["batchSize"] == 1 and single["batchFallback"] is True and single["kami"] == b.config["kamiIndex"]
    assert rows[b1.id][1]["status"] == "dry_run" and rows[b3.id][1]["status"] == "dry_run" and rows[b2.id][1]["status"] == "not_sent"
    rt = env.keeper.runtime
    assert rt[b1.id]["lastCollectAt"] == T + 1000 and rt[b3.id]["lastCollectAt"] == T + 1000  # landed: the clock moved
    assert rt[b2.id]["lastCollectAt"] == T  # refused: the clock did not move
    assert "did not land" in rt[b2.id]["lastError"] and rt[b1.id]["lastError"] is None
    assert json.loads(runtime_path(env.data_dir / "books" / b2.id).read_text())["runtime"]["lastCollectAt"] == T
    # next tick: only 1002 is due again, offered alone (no fallback rows for a batch of one)
    env.keeper.tick_due(now=T + 1030)
    later = [r for r in signer.recorded if r.system == "system.harvest.collect"][4:]
    assert [(r.kami, r.count, r.status) for r in later] == [(1002, 1, "not_sent")]
    assert [r["tx"].get("batchFallback") for r in _rows(env.keeper, b2, "tx")][-1] is None
    assert any("re-offering each member as a single collect once" in l["line"] for l in env.keeper.logs)


def test_a_single_collect_that_does_not_land_leaves_the_clock_and_is_retried(env):
    env.grant()
    book = _start(env, collectEvery=1000)
    signer = _RefusingSigner.make(lambda ks: True)
    signer._grants = env.keeper.signer._grants
    env.keeper.signer = signer
    env.keeper.tick_book(book, now=0.0)
    s = env.keeper.tick_book(book, now=1000.0)
    assert s["collectPooled"] == 1 and env.keeper.runtime[book.id]["lastCollectAt"] == 0.0
    assert [r["tx"]["status"] for r in _rows(env.keeper, book, "tx")] == ["not_sent"]
    s = env.keeper.tick_book(book, now=1030.0)  # still due: retried next tick
    assert "collect" in s["decisions"] and len(_rows(env.keeper, book, "decision")) == 2
    assert env.keeper.runtime[book.id]["lastCollectAt"] == 0.0
    signer2 = _RefusingSigner.make(lambda ks: False)
    signer2._grants = signer._grants
    env.keeper.signer = signer2
    env.keeper.tick_book(book, now=1060.0)
    assert env.keeper.runtime[book.id]["lastCollectAt"] == 1060.0  # landed: the clock moves from the landing tick
    assert "collect" not in env.keeper.tick_book(book, now=2000.0)["decisions"]
    assert "collect" in env.keeper.tick_book(book, now=2060.0)["decisions"]


def test_a_start_that_lands_moves_the_collect_clock_and_a_refused_one_does_not(env):
    env.grant()
    env.lens.set_kami(1001, state="RESTING", node=None, hp=(200, 200))  # adopted RESTING at start
    book = _start(env, collectEvery=1000)
    from keeper.signer import DryRunSigner

    class Refuse(DryRunSigner):
        def _send(self, grant, intents):
            recs = super()._send(grant, intents)
            for r in recs:
                r.status, r.precheck = "not_sent", "reverted"
            return recs

    refuse = Refuse()
    refuse._grants = env.keeper.signer._grants
    ok = env.keeper.signer
    env.keeper.signer = refuse
    assert env.keeper.tick_book(book, now=500.0)["decisions"] == ["start"]
    assert "lastCollectAt" not in env.keeper.runtime[book.id] and book.plan["1001"]["state"] == "RESTING"
    env.keeper.signer = ok
    assert env.keeper.tick_book(book, now=600.0)["decisions"] == ["start"]
    assert env.keeper.runtime[book.id]["lastCollectAt"] == 600.0 and book.plan["1001"]["state"] == "HARVESTING"


def test_batch_fallback_on_the_tester_chain_with_a_reverting_collect_system(make_keeper, tmp_path):
    """The real pre-check path: the collect system is a contract that
    reverts, so the batch is refused by eth_call, the three singles are
    each refused by their own eth_call, no clock moves, all stay due."""
    from evm_stubs import deploy, reverter

    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    world = deploy_world(w3, w3.eth.accounts[1])
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})  # the pre-check calls FROM the operator
    chain = Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3)
    chain._systems["system.harvest.collect"] = deploy(w3, reverter("Harvest: starving"))
    e = make_keeper()
    cfg = load_config(flags={"profile": "lab", "dataDir": str(tmp_path / "dry"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    assert k.handle("grant", {"account": "lab"})["ok"]
    books = []
    for kami in (1001, 1002, 1003):
        e.lens.set_kami(kami, state="HARVESTING", node=62, hp=(120, 200), account=4242)
        s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": dict(HAF_CONFIG, kamiIndex=kami, collectEvery=1000)})
        books.append(k.store.get(s["data"]["book"]["id"]))
    k.tick_due(now=T)
    k.tick_due(now=T + 1000)
    recs = [r for r in k.signer.recorded if r.system == "system.harvest.collect"]
    # a dry-run keeper with the chain: the row keeps status dry_run and carries the chain's verdict in precheck
    assert [(r.count, r.precheck, r.status) for r in recs] == [(3, "reverted", "dry_run")] + [(1, "reverted", "dry_run")] * 3
    assert all("starving" in r.reason for r in recs)
    for b in books:
        assert k.runtime[b.id]["lastCollectAt"] == T and "did not land" in k.runtime[b.id]["lastError"]
        rows = [r["tx"] for r in _rows(k, b, "tx")]
        assert [r["precheck"] for r in rows] == ["reverted", "reverted"] and rows[1]["batchFallback"] is True and rows[0]["batchSize"] == 3
    assert k.tick_due(now=T + 1030) == [b.id for b in books]
    assert len([r for r in k.signer.recorded if r.system == "system.harvest.collect"]) == 8  # due again: one batch + three singles
    k.stop_engine()
