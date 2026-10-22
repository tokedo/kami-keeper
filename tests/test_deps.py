"""SPEC P9 — dependency scan: no model client, no Kamiden, no Kamibots,
no oracle token, no harness import; the requirement set is pinned."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "keeper"

ALLOWED_THIRD_PARTY = {"web3", "eth_account", "jsonschema", "yaml", "google"}
FORBIDDEN_NAMES = re.compile(r"\b(kamibots|grpc|anthropic|openai|requests|httpx|aiohttp|KAMI_ORACLE_TOKEN|KAMIBOTS_API_KEY|kami_harness)\b")


def _stdlib(name: str) -> bool:
    return name in sys.stdlib_module_names


def test_imports_are_stdlib_or_the_allowed_set():
    for p in sorted(PKG.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for n in names:
                assert _stdlib(n) or n in ALLOWED_THIRD_PARTY or n == "keeper", f"{p}: import {n}"


def test_no_forbidden_names_in_the_package():
    for p in sorted(PKG.rglob("*.py")):
        src = p.read_text(encoding="utf-8")
        m = FORBIDDEN_NAMES.search(src)
        assert m is None, f"{p}: {m.group(0)}"


def test_requirements_are_the_small_set_with_hashes():
    req = (ROOT / "requirements.in").read_text()
    names = {l.split(">")[0].split("=")[0].strip().lower() for l in req.splitlines() if l and not l.startswith("#")}
    assert names == {"web3", "pyyaml", "jsonschema"}
    pinned = (ROOT / "requirements.txt").read_text()
    assert "--hash=sha256:" in pinned
    for l in pinned.splitlines():
        if l and not l.startswith(("#", " ", "-")) and "==" in l:
            assert l.rstrip().endswith("\\"), f"unhashed line: {l}"
    gcp = (ROOT / "requirements-gcp.in").read_text()
    assert "google-cloud-secret-manager" in gcp
