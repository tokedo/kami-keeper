"""The chain-backed HP fallback for the feed loop:

  1. the HP arithmetic and ids (unpack_config, signed, entity ids, the
     node-affinity table in keeper/data/nodes.csv);
  2. the reader replaying a recorded fixture through a fake chain — every
     decoded field and the projected HP equal the fixture's `expected`
     (computed independently of the reader for the synthetic file), the
     batched read shape (calls, bodies <= 100, config read once);
  3. the LIVE check against the lens `kami` HP for one kami when
     KEEPER_LENS_SOCKET + KEEPER_LIVE_KAMI are set (self-skips);
  4. `Chain.batch_calls` (chunking, retry, serial fallback, a revert is
     None) and `view_system_address` outside the send-path whitelist;
  5. the branch through the core: a harvestAndFeed / auto FEED book under
     `lens-unavailable` / `lens-not-live` feeds from the chain after
     `chainFallbackAfterSec`, once per `chainFeedCooldownSec`, with
     `underChain` / `chainBlock` on every row, and signs nothing else;
     `lens-stale` and `lens-incomplete` never take it; `chainFallback:
     false`, no chain, and an RPC failure each say so; the scrub.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
from pathlib import Path
from typing import Any

import pytest
from web3 import Web3

from conftest import Env
from fakechain import FakeChain
from keeper import chainhp
from keeper.chain import Chain, ChainError, RpcUnavailable, harvest_entity_id, kami_entity_id
from keeper.chainhp import BONUS_TYPES, CONFIG_NAMES, ChainHpReader, ChainHpReading, ChainKami, bonus_type_id, config_id, load_node_affinities, node_entity_id, project_hp, signed, unpack_config
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.scrub import collect_strings, forbidden_in
from keeper.whitelist import WhitelistRefused
from test_phase2a import BOOK_43, CAROL, NODE, _config, _last_tick, _rows, _seed, _set_party, _start

FIX_PATH = Path(__file__).with_name("fixtures") / "chain" / "kami-1001-synthetic.json"
FIX = json.loads(FIX_PATH.read_text())
T = 2_000_000_000.0


def _pack(slots: list[int]) -> int:
    raw = 0
    for i, v in enumerate(slots):
        raw |= ((v + (1 << 32)) if v < 0 else v) << (32 * (7 - i))
    return raw


class FakeHpChain:
    """Answers the reader's batched eth_calls from a fixture's `calls` map
    (the shape scripts/record_kami_chain.py writes). Addresses are
    deterministic per id so the reverse map is exact."""

    def __init__(self, fixture: dict[str, Any], head: int | None = None):
        self.fix = fixture
        self.head = int(head if head is not None else fixture["block"])
        self.fail_reads = False
        self.bodies: list[int] = []
        self._by_addr: dict[str, str] = {}
        comp = Web3().eth.contract(abi=chainhp._COMPONENT_ABI)
        self._gew = comp.encode_abi("getEntitiesWithValue", args=[0])[2:10]
        self._safe = comp.encode_abi("safeGet", args=[0])[2:10]

    def _addr(self, ident: str) -> str:
        a = Web3.to_checksum_address("0x" + hashlib.sha1(ident.encode()).hexdigest()[:40])
        self._by_addr[a.lower()] = ident
        return a

    def _guard(self) -> None:
        if self.fail_reads:
            raise RpcUnavailable("eth_call: ConnectionError")

    def block_number(self) -> int:
        self._guard()
        return self.head

    def component_address(self, component_id: str) -> str:
        self._guard()
        return self._addr(component_id)

    def view_system_address(self, system_id: str) -> str:
        self._guard()
        return self._addr(system_id)

    def batch_calls(self, calls: list[tuple[str, bytes]], chunk: int = 100, attempts: int = 3, sleep: Any = None) -> tuple[list[bytes | None], int]:
        self._guard()
        assert len(calls) <= 10_000
        for i in range(0, len(calls), chunk):
            self.bodies.append(len(calls[i:i + chunk]))
        out: list[bytes | None] = []
        nbytes = 0
        for to, data in calls:
            ident = self._by_addr[to.lower()]
            sel = data[:4].hex()
            arg = int.from_bytes(data[4:36], "big")
            if ident == "system.getter":
                key = f"system.getter:getKamiByIndex:{arg}"
            elif sel == self._gew:
                key = f"{ident}:getEntitiesWithValue:{arg}"
            else:
                assert sel == self._safe, sel
                key = f"{ident}:{arg}"
            hexval = self.fix["calls"].get(key)
            if hexval is None:
                out.append(None)
            else:
                raw = bytes.fromhex(hexval[2:])
                nbytes += len(raw)
                out.append(raw)
        return out, nbytes


class FakeHpReader:
    """A scripted reader for the core tests: {kami: (state, hp, hpMax,
    node)} -> a ChainHpReading; `fail` raises on read."""

    def __init__(self, script: dict[int, tuple[str, int, int, int | None]], block: int = 12278391):
        self.script = dict(script)
        self.block = block
        self.reads = 0
        self.fail: Exception | None = None
        self.last_args: tuple[Any, ...] | None = None

    def read(self, kami_indices: list[int], node_index: int, at: float) -> ChainHpReading:
        self.reads += 1
        self.last_args = (list(kami_indices), node_index, at)
        if self.fail is not None:
            raise self.fail
        r = ChainHpReading(block=self.block, at=at, calls=1 + 8 * len(kami_indices), bytes=640 * len(kami_indices))
        for k in kami_indices:
            if k not in self.script:
                r.kamis[k] = ChainKami(index=k, eid=kami_entity_id(k))
                continue
            state, hp, hp_max, node = self.script[k]
            r.kamis[k] = ChainKami(index=k, eid=kami_entity_id(k), state=state, hp_max=hp_max, hp_sync=hp, harvest_source=node_entity_id(node) if node is not None else None, node_index=node, read=True, level=12)
            r.projected[k] = hp
        return r


# ------------------------------------------------------------ 1. the HP arithmetic and ids


def test_config_unpacking_signed_and_entity_ids_follow_the_guard():
    slots = [20, 0, 6500, 3, -7, 0, 1000, 3]
    assert unpack_config(_pack(slots)) == slots
    assert unpack_config(0) == [0] * 8 and unpack_config((1 << 256) - 1) == [-1] * 8
    assert signed(5) == 5 and signed((1 << 256) - 50) == -50
    assert node_entity_id(62) == int.from_bytes(Web3.solidity_keccak(["string", "uint32"], ["node", 62]), "big")
    assert config_id("KAMI_HARV_STRAIN") == int.from_bytes(Web3.solidity_keccak(["string", "string"], ["is.config", "KAMI_HARV_STRAIN"]), "big")
    assert bonus_type_id("STND_STRAIN_BOOST", kami_entity_id(1001)) == int.from_bytes(Web3.solidity_keccak(["string", "string", "uint256"], ["bonus.type", "STND_STRAIN_BOOST", kami_entity_id(1001)]), "big")
    assert len(CONFIG_NAMES) == 9 and BONUS_TYPES == ("HARV_FERTILITY_BOOST", "HARV_INTENSITY_BOOST", "HARV_BOUNTY_BOOST", "STND_STRAIN_BOOST")


def test_the_node_affinity_catalog_is_the_hybrid_play_copy_and_projects_normal_when_unlisted():
    affs = load_node_affinities()
    assert affs[62] == FIX["expected"]["1001"]["nodeAffinities"] and affs[63] == FIX["expected"]["1004"]["nodeAffinities"]
    assert all(a in ("NORMAL", "EERIE", "SCRAP", "INSECT") for v in affs.values() for a in v)
    # an unlisted node projects with the NORMAL rule (harvest_efficacy over [])
    k = ChainKami(index=1, eid=1, state="HARVESTING", hp_max=200, hp_sync=200, power=60, harmony=40, violence=30, body="EERIE", hand="NORMAL", t_last=1000, t_reset=1000, read=True)
    cfg = FIX["config"]
    assert chainhp.harvest_efficacy(k, [], cfg) == chainhp.harvest_efficacy(k, ["NORMAL"], cfg)
    assert project_hp(k, [], cfg, 1000) == 200 and project_hp(k, [], cfg, 1000 + 5 * 3600) < 200
    resting = ChainKami(index=2, eid=2, state="RESTING", hp_max=200, hp_sync=150, read=True)
    assert project_hp(resting, ["EERIE"], cfg, 10 ** 9) == 150  # not harvesting: the sync value


# ------------------------------------------------------------ 2. the reader on the fixture


def test_the_reader_replays_the_fixture_and_the_projection_equals_the_oracle_expectation():
    chain = FakeHpChain(FIX)
    reader = ChainHpReader(chain)
    reading = reader.read(FIX["kamis"], FIX["node"], FIX["at"])
    assert reading.block == FIX["block"] and reader.config == {k: list(v) for k, v in FIX["config"].items()} and reading.config_read
    for idx, exp in FIX["expected"].items():
        k = reading.kamis[int(idx)]
        assert k.read and k.state == exp["state"] and k.hp_max == exp["hpMax"] and k.hp_sync == exp["hpSync"]
        assert (k.power, k.harmony, k.violence) == (exp["power"], exp["harmony"], exp["violence"]) and (k.body, k.hand) == (exp["body"], exp["hand"])
        assert k.t_last == exp["tLast"] and k.t_reset == exp["tReset"] and k.node_index == exp["nodeIndex"] and k.bonus == exp["bonus"]
        assert reading.projected[int(idx)] == exp["projectedHp"], idx
    assert reading.kamis[1001].harvesting_on(62) and not reading.kamis[1004].harvesting_on(62) and reading.kamis[1004].harvesting_on(63)
    assert not reading.kamis[1002].harvesting_on(62) and reading.kamis[1003].state == "DEAD"
    assert reading.projected[1001] == 108 < 165 and reading.projected[1002] == 100 and reading.projected[1004] == 180
    assert reading.node_affinity_unknown == []
    n = len(FIX["kamis"])
    n_inst = sum(len(v) for k, v in FIX["expected"].items() for v in [[1 for t, b in v["bonus"].items() if b]])  # instances carried in the fixture
    assert reading.calls == 1 + len(CONFIG_NAMES) + 8 * n + 3 * n_inst and reading.bytes > 0
    assert all(b <= 100 for b in chain.bodies)
    # the config is read once per process; a second read costs the kami calls only
    r2 = reader.read([1001], 62, FIX["at"] + 3600)
    assert not r2.config_read and r2.calls == 1 + 8 + 3 * 2 and r2.projected[1001] < reading.projected[1001]
    # a kami the getter answers nothing for is `read: False`, never a plausible zero
    r3 = reader.read([9999], 62, FIX["at"])
    assert r3.kamis[9999].read is False and 9999 not in r3.projected


def test_the_reader_batches_at_most_one_hundred_calls_per_body_and_an_rpc_failure_raises():
    fix = copy.deepcopy(FIX)
    fix["kamis"] = list(range(2000, 2030))
    for i in fix["kamis"]:
        fix["calls"][f"system.getter:getKamiByIndex:{i}"] = fix["calls"]["system.getter:getKamiByIndex:1002"]
    chain = FakeHpChain(fix)
    reading = ChainHpReader(chain).read(fix["kamis"], 62, fix["at"])
    assert len(reading.kamis) == 30 and all(k.read for k in reading.kamis.values())
    assert chain.bodies[0] == 9 and chain.bodies[1:] == [100, 100, 40]  # 30 kamis x 8 calls = 240 -> three bodies
    chain.fail_reads = True
    with pytest.raises(RpcUnavailable):
        ChainHpReader(chain).read([1001], 62, fix["at"])


# ------------------------------------------------------------ 3. the live check


@pytest.mark.live
def test_live_chain_read_matches_the_lens_kami_hp_within_two():
    """READ-ONLY against the public RPC and a lens socket: |chain projection
    - lens HP| <= 2 for KEEPER_LIVE_KAMI (a harvesting kami).
    Self-skips without KEEPER_LENS_SOCKET / KEEPER_LIVE_KAMI or network."""
    sock, kami = os.environ.get("KEEPER_LENS_SOCKET"), os.environ.get("KEEPER_LIVE_KAMI")
    if not sock or not kami:
        pytest.skip("KEEPER_LENS_SOCKET / KEEPER_LIVE_KAMI unset")
    import time

    chain = Chain("https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz", "0x2729174c265dbBd8416C6449E0E813E88f43D0E7", 428962654539583, request_timeout_sec=20)
    try:
        chain.verify_chain_id()
        lens = LensClient(sock, 10).kami(int(kami)).data
        reading = ChainHpReader(chain).read([int(kami)], int((lens.get("node") or {}).get("index") or 0), time.time())
    except Exception as exc:  # noqa: BLE001 — no network / no lens
        pytest.skip(f"chain or lens unavailable: {type(exc).__name__}")
    k = reading.kamis[int(kami)]
    assert k.read and k.state == lens["state"] and k.hp_max == lens["hp"]["total"]
    assert abs(reading.projected[int(kami)] - int(lens["hp"]["current"])) <= 2, (reading.projected[int(kami)], lens["hp"]["current"])


# ------------------------------------------------------------ 4. Chain.batch_calls and the view resolver


class _StubProvider:
    def __init__(self, answers: dict[str, str | None], fail_times: int = 0):
        self.answers = answers  # calldata hex -> result hex (None = error)
        self.fail_times = fail_times
        self.bodies: list[int] = []
        self.request_counter = itertools.count(7)

    def make_batch_request(self, body: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("simulated")
        self.bodies.append(len(body))
        out = []
        for _method, params in body:
            rid = next(self.request_counter)
            res = self.answers.get(params[0]["data"])
            out.append({"jsonrpc": "2.0", "id": rid, "result": res} if res is not None else {"jsonrpc": "2.0", "id": rid, "error": {"code": 3, "message": "execution reverted"}})
        return list(reversed(out))  # out of order: answers map back by id


class _StubEth:
    def __init__(self, provider: _StubProvider, serial_fails: bool = False):
        self.provider = provider
        self.serial_fails = serial_fails
        self.calls = 0

    def call(self, params: dict[str, Any]) -> bytes:
        self.calls += 1
        if self.serial_fails:
            raise ConnectionError("serial simulated")
        res = self.provider.answers.get(params["data"])
        if res is None:
            raise ValueError("execution reverted")
        return bytes.fromhex(res[2:])


class _StubW3:
    def __init__(self, provider: _StubProvider, serial_fails: bool = False):
        self.provider = provider
        self.eth = _StubEth(provider, serial_fails)


def test_batch_calls_chunks_at_one_hundred_maps_answers_by_id_retries_and_falls_back_serially():
    to = "0x" + "11" * 20
    answers = {"0x" + i.to_bytes(4, "big").hex(): ("0x" + i.to_bytes(32, "big").hex() if i % 7 else None) for i in range(250)}
    calls = [(to, i.to_bytes(4, "big")) for i in range(250)]
    p = _StubProvider(answers)
    chain = Chain("http://stub.invalid", "0x" + "22" * 20, 1, web3=_StubW3(p))
    out, nbytes = chain.batch_calls(calls, sleep=lambda s: None)
    assert p.bodies == [100, 100, 50] and len(out) == 250 and nbytes == 32 * sum(1 for i in range(250) if i % 7)
    assert out[1] == (1).to_bytes(32, "big") and out[0] is None and out[7] is None and out[249] == (249).to_bytes(32, "big")
    # one failed body is retried (sleep injected), then answered
    p2 = _StubProvider(answers, fail_times=1)
    chain2 = Chain("http://stub.invalid", "0x" + "22" * 20, 1, web3=_StubW3(p2))
    slept: list[float] = []
    out2, _ = chain2.batch_calls(calls[:5], sleep=slept.append)
    assert out2[1] == (1).to_bytes(32, "big") and slept == [0.4] and p2.bodies == [5]
    # every attempt failed: the serial road, a revert is None
    p3 = _StubProvider(answers, fail_times=3)
    w3 = _StubW3(p3)
    out3, _ = Chain("http://stub.invalid", "0x" + "22" * 20, 1, web3=w3).batch_calls(calls[:8], sleep=lambda s: None)
    assert w3.eth.calls == 8 and out3[0] is None and out3[7] is None and out3[3] == (3).to_bytes(32, "big")
    # the serial road failing too is RpcUnavailable, never a plausible answer
    p4 = _StubProvider(answers, fail_times=3)
    with pytest.raises(RpcUnavailable):
        Chain("http://stub.invalid", "0x" + "22" * 20, 1, web3=_StubW3(p4, serial_fails=True)).batch_calls(calls[:2], sleep=lambda s: None)


def test_view_system_address_resolves_outside_the_send_path_whitelist():
    from evm_stubs import make_tester_chain

    _provider, w3, chain = make_tester_chain()
    addr = chain.view_system_address("system.getter")
    assert Web3.is_checksum_address(addr) and addr == w3.eth.accounts[1]
    with pytest.raises(WhitelistRefused):
        chain.resolve_system("system.getter")
    assert "system.getter" not in chain.describe()["resolvedSystems"] and all(":" not in k for k in chain.describe()["resolvedSystems"])
    assert chain.view_system_address("system.getter") == addr  # cached


# ------------------------------------------------------------ 5. through the core


def _chain_env(make_keeper, tmp_path, reader: FakeHpReader, profile: str = "lab") -> Env:
    e = make_keeper(cap=6)
    cfg = load_config(flags={"profile": profile, "dataDir": str(tmp_path / "kc"), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 6 if profile == "lab" else 0, "requestTimeoutSec": 5}, env={})
    k = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5), chain=FakeChain(head=12278391))
    clock = {"now": T}
    k.clock = lambda: clock["now"]
    k.chainhp = reader
    k.boot()
    e.keeper.stop_engine()
    e.keeper = k
    e.data_dir = Path(cfg.dataDir)
    e.clock = clock  # type: ignore[attr-defined]
    assert k.handle("grant", {"account": "lab"})["ok"]
    return e


def _tick(e: Env, book, dt: float = 30.0) -> dict[str, Any]:
    e.clock["now"] += dt
    return e.keeper.tick_book(book, e.clock["now"])


def _feeds(e: Env) -> list[Any]:
    return [r for r in e.keeper.signer.recorded if r.system == "system.kami.use.item"]


def test_haf_feeds_from_the_chain_after_chain_fallback_after_sec_once_per_chain_feed_cooldown(make_keeper, tmp_path):
    reader = FakeHpReader({1001: ("HARVESTING", 40, 200, 62)})
    e = _chain_env(make_keeper, tmp_path, reader)
    s = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}})
    assert s["ok"], s
    book = e.keeper.store.get(s["data"]["book"]["id"])
    assert book.config["chainFallback"] is True and book.config["chainFallbackAfterSec"] == 300 and book.config["chainFeedCooldownSec"] == 1800
    e.lens.set_kami(1001, hp=(40, 200))
    e.lens.stop()  # the outage begins
    t0 = e.clock["now"] + 30
    s1 = _tick(e, book)
    assert s1["reason"] == "perception gated" and s1["degraded"] == ["lens-unavailable"] and s1["chainFallback"].startswith("waiting") and s1["chainFallbackAfterSec"] == 300 and s1["outageSec"] == 0
    assert reader.reads == 0 and _feeds(e) == [] and e.keeper.lens_outage_since == t0
    st = e.keeper.handle("status")["data"]
    assert st["lens"]["outageSince"] and st["lens"]["outageSince"].endswith("Z")
    s2 = _tick(e, book, dt=269.0)  # 269 s into the outage: still waiting
    assert reader.reads == 0 and _feeds(e) == [] and s2["outageSec"] == 269
    s3 = _tick(e, book, dt=31.0)  # 300 s: the chain read, the feed
    assert s3["acted"] is True and s3["decisions"] == ["feed"] and s3["underChain"] is True and s3["chainBlock"] == 12278391 and s3["degraded"] == ["lens-unavailable"]
    assert s3["chainReads"] == {"calls": 9, "bytes": 640, "block": 12278391, "kamis": 1, "configRead": False} and s3["chainHp"] == {"1001": 40}
    assert reader.reads == 1 and reader.last_args == ([1001], 62, e.clock["now"]) and len(_feeds(e)) == 1
    dec = _rows(e, book, "decision")[-1]
    assert dec["decision"]["parameters"]["underChain"] is True and dec["decision"]["parameters"]["chainBlock"] == 12278391 and dec["decision"]["parameters"]["hpProjected"] == 40 and dec["decision"]["parameters"]["inventoryBalance"] is None
    assert dec["degraded"] == ["lens-unavailable"] and dec["lensBlock"] is None
    tx = _rows(e, book, "tx")[-1]
    assert tx["tx"]["underChain"] is True and tx["tx"]["chainBlock"] == 12278391 and tx["tx"]["system"] == "system.kami.use.item" and tx["degraded"] == ["lens-unavailable"]
    assert any("chain fallback" in l["line"] for l in e.keeper.logs)
    # once per chainFeedCooldownSec: the chain read cannot show the feed applied, only time clears the withhold
    s4 = _tick(e, book)
    assert s4["acted"] is False and s4["feedWithheld"]["reason"] == "chain fallback; cooldown not elapsed" and s4["feedWithheld"]["feedCooldownSec"] == 1800 and len(_feeds(e)) == 1
    for _ in range(3):
        _tick(e, book, dt=500.0)
    assert len(_feeds(e)) == 1
    s5 = _tick(e, book, dt=300.0)  # 1830 s after the feed
    assert s5["decisions"] == ["feed"] and len(_feeds(e)) == 2 and reader.reads == 6
    assert book.state == "running" and [r["event"] for r in _rows(e, book, "lifecycle")] == ["started"]
    # the lens is back: the outage clock clears and the next tick is an ordinary one
    e.lens.start()
    e.lens.set_kami(1001, hp=(150, 200))
    s6 = _tick(e, book)
    assert "underChain" not in s6 and e.keeper.lens_outage_since is None and e.keeper.handle("status")["data"]["lens"]["outageSince"] is None
    # a new outage starts the clock afresh: no chain read for another 300 s
    e.lens.stop()
    s7 = _tick(e, book)
    assert s7["chainFallback"].startswith("waiting") and reader.reads == 6


def test_under_the_chain_fallback_nothing_but_a_feed_is_ever_signed_and_no_p7_or_death_row(make_keeper, tmp_path):
    reader = FakeHpReader({1001: ("RESTING", 200, 200, None)})
    e = _chain_env(make_keeper, tmp_path, reader)
    s = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100, "collectEvery": 60, "chainFallbackAfterSec": 0}})
    book = e.keeper.store.get(s["data"]["book"]["id"])
    _tick(e, book)  # a valid tick: the collect clock starts
    e.lens.stop()
    s1 = _tick(e, book, dt=5000.0)  # collectEvery long elapsed; the chain says RESTING at full HP: no start, no collect
    assert s1["underChain"] is True and s1["decisions"] == [] and e.keeper.signer.recorded == [] and reader.reads == 1
    reader.script[1001] = ("DEAD", 0, 200, None)
    s2 = _tick(e, book)
    assert s2["chainDead"] == [1001] and s2["decisions"] == [] and _rows(e, book, "death") == [] and book.state == "running"
    reader.script[1001] = ("HARVESTING", 20, 200, 63)  # harvesting elsewhere: a contradiction on a lens read, nothing here
    for _ in range(4):
        s3 = _tick(e, book)
        assert s3["decisions"] == [] and "contradiction" not in s3
    assert book.state == "running" and e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0 and e.keeper.signer.recorded == []
    reader.script[1001] = ("HARVESTING", 20, 200, 62)
    s4 = _tick(e, book)
    assert s4["decisions"] == ["feed"] and [r.system for r in e.keeper.signer.recorded] == ["system.kami.use.item"]
    assert all(r["tx"]["system"] == "system.kami.use.item" for r in _rows(e, book, "tx"))
    # a kami the chain answers nothing for: noted, nothing signed
    reader.script.pop(1001)
    s5 = _tick(e, book, dt=2000.0)
    assert s5["chainUnread"] == [1001] and s5["decisions"] == [] and len(e.keeper.signer.recorded) == 1


def test_lens_stale_and_lens_incomplete_never_take_the_chain_branch(make_keeper, tmp_path):
    reader = FakeHpReader({1001: ("HARVESTING", 40, 200, 62)})
    e = _chain_env(make_keeper, tmp_path, reader)
    s = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100, "chainFallbackAfterSec": 0}})
    book = e.keeper.store.get(s["data"]["book"]["id"])
    e.lens.set_kami(1001, hp=(40, 200))
    e.lens.mode, e.lens.lag = "stale", 500
    s1 = _tick(e, book)
    assert s1["underStale"] is True and s1["decisions"] == ["feed"] and reader.reads == 0 and e.keeper.lens_outage_since is None  # the lens read feeds
    e.lens.mode = "live"
    e.lens.set_kami(1001, node=None)  # a served row lacking a field: lens-incomplete, not an outage
    s2 = _tick(e, book, dt=400.0)
    assert "lens-incomplete" in s2["degraded"] and "chainFallback" not in s2 and reader.reads == 0 and e.keeper.lens_outage_since is None
    e.lens.set_kami(1001, node=62)
    e.lens.hooks["status"] = lambda args: {**e.lens._status(), "degraded": ["stream-stalled:128s"]}  # the mirror KNOWN incomplete: an outage
    s3 = _tick(e, book, dt=2000.0)
    assert s3["degraded"] == ["lens-degraded:stream-stalled"] and s3["underChain"] is True and s3["decisions"] == ["feed"] and reader.reads == 1 and s3["lensDegraded"] == ["stream-stalled:128s"]
    e.lens.hooks.pop("status")
    e.lens.mode, e.lens.lag = "not-ready", 0
    s4 = _tick(e, book, dt=2000.0)
    assert "lens-not-live" in s4["degraded"] and s4["underChain"] is True and reader.reads == 2


def test_chain_fallback_off_no_chain_and_a_failed_read_each_say_so(make_keeper, tmp_path):
    reader = FakeHpReader({1001: ("HARVESTING", 40, 200, 62)})
    e = _chain_env(make_keeper, tmp_path, reader)
    off = e.keeper.store.get(e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100, "chainFallback": False, "chainFallbackAfterSec": 0}})["data"]["book"]["id"])
    on = e.keeper.store.get(e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100, "chainFallbackAfterSec": 0}})["data"]["book"]["id"])
    e.lens.stop()
    s = _tick(e, off)
    assert s["reason"] == "perception gated" and "chainFallback" not in s and reader.reads == 0
    reader.fail = RpcUnavailable("eth_call: ConnectionError")
    s = _tick(e, on)
    assert s["reason"] == "perception gated" and s["degraded"] == ["lens-unavailable", "rpc-unavailable"] and s["chainFallback"] == "read unavailable" and e.keeper.signer.recorded == []
    assert e.keeper.handle("status", {"book": on.id})["data"]["books"][0]["degraded"] == ["lens-unavailable", "rpc-unavailable"] and on.state == "running"
    reader.fail = ChainError("system not found on chain: system.getter")
    s = _tick(e, on)
    assert s["chainFallback"] == "read failed: ChainError" and "rpc-unavailable" in s["degraded"]
    reader.fail = None
    assert _tick(e, on)["decisions"] == ["feed"]
    # no chain configured (the conftest keeper): the gated tick says so
    plain = make_keeper()
    plain.grant()
    b = plain.keeper.store.get(plain.start_haf(chainFallbackAfterSec=0)["data"]["book"]["id"])
    plain.lens.stop()
    s = plain.keeper.tick_book(b)
    assert s["reason"] == "perception gated" and s["chainFallback"] == "no chain configured" and plain.keeper.signer.recorded == []


def test_auto_feed_book_feeds_at_the_floor_and_a_rest_book_is_idle_under_the_chain_fallback(make_keeper, tmp_path):
    reader = FakeHpReader({263: ("HARVESTING", 20, 240, NODE), 508: ("HARVESTING", 100, 220, NODE), 699: ("HARVESTING", 20, 220, 80)})
    e = _chain_env(make_keeper, tmp_path, reader)
    _seed(e.lens)
    e.books = []  # type: ignore[attr-defined]
    feed = _start(e, [263, 508, 699], regenMethod="FEED", foodItem=11304, chainFallbackAfterSec=0)
    rest = _start(e, BOOK_43[3:8], chainFallbackAfterSec=0)
    e.lens.mode = "not-ready"
    s = _tick(e, feed)
    assert "lens-not-live" in s["degraded"] and s["underChain"] is True and s["decisions"] == ["feed"]
    d = _rows(e, feed, "decision")[-1]["decision"]["parameters"]
    assert d["kami"] == 263 and d["underChain"] is True and d["hpProjected"] == 20 and d["restFloor"] == 24 and d["bound"] == 0 and d["boundBy"] is None and d["item"] == 11304
    assert s["chainHp"] == {"263": 20, "508": 100} and s["chainReadKamis"] == 3  # the third kami harvests elsewhere: no feed, not counted at the floor
    assert reader.last_args[0] == [263, 508, 699] and reader.last_args[1] == NODE
    s2 = _tick(e, rest)  # the REST book takes the branch too — none of ITS kamis is in the reading, so it signs nothing
    assert s2["underChain"] is True and s2["decisions"] == [] and reader.reads == 2 and len(e.keeper.signer.recorded) == 1
    s3 = _tick(e, feed)
    assert s3["feedWithheld"]["reason"] == "chain fallback; cooldown not elapsed" and len(e.keeper.signer.recorded) == 1
    assert all(r.system == "system.kami.use.item" for r in e.keeper.signer.recorded) and feed.state == "running" and rest.state == "running"


def test_the_chain_fallback_surfaces_are_clean(make_keeper, tmp_path):
    reader = FakeHpReader({1001: ("HARVESTING", 40, 200, 62)})
    e = _chain_env(make_keeper, tmp_path, reader)
    s = e.keeper.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100, "chainFallbackAfterSec": 0}})
    book = e.keeper.store.get(s["data"]["book"]["id"])
    e.lens.stop()
    _tick(e, book)
    reader.fail = RpcUnavailable("eth_call: ConnectionError")
    _tick(e, book, dt=2000.0)
    frames = [e.keeper.handle("describe", {"type": "harvestAndFeed"}), e.keeper.handle("describe", {"type": "auto"}), e.keeper.handle("status"), e.keeper.handle("logs"), e.keeper.handle("ledger", {"book": book.id})]
    assert [(p, w) for f in frames for (p, w) in forbidden_in(f)] == []
    assert collect_strings(frames)
    desc = frames[0]["data"]
    assert {"chainFallback", "chainFallbackAfterSec", "chainFeedCooldownSec"} <= set(desc["defaults"]) and "chainFallback" in desc["description"]
