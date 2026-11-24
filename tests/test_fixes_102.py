"""1.0.2 — the fix list.

Part 1, the redeploy fix (Chain.recheck_system), what its first tests left
open:
  T1 the keeper WIRES the log line: one line per move, naming the system
     id and the time, holding no address; none inside the minute after.
  T2 a log callback that raises never reaches the pre-check.
  T3 an RPC failure of the pre-check asks the registry nothing and stays
     `rpc-unavailable`.
  T4 the moved list in `status` holds the newest 20, oldest first.
  T5 through the signer, armed: a burst whose first pre-check reverts at
     the retired address is signed to the NEW address and lands.
  T6 the same in a dry-run keeper.

Part 2:
  1  every BAD_ARGS answer the CLI prints carries a usage line; `--help`
     lists every command's flags. The frames on the socket are unchanged.
  2  the tick summary's `txCountInTick` / `txStatusInTick` (the tick's own
     burst), the old names kept for one release.
  3  the `kamiden-unavailable` flag's transitions are logged with the
     lens's text, and `status` serves the probe.

Part 3: the tested-lens rows.
"""

from __future__ import annotations

import json
import re

import pytest
from web3 import Web3

from conftest import FIXTURE_OPERATOR_ADDRESS, HAF_CONFIG
from evm_stubs import BatchingTesterProvider, deploy, deploy_world, reverter
from keeper import LENS_TESTED_VERSIONS, cli
from keeper import chain as chainmod
from keeper.chain import Chain, PrecheckReverted, RpcUnavailable, encode
from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient
from keeper.protocol import COMMANDS, META_KEYS
from keeper.secrets import OperatorKey
from keeper.signer import DryRunSigner
from keeper.whitelist import Intent

from conftest import FIXTURE_OPERATOR_KEY

TEST_GAS_PRICE = {"maxFeePerGas": 2_000_000_000, "maxPriorityFeePerGas": 0}  # the tester's base fee is above the chain's flat fee
FEED = "system.kami.use.item"
CRAFT = "system.craft"
ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


def _craft_call():
    return encode(Intent(CRAFT, "lab", None, {"recipeIndex": 29, "amount": 1}))


def _tester():
    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]  # no code: any call returns empty, any tx lands
    world = deploy_world(w3, sink)
    w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": FIXTURE_OPERATOR_ADDRESS, "value": 10**18})
    return provider, w3, Chain("http://tester.invalid", world, w3.eth.chain_id, web3=w3), sink


def _keeper(make_keeper, tmp_path, chain, armed: bool):
    e = make_keeper()  # a fake lens to borrow
    flags = {"profile": "lab", "dataDir": str(tmp_path / ("armed" if armed else "dry")), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4, "rpcUrl": "http://tester.invalid", "worldAddress": chain.world_address, "chainId": chain.chain_id, "requestTimeoutSec": 5}
    if armed:
        flags["armed"] = True
    k = Keeper(load_config(flags=flags, env={}), lens=LensClient(e.lens.socket_path, 5), chain=chain)
    k.boot()
    if armed:
        k.signer.gas_price = TEST_GAS_PRICE
    assert k.handle("grant", {"account": "lab"})["ok"]
    return e, k


def _retire(w3, chain, system_id: str, sink: str, message: str = "") -> tuple[str, str]:
    """The state right after a game redeploy: the keeper's cache holds the
    RETIRED address of `system_id` (it reverts every call) while the
    registry names the new one (`sink`). Returns (retired, new)."""
    live_world = chain.world_address
    old = deploy(w3, reverter(message))
    chain.world_address = deploy_world(w3, old)
    assert chain.resolve_system(system_id) == old
    chain.world_address = live_world
    return old, sink


def _redeploy_lines(k) -> list[str]:
    return [l["line"] for l in k.logs if l["line"].startswith("game redeploy:")]


# ------------------------------------------------- T1. the keeper's log line


def test_t1_the_keeper_logs_one_line_per_move_naming_the_system_and_the_time_and_no_address(make_keeper, tmp_path):
    provider, w3, chain, sink = _tester()
    e, k = _keeper(make_keeper, tmp_path, chain, armed=False)
    t = [5000.0]
    chain._clock = lambda: t[0]
    call = _craft_call()
    # the redeploy: the cache holds a retired address, the registry names a
    # NEW system that refuses this call for a reason of its own
    old = deploy(w3, reverter(""))
    chain.world_address = deploy_world(w3, old)
    assert chain.resolve_system(CRAFT) == old
    new = deploy(w3, reverter("craft: not enough stamina"))
    chain.world_address = deploy_world(w3, new)
    assert _redeploy_lines(k) == []
    with pytest.raises(PrecheckReverted) as ei:
        chain.precheck(call, FIXTURE_OPERATOR_ADDRESS)
    assert "not enough stamina" in ei.value.reason  # the NEW address's verdict
    moved = chain.describe()["movedSystems"]
    lines = _redeploy_lines(k)
    assert len(moved) == 1 and len(lines) == 1
    line = lines[0]
    assert CRAFT in line and moved[0]["at"] in line
    assert not ADDRESS_RE.search(line) and old[2:].lower() not in line.lower() and new[2:].lower() not in line.lower()
    assert [l["book"] for l in k.logs if l["line"] == line] == [None]  # a keeper-level line
    # a second redeploy inside the minute: the pre-check reverts again, the
    # registry is not asked, nothing is logged
    chain.world_address = deploy_world(w3, sink)
    t[0] += 30
    with pytest.raises(PrecheckReverted):
        chain.precheck(call, FIXTURE_OPERATOR_ADDRESS)
    assert len(_redeploy_lines(k)) == 1 and len(chain.describe()["movedSystems"]) == 1
    # past the minute it is found, and logged once more
    t[0] += 31
    chain.precheck(call, FIXTURE_OPERATOR_ADDRESS)
    assert len(_redeploy_lines(k)) == 2 and chain.resolve_system(CRAFT) == sink
    assert k.handle("logs")["ok"]  # the line passes the frame rules


# ------------------------------------------- T2. a raising callback is held


def test_t2_a_log_callback_that_raises_does_not_stop_the_precheck():
    provider, w3, chain, sink = _tester()
    calls = []

    def boom(move):
        calls.append(move)
        raise RuntimeError("the log sink is gone")

    chain.on_system_moved = boom
    old, new = _retire(w3, chain, CRAFT, sink)
    chain.precheck(_craft_call(), FIXTURE_OPERATOR_ADDRESS)  # passes at the new address; the callback's error went nowhere
    assert len(calls) == 1 and chain.resolve_system(CRAFT) == new
    assert chain.describe()["resolvedSystems"][CRAFT] == new and len(chain.describe()["movedSystems"]) == 1


# ------------------------------- T3. an RPC failure asks the registry nothing


def test_t3_an_rpc_failure_of_the_precheck_makes_no_registry_read_and_stays_rpc_unavailable(monkeypatch):
    provider, w3, chain, sink = _tester()
    monkeypatch.setattr(chainmod.time, "sleep", lambda s: None)
    call = _craft_call()
    assert chain.resolve_system(CRAFT) == sink
    other = w3.eth.accounts[2]
    chain.world_address = deploy_world(w3, other)  # the registry WOULD name another address, and it is reachable

    def down(method, params):
        # only the pre-check's own eth_call fails; a registry read would pass
        if method == "eth_call" and str(params[0].get("to", "")).lower() == sink.lower():
            raise ConnectionError("simulated: the node dropped the call")

    provider.inject = down
    before = provider.count("eth_call")
    with pytest.raises(RpcUnavailable):
        chain.precheck(call, FIXTURE_OPERATOR_ADDRESS)
    assert provider.count("eth_call") == before + 2  # the two attempts of the pre-check, nothing else
    assert chain.resolve_system(CRAFT) == sink and chain.describe()["movedSystems"] == [] and CRAFT not in chain._rechecked
    # and through the dry-run recorder the record reads rpc-unavailable
    signer = DryRunSigner(chain)
    signer.grant("lab", OperatorKey("LAB_OPERATOR_KEY", FIXTURE_OPERATOR_KEY))
    before = provider.count("eth_call")
    rec = signer.submit([Intent(CRAFT, "lab", None, {"recipeIndex": 29, "amount": 1})], {"recipes": [{"recipeIndex": 29}]})[0]
    assert rec.precheck == "rpc-unavailable" and provider.count("eth_call") == before + 2
    assert chain.resolve_system(CRAFT) == sink


# --------------------------------------------- T4. the moved list is capped


def test_t4_twenty_five_moves_leave_the_newest_twenty_oldest_first(monkeypatch):
    provider, w3, chain, sink = _tester()

    def addr(n: int) -> str:
        return Web3.to_checksum_address("0x" + format(n + 1, "040x"))

    chain._systems[CRAFT] = addr(0)
    t = [100.0]
    chain._clock = lambda: t[0]
    step = [0]

    def registry(system_id):
        step[0] += 1
        return addr(step[0])

    monkeypatch.setattr(chain, "_registry_system_address", registry)
    for _ in range(25):
        t[0] += 61
        assert chain.recheck_system(CRAFT) is True
    moved = chain.describe()["movedSystems"]
    assert len(moved) == 20
    assert [m["to"] for m in moved] == [addr(n) for n in range(6, 26)]  # the newest 20, oldest first
    assert [m["from"] for m in moved] == [addr(n) for n in range(5, 25)]
    assert chain.resolve_system(CRAFT) == addr(25)


# ----------------------------------------------- T5. through the live signer


def test_t5_an_armed_burst_whose_precheck_reverts_at_the_retired_address_is_signed_to_the_new_one_and_lands(make_keeper, tmp_path):
    provider, w3, chain, sink = _tester()
    e, k = _keeper(make_keeper, tmp_path, chain, armed=True)
    assert k.signer.kind == "live" and k.dry_run is False
    s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": HAF_CONFIG})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    old, new = _retire(w3, chain, FEED, sink)
    e.lens.set_kami(1001, hp=(40, 200))  # below feedBelowHp 100 -> feed
    summary = k.tick_book(k.store.get(bid))
    assert summary["decisions"] == ["feed"] and summary["txStatusInTick"] == ["success"]
    txs = [r["tx"] for r in k.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tx"]
    assert [t["stage"] for t in txs] == ["broadcast", "receipt"]
    assert all(t["precheck"] == "ok" for t in txs) and txs[-1]["status"] == "success"
    assert w3.eth.get_transaction(txs[-1]["txHash"])["to"] == new  # signed to the NEW address
    assert w3.eth.get_transaction_receipt(txs[-1]["txHash"]).status == 1
    st = k.handle("status")["data"]
    assert st["signer"]["chain"]["resolvedSystems"][FEED] == new
    moved = st["signer"]["chain"]["movedSystems"]
    assert len(moved) == 1 and moved[0]["system"] == FEED and moved[0]["from"] == old and moved[0]["to"] == new
    assert len(_redeploy_lines(k)) == 1 and k.runtime[bid]["lastError"] is None


# -------------------------------------------- T6. the same, dry-run keeper


def test_t6_a_dry_run_keeper_finds_the_redeployed_system_the_same_way(make_keeper, tmp_path):
    provider, w3, chain, sink = _tester()
    e, k = _keeper(make_keeper, tmp_path, chain, armed=False)
    assert k.signer.kind == "dry-run" and k.signer.describe()["precheck"] is True
    s = k.handle("start", {"account": "lab", "strategyType": "harvestAndFeed", "config": HAF_CONFIG})
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    old, new = _retire(w3, chain, FEED, sink)
    sent = provider.count("eth_sendRawTransaction")
    e.lens.set_kami(1001, hp=(40, 200))
    summary = k.tick_book(k.store.get(bid))
    assert summary["decisions"] == ["feed"] and summary["txStatusInTick"] == ["dry_run"]
    rec = k.signer.recorded[-1]
    assert rec.precheck == "ok" and rec.reason is None
    txs = [r["tx"] for r in k.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tx"]
    assert txs and txs[-1]["precheck"] == "ok" and txs[-1]["status"] == "dry_run"
    st = k.handle("status")["data"]
    assert st["dryRun"] is True and st["chain"]["resolvedSystems"][FEED] == new
    assert [(m["system"], m["from"], m["to"]) for m in st["chain"]["movedSystems"]] == [(FEED, old, new)]
    assert len(_redeploy_lines(k)) == 1
    assert provider.count("eth_sendRawTransaction") == sent  # nothing was signed


# ---------------------------------------- Part 2, item 1. the CLI's usage line


@pytest.mark.parametrize("cmd", sorted(COMMANDS))
def test_every_bad_args_answer_of_the_cli_carries_the_commands_usage_line(cmd, capsys):
    for argv in ([cmd, "--socket", "/nonexistent.sock", "--bogus", "1"], [cmd, "--socket", "/nonexistent.sock", "stray"]):
        rc = cli.main(argv)
        out = json.loads(capsys.readouterr().out)
        assert rc == cli.EXIT_BAD_ARGS and out["error"]["code"] == "BAD_ARGS"
        usage = out["error"]["usage"]
        assert usage == cli.usage_line(cmd) and usage.startswith(f"usage: keeper {cmd}")
        for a in COMMANDS[cmd].args:
            assert f"--{a.name}" in usage
            assert (f"[--{a.name}" in usage) is (not a.required)  # a required flag is unbracketed
        assert set(out) == {"id", "ok", "error", "meta"} and set(out["meta"]) == META_KEYS


def test_the_usage_lines_read_as_the_fix_list_asked():
    assert cli.usage_line("grant") == "usage: keeper grant --account <label> [--socket <path>]"
    assert cli.usage_line("describe") == "usage: keeper describe [--type harvestAndFeed|auto|craft] [--socket <path>]"
    assert cli.usage_line("start") == "usage: keeper start --account <label> --strategyType harvestAndFeed|auto|craft --config <json|@file> [--socket <path>]"
    assert cli.usage_line("ledger").startswith("usage: keeper ledger --book <book-id> [--since <n>] [--kind tick|")


def test_the_positional_forms_a_model_tried_are_answered_with_the_flag_form(capsys):
    assert cli.main(["grant", "main"]) == cli.EXIT_BAD_ARGS
    out = json.loads(capsys.readouterr().out)
    assert out["error"]["message"] == "grant: unexpected positional argument" and out["error"]["usage"] == "usage: keeper grant --account <label> [--socket <path>]"
    assert cli.main(["describe", "auto"]) == cli.EXIT_BAD_ARGS
    assert "--type harvestAndFeed|auto|craft" in json.loads(capsys.readouterr().out)["error"]["usage"]
    assert cli.main(["start", "--account", "lab"]) == cli.EXIT_BAD_ARGS  # a missing required flag
    out = json.loads(capsys.readouterr().out)
    assert "missing required argument 'strategyType'" in out["error"]["message"] and "--strategyType harvestAndFeed|auto|craft --config" in out["error"]["usage"]


def test_help_lists_every_command_with_its_flags(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for cmd in COMMANDS:
        assert cli.usage_line(cmd)[len("usage: "):] in text
    with pytest.raises(SystemExit):
        cli.main(["start", "--help"])
    assert "keeper start --account <label> --strategyType" in capsys.readouterr().out


def test_the_usage_line_is_the_clis_own_the_frame_on_the_socket_is_unchanged(env):
    env.grant()
    frame = env.keeper.handle("start", {"account": "lab"})  # what the socket answers
    assert frame["error"]["code"] == "BAD_ARGS" and set(frame["error"]) == {"code", "message"}
    printed = cli.with_usage(frame, "start")  # what the CLI prints of it
    assert printed["error"]["usage"] == cli.usage_line("start") and printed["error"]["message"] == frame["error"]["message"]
    assert "usage" not in frame["error"]  # the frame itself was not touched
    missing = env.keeper.handle("ledger", {"book": "missing"})
    assert missing["error"]["code"] == "NOT_FOUND" and cli.with_usage(missing, "ledger") is missing  # only BAD_ARGS gains the line
    ok = env.keeper.handle("status")
    assert cli.with_usage(ok, "status") is ok
    assert "usage" not in json.dumps(env.keeper.handle("describe")["data"]["commands"])  # `describe` is as it was


# -------------------------------- Part 2, item 2. the tick summary's tx fields


def test_the_tick_summary_names_its_own_burst_and_keeps_the_old_names_for_one_release(env):
    env.grant()
    bid = env.start_haf(collectEvery=1000)["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    env.keeper.tick_book(book, now=0.0)
    env.lens.set_kami(1001, hp=(40, 200))  # a feed (the tick's own burst) ...
    s = env.keeper.tick_book(book, now=1000.0)  # ... and a collect, which is POOLED
    assert s["decisions"] == ["feed", "collect"] and s["collectPooled"] == 1
    assert s["txCountInTick"] == 1 and s["txStatusInTick"] == ["dry_run"]  # the feed alone: the pooled collect is not in this tick's burst
    assert s["txCount"] == s["txCountInTick"] and s["txStatus"] == s["txStatusInTick"]  # the old names, same values
    row = [r for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tick"][-1]["summary"]
    assert row["txCountInTick"] == 1 and row["txStatusInTick"] == ["dry_run"] and row["collectPooled"] == 1
    txs = [r["tx"]["system"] for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "tx"]
    assert txs.count("system.harvest.collect") == 1 and txs.count(FEED) == 1  # the tx rows are complete


# ------------------------------------ Part 2, item 3. the Kamiden probe


def _kamiden_lines(k) -> list[str]:
    return [l["line"] for l in k.logs if l["line"].startswith("kamiden-unavailable ")]


def test_the_kamiden_flags_transitions_are_logged_with_the_lens_text_and_status_names_the_probe(env):
    k = env.keeper
    assert k.kamiden == {"probedAt": None, "ok": None, "error": None}
    st = k.handle("status")["data"]
    assert st["kamiden"]["ok"] is True and st["kamiden"]["error"] is None and st["kamiden"]["probedAt"]
    assert _kamiden_lines(k) == []  # a healthy first probe is no transition
    env.lens.status_data["feedsDegraded"] = ["kamiden-stream: rate limit exceeded, retry in 12 s"]
    st = k.handle("status")["data"]
    assert st["degraded"] == ["kamiden-unavailable"]
    assert st["kamiden"]["ok"] is False and "rate limit exceeded" in st["kamiden"]["error"] and st["kamiden"]["probedAt"]
    lines = _kamiden_lines(k)
    assert len(lines) == 1 and lines[0].startswith("kamiden-unavailable set:") and "rate limit exceeded" in lines[0]
    k.handle("status")
    assert len(_kamiden_lines(k)) == 1  # still set: no second line
    env.lens.status_data["feedsDegraded"] = []
    st = k.handle("status")["data"]
    assert st["degraded"] == [] and st["kamiden"] == {"probedAt": st["kamiden"]["probedAt"], "ok": True, "error": None}
    lines = _kamiden_lines(k)
    assert len(lines) == 2 and lines[1].startswith("kamiden-unavailable cleared:")
    assert set(st["kamiden"]) == {"probedAt", "ok", "error"}


def test_a_tick_probes_too_and_an_unanswered_lens_probes_nothing(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    k = env.keeper
    env.lens.status_data["feedsDegraded"] = ["kamiden-stream"]
    k.tick_book(book, now=3_000_000.0)
    assert k.kamiden["ok"] is False and k.kamiden["error"] == "kamiden-stream" and len(_kamiden_lines(k)) == 1
    probed = dict(k.kamiden)
    env.lens.stop()  # the lens does not answer: nothing was probed, the last probe stands
    k.tick_book(book, now=3_000_030.0)
    assert k.kamiden == probed and len(_kamiden_lines(k)) == 1


def test_the_probes_text_is_bounded_and_carries_no_key_shaped_value(env):
    k = env.keeper
    env.lens.status_data["feedsDegraded"] = ["kamiden-stream 0x" + "ab" * 32 + " " + "x" * 400]
    st = k.handle("status")
    assert st["ok"], st  # the frame rule holds: a key-shaped run would have refused the whole answer
    err = st["data"]["kamiden"]["error"]
    assert "<hash>" in err and "ab" * 32 not in err and len(err) <= 200


def test_lens_text_carrying_forbidden_language_is_withheld_from_status_and_from_the_log(env):
    """Round 2 (R2): the guard in `_note_kamiden` — the lens's own text is
    never served or logged with language the keeper's scrub forbids. The
    term is taken from the scrub's list, so this file names none."""
    from keeper.scrub import FORBIDDEN_WORDS, find_forbidden
    term = FORBIDDEN_WORDS[0]
    k = env.keeper
    env.lens.status_data["feedsDegraded"] = [f"kamiden-stream: {term} feed stalled"]
    st = k.handle("status")
    assert st["ok"], st
    assert st["data"]["kamiden"]["ok"] is False
    assert st["data"]["kamiden"]["error"] == "lens text withheld: it carried forbidden language"
    lines = _kamiden_lines(k)
    assert len(lines) == 1 and lines[0].startswith("kamiden-unavailable set:")
    assert find_forbidden(lines[0]) == [] and find_forbidden(st["data"]["kamiden"]["error"]) == []
    assert term.lower() not in lines[0].lower() and term.lower() not in repr(st["data"]["kamiden"]).lower()


# ------------------------------------------------ Part 3. the tested lenses


def test_the_tested_lens_rows_name_the_three_lenses_released_since_1_0_1():
    assert LENS_TESTED_VERSIONS == ("1.0.5", "1.1.0", "1.1.1", "1.2.0", "1.2.1")
