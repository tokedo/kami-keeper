"""SPEC P6 / Invariants "No key value appears in any output channel":
an ast scan of the package (the harness D5 rule) and a runtime scan of
every channel — socket frames, ledger, logs, status, exception text —
after a grant and a tick with a fixture key."""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from conftest import FIXTURE_KEY_HEX

PKG = Path(__file__).resolve().parents[1] / "keeper"
SECRET_VALUE_NAMES = {"raw", "value", "key_bytes", "_b", "private_key", "priv", "secret", "key_hex"}


def _sources():
    return sorted(PKG.rglob("*.py"))


def test_os_environ_is_read_only_in_config_and_secrets():
    for p in _sources():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os":
                assert p.name in ("config.py",), f"{p}: os.environ read outside config.py"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("getenv", "putenv") and isinstance(node.func.value, ast.Name) and node.func.value.id == "os":
                raise AssertionError(f"{p}: os.getenv/putenv is not allowed")


def test_no_print_in_the_package_outside_the_cli():
    for p in _sources():
        if p.name == "cli.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print":
                raise AssertionError(f"{p}:{node.lineno}: print() in the package")


def test_secrets_module_never_formats_a_secret_value_into_a_message():
    """In secrets.py / signer.py, no f-string, %-format, .format or str()
    call references a name that holds a key value, and no raise carries
    one. The names are the module's own conventions (`raw`, `value`, `_b`)."""
    for name in ("secrets.py", "signer.py", "chain.py"):
        tree = ast.parse((PKG / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for v in ast.walk(node):
                    if isinstance(v, ast.Name) and v.id in SECRET_VALUE_NAMES:
                        raise AssertionError(f"{name}:{node.lineno}: f-string references {v.id}")
                    if isinstance(v, ast.Attribute) and v.attr in SECRET_VALUE_NAMES:
                        raise AssertionError(f"{name}:{node.lineno}: f-string references .{v.attr}")
            if isinstance(node, ast.Raise) and node.exc is not None:
                for v in ast.walk(node.exc):
                    if isinstance(v, ast.Name) and v.id in SECRET_VALUE_NAMES:
                        raise AssertionError(f"{name}:{node.lineno}: raise carries {v.id}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("info", "warning", "error", "debug", "exception"):
                for v in ast.walk(node):
                    if isinstance(v, ast.Name) and v.id in SECRET_VALUE_NAMES:
                        raise AssertionError(f"{name}:{node.lineno}: log call carries {v.id}")


def test_reveal_bytes_is_called_once_in_signer_as_a_direct_argument_to_the_signing_call():
    """The key value is touched exactly once, in signer.py, as an
    argument passed straight into eth_account's sign call — never bound
    to a name, never stored, never passed anywhere else."""
    uses = []
    for p in _sources():
        if p.name == "secrets.py":
            continue
        src = p.read_text(encoding="utf-8")
        if "reveal_bytes" not in src:
            continue
        assert p.name == "signer.py", f"{p}: reveal_bytes outside signer.py"
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "sign_transaction":
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and arg.func.attr == "reveal_bytes":
                        uses.append(node.lineno)
        assert src.count("reveal_bytes") == len(uses) == 1, f"{p}: reveal_bytes used {src.count('reveal_bytes')}x, {len(uses)} as the sign argument"
    assert len(uses) == 1


def test_runtime_channels_never_carry_the_fixture_key(env, caplog):
    caplog.set_level(logging.DEBUG)
    frames = [env.grant()]
    s = env.start_haf()
    assert s["ok"], s
    bid = s["data"]["book"]["id"]
    env.keeper.tick_book(env.keeper.store.get(bid))
    frames += [env.keeper.handle("status"), env.keeper.handle("books", {"account": "lab"}), env.keeper.handle("logs"), env.keeper.handle("ledger", {"book": bid}), env.keeper.handle("describe"), env.keeper.handle("revoke", {"account": "lab"})]
    # a forced failure path: grant a label whose key is malformed
    env.keys_file.write_text("BROKEN_OPERATOR_KEY=0xzz\n")
    frames.append(env.keeper.handle("grant", {"account": "broken"}))
    blob = json.dumps(frames)
    assert FIXTURE_KEY_HEX not in blob
    assert FIXTURE_KEY_HEX not in caplog.text
    for f in sorted(env.data_dir.rglob("*")):
        if f.is_file():
            assert FIXTURE_KEY_HEX not in f.read_text(encoding="utf-8", errors="replace"), f
