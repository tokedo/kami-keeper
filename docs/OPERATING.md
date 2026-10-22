# Operating kami-keeper

Mechanism only. `SPEC.md` is the contract; the section named in each
heading is the normative text.

## Profiles (SPEC P10)

`--profile lab | fleet` (env `KAMI_KEEPER_PROFILE`, config `profile`),
set at start-up and served as `meta.profile` on every answer. `lab` is
the default. Every profile-gated feature is a row of
`keeper/profiles.py`; a feature the profile does not allow answers
`PROFILE_FORBIDS`.

- `lab`: books and grants arrive over the socket only. The config file's
  `books:` and `grants:` are refused. `maxBooksPerAccount` is required:
  without it the process boots and serves read-only commands, but the
  tick engine does not start and `start` / `resume` answer `BAD_ARGS`.
- `fleet`: books and grants may also be declared in the config file.
  `maxBooksPerAccount: 0` means no cap.

## Declaring a book

A book is `{account, strategyType, config}`. `account` is the grant
LABEL; lens reads use the on-chain index the config names
(`accountIndex`). `describe --type <type>` serves the config schema.
Keys you leave out take their defaults from
`keeper/schemas/defaults.lock.json`.

Over the socket (both profiles):

```
python -m keeper start --socket <sock> --account alice --strategyType auto --config @book.json
```

`--config` takes JSON or `@file`. On `auto`, `start` adopts the kamis
already HARVESTING on the node.

In the config file (`fleet` only): a `books:` list of the same three
fields (see `keeper/config.example.yaml` and `deploy/examples/`).
Declared books start at boot, once their account is granted and the
lens answers. A declaration is matched to a running book by identity:
type and account, plus the kami (`harvestAndFeed`), the kami set and
node (`auto`), or the recipe set (`craft`). A restart therefore never
duplicates a book. `status.configBooks` shows declared / pending /
started / failed.

A book cannot change after `start`. To change its config, `stop` it and
start a new book; the new book gets a new id and a new ledger.

## Grant and revoke (SPEC P6, P8)

```
python -m keeper grant  --socket <sock> --account alice
python -m keeper revoke --socket <sock> --account alice
```

`grant` loads `<LABEL>_OPERATOR_KEY` from the configured backend
(`env` / `gcp-sm` / `keychain`) when it arrives, never at boot, and
re-reads it on every grant. Only the label crosses the socket. With no
grant the keeper signs nothing for that account (`NOT_GRANTED`).
`revoke` stops every running book of the account and returns the
quiescence proof. Under `fleet`, `grants:` in the config file grants at
boot.

## Socket commands (SPEC P1, P8)

`python -m keeper <cmd> --socket <path>`; `<cmd> --help` lists the
command's arguments. An undeclared argument is `BAD_ARGS`.

| command | does |
|---|---|
| `describe [--type T]` | protocol description; with a type, its config JSON Schema and the description generated from it |
| `books [--account A] [--all]` | the caller's books; `--all` (fleet) lists every account's |
| `start --account A --strategyType T --config C` | create and start a book |
| `stop --book B` / `stop --all --account A [--afterBurst]` | stop one book or all of an account; idempotent; returns the stopped set and a quiescence proof (pending nonce == confirmed nonce) |
| `park --book B` | `auto` only: start, feed and collect nothing; stop its kamis HARVESTING on the node in packs (at most 10 a pack, 5 packs a tick); keep reviving; stop itself once a valid read shows none HARVESTING and none DEAD |
| `pause --book B` / `resume --book B` | pause a running book / resume a paused one (refused against a stale or incomplete read; `PLAN_CONTRADICTED` while an external change stands) |
| `grant --account A` / `revoke --account A` | load / drop the account's operator key |
| `status [--book B]` | profile, engine, grants, degraded flags, every book's status |
| `logs [--book B] [--tail N]` | formatted log lines, newest last |
| `ledger --book B [--kind K] [--since BLOCK]` | ledger rows of a book |

A request is `{id, cmd, args}`; an answer is `{id, ok, data | error,
meta}`. The error codes are a closed set (SPEC P1).

## Dry run and armed (SPEC P8)

`armed: false` is the default: full perception and decisions, every
intent encoded and pre-checked by `eth_call`, nothing signed, ledger
rows marked `dry_run`. `armed: true` / `--armed` signs, and it needs
`rpcUrl`, `worldAddress` and `chainId`.

## Reading status (SPEC P3)

`status` is derived from the keeper's state when it answers, not from a
cache. Per book it shows:
- the state: `created → running → paused | stopped | failed`, with a
  `reason` on a terminal state and `pausedReason` on a pause;
- `lastTickAt`, `lastActAt`, `lastActBlock`, `lastTxHash` and
  `ticksSinceAct`;
- `degraded[]` and `openTx`;
- `strategy`, the type's own counts. On `auto` these are `kamis
  {harvesting, resting, dead, held, …}` and `owners` by rendered class.

At keeper level it shows `lens` (the state, the staleness in force,
`appliedThrough`, the version) and `blockClock`.

## Reading the ledger (SPEC P5)

Each book writes `<dataDir>/books/<id>/ledger.jsonl`, one JSON row a
line, append-only, with `schemaVersion` on every row. The row kinds
(`keeper/schemas/ledger-row.json`):

| kind | written |
|---|---|
| `tick` | the assessment summary, when it changes and at least every `tickHeartbeat` ticks |
| `decision` | each decision, with its kind and the parameters it rests on |
| `tx` | `stage: broadcast` before the receipt is awaited, then `stage: receipt`; `status` `success / reverted / unconfirmed / not_sent / dry_run` |
| `death` | a kami read DEAD, then its revive outcome |
| `kill` | a liquidate or cast the chain kill scan attributed to the node |
| `deviation` | an action the chain refused and the book's response (backoff) |
| `lifecycle` | `started`, `stopped_by`, `paused`, `resumed`, `external-change`, `readopted`, `parking`, `failed` |

`--kind` filters by kind. `--since <block>` exports rows whose lens
block is at or above it.

## Degradation (SPEC P4)

`degraded[]` names every cause. A flag is never silently absent.

- `lens-unavailable`, `lens-not-live`, `lens-stale:<blocks>`,
  `lens-degraded:<name>`, `lens-incomplete` gate the tick: no action, no
  external-change evaluation.
- Exceptions:
  - under `lens-stale` alone, a feeding book feeds at most once per
    `staleFeedCooldownSec`;
  - under a lens outage of at least `chainFallbackAfterSec`
    (`chainFallback: true`), a book reads its own kamis from the chain.
    A FEED book may then feed, and a REST book may stop at the rest
    floor, at most once per kami per `chainFeedCooldownSec`.
- The kill scan's `rpc-unavailable` and `ws-logs-gap:<N>` ride the tick
  and gate nothing.

## Running the suite

```
pytest -o addopts="" -m "not live"
pytest -o addopts="" -m live
```

`pyproject.toml` sets `addopts = "-q"`; `-o addopts=""` restores the
summary line. The `live` tests are read-only `eth_call` / `eth_getLogs`
checks against the public Yominet RPC, and they skip without network.
Python 3.13 is the test basis.
