"""kami-keeper — a deterministic strategy process next to a kami-lens daemon.

SPEC.md is the contract. The package holds the control socket, the book
store, the ledger, the profile registry, the secrets adapter, the lens
client with its staleness gate, the strategies, and the signer (dry-run by
default; it signs only when the keeper is armed).

Python 3.11 is the target interpreter (SPEC D4); nothing here uses a
3.12+ construct.
"""

from __future__ import annotations

# SPEC P11: SemVer. `KEEPER_VERSION` is the package version; `SCHEMA_VERSION`
# stamps every response and ledger row.
KEEPER_VERSION = "1.0.0"
SCHEMA_VERSION = "0.1.1"

# SPEC D1: the oldest lens the keeper is written against.
LENS_MIN_VERSION = "0.6.1"

# SPEC D1: the lens versions this code is TESTED against, both of them in
# one build — 0.6.3 (no applied mark, none of the 1.0.0 options) and 1.0.0.
# Every 1.0.0 feature is gated on `status.version` (keeper/lens.py
# `features_for`), so the same client serves either.
LENS_TESTED_VERSIONS = ("0.6.3", "1.0.0")

__all__ = ["KEEPER_VERSION", "SCHEMA_VERSION", "LENS_MIN_VERSION", "LENS_TESTED_VERSIONS"]
