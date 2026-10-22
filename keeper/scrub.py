"""The agent-visible string boundary (SPEC P9, P3a, P1 frame rule).

Every FORMATTED string the keeper serves — status text, log lines, ledger
`class` values, error messages — passes through `assert_clean` before it
leaves the process, and every socket frame passes through
`assert_no_key_shaped` in both directions. The forbidden-word list below IS
the boundary: it is the list the scrub test enforces, so a new policy word
is a change to this file and to that test, never a silent change.

Two checks, deliberately separate:
  * forbidden words — policy language (the SPEC P9 and P3a lists) plus
    rate / budget / deadline phrasing. Whole-word, case-insensitive, on
    string VALUES (a JSON key such as `failsafeRest` is a schema name, not
    a formatted string; a value such as "rest: safe" is).
  * key-shaped values — 32-byte hex, with or without 0x, anywhere in a
    frame, key or value (SPEC P1, P6).
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Iterator

# SPEC P9 (with the A8 additions). Keep this list in one place.
FORBIDDEN_WORDS: tuple[str, ...] = (
    "predator",
    "enemy",
    "danger",
    "dangerous",
    "threat",
    "safe",
    "unsafe",
    "safety",
    "trusted",
    "saves",
    "efficient",
    "efficiently",
    "consider",
    "harmless",
    "armed",
    "unarmed",
    "hostile",
    "passive",
)

# SPEC P9: "MUSU/day rates, budgets, deadlines" — phrasing, not single words.
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bmusu\s*/\s*(day|hour|hr|h|min)\b", re.IGNORECASE),
    re.compile(r"\bper[-\s](day|hour|kami[-\s]day)\b", re.IGNORECASE),
    re.compile(r"\bbudget(s|ed)?\b", re.IGNORECASE),
    re.compile(r"\bdeadline(s)?\b", re.IGNORECASE),
    re.compile(r"\brun[-\s]window\b", re.IGNORECASE),
)

_WORD_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in FORBIDDEN_WORDS) + r")\b",
    re.IGNORECASE,
)

# A 32-byte hex string is key-shaped whatever surrounds it. An address is
# 20 bytes (40 hex) and a tx hash is 32 bytes too — a tx hash IS 64 hex, so
# hashes are carried as `0x…` strings under keys the frame scrub whitelists
# by NAME (`txHash`, `lastTxHash`, `block hash` never appears). See
# KEY_SHAPED_ALLOWED_KEYS.
KEY_SHAPED_RE = re.compile(r"(?<![0-9a-fA-F])(0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")

# Frame keys whose values are 32-byte hashes by definition, not keys. A
# value under any OTHER key that matches KEY_SHAPED_RE is refused.
# `schemaSha256` / `defaultsSha256` are SHA-256 digests of checked-in
# schema files (describe), 64 hex by construction.
KEY_SHAPED_ALLOWED_KEYS: frozenset[str] = frozenset({"txHash", "lastTxHash", "hash", "schemaSha256", "defaultsSha256"})


class ScrubError(ValueError):
    """A formatted string carried forbidden language, or a frame carried a
    key-shaped value. The offending TEXT is never included for the key
    case; for the word case only the matched word is named."""


def find_forbidden(text: str) -> list[str]:
    """Return every forbidden word / phrase found in `text` (may be empty)."""
    hits: list[str] = [m.group(0) for m in _WORD_RE.finditer(text)]
    for pat in FORBIDDEN_PATTERNS:
        hits.extend(m.group(0) for m in pat.finditer(text))
    return hits


def is_clean(text: str) -> bool:
    return not find_forbidden(text)


def assert_clean(text: str, where: str = "formatted string") -> str:
    """Raise ScrubError naming the matched words if `text` is not clean.
    Returns `text` unchanged so formatters can `return assert_clean(...)`."""
    hits = find_forbidden(text)
    if hits:
        raise ScrubError(f"{where} carries forbidden language: {sorted(set(h.lower() for h in hits))}")
    return text


def iter_strings(obj: Any, path: str = "$") -> Iterator[tuple[str, str, str | None]]:
    """Generate (path, string, parent_key) for every string value in a JSON-like
    object. Keys are yielded too, with parent_key None, so a key-shaped KEY is
    caught as well."""
    if isinstance(obj, str):
        yield path, obj, None
    elif isinstance(obj, dict):
        for k, v in obj.items():
            ks = str(k)
            yield f"{path}.<key>", ks, None
            if isinstance(v, str):
                yield f"{path}.{ks}", v, ks
            else:
                yield from iter_strings(v, f"{path}.{ks}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from iter_strings(v, f"{path}[{i}]")


def key_shaped_paths(obj: Any) -> list[str]:
    """Paths of every key-shaped value in a JSON-like object, ignoring values
    that sit directly under a hash-carrying key."""
    out: list[str] = []
    for path, s, parent_key in iter_strings(obj):
        if parent_key in KEY_SHAPED_ALLOWED_KEYS:
            continue
        if KEY_SHAPED_RE.search(s):
            out.append(path)
    return out


def assert_no_key_shaped(obj: Any, where: str = "frame") -> Any:
    """Raise ScrubError (naming only the PATH, never the value) if any
    key-shaped string sits in the object. Returns `obj` unchanged."""
    paths = key_shaped_paths(obj)
    if paths:
        raise ScrubError(f"{where} carries a key-shaped value at {paths[0]}")
    return obj


def forbidden_in(obj: Any) -> list[tuple[str, str]]:
    """(path, word) for every forbidden word in every string VALUE of a
    JSON-like object. Keys are schema names and are not scanned."""
    out: list[tuple[str, str]] = []
    for path, s, parent_key in iter_strings(obj):
        if path.endswith(".<key>"):
            continue
        for hit in find_forbidden(s):
            out.append((path, hit))
    return out


def collect_strings(objs: Iterable[Any]) -> list[str]:
    """Every string value across several JSON-like objects (test helper)."""
    out: list[str] = []
    for o in objs:
        for path, s, _ in iter_strings(o):
            if not path.endswith(".<key>"):
                out.append(s)
    return out
