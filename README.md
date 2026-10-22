# kami-keeper

A strategy process for Kamigotchi that runs beside a
kami-lens daemon. It reads the
world through the lens socket and the chain RPC, and signs a bounded,
whitelisted set of transactions with the operator keys it has been
granted. It serves a local control socket. It is deterministic: no model
calls. `SPEC.md` is the contract; the code implements it and the tests
enforce its Invariants table.

## Strategy types

A **book** is one strategy instance: `{account, strategyType, config}`.
Each book runs once per `tickSeconds` (default 30).

- **`harvestAndFeed`** — one kami on one node. Each tick it reads the
  kami from the lens. In `feedMode: hp` it feeds `foodItem` from
  inventory when HP is below `feedBelowHp`; in `feedMode: interval` it
  feeds every `feedInterval` seconds. It collects on an interval drawn
  per book from [`collectEveryMinSec`, `collectEveryMaxSec`], revives a
  DEAD kami with `reviveItems` from inventory, and with `failsafeRest`
  stops the harvest when no food can be fed. With `foodLadder` set it
  also reads the node (census, liquidation previews, kill scan) and picks
  the food item for each feed from the ladder.
- **`auto`** — a kami set on one node. `start` adopts the kamis already
  HARVESTING there and deploys the RESTING ones. Each tick it reads the
  account's party, the node census and the lens liquidation previews,
  and scans the chain for kills on the node. For each kami it computes a
  bound: the highest threshold the lens served for the attackers that
  bind (by owner class, SPEC P3a), plus `safetyMarginPct` or
  `passiveMarginPct`. A harvesting kami at or below the larger of the
  bound and `restFloorPct` is rested (`regenMethod: REST`, a harvest
  stop) or fed (`regenMethod: FEED`). A resting kami is redeployed at
  `resumeHpPct`. With `autoCollect` it collects at
  `bountyCollectThreshold`, and with `reviveOnDeath` it revives DEAD
  kamis.
- **`craft`** — an account and a recipe list. Each tick it reads the
  account's stamina and signs one craft of the first recipe, in priority
  order, that has an amount left and the stamina it needs above
  `staminaFloor`. A refusal parks that recipe.

Every config key, its unit, range and default is served by
`describe <type>` and pinned in `keeper/schemas/` (SPEC P2).

## Profiles (SPEC P10)

The profile is set at start-up and reported in every answer's `meta`.
The registry is `keeper/profiles.py`.

| | `lab` (default) | `fleet` |
|---|---|---|
| books | started over the socket (`start`) | may also be declared under `books:` in the config file; they start at boot |
| grants | over the socket only (`grant`) | may also be declared under `grants:` in the config file |
| `maxBooksPerAccount` | required: without it the process serves read-only commands and refuses `start` / `resume` | 0 = no cap |
| key backend default | `env` | `gcp-sm` |
| `books --all`, friends files, loopback status page, `readoptAfterBlocks` | refused (`PROFILE_FORBIDS`) | allowed |

## Install and test

```
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest -o addopts="" -m "not live"
.venv/bin/pytest -o addopts="" -m live
```

`-m live` runs read-only `eth_call` / `eth_getLogs` checks against the
public Yominet RPC; it needs network and skips without it. Python 3.13
is the test basis; the Dockerfile and the systemd unit run 3.11. The
`gcp-sm` backend also needs `requirements-gcp.txt`.

## Run

```
python -m keeper daemon --profile lab --data-dir <dir> --max-books-per-account <n>
python -m keeper status --socket <dir>/keeper.sock
python -m keeper --help              # every command; <cmd> --help for its arguments
```

The daemon runs dry by default: it perceives, decides, encodes every
intent and pre-checks it with `eth_call`, signs nothing, and marks its
ledger rows dry run. `--armed` (config `armed: true`) signs and needs
`rpcUrl`, `worldAddress` and `chainId`. Client commands: `describe books
start stop park pause resume grant revoke status logs ledger`; an
undeclared argument is `BAD_ARGS`. `docs/OPERATING.md` covers them.

## Configuration

Copy `keeper/config.example.yaml` as a template. It holds two
placeholder accounts, `alice` and `bob`, and one book of each type,
with every key at its default. Precedence is flag > env
(`KAMI_KEEPER_*`) > file > default. Every book key has a default in
`keeper/schemas/defaults.lock.json`, which is pinned by SHA: changing a
default is a MAJOR version. `deploy/examples/` has one `books:` fragment
per strategy type.

## Keys

Operator keys only: the keeper resolves `<LABEL>_OPERATOR_KEY` and never
reads an `*_OWNER_KEY`. Backends (`secretsBackend` / `--secrets-backend`):
`env` (a dotenv-style `keysFile`), `gcp-sm` (Google Secret Manager through
the host's service account, `gcpProject`), `keychain` (macOS login
Keychain item `<keychainServicePrefix><NAME>`, development only).
A key is loaded when `grant` arrives, never at boot, and is re-read on
every grant. Only the label crosses the socket. A key value never
reaches the environment, argv, logs, `status`, the ledger or an
exception message.

## Ledger (SPEC P5)

Each book has an append-only `ledger.jsonl` at
`<dataDir>/books/<id>/ledger.jsonl`, and every row carries
`schemaVersion`. A `tick` row is written when the assessment summary
changes and at least every `tickHeartbeat` ticks; every transaction has
a `tx` row at the `broadcast` stage before its receipt is awaited and
one at the `receipt` stage after. Read it with
`python -m keeper ledger --book <id> [--kind <kind>] [--since <block>]`.

## What it never does (SPEC P9)

- No model calls, and no network beyond the chain RPC and the lens socket.
- No Kamiden client, no Kamibots API, no oracle token.
- It never attacks, casts, transfers, trades or moves. It never buys:
  revives use items from inventory only. The send path signs harvest
  start / stop / collect, use-item restricted to the book's food and
  revive items, and craft. Everything else is refused before signing.
- No policy language in agent-visible strings. Status, logs and errors
  state observation, action and parameters, and a forbidden-word scrub
  test covers every formatted string.
- No book-level field reveals a run window.

## License

MIT — see `LICENSE`.
