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
KEEPER_VERSION = "1.0.1"
SCHEMA_VERSION = "0.1.1"

# SPEC D1: the oldest lens the keeper is written against.
LENS_MIN_VERSION = "0.6.1"

# SPEC D1: the kami-lens versions this build is TESTED against — 1.0.5
# (`e604290`) and 1.1.0 (`379d2e7`), the suite run against each daemon. The
# row states what was tested, not what is required: every 1.0.0 feature is
# gated on `status.version` (keeper/lens.py `features_for`, all flags on at
# MAJOR >= 1), so the same client serves either, and a pre-1.0.0 daemon (no
# applied mark, none of the 1.0.0 options) is still served — the suite's
# fake lens covers that contract shape as well.
LENS_TESTED_VERSIONS = ("1.0.5", "1.1.0")

__all__ = ["KEEPER_VERSION", "SCHEMA_VERSION", "LENS_MIN_VERSION", "LENS_TESTED_VERSIONS"]
