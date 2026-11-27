# Changelog

SemVer (SPEC P11): a `describe` text or default change is MAJOR.

## 1.1.0 — 2026-11-27

MINOR: one keeper-level config key with an empty default. No book schema
key, no default moved, no `describe` text changed, the defaults lock
unchanged, no protocol frame field removed or renamed (`txCount` /
`txStatus` stay beside `txCountInTick` / `txStatusInTick`).

- **Standing kamis.** `standingKamis` (kami INDICES; env
  `KAMI_KEEPER_STANDING_KAMIS`; the same shape and parser as
  `standingOwners`; a bad entry is one error naming the key): a kami the
  operator names is an attacker of EVERY node a book watches, whether the
  node read shows it or not. Absent, it is a synthetic occupant — its
  owner, level and cooldown from one `kami <index>` read per pass and
  node — previewed in its turn like any attacker the policy previews
  (attackers never previewed go in index order, `previewsPerTick` a
  pass: the book's first pass under `previewPolicy killed-here`, several
  passes under `previewPolicy all` on a busy node; the book holds its
  kamis until the preview lands — no start, no rest on the bound, the
  floor still rests), refreshed at
  `previewRefreshSec` while absent (also when every line it serves is 0),
  classed `standing` (no new rendered name; `boundBy.hostileBy`
  `standing`), bound with `safetyMarginPct`, never remembered, dropped or
  expired. A listed owner still wins; a kill by it still renders
  `killed-here-<window>` inside the window; its owner is not made
  standing by it. For an attacker that is on a node only for the seconds
  of a kill and never at a tick: without the key no preview of it exists
  and nothing binds. Additive: the tick note `standingKamisAbsent`; one
  boot log line `standing kamis: [...]`; the keyword parameter
  `refresh_always` on `PreviewCache.due` / `next_due_at`. With the key
  empty (the default) nothing is read and nothing changes. The key is
  documented in SPEC P3 and `keeper/config.example.yaml`. Tests:
  `tests/test_phase2hc.py` (24); the fake lens serves an absent
  attacker's lines, as the daemon does.
- **An index the lens cannot read holds the book.** A listed index the
  lens does not know is still an attacker of every watched node — an
  unknown one, so each book holds its kamis (no start, no rest on the
  bound; the floor still rests) on every tick until the index is read
  or removed from the list. The only sign is on the tick row
  (`previewsRefused[]`, `attackerVitalsUnread[]`, the bound's
  `unknown[]`): check the list against the lens when you write it.
- **`attackerVitalsCalls` counts the standing reads.** The tick's count
  of `kami <index>` reads includes the read of an absent standing kami
  (one per pass and node, made by the first book of the pass on that
  node); it takes its unit of the `attackerVitalsPerTick` budget like
  any attacker read.
- **`txCount` / `txStatus` stay.** 1.0.2 said the old tick-summary names
  would "go with the next" release. They do not: removing a field an
  agent may read is a behaviour change on a default call (SPEC P11), so
  both names are kept, with the same values as `txCountInTick` /
  `txStatusInTick`, until the next MAJOR.

## 1.0.2 — 2026-11-24

PATCH: no default moved, no `describe` text changed, no protocol frame
field removed or renamed. Added fields only (`status`, the tick summary,
the CLI's printed `BAD_ARGS` frame).

- **A redeployed game system is found again without a restart.** When a
  pre-check reverts, the keeper re-reads that system's address from the
  World's registry (at most once a minute per system); if the address
  moved, the cache takes the new one, the call is judged once more
  there and that verdict stands, the transaction is signed to the new
  address, one log line names the system and the time, and `status`
  lists the move under `movedSystems` of the chain description (the
  newest 20). Before, the address was resolved once per process, and
  after a redeploy every action was refused until the keeper was
  restarted by hand. A pre-check that ends `rpc-unavailable` asks the
  registry nothing; a failed registry read changes nothing; a log
  callback that raises never reaches the pre-check. Tests: the chain
  layer, the keeper's log line, and the whole path through the live
  signer and the dry-run recorder. Not covered: the view-only getter
  address of the chain HP fallback is still resolved once per process.
- **The CLI prints a usage line with every `BAD_ARGS` answer** —
  `error.usage`, for example `usage: keeper grant --account <label>
  [--socket <path>]` — and `keeper --help` lists every command with its
  flags, the required ones unbracketed. A positional form (`keeper grant
  main`, `keeper describe auto`) is still refused, and the answer now
  shows the flag form. The CLI adds the line when it prints; the frames
  on the socket are unchanged.
- **The tick summary names what its transaction fields count:**
  `txCountInTick` / `txStatusInTick` — the tick's own burst and its
  fallback singles. A collect or a stop the tick pooled goes out after
  the tick row is written and was never among them (`collectPooled` /
  `stopPooled` count those; their `tx` rows are complete). `txCount` /
  `txStatus` carry the same values under the old names for this release
  and go with the next.
- **The `kamiden-unavailable` flag's transitions are logged, and
  `status` names the probe:** one keeper-level log line when the flag
  sets (with the lens's own `feedsDegraded` text) and one when it
  clears; `status` gains `kamiden: {probedAt, ok, error}`. The flag
  still gates nothing.
- `LENS_TESTED_VERSIONS` and SPEC D1 gain kami-lens 1.1.1 (`3fec1e0`),
  1.2.0 (`4c83ddd`) and 1.2.1 (`6828a11`)
  (run against, not suite-tested: SPEC D1).

## 1.0.1 — 2026-10-23

- `LENS_TESTED_VERSIONS` and SPEC D1 name kami-lens 1.0.5 (`e604290`) and 1.1.0 (`379d2e7`, tag `v1.1.0`; the row read `7131a52` until 2026-11-02) as the tested daemons; a `keeper` console script (≡ `python -m keeper`) and the package metadata that installs it.
- `NOTICE.md` for the game content the repository names, with a README line. No behaviour, no default and no `describe` text changed.

## 1.0.0 — 2026-10-22

- First public release.
