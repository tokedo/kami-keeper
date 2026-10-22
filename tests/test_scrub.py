"""SPEC P9 / P3a — the forbidden-word scrub over every formatter, and a
static scan of the package's string literals."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from keeper import owner_class
from keeper.protocol import ERROR_CODES, KeeperError
from keeper.scrub import FORBIDDEN_WORDS, ScrubError, assert_clean, collect_strings, find_forbidden, forbidden_in

PKG = Path(__file__).resolve().parents[1] / "keeper"

# SPEC P8 names the dry-run switch `armed: false`. These three literals are
# that config key, its env name and its CLI flag — identifiers, never
# formatted (the served field is `dryRun`). Nothing else is exempt.
IDENTIFIER_LITERALS = {"armed", "--armed", "KAMI_KEEPER_ARMED"}


def test_forbidden_list_is_the_spec_list():
    assert {"predator", "enemy", "danger", "threat", "safe", "unsafe", "trusted", "saves", "efficient", "consider", "harmless", "armed", "hostile", "passive"} <= set(FORBIDDEN_WORDS)
    assert find_forbidden("the kami is safe here") == ["safe"]
    assert find_forbidden("failsafeRest safetyMarginPct hostileWindowHours") == []  # schema key names are not words
    assert find_forbidden("about 400 MUSU/day; the budget is tight; deadline noon") != []
    with pytest.raises(ScrubError):
        assert_clean("a predator arrived")
    with pytest.raises(ScrubError):
        KeeperError("BAD_ARGS", "unsafe to proceed")


def test_every_owner_class_renders_to_the_closed_set():
    seen = set()
    for cls in owner_class.OwnerClass:
        name = owner_class.render(cls, 24)
        assert owner_class.is_observation_name(name), name
        assert find_forbidden(name) == []
        seen.add(name)
    assert seen == {"listed", "no-threshold", "threshold-no-kill", "standing", "killed-here-24h"}  # `standing` is the fifth


def test_every_formatter_output_is_clean(env):
    """Drive every agent-visible surface and scrub every string value."""
    frames = [env.grant(), env.keeper.handle("describe")]
    for t in ("harvestAndFeed", "auto", "craft"):
        frames.append(env.keeper.handle("describe", {"type": t}))
    s = env.start_haf()
    bid = s["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    env.lens.set_kami(1001, hp=(40, 200))
    env.keeper.tick_book(book)  # feed
    env.lens.set_kami(1001, state="RESTING", node=None)
    for _ in range(3):
        env.keeper.tick_book(book)  # external change on 3 valid ticks -> paused (lab)
    env.lens.set_kami(1001, state="HARVESTING", node=63)  # resume is refused: still contradicted
    frames += [
        env.keeper.handle("status"), env.keeper.handle("books", {"account": "lab"}), env.keeper.handle("logs"),
        env.keeper.handle("ledger", {"book": bid}), env.keeper.handle("resume", {"book": bid}),
        env.keeper.handle("stop", {"all": True, "account": "lab"}), env.keeper.handle("revoke", {"account": "lab"}),
        env.keeper.handle("books", {"all": True}), env.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": {"kamiIndices": [1], "nodeIndex": 62}}),
        env.keeper.handle("pause", {"book": "nope"}), env.keeper.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": []}}),
        env.keeper.handle("describe", {"type": "x"}), env.keeper.handle("status", {"bogus": 1}),
    ]
    hits = [(p, w) for f in frames for (p, w) in forbidden_in(f)]
    assert hits == []
    assert collect_strings(frames)  # the surface was non-empty


def test_error_paths_for_every_code_are_clean(env):
    """Every KeeperError raised by the package passes the scrub at
    construction; here the codes the lab profile can trigger are exercised."""
    codes = set()
    env.keeper.engine_refusals.append("maxBooksPerAccount is required under this profile and is absent")
    codes.add(env.keeper.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 1}]}})["error"]["code"])
    env.keeper.engine_refusals.clear()
    codes.add(env.keeper.handle("start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 1}]}})["error"]["code"])  # NOT_GRANTED
    codes.add(env.keeper.handle("pause", {"book": "x"})["error"]["code"])  # NOT_FOUND
    codes.add(env.keeper.handle("books", {"all": True})["error"]["code"])  # PROFILE_FORBIDS
    assert codes == {"BAD_ARGS", "NOT_GRANTED", "NOT_FOUND", "PROFILE_FORBIDS"}
    assert set(ERROR_CODES) >= codes


def test_package_string_literals_carry_no_forbidden_word():
    """Static: every non-docstring string literal in keeper/ (scrub.py
    excepted — it holds the list) is clean, so no formatter can emit one."""
    for p in sorted(PKG.rglob("*.py")):
        # scrub.py holds the list; owner_class.py holds the INTERNAL policy
        # identifiers SPEC P3a explicitly permits ("friend / harmless /
        # armed-passive / hostile") — its formatter is the boundary, and
        # test_every_owner_class_renders_to_the_closed_set + the runtime
        # scrub of every frame bound what leaves it.
        if p.name in ("scrub.py", "owner_class.py"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                    docstrings.add(id(node.body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                if node.value in IDENTIFIER_LITERALS:
                    continue
                hits = find_forbidden(node.value)
                assert hits == [], f"{p}:{node.lineno}: {hits} in {node.value!r}"
    for s in sorted((PKG / "schemas").glob("*.json")):
        assert forbidden_in(json.loads(s.read_text())) == [], s
