"""SPEC P1 — schema tests on every command in both directions; undeclared
arguments refused on the socket AND the CLI; the meta key set; the
closed error-code set; describe == the pinned schema file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from keeper import cli
from keeper.books import SCHEMA_DIR
from keeper.protocol import COMMANDS, ERROR_CODES, META_KEYS, STRATEGY_TYPES, KeeperError, validate_request

ALL_CMDS = sorted(COMMANDS)


def test_command_set_is_the_spec_set():
    assert set(COMMANDS) == {"describe", "books", "start", "stop", "park", "pause", "resume", "grant", "revoke", "status", "logs", "ledger"}  # `park` is a command too


def test_error_codes_are_the_closed_set():
    assert set(ERROR_CODES) == {"NOT_READY", "NOT_FOUND", "BAD_ARGS", "NOT_GRANTED", "LENS_UNAVAILABLE", "LENS_STALE", "RPC_UNAVAILABLE", "SIGNER_BUSY", "BOOK_CAP", "PROFILE_FORBIDS", "PLAN_CONTRADICTED", "INTERNAL"}
    with pytest.raises(ValueError):
        KeeperError("NOPE", "x")


@pytest.mark.parametrize("cmd", ALL_CMDS)
def test_undeclared_argument_refused_by_router(cmd):
    with pytest.raises(KeeperError) as ei:
        validate_request(cmd, {"bogus": 1})
    assert ei.value.code == "BAD_ARGS"
    assert "undeclared argument 'bogus'" in ei.value.message


@pytest.mark.parametrize("cmd", ALL_CMDS)
def test_undeclared_argument_refused_by_cli_with_the_same_wording(cmd, capsys):
    rc = cli.main([cmd, "--socket", "/nonexistent.sock", "--bogus", "1"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_BAD_ARGS
    assert out["ok"] is False and out["error"]["code"] == "BAD_ARGS"
    assert f"{cmd}: undeclared argument 'bogus'" in out["error"]["message"]
    assert set(out["meta"]) == META_KEYS


@pytest.mark.parametrize("cmd", ALL_CMDS)
def test_every_command_answers_with_the_meta_key_set_both_ways(env, cmd):
    """Every command, valid or not, answers with EXACTLY the meta keys."""
    env.grant()
    valid_args = {
        "describe": {}, "books": {"account": "lab"}, "start": {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}},
        "stop": {"all": True, "account": "lab"}, "park": {"book": "missing"}, "pause": {"book": "missing"}, "resume": {"book": "missing"},
        "grant": {"account": "lab"}, "revoke": {"account": "lab"}, "status": {}, "logs": {}, "ledger": {"book": "missing"},
    }[cmd]
    ok_frame = env.keeper.handle(cmd, valid_args)
    bad_frame = env.keeper.handle(cmd, {"bogus": 1})
    for frame in (ok_frame, bad_frame):
        assert set(frame["meta"]) == META_KEYS, frame
        assert set(frame) == ({"id", "ok", "data", "meta"} if frame["ok"] else {"id", "ok", "error", "meta"})
    assert bad_frame["error"]["code"] == "BAD_ARGS"


def test_required_and_typed_arguments(env):
    f = env.keeper.handle("start", {"account": "lab"})
    assert f["error"]["code"] == "BAD_ARGS" and "missing required argument 'strategyType'" in f["error"]["message"]
    f = env.keeper.handle("ledger", {"book": "x", "since": "notint"})
    assert f["error"]["code"] == "BAD_ARGS" and "must be integer" in f["error"]["message"]
    f = env.keeper.handle("describe", {"type": "nope"})
    assert f["error"]["code"] == "BAD_ARGS"


def test_meta_basis_is_null_without_a_lens_read_and_filled_with_one(env):
    d = env.keeper.handle("describe")
    assert d["meta"]["lensBlock"] is None and d["meta"]["lensReconciledThrough"] is None
    s = env.keeper.handle("status")
    assert isinstance(s["meta"]["lensBlock"], int) and isinstance(s["meta"]["lensReconciledThrough"], int)
    assert s["meta"]["profile"] == "lab"


@pytest.mark.parametrize("stype", STRATEGY_TYPES)
def test_describe_serves_exactly_the_pinned_schema_file(env, stype):
    served = env.keeper.handle("describe", {"type": stype})
    assert served["ok"], served
    on_disk = json.loads((SCHEMA_DIR / f"{stype}.json").read_text())
    assert served["data"]["schema"] == on_disk
    assert served["data"]["description"].startswith(stype + ":")
    # the generated text names every key with its default where it has one
    for k, p in on_disk["properties"].items():
        assert f"- {k} (" in served["data"]["description"]
        if "default" in p:
            assert f"default {json.dumps(p['default'])}" in served["data"]["description"]


def test_describe_protocol_lists_every_command_and_the_registry(env):
    d = env.keeper.handle("describe")["data"]
    assert {c["name"] for c in d["commands"]} == set(COMMANDS)
    assert d["errorCodes"] == list(ERROR_CODES)
    assert set(d["metaKeys"]) == META_KEYS
    assert d["requestTimeoutSec"] > 0
    assert {r["feature"] for r in d["profiles"]} >= {"book-cap-required", "readoptAfterBlocks"}


def test_common_keys_identical_across_type_schemas():
    schemas = {t: json.loads((SCHEMA_DIR / f"{t}.json").read_text()) for t in STRATEGY_TYPES}
    for key in ("tickSeconds", "tickHeartbeat", "maxStalenessBlocks", "externalChangeTicks", "readoptAfterBlocks", "staleFeedCooldownSec"):
        ref = schemas["harvestAndFeed"]["properties"][key]
        for t in STRATEGY_TYPES:
            assert schemas[t]["properties"][key] == ref, (t, key)


def test_cli_help_and_version(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    assert "daemon" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "kami-keeper" in capsys.readouterr().out
