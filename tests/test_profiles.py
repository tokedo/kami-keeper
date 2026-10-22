"""SPEC P10 — registry completeness: every profile branch in the codebase
maps to a row; every forbidden row answers PROFILE_FORBIDS under lab;
`books` under lab never lists another account's book; lab is default."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from keeper import profiles
from keeper.books import SCHEMA_DIR
from keeper.protocol import STRATEGY_TYPES

PKG = Path(__file__).resolve().parents[1] / "keeper"


def test_lab_is_the_default_and_fleet_must_be_asked_for(make_keeper):
    assert profiles.DEFAULT_PROFILE == "lab"
    e = make_keeper(flags={"profile": None})
    assert e.keeper.handle("status")["meta"]["profile"] == "lab"
    with pytest.raises(ValueError):
        profiles.validate_profile("prod")


def test_every_profile_branch_maps_to_a_registry_row():
    """No `== "lab"` / `== "fleet"` outside profiles.py; every feature name
    passed to profiles.allows/value/require is a registry row; every
    `x-profile` key in a schema is a row."""
    literal = re.compile(r'profile\s*(==|!=|in)\s*[\("\']')
    for p in sorted(PKG.rglob("*.py")):
        if p.name == "profiles.py":
            continue
        src = p.read_text(encoding="utf-8")
        for i, line in enumerate(src.splitlines(), 1):
            assert not literal.search(line), f"{p}:{i}: profile compared directly; use the registry"
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("allows", "value", "require", "row") and isinstance(node.func.value, ast.Name) and node.func.value.id == "profiles":
                arg = node.args[0]
                if isinstance(arg, ast.Constant):
                    assert arg.value in profiles.FEATURES, f"{p}:{node.lineno}: {arg.value!r} is not a registry row"
    for t in STRATEGY_TYPES:
        schema = json.loads((SCHEMA_DIR / f"{t}.json").read_text())
        for k, v in schema["properties"].items():
            if "x-profile" in v:
                assert k in profiles.FEATURES, f"{t}.{k}: x-profile without a registry row"
                assert profiles.value(k, v["x-profile"]) is True


def test_registry_rows_match_the_spec_table():
    rows = {r.feature: r for r in profiles.REGISTRY}
    assert rows["key-backend-default"].fleet == "gcp-sm" and rows["key-backend-default"].lab == "env"
    assert rows["grant-in-config"].lab is False and rows["grant-in-config"].fleet is True
    assert rows["friends-files"].lab is False
    assert rows["external-change-readopt"].lab is False
    assert rows["loopback-status-page"].lab is False
    assert rows["book-cap-required"].lab is True and rows["book-cap-required"].fleet is False
    assert rows["readoptAfterBlocks"].lab is False


def test_readopt_after_blocks_is_refused_under_lab_and_allowed_under_fleet(make_keeper):
    auto_cfg = {"kamiIndices": [1001], "nodeIndex": 62, "accountIndex": 4242, "readoptAfterBlocks": 240}
    lab = make_keeper()
    lab.grant()
    r = lab.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": auto_cfg})
    assert r["ok"] is False and r["error"]["code"] == "PROFILE_FORBIDS"
    from keeper.books import ProfileForbids, validate_config
    with pytest.raises(ProfileForbids):
        validate_config("auto", auto_cfg, "lab")
    full = validate_config("auto", auto_cfg, "fleet")
    assert full["readoptAfterBlocks"] == 240


def test_books_all_is_profile_forbids_under_lab_and_allowed_under_fleet(make_keeper):
    lab = make_keeper()
    r = lab.keeper.handle("books", {"all": True})
    assert r["error"]["code"] == "PROFILE_FORBIDS"
    fleet = make_keeper(profile="fleet", cap=None)
    assert fleet.keeper.handle("books", {"all": True})["ok"]


def test_config_grants_status_page_friends_files_refused_under_lab(make_keeper):
    e = make_keeper(flags={"grants": ["lab"], "statusPage": "127.0.0.1:8765", "friendsFiles": ["/x/roster.yaml"]})
    refusals = e.keeper.engine_refusals
    assert any("grants" in r for r in refusals) and any("statusPage" in r for r in refusals) and any("friendsFiles" in r for r in refusals)
    assert e.keeper.signer.grants() == []  # no grant taken from config under lab
    f = make_keeper(profile="fleet", cap=None, grants=["lab"])
    assert f.keeper.engine_refusals == [] and f.keeper.signer.grants()[0]["account"] == "lab"


def test_books_under_lab_never_lists_another_accounts_book(make_keeper, tmp_path):
    from conftest import write_keys_file
    kf = tmp_path / "two.env"
    kf.write_text("A_OPERATOR_KEY=0x" + "11" * 32 + "\nB_OPERATOR_KEY=0x" + "22" * 32 + "\n")
    e = make_keeper(keys_file=kf)
    assert e.keeper.handle("grant", {"account": "a"})["ok"] and e.keeper.handle("grant", {"account": "b"})["ok"]
    sa = e.start_haf(account="a")
    sb = e.start_haf(account="b")
    assert sa["ok"] and sb["ok"], (sa, sb)
    a_books = e.keeper.handle("books", {"account": "a"})["data"]["books"]
    b_books = e.keeper.handle("books", {"account": "b"})["data"]["books"]
    assert {b["account"] for b in a_books} == {"a"} and {b["account"] for b in b_books} == {"b"}
    assert e.keeper.handle("books", {})["error"]["code"] == "BAD_ARGS"
