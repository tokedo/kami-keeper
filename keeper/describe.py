"""SPEC P2 — `describe <type>`: the pinned JSON Schema file verbatim, plus
a description GENERATED from it (the harness tool text is derived from
this output, never hand-copied)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import profiles
from .books import load_schema, schema_defaults
from .protocol import STRATEGY_TYPES, KeeperError
from .scrub import assert_clean


def _range(p: dict[str, Any]) -> str:
    parts = []
    if "enum" in p:
        parts.append("one of " + "|".join(str(e) for e in p["enum"]))
    if "minimum" in p and "maximum" in p:
        parts.append(f"{p['minimum']}..{p['maximum']}")
    elif "minimum" in p:
        parts.append(f">= {p['minimum']}")
    elif "maximum" in p:
        parts.append(f"<= {p['maximum']}")
    if p.get("type") == "array":
        items = p.get("items", {})
        parts.append("array of " + str(items.get("type", "object")))
    return ", ".join(parts)


def render_text(strategy_type: str, schema: dict[str, Any], profile: str) -> str:
    required = set(schema.get("required", []))
    lines = [f"{strategy_type}: {schema.get('description', '')}".rstrip(), ""]
    for name, p in schema["properties"].items():
        unit = p.get("unit")
        default = p.get("default")
        flags = []
        if name in required:
            flags.append("required")
        if "default" in p:
            flags.append(f"default {json.dumps(default)}")
        for rule in p.get("x-defaultWhen") or []:
            # a default that depends on another key of the config
            if "sameAs" in rule:
                flags.append(f"default: the value of {rule['sameAs']}")
            else:
                flags.append(f"default {json.dumps(rule.get('default'))} when " + " and ".join(f"{k} is {json.dumps(v)}" for k, v in (rule.get("when") or {}).items()))
        gate = p.get("x-profile")
        if gate:
            flags.append(f"{gate} profile only" + ("" if profiles.allows(name, profile) else " (refused under this profile)"))
        unless = p.get("x-default-unless")
        if unless:
            # a default withheld by the config as given
            flags.append(f"default not applied when {', '.join(unless.get('present', []))} is set and none of {', '.join(unless.get('absent', []))}")
        rng = _range(p)
        head = f"- {name} ({p.get('type')}" + (f", {unit}" if unit else "") + ")"
        tail = "; ".join(x for x in (rng, ", ".join(flags)) if x)
        lines.append(f"{head}: {p.get('description', '')}" + (f" [{tail}]" if tail else ""))
    text = "\n".join(lines)
    return assert_clean(text, f"describe {strategy_type}")


def describe_type(strategy_type: str, profile: str) -> dict[str, Any]:
    if strategy_type not in STRATEGY_TYPES:
        raise KeeperError("BAD_ARGS", f"unknown strategy type {strategy_type!r}")
    schema = load_schema(strategy_type)
    raw = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "strategyType": strategy_type,
        "schema": schema,
        "schemaSha256": hashlib.sha256(raw).hexdigest(),
        "defaults": schema_defaults(strategy_type),
        "description": render_text(strategy_type, schema, profile),
    }
