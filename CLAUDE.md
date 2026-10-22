# CLAUDE.md — kami-keeper

Read `SPEC.md` first. It is the contract; code implements it, tests enforce
its Invariants table, and every row without a test says `unenforced`.

Rules that are not negotiable in this repo:
- No model calls, no network beyond the chain RPC and the lens socket.
- Operator keys only; a key value never reaches env, argv, logs, status,
  ledger or exceptions. Load-on-grant, never at boot.
- The send path has a systems whitelist (SPEC P6). Nothing else is signed.
- `lab` is the default profile. Every profile-gated feature is a registry row.
- Agent-visible strings are observation + action + parameters. The scrub
  test's forbidden-word list is the boundary.
- Thresholds come from the lens preview; the chain-backed HP read is a
  fallback for feeds and floor stops only.
- A default change is MAJOR. `describe` serves the schema; descriptions are
  generated from it.

Layout: `keeper/` package (socket, cli, lens client, secrets, signer,
ledger, profiles, strategies/), `tests/`, `scripts/`, `Dockerfile`,
`deploy/` (systemd unit).
