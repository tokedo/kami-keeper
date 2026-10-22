---
module: kami-keeper
version: 1
---

# kami-keeper — SPEC v1.0

A strategy process that runs next to a kami-lens daemon, reads the world
through the lens socket and the chain RPC, and signs a bounded set of
transactions with operator keys it has been granted. It serves a local
control socket. It is deterministic: no model calls, ever.

This contract describes a daemon that has run armed; the measurement
record is held by the operator.

Enforcement cells name the test or gate that checks the row, or
`unenforced` (visible backlog).

# Provides

## P1 — Control socket protocol

- Unix socket, path from `KAMI_KEEPER_SOCKET` (default `<dataDir>/keeper.sock`).
- One request, one response per connection, request timeout stated in
  `describe`. Same framing as the lens so a harness client is a copy of
  `_lens_request`.
- Request `{id, cmd, args}`; response `{id, ok: true, data, meta}` or
  `{id, ok: false, error: {code, message}}`.
- `meta` on every answer: `keeperVersion`, `schemaVersion`, `servedAt`,
  `profile`, and the perception basis — `lensBlock`,
  `lensReconciledThrough`, `lensAppliedThrough` — always present, `null`
  when the answer rests on no lens read.
  `lensAppliedThrough` (the lens 1.0.0 applied mark: every write of every
  block up to it is in the mirror) is null on EVERY answer taken against
  a 0.6.3 daemon, which serves no such mark. The key set of `meta` is
  asserted in both directions by a test.
- Error codes (closed set): `NOT_READY`, `NOT_FOUND`, `BAD_ARGS`,
  `NOT_GRANTED`, `LENS_UNAVAILABLE`, `LENS_STALE`, `RPC_UNAVAILABLE`,
  `SIGNER_BUSY`, `BOOK_CAP`, `PROFILE_FORBIDS`, `PLAN_CONTRADICTED`,
  `INTERNAL`.
  `LENS_STALE` is a REFUSAL on `start` (and `resume`) against a lens
  whose `reconciledThrough` lags by more than `maxStalenessBlocks`, whose
  `status.degraded` carries `stream-stalled…` / `unhealed-ranges…`, or
  whose answer for the command's adoption read is INCOMPLETE (P4);
  an already-running book never errors on staleness — it stops acting
  (with the P4 feed exception) and raises the `lens-stale:<blocks>` flag
  (P4). `PLAN_CONTRADICTED` is the refusal on `resume` of a book
  paused by `external-change` while a VALID read still shows the world
  contradicting the plan (P7).
- No key-shaped value ever appears in a socket frame in either
  direction; a frame test asserts it.
- Undeclared arguments are refused with `BAD_ARGS` on the socket AND
  the CLI (one router, both entry points).
- Commands: `describe [type]`, `books`, `start`, `stop`, `park`, `pause`,
  `resume`, `grant`, `revoke`, `status`, `logs`, `ledger`.

Enforcement: schema tests on every command in both directions;
undeclared-argument test over the whole registry on both entry points.

## P2 — Book schema

- A book = `{id, account, strategyType, config, state, createdAt}`.
  Exactly three `strategyType` values: `harvestAndFeed`, `auto`, `craft`.
- Per-type config is a JSON Schema with every key's type, unit, DEFAULT
  and range stated. `describe <type>` serves it; the harness description
  is generated from it, never hand-copied. Defaults are pinned by SHA;
  a default change is MAJOR. A key may carry `x-default-unless
  {present[], absent[]}`: its default is NOT materialised into the stored
  config when the config as given sets every `present` key and none of
  the `absent` ones; `describe` renders the rule on the key's line. One
  use: the `harvestAndFeed` collect window below.
  A default may depend on another key of
  the same config: the schema annotation `x-defaultWhen` is an ordered
  list of rules, `{when: {key: value, …}, default: v}` or `{sameAs:
  key}`, evaluated at `start` AFTER the plain defaults are in, in schema
  order (a rule may read a key an earlier rule filled); the first rule
  that holds fills the key, else its plain `default`. An explicit key
  always wins. `describe` states the rules, and the lock pins them beside
  the plain defaults (`conditionalDefaults`, inside the same SHA): a rule
  change is a default change. A stored config is complete as of ITS
  `start`: a key that did not exist then is absent, and the strategy reads
  an absent key as the behaviour without that key — never as the
  conditional default, which applies at `start` only.
- Binding: `harvestAndFeed` → one kami; `auto` → a kami set on one
  node, at most `maxKamisPerBook` (config table, default 50, range 1–1000:
  one book may hold all of an account's kamis on a node);
  `craft` → an account and a recipe list.
- A book is immutable after `start` except through `stop`. Adding a
  kami is a new book.
- `start` on `auto` ADOPTS kamis already HARVESTING on the node and
  deploys RESTING ones.
- Config schemas (the schema files pin them):

| type | keys (default) |
|---|---|
| `harvestAndFeed` | `kamiIndex`; `nodeIndex`; `foodItem` (required); `feedMode` `hp\|interval` (`hp`); `feedBelowHp` absolute HP (required when `hp`); `feedInterval` s (3600, `interval` mode); the collect window `collectEveryMinSec` s ≥ 60 (108000) and `collectEveryMaxSec` s ≥ 60 (144000) — the book draws each collect interval uniform in [min, max] (whole seconds, both ends included) when its collect clock starts and again after every collect or start that LANDED (P6 clock rule), so the books of one node do not collect in step; `min > max` is a config refusal (`BAD_ARGS`, the schema-refusal path); `collectEvery` s ≥ 60, NO default: set WITHOUT either window key it pins the period (min = max = `collectEvery`) and the window defaults are not materialised (`x-default-unless`), set with either window key it is ignored — so a stored config that carries `collectEvery` and no window key keeps that fixed period until it is re-declared; `failsafeRest` (true); `resumeHpPct` (100 — rest to full, one deploy per full harvest); `reviveItems[]` optional, inventory only, no default; `reviveRetrySec` 30–3600 (**200**) — after ANY revive attempt (offered, refused, reverted, lost, or skipped for a missing item) the kami, if the world still reads it DEAD, is offered a revive again once this many seconds have passed, and a plan entry with no `reviveRetryAt` key is offered once on the first tick that reads it DEAD; Spirit Glue (item 19001) locks a kami for 180 s, so a revive can be refused pre-send; `feedCooldownSec` s (300) — after a feed lands the kami is not fed again until the lens HP read shows the feed applied (HP above the pre-feed read and at or above `lastFeedHp + heal` less the projected drain since; the heal per food item is a table of item heal values in `strategies/base.py`, `+1` for an unlisted item) or this many seconds have elapsed; under a stale lens only the elapsed time counts; chain-fallback keys: `chainFallback` (true — under a lens OUTAGE, `lens-unavailable` / `lens-not-live` / `lens-degraded:*`, of at least `chainFallbackAfterSec`, the book reads its own kami from the chain and may sign a FEED from that reading, nothing else — P4); `chainFallbackAfterSec` 0–86400 (300 — a lens restart shorter than this never triggers a chain read); `chainFeedCooldownSec` 60–86400 (1800 — one feed per kami per this window under the fallback; the chain read cannot show a feed applied, so only time clears the guard); **the food ladder — the MODE SWITCH is `foodLadder`**: 1–8 item indices, unique, NO default, every one with a known heal (`FOOD_HEAL_HP`, the feed guard's table) and `foodItem` among them (`foodItem` stays required and is the ladder's anchor; ladder mode reads nothing else from it). WITHOUT the key nothing below applies: the book feeds `foodItem` by `feedMode` and reads neither the node census nor the previews; WITH it the book takes the node reads and the bound of an `auto` book (the `NodeThreat` mixin) and sizes every feed itself, so the key also makes `accountIndex` (no default, the account's ON-CHAIN index) REQUIRED, refuses `feedBelowHp` (ladder mode computes the feed point) and any `feedMode` but `hp`, and adds `friends[]` ([]) plus the node-threat keys with the same names, ranges, descriptions and defaults as on `auto` — `safetyMarginPct` (5), `passiveMarginPct` (2), `passiveOwnersBind` (**true**), `hostileWindowHours` (48), `restFloorPct` (10), `cooldownHorizonSec` (600), `previewRefreshSec` (3600), `previewsPerTick` (10), `previewPolicy` (`all`, **no `x-defaultWhen`** here), `nodeCensus` (`vitals`, no `x-defaultWhen`), `attackerVitalsPerTick` (10), `logScanBlocks` (40), `killScanOverlapBlocks` (12), `occupantMemoryBlocks` (120); `nodeCensusEverySec` is NOT among them — a ladder book reads the census every pass. `previewPolicy` `killed-here` and `nodeCensus` `plain` are refused with `passiveOwnersBind` true, exactly as on `auto`. Its own four keys: `ladderMinFloorPct` 0–95 (**60** — the chosen rung's no-waste floor never sits under this share of max HP, whatever the node reads), `maxOverhealHp` 0–150 (**25** — the HP an emergency top-up may overheal), `tierUpCooldownSec` 0–86400 (3600 — one rung UP per window; a step DOWN is immediate and a book's first choice is never held), `foodLowUnits` 0–100000 (100 — a balance above 0 and below it rides `status.strategy.foodLow`). **The MEASURED dip cap**: `dipCapMode` `flat \| measured` (**`flat`** — with `flat` nothing below is read and the cap is `ladderMinFloorPct`), refused without `foodLadder`; with `measured` the cap of a kami is the highest threshold served against THAT kami by a kami of `<dataDir>/watchlist.json` (the world killer list, written by the operator's own tooling and only ever READ here) whose last liquidate is inside `dipWindowDays` 1–60 (7) and whose account is not in `friends`, plus `safetyMarginPct`, never under `dipMeasuredMinPct` 0–95 (30) — and therefore possibly ABOVE the flat cap; with no listed kami inside the window the cap is `dipMeasuredMinPct` alone; the FLAT cap stands while the list is missing or its `updatedAt` older than `watchlistMaxAgeSec` 3600–1209600 (86400), or a listed kami has not been read for the node yet. The reads: one `node <i> <kami> --with-vitals --full` per listed kami per node — a listed kami is normally NOT on the node and the lens serves its thresholds against the occupants all the same — refreshed at `watchRefreshSec` 300–86400 (3600), at most `watchPreviewsPerTick` 1–50 (5) per tick pass and node (shared by the books on the node, like `previewsPerTick`), taken AFTER every other read of the book |
| `auto` | `kamiIndices[]`; `nodeIndex`; `accountIndex` (**required**: the account's ON-CHAIN index, what the `party` / `inventory` reads take — the book's `account` is the grant LABEL and labels never cross into lens queries); `maxKamisPerBook` (50; ≤ 1000); `friends[]` **account indices** ([]) — never display names, which are authored, mutable, non-unique strings in the lens taint model; `regenMethod` `REST\|FEED` (`REST`); `foodItem` (required when FEED); `safetyMarginPct` 0–10 (5); `passiveMarginPct` (2); `hostileWindowHours` 1–720 (48 — the ONE window, measured from the LAST such event, over which a liquidate or cast on this node, or on one of this account's kamis on any node, by an owner — or by the attacker kami itself, whoever owns it now — counts toward the attacker's class, and for which a departed attacker of such an owner, or such a kami, is remembered; per book, and the keeper's prune / seed / first-boot scan use the largest among running books); `autoCollect` (true — `describe` states that a mid-harvest collect resets the harvest's intensity clock); `bountyCollectThreshold` MUSU (10000); `reviveOnDeath` (true); `reviveItems[]` ordered item indices, inventory only ([11001 Red Ribbon Gummy], stated in `describe`); `reviveRetrySec` 30–3600 (**200**) — after ANY revive attempt (offered, refused, reverted, lost, or skipped for a missing item) the kami, if the world still reads it DEAD, is offered a revive again once this many seconds have passed, and a plan entry with no `reviveRetryAt` key is offered once on the first tick that reads it DEAD; Spirit Glue (item 19001) locks a kami for 180 s, so a revive can be refused pre-send; `resumeHpPct` (100 — rest to full, one deploy per full harvest); `feedCooldownSec` (300, as above); node-threat keys: `restFloorPct` 1–50 (10 — a harvesting kami at or below this share of max HP is rested or fed whatever the bound: `harvest stop` reverts on a kami at HP 0); `cooldownHorizonSec` 0–86400 (600 — an attacker whose liquidation cooldown exceeds it is not counted in the bound this tick, noted `cooldownSkipped`); `previewRefreshSec` 300–86400 (3600 — age at which a preview with any positive clamped threshold is fetched again; 86400 when every threshold is ≤ 0; an arrival and a level change on either side refresh at once); `previewRetrySec` 30–86400 (300 — age at which a preview whose last read served a row that was NOT a line (no vitals, or a non-positive threshold under an attacker-side reason while the ATTACKER's own row also lacked vitals) is fetched again; such rows are carried, never applied, and the entry never waits `previewRefreshSec` or 86400 instead); `previewsPerTick` 1–50 (10 — preview reads per tick PASS and node, shared by the books on the node, arrivals first, then no-rows entries, then level changes and aged entries by binding weight — highest clamped threshold first, oldest fetch first among equals); `startBackoffSec` 60–86400 (3600 — a kami the chain refused to start or stop is not offered again for this long; a refusal whose reason says `on cooldown` waits min(this, `COOLDOWN_REFUSAL_BACKOFF_SEC` = **300 s**, a module constant of `strategies/auto.py`, not a config key) instead — the cooldown clears by itself, so the wait is one re-offer and not an hour. The book normally never reaches that refusal: a kami the lens reads with `cooldownSec` > 0 is not offered a start or a stop at all (`kamis.cooldown`, `cooldownDeferred[]`), so the constant covers only a lens / chain disagreement. A refusal that is NOT the chain's verdict on the kami — the pre-check passed and the node refused the BROADCAST with a non-revert text (a readiness race, a missing batch answer, a transport failure, a chunk not offered) — waits min(this, `TRANSIENT_REFUSAL_BACKOFF_SEC` = **60 s**, a module constant of `strategies/auto.py`, not a config key) and its `deviation` row reads `transient: true`; the re-offer's own pre-check is the guard, so a kami whose action really reverts takes the full wait then); start-batching keys: `startBatchWaitSec` 0–1800 (0 = off — a READY kami (RESTING at or above `resumeHpPct`, above its bound plus an hour of drain, not held, not in backoff) is started on the tick it is ready; above 0, ready kamis wait until `startBatchMin` of the book are ready or the OLDEST has waited this long, then every ready kami goes out as one batched start (cap 10); a kami whose bound rises above its HP while waiting leaves the queue; a start that does not land keeps the kami's place), `startBatchMin` 1–31 (4); the stop PACK: `stopJoinWindowMin` 0–60 min (**0 = off** — the book rests exactly the kamis at or below their limit; above 0, on a tick that rests at least one kami (an ANCHOR) the book also rests the harvesting kamis of its own whose PROJECTED minutes to their own limit — (hp − max(bound, restFloor)) ÷ this tick's drain rate × 60 — are at most this many, nearest first, up to `stopPackMax`; a joiner is only ever stopped EARLIER than its limit and no kami is ever held past it, so the rule is one-directional), `stopPackMax` 1–12 (**12** — the most kamis one pack holds, anchors first and joiners after; anchors beyond it still go out this tick, chunked at the per-transaction cap of 12, and when the anchors alone reach it no joiner is added); the START join: `startJoinWindowMin` 0–30 min (**0 = off** — a RESTING kami starts only at `resumeHpPct`; above 0, on a tick where the book STARTS at least one kami anyway it also starts the RESTING kamis of its own whose PROJECTED minutes to `resumeHpPct` — (ceil(`resumeHpPct` × hpTotal ÷ 100) − hp) ÷ this tick's regen rate × 60 — are at most this many, nearest first, filling only the FREE places of the last start chunk (cap 10), so a joiner never adds a transaction; a joiner is only ever started EARLIER than its own readiness and nothing is ever held longer); `logScanBlocks` 1–1000 (40 — blocks per `eth_getLogs` call of the kill scan); `killScanOverlapBlocks` 0–200 (**12** — blocks re-read BEHIND the scan's cursor every pass: a DEAD write the RPC node served late, its log index behind its own `eth_blockNumber`, is caught on the next pass, and a row the owner ledger already holds is skipped without a transaction read; 12 blocks ≈ 30 s ≈ one tick); `occupantMemoryBlocks` 0–1440 (**120** — how far behind the cursor a kami listed by an EARLIER census is still resolved as that node's occupant, because the census is read at a lens block INSIDE the range the next pass scans and a kami killed before it is already gone; 120 blocks ≈ 5 min, 0 = only the previous census; the books' OWN kamis are always resolved, whatever the value); `passiveOwnersBind` (true — when false, a threshold-no-kill owner's thresholds are read and kept but do not bind or hold the book's kamis, only the rest floor does, until that owner's first recorded kill or cast on the node or on this account's kamis (or the attacker kami's own, and only inside `hostileWindowHours` of the last one); killed-here always binds with `safetyMarginPct`); read-policy keys (the node reads and the previews): `previewPolicy` `killed-here\|all` (`all`; **`killed-here` when `passiveOwnersBind` is false** — only a present attacker whose owner, or the kami itself, is killed-here inside `hostileWindowHours`, plus a remembered one that came back, is fetched, refreshed or counted `previewsPending`; with `passiveOwnersBind` false nobody else binds, so nobody else's preview changes a decision; hostility is known WITHOUT a preview (the chain-fed owner ledger); the tick after a kill row that owner's present attackers are ARRIVALS — front of the queue, `unknown` — held from STARTING until fetched, and rested at the larger of the floor and the bound already known from the previewed attackers (the owner's served maximum stands in for its unread attackers, which then neither hold a start nor count as unknown); a stand-in is SCALED — a kami without a row of its own (resting when the preview was fetched) takes the attacker's highest served ratio, threshold ÷ that occupant's served max HP, over the account's occupants, times its own max HP, and an owner's unread attacker takes the owner's highest such ratio; a cached row that carries no `hpTotal` takes the BOOK's own max HP for that kami as its denominator (the figure the party read already gives the book), so a stand-in is scaled whenever ANY counted row's max HP is known from either source and the absolute maximum stands in only when none is; an ABSENT but remembered attacker's entry is never refreshed, because only present occupants are due. A stand-in is never a threshold: the kami's own served row replaces it on the entry's next refresh; an attacker whose window expired is not refreshed any more, kept until it leaves, then dropped; **refused with `passiveOwnersBind` true**: an owner that binds on its threshold needs the threshold. The value names the RENDERED class it previews (P3a): the internal word is on the P9 scrub list and a config value is a served string), `nodeCensus` `vitals\|plain` (`vitals`; **`plain` when `previewPolicy` is `killed-here`** — the census is `node <n> --full` without `--with-vitals`: presence, account indices and the kill scanner's occupant table; refused with `passiveOwnersBind` true), `nodeCensusEverySec` 5–86400 (**the book's `tickSeconds`**, i.e. every pass; refused below `tickSeconds`; above it the last census is reused between reads — `censusAgeSec` on the tick — while the book's own kamis still come from `party --full` every pass; a kill or cast row on the node forces a read; per node in the keeper, shared by the books on it, never persisted), `attackerVitalsPerTick` 0–50 (10 — under `plain`: `kami <index>` reads per tick PASS and node, shared by the books on it, for the cooldown and level of present killed-here attackers that no preview of the pass answered for; over the cap, or refused by the lens = NO cooldown = the attacker binds). A stored config without these four keys keeps `all` + `vitals` + a census every pass until it is declared again; chain-fallback keys as on `harvestAndFeed`: `chainFallback` (true), `chainFallbackAfterSec` (300), `chainFeedCooldownSec` (1800) — under the fallback a kami the chain says is HARVESTING on the node at or below `restFloorPct` × hpTotal is fed by a FEED book, and STOPPED by a REST book, one attempt per kami per `chainFeedCooldownSec` (the floor, never the bound: no preview, attacker table or owner class is read from the chain) |
| `craft` | `recipes[] {recipeIndex, amount (0 = until inputs run out), priority, staminaCost (optional: the recipe's stamina cost when known — the recipe is not offered below it)}`; `staminaFloor` (0, kept unspent on top of the need). **Stamina need:** a stamina refusal at lens reading S sets the learned floor `staminaMin[recipe] = S + 1` (persisted, monotone) and parks the recipe for `(need − S) × 60 s` (the regen rate, 1/min) instead of a flat 300 s; the need is the larger of the declared cost and the learned floor; a recipe below its need is a `staminaWait` note on the tick summary, not a pre-check; `accountKey` (optional: the lens `account` key — index, in-game name or address — for the stamina read when the account's in-game name is not its label, e.g. label `alice`, in-game name `alice-1`; absent, the label; the label still names the grant, the lane and the ledger) |
| all | `tickSeconds` (30); `maxStalenessBlocks` (90 = about two lens reconcile intervals: the lens 0.6.1 pass runs every 120 s ≈ 48 Yominet blocks, so `reconciledThrough` legitimately lags the live block by up to ~48 plus the pass itself, and a smaller value reads `lens-stale` on a healthy daemon); `externalChangeTicks` (3) — consecutive VALID ticks on which an external change must be observed before the book pauses (P7); `readoptAfterBlocks` (240 ≈ 10 min: a common key, `fleet` only, refused under `lab`) — blocks of valid reads on which the world matches the plan after an `external-change` pause before the book re-adopts (P7); `staleFeedCooldownSec` (1800) — under `lens-stale` alone a kami below its feed floor is fed at most once per this many seconds (the normal branch keeps `feedCooldownSec`). A profile-refused key is never defaulted into a book's stored config |
| keeper-level | `maxBooksPerAccount`: `fleet` default 0 = unlimited; under `lab` **required** — an absent value makes the keeper refuse to RUN books: the process boots and serves read-only commands, the tick engine never starts, persisted running books are not resumed, and `start` / `resume` answer `BAD_ARGS` naming the key; it never reads as 0 or ∞ by omission. `tickHeartbeat` (20): a `tick` ledger row is written when the assessment summary changes and at least every this many ticks. **`standingOwners`** (`[]`, env `KAMI_KEEPER_STANDING_OWNERS`): account INDICES — never display names — of the owners the OPERATOR names standing. An attacker whose OWNER is on this list is classed `standing` (P3a) and treated like a killed-here owner's at ALL times, with no kill and no window: its present attackers are previewed and refreshed under `previewPolicy` `killed-here`, its arrivals are held from starting until fetched, it binds with `safetyMarginPct` (not `passiveMarginPct`), and a departed attacker of its is remembered for `hostileWindowHours` from its DEPARTURE and then expires — so a standing owner that leaves the node for the window stops binding until it comes back, and binds again the tick it is seen. A kill by it still records and renders `killed-here-<window>` as it does without the list, and `listed` wins (the two lists are the operator's to keep disjoint). An entry that is not a non-negative integer is one `errors` line naming the key; duplicates collapse; `[]` means no standing owner; no profile gate (allowed under `lab` and `fleet` alike, no P10 registry row). KEEPER-level and not a book key because a live book is matched to its declaration by (type, account, kami set, node) (`core.config_book_identity`), so a book key would reach a running book only through a stop + re-declare = a new book id and a new ledger; a keeper-level list reaches every node-watching book at the next restart, book ids unchanged |

Enforcement: JSON Schema validation on `start`; a test that `describe`
output equals the pinned schema file; a MAJOR-bump check on default
diffs (CI compares defaults against the last tag).

## P3 — Status semantics

- State machine: `created → running → paused | stopped | failed`.
  Terminal states carry an enumerated `reason` (`stopped_by: agent |
  operator | keeper`; `failed: signer | lens | rpc | config`).
- `status` carries per book: `lastTickAt`, `lastActAt`, `lastActBlock`
  (the receipt block of the last landed tx, null until one lands),
  `lastTxHash`,
  `nextTickAt`, `lastError`, `ticksSinceAct`, `degraded[]`, and
  `pausedReason` (`agent` | `external-change` | `kami-dead`) with
  `pausedAt`, both null unless the book is `paused`, and
  `parking` `{since, by}` — the mark of an `auto` book asked to park,
  null unless it is parking, and KEPT on the stopped book (it is the
  history of why that book stopped); `books` shows the
  same rows. `kami-dead`: every book kami is DEAD and none has a
  revive retry pending — revives disabled; a pending `reviveRetryAt` (an
  offered, refused, reverted, lost or `no-item` revive) keeps the book
  running until the retry lands or the item arrives. `strategy`:
  the type's own counts, null for
  `craft`; for `harvestAndFeed` `nextCollectAt` (ISO, the drawn
  collect due time) and `collectIntervalSec` (the draw it came from),
  both null until the collect clock has started, and — a LADDER book
  only — `ladderItem` (the rung in force), `ladderFloorHp` (its
  no-waste floor, `hpTotal − heal`), `foodStock` (every rung's balance at
  the last inventory read, or null for a rung when the read was refused)
  and `foodLow` (the rungs above 0 and under `foodLowUnits`), all four
  null until the book has assessed once and ABSENT on a book without
  `foodLadder`; for `auto` `kamis {harvesting, resting,
  dead, held, backoff, elsewhere, waiting, pulled, joined, cooldown}` (`waiting` = ready
  kamis held for a batched start; `pulled` = the JOINERS this tick's
  stop pack took with its anchors, 0 with `stopJoinWindowMin` 0;
  `joined` = the JOINERS this tick's start transaction took with the ready
  kamis, 0 with `startJoinWindowMin` 0 (a joiner is still RESTING at read
  time, so `resting` counts it too);
  `cooldown` = own kamis the lens reads on the chain's action cooldown — a
  RESTING one is not offered a start and leaves the start queue, a HARVESTING
  one at or below its limit is not offered a stop this tick), `owners {listed, no-threshold,
  threshold-no-kill, killed-here}` (counts of OWNERS by RENDERED class,
  P3a; the class is per ATTACKER kami, so an owner whose attackers
  differ — a clean owner holding a kami that killed — counts once, under
  the worst of them; under `previewPolicy` `killed-here` a fifth key
  `unpreviewed` — owners with no kill inside the window and no preview on
  file, which the policy did not need to read; a COUNT, not a class, P3a;
  absent under `all`),
  `minMarginHp`, `boundKamis`, `previewsPending`, `nextPreviewDueAt`.
  `RUNNING ≠ executing` is closed by construction: a running book with
  `ticksSinceAct` above its own expectation is visible as such.
- `books` lists every book the keeper holds for the CALLER's account
  and nothing else; the operator profile may list all.
- Status is derived from the keeper's own state at answer time, never a
  cache. Stopped books stay enumerable with their reason until purged
  by the operator.
- Keeper-level staleness: the `status` answer's own lens gate
  (`degraded`, `lens.stalenessBlocks`) uses the LARGEST
  `maxStalenessBlocks` among the running books — the loosest bound any
  book acts under — or the schema default (90) when none runs; the
  value in force is served as `lens.maxStalenessBlocks`. `lens.outageSince`: the
  ISO time of the first tick that read a lens OUTAGE flag, null while
  the lens answers — the keeper-level clock `chainFallbackAfterSec` runs
  against. `openTx` per book: the count of `broadcast` tx rows with
  no `receipt` row yet — `0` normally; after a restart inside a receipt
  wait the rows the chain has not yet answered for, until the boot / tick
  probe closes them (P5). `lens.passCalls` and `lens.passBytes`, each
  `{last, p95, passes}`: what one tick pass asked of the lens — daemon
  round-trips and bytes on the wire over every book of the pass, the
  per-book `status` reads included — for the last pass and the p95
  (nearest rank) over the last 100 passes that ticked at least one book;
  both null before the first such pass. Never persisted; the keeper's own
  `status` answer reads through a separate client and is not counted;
  that client's timeout is `STATUS_LENS_TIMEOUT_SEC` = **6 s**, so a
  daemon slowed by a preview burst does not read as `lens-unavailable`
  on `status` while every book's own 30 s client reads fine.
  `lens.watchlist` `{updatedAt (ISO | null), killers (count)}`:
  what `<dataDir>/watchlist.json` said when the last tick pass read it
  (the pass is the only place it is read, and the keeper never writes
  it) — `{null, 0}` before the first pass and whenever the file is
  missing or unreadable. (lens 1.0.0) `lens.appliedThrough` — the applied
  mark of the status answer, null against a 0.6.3 daemon, which serves
  none — and `lens.version`, the daemon's own version string: the two
  facts that say WHICH lens contract the keeper is running against.

Enforcement: state-machine transition test; a test that `books` never
returns another account's book under a lab profile.

## P3a — Owner class names in agent-visible strings

Internal policy identifiers may be `friend / harmless / armed-passive /
hostile`; every FORMATTED string (status, logs, ledger `class`, error
text) uses observation-only names: `listed` (in the configured
`friends`), `no-threshold` (threshold 0 against every kami of the
book), `threshold-no-kill` (threshold > 0 and no kill inside the window,
below), `standing` (the attacker's OWNER is on the keeper's
`standingOwners` — an observation of the CONFIGURATION, like `listed`,
and the fifth name of the closed set), `killed-here-<window>` (below;
`<window>` is the BOOK's `hostileWindowHours`, e.g. `killed-here-48h`).
The boundary is the formatter; a test renders every class through it and
matches the closed set. `unpreviewed` in the `owners` summary is NOT a fifth class: it
counts the owners a `previewPolicy` `killed-here` book did not need to
read (no kill inside the window, no preview on file). The closed set of
rendered names is unchanged, and `ownerClassAfter` on a `kill` row still
renders from what is known — `no-threshold` for an owner never read,
`killed-here-<window>` once the kill is recorded.

**The hostility policy.**
It is the same for every book that WATCHES A NODE — every `auto` book and
a `harvestAndFeed` book with a `foodLadder` (which computes
the same `bounds()` from the same preview cache and the same owner ledger,
and carries the same keys; its `passiveOwnersBind` default is **true**).
The class is evaluated PER ATTACKER — a kami on, or remembered for, the
book's node, under the owner the node read names NOW:

- a listed owner's kami is `listed`, whatever that kami did under anyone;
- `killed-here-<window>` when, INSIDE the window, (a) the attacker's OWNER
  liquidated or cast on this node (any victim), or liquidated / cast on one
  of this account's kamis on any node, OR (b) the attacker KAMI itself
  liquidated any victim on this node or one of this account's kamis on any
  node — whoever owned it then (attacker memory by kami AND by owner, the
  union: a kami transferred to a clean account is still that kami).
  `boundBy.hostileBy` says which: `owner`, `kami` or `both`;
- `standing` when the attacker's OWNER is on the keeper's `standingOwners`
  and no clause above holds — `boundBy.hostileBy` is `standing`. It binds
  with `safetyMarginPct` like a killed-here attacker, is previewed and
  refreshed under `previewPolicy` `killed-here`, its arrivals are held from
  starting until fetched, and on departure it is remembered for
  `hostileWindowHours` from the DEPARTURE and then expires;
- otherwise `no-threshold` when the owner's served threshold is 0 against
  every kami of the book, else `threshold-no-kill`.

**Forgiveness: there is no permanent class, except the operator-named standing list, which is configuration, not memory.** Every clause above is
measured over the ONE window `hostileWindowHours` (default 48), from that
owner's / kami's LAST such kill, in blocks below the lens block of the
read (1,440 blocks to the hour at the D2 fallback rate); past it the attacker is classed by its
threshold alone, and a new kill re-binds it for a fresh window. The same
window is how long a departed attacker is remembered when its owner OR
the kami is `killed-here` at departure. With `passiveOwnersBind`
false an attacker with a positive threshold and no kill inside the window
is ignored until its first kill row. A KAMI_CAST names the caster's account, never a kami: a cast
counts toward the OWNER only.

**A preview without rows.** The lens previews
OCCUPANTS, so an attacker previewed while NONE of the account's kamis was
on the node has an entry with no rows at all: no threshold for any kami,
no served maximum to stand in, and therefore `unknown` — which holds
every start. A refetch answers the same nothing while nothing of the
account is there, so the account would stay off the node for as long as that
attacker stays and binds (up to `hostileWindowHours`). The hold is
therefore lifted for AT MOST ONE kami per book per tick — the first ready
kami in the book's `kamiIndices` order — when every attacker in `unknown`
is an entry with no rows, no kami of the ACCOUNT is on the node, and no
other start of that book goes out this tick. That start carries `probe:
true` on its detail row and `probeStart: <kami>` on the tick summary;
every other kami stays held with its existing reason. Next tick the probe kami
is an occupant, the empty entry is due again (queued straight after the
arrivals, reason `no rows for the account; a kami is on the node now`),
its row arrives, and the served maximum over the account's occupants
serves every other kami as usual.

**A served line has vitals.** A row of a `node <n> <attacker>
--with-vitals --full` answer is a LINE only when it carries vitals
(`level`, `hp.current`, `hp.total`) AND either its threshold is positive,
or its non-positive threshold comes with a target-side reason, or its
non-positive threshold comes with an `ATTACKER_*` reason
(`ATTACKER_STARVING`, `ATTACKER_COOLDOWN`) while the ATTACKER's OWN row
of that same answer carries vitals — that shape is the lens's
normal answer for a weak attacker read inside its cooldown (the same
pairs serve the same numbers under `THRESHOLD_ZERO` once it is off
cooldown; the threshold is a stat fact and the `reason` reports the
attacker's gate on top of it), so it is stored as served. An
attacker-side non-positive threshold is INVALID only when the attacker's
own row lacks vitals as well, or the attacker has no row in the answer at
all — the shape of a lens answer that dropped the attacker's vitals with everyone
else's. Every other row with a threshold is INVALID: it never replaces a
line and never lowers one. On a fetch an invalid row for a kami that has a row in the
old entry CARRIES that row (marked `carriedFrom`, the old entry's fetch
time), an invalid row for a kami with no old row adds no row (stand-in /
unknown as before), and an entry that took any invalid row is due again
after `previewRetrySec` (300, an optional `auto` key) — never at
`previewRefreshSec` and never at the 86400 s no-threshold interval, so an
invalid zero is never held for a day. A `THRESHOLD_ZERO` row WITH vitals stays a valid
0 (the lens says this attacker cannot kill that kami). The tick summary
counts the rows the answers of that tick served and the cache refused as
`previewInvalidRows`, which is NOT an ignored measurement key: a degraded
read writes its tick row. Nothing else moves — `bounds()`, the ratio
stand-in, presence reconciliation, the killed-here policy and the
per-pass fetch cap are unchanged.

**The measured line.** A kill of one of the book's
kamis on the book's node is a MEASUREMENT of the killer's line. When the
victim's HP at the kill was ABOVE the served threshold of the (killer,
victim) pair, the served line was too low by at least that gap, and the
keeper LIFTS every line of the killer's OWNER on that node — the killer
and the owner's other attackers, present or remembered — by that gap as a
ratio of the victim's max HP, plus one
HP, rounded UP to 0.1 % (`lift_ratio`; `liftPct` on `boundBy` and the
tick's `liftMaxPct`). Three sources, each named: the SERVED threshold is
the lens's own number for that pair (never the lifted one); the HP the
victim had is the PREVIOUS pass's projected HP of it (the kill scan runs
before any book observes, and this pass reads the kami DEAD) — one
source, always in hand, and the +1 HP plus the rounding cover the <= 2
min of drain between the reading and the kill; the kill itself is the
chain's (block, txHash, the owner ledger's attribution). **The line is
still the lens's number: a chain-measured gap is added to it, nothing is
computed from stats.** The lift is capped at 25 % of a kami's max HP
(`LIFT_CAP`: a bigger gap is treated as a wrong measurement, and the cap
is reported as `capped`); it only ever GROWS — a second kill above the
lifted line lifts again, measured from the SERVED threshold; it is a
property of the preview ENTRY, so it survives that entry's refresh (a
refresh taken after the bonus has ended serves the earlier number
again), it is persisted with it, and it
leaves when the entry is forgotten (departure + window). The same pass
then assesses with the lifted lines, so the kamis now inside the pack's
reach are rested in that pass. Two limits of the rule: a kill at or below
the served line measures nothing (`lift: null`, "a late rest" — the line
explained the kill), and a kill by an attacker never previewed lifts
nothing (there is no line yet; the arrival path previews it this pass and
the next kill above it will lift it). A timed bonus is visible on chain
and in the lens while it lasts, and an entry fetched before it serves the
earlier number until its next refresh; so a lift HOLDS for
`LIFT_HOLD_SEC` (6 h; a renewed kill extends the hold) and then expires,
before anything is fetched in that pass (`liftsExpired`); and a kill by
an owner, measured or not, flags that owner's PRESENT entries for an
immediate refetch, offered FIRST by `due()` under the same
`previewsPerTick` cap, so that while the bonus lasts the lens's own
raised lines are read and the measured lift is only the floor under
them. The two halves of the rule: the hold keeps the measurement as a
floor after the refetch; the refetch reads the exact lines.

## P4 — Never-silent degradation

- `status.degraded[]` with named causes: `lens-unavailable`,
  `lens-not-live`, `lens-stale:<blocks>`, `lens-degraded:<name>`
  (the lens's own `status.degraded` carried `stream-stalled…` or
  `unhealed-ranges…` — the mirror is KNOWN incomplete; the flag carries
  the NAME only, `lens-degraded:stream-stalled` /
  `lens-degraded:unhealed-ranges`, so a continuing stall is one
  unchanged flag and does not write a tick row every tick; the lens's
  full text rides the tick summary as `lensDegraded[]`, which the
  tick-row change key ignores), `lens-incomplete`
  (a kami row of the answer lacked a field the decision needs;
  also a kami with NO ROW in the account's answer, for the first
  `externalChangeTicks − 1` such reads — see P7),
  `rpc-unavailable`, `ws-logs-gap:<N>`, `signer-nonce-stuck`,
  `kamiden-unavailable` (enrichment only; never gates a decision).
  For an `auto` book the KILL SCAN's `rpc-unavailable` (an RPC
  error on the scan) and `ws-logs-gap:<N>` (N = head − lastScannedBlock
  when more than one `logScanBlocks` call is still unscanned after the
  pass's capped catch-up) ride every auto tick of the pass and every
  row it writes and GATE NOTHING: the owner ledger is a lag of the
  class, not an unknown reading of HP; the book acts on the last ledger.
  The `rpc-unavailable` of a refused chain READ on the send path
  is unchanged.
- **Perception validity.** A `kami` / `party` / `node` answer whose
  kami row lacks `state`, `hp.current`, or `node.index` while the state
  is HARVESTING is INCOMPLETE. An incomplete observation is a gated tick
  (`lens-incomplete`, no action, no death row, no P7 evaluation) —
  never an observation of the world and never an external change. A
  missing or null field is an unknown. For an `auto` book the rule
  applies to every OWNED row the decision uses, and a kami absent from
  the `party` answer is an incomplete observation of that kami ("not
  found" is unknown, never RESTING); a FOREIGN occupant row lacking
  vitals is NOT a gated tick — that occupant's threshold is unknown
  until previewed (noted `occupantsWithoutVitals`; under `nodeCensus`
  `plain` no row carries vitals by construction — a plain occupant, not a
  row that lacked something — and the key is omitted). A row served
  without `node` is therefore a gated tick, never "observed HARVESTING on
  node None". `stream-stalled` / `unhealed-ranges` on the
  lens status gate the tick the same way (`lens-degraded:<flag>`).
  A kami ABSENT from the party answer while
  every served row is complete and the lens is not degraded is an
  incomplete observation of THAT kami (unknown, never RESTING) that
  COUNTS toward an external change of it: the read is gated
  (`lens-incomplete`, the tick notes `contradiction: kami N: not in the
  account's party`, `observedTicks`, `confirmAfterTicks`) and on
  `externalChangeTicks` consecutive such reads the book pauses (P7). A
  row lacking a field anywhere in the same answer is the plain gated
  tick above and neither counts nor resets.
- **The chain-backed HP fallback.** Under a lens OUTAGE —
  `lens-unavailable`, `lens-not-live` or `lens-degraded:*` (with or
  without `lens-stale:<n>` alongside; never `lens-incomplete`, never
  `rpc-unavailable`) — that has lasted at least `chainFallbackAfterSec`
  on the keeper-level outage clock (`status.lens.outageSince`), a
  `harvestAndFeed` book (feedMode `hp`), an `auto` FEED book or an
  `auto` REST book with
  `chainFallback` on and a chain configured reads ITS OWN kamis from
  the chain (keeper/chainhp.py: one point read — `GetterSystem.
  getKamiByIndex`, the harvest entity's `Time.Last` / `Time.Reset` /
  `id.source`, the bonus totals, the config constants once — batched
  ≤ 100 `eth_call`s a body) and projects their HP with the game's
  strain arithmetic. From
  that reading a feeding book may sign FEEDS only;
  everything else stays gated. A REST `auto` book may sign
  `harvest stop` only — batched as any rest (cap 12,
  `executeBatchedAllowFailure`, the members planned `pending`) — and
  only for its own kamis the chain says are HARVESTING on the book's
  node with projected HP at or below `restFloorPct` × hpMax: the
  FLOOR, never the bound (no preview, no attacker table and no owner
  class is read here), at most ONE attempt per kami per
  `chainFeedCooldownSec` (a stop that landed reads
  RESTING on the next chain read and is not offered again; one that did
  not land is retried after the cooldown — `chainRests` in the runtime
  is kami → the clock of its last attempt, persisted, so a keeper
  restart mid-outage keeps the guard).
  Never a start, collect, feed or revive. The tick summary carries
  `underChain`, `chainBlock`,
  `chainReads {calls, bytes, block, kamis, configRead}`, `chainHp`,
  `chainRested[]` (the kamis offered a stop this tick),
  `chainDead` / `chainUnread` (notes, never a death row),
  `nodeAffinityUnknown`; every decision and tx row carries the gating
  flags plus `underChain: true` and `chainBlock`. A gated tick before
  the clock elapses says `chainFallback: waiting…` with
  `chainFallbackAfterSec` and `outageSec`; `no chain configured` when
  there is none; an RPC failure on the read is a gated tick with
  `rpc-unavailable` and `chainFallback: read unavailable`, as on any
  other refused chain read.
- **Lens 1.0.0, one rule each.** (a) A row the daemon flags
  `incomplete: true` and a read it refuses `INCOMPLETE` are P4-INCOMPLETE:
  the row is an unknown, so `incomplete` is the first entry of that
  kami's `missing` and the tick is gated `lens-incomplete` exactly as a
  row lacking a field is; a preview row of the book's own kamis that is incomplete never
  becomes a LINE (the old row is carried, the entry is due again at
  `previewRetrySec`), and an attacker the daemon refuses `INCOMPLETE`
  is a refused preview — counted `previewsRefused` + `previewsIncomplete`,
  binding nothing new, never a reading. (b) The freshness bar of P7 is
  sent to the daemon: the tick after an act reads with `--at-least <the
  receipt block>`, and a `NOT_APPLIED` refusal is that tick's
  `awaiting a read past the last act` — no `lastError`, no lifecycle row.
  And `reconcile-stalled:<sec>` in the lens's own `degraded` gates
  NOTHING (it says the chain re-check is behind, not that the mirror is
  incomplete); `stream-stalled` and `unhealed-ranges` gate as before.
- Fail-closed: a book that cannot perceive within `maxStalenessBlocks`
  does not act, and says so in status and in the ledger — with these
  exceptions: under `lens-stale:<n>` alone a
  `harvestAndFeed` book may still feed — and under a lens OUTAGE a
  feed book may feed from a chain point read of its own kamis, and
  an `auto` REST book may stop its own kamis at the floor from the same
  reading.
- Every decision taken under any degraded flag carries the flag in its
  ledger row; a feed taken under `lens-stale` carries `underStale: true`
  on its decision and tx rows as well.
- On lens failure the keeper never substitutes a mirror of its own.

Enforcement: fault-injection tests (lens socket absent; lens answering
`NOT_READY`; stale `reconciledThrough`; a node-less HARVESTING row; a
stalled lens status) asserting no send and the recorded flag; the
stale-feed tests (one feed per cooldown window, no start / stop /
collect, P7 off).

## P5 — Ledger

- Append-only `ledger.jsonl` per book under `<dataDir>/books/<id>/`,
  versioned `schemaVersion` on every row.
- Row kinds: `tick` (assessment summary; written on change and at least every `tickHeartbeat` ticks; an UNCONFIRMED contradiction — fewer than `externalChangeTicks` consecutive valid reads — is a `tick` summary note (`contradiction`, `observedTicks`, `confirmAfterTicks`), never a lifecycle row; a withheld feed is a `feedWithheld` note; a pooled collect is a `collectPooled` count, its `tx` row follows from the account's batched collect; a pooled stop is a `stopPooled` count — the kamis this tick's pooled stop holds, its `tx` rows following from the account+node burst — and a tick a binding kill pulled forward carries `killForcedTick: true`; neither is an ignored key: both move only when the thing happens, like `chainRested`; an `auto` tick: `kamis {harvesting, resting, dead, held, backoff, elsewhere}` (plus `cooldown` — own kamis the lens reads on the chain's action cooldown), `owners {listed, no-threshold, threshold-no-kill, killed-here}` by rendered class, `minMarginHp` (min over harvesting owned kamis of hp − bound), `boundKamis`, `boundKamisBy {<rendered class>[/<hostileBy>]: n}` — the bound kamis by the class of the attacker that binds each — and `hostileKamis` (PRESENT attackers whose own kills put them inside the window, whoever owns them), `liftedAttackers` (PRESENT attackers whose entry carries a measured lift) and `liftMaxPct` (the largest, one decimal), both ABSENT while no lift is in force, plus `liftsExpired[]` (the attackers whose lift hold passed this pass) when it applies — none of the three an ignored key: they move only when a lift is learnt, in force or lost — `attackers`, `remembered[]`, `cooldownSkipped[]`, `held[] {kami, reason, …}`, `restNotApplied[]`, `stopPulled[] {kami, projectedMinutes}` — the joiners this tick's stop pack took, which is NOT an ignored key: it moves only when a stop is signed, like `chainRested` — `startJoined[] {kami, projectedMinutes}` — the joiners this tick's START transaction took, not an ignored key either and for the same reason — `cooldownDeferred[] {kami, action `start|rest`, cooldownSec, hp?, limit?}`, at most 10 — the own kamis whose start or stop this tick deferred because the lens reads them on the chain's action cooldown (a RESTING one leaves the batched-start queue and its `readySinceSec` starts over when the cooldown ends; a HARVESTING one at or below its limit is offered the stop on a later tick, and is never a stop-pack joiner meanwhile; a FEED book's feed decision is NOT deferred), which is NOT an ignored key either: it moves only when a kami is deferred, like `held` — `backoffKamis[]`, `batchFallbackTx`, (a PARKING tick only)
  `parking: true`, `parkOffered` (the kamis whose stop this tick offered),
  `parkPacks` (the transactions it took) and `parkRemaining` (the kamis
  HARVESTING on the node this tick did not offer — the per-tick cap, a
  cooldown, a backoff or a pending stop), NONE of them an ignored key:
  they move only while the book parks, like `stopPulled`; a GATED tick of
  a parking book carries `parking: true` beside `reason: perception
  gated`, `killScan {head, lastScanned, logCalls, deadWrites, ignored, kills, casts, unattributed, overlapSkipped, remembered, own, gap, tsUnread, rpcError?}` (`overlapSkipped` = rows of the overlap re-read the owner ledger already held, `remembered` / `own` = entries of the scanner's occupant table that came from the memory alone and from the books' own kamis; `tsUnread` = blocks whose header this pass could not read, one per distinct block, whose rows are recorded undated — CARRIED on the row and not compared, like `overlapSkipped`) or the text `no chain configured…`, and the measured lines `lensCalls`, `lensCallsServedFromPass`, `lensBytes`, `previewCalls`, `previewBytes`, `previewChangedThreshold`, `previewInvalidRows` — served rows this tick's preview answers gave that were not lines, carried instead of applied, NOT an ignored key — `previewsPending`, `foreignOccupants`, and the read lines of the node pass: `nodeReadBytes` (what the census cost THIS tick on the wire; 0 when the pass cache or an earlier pass served it), `censusAgeSec` (0 when read this pass), `previewBytes`, `attackerVitalsCalls` (the `kami <index>` reads this tick made), with `attackerVitalsOverCap[]` / `attackerVitalsUnread[]` (killed-here attackers with no cooldown read this pass: they bind) and `previewsRefused[]` (a preview the lens refused: the attacker stays as it was, `unknown` when never read, and the census is read again next pass — under a census read THIS pass as under a reused one, a refused preview never refuses the tick) when they apply; `occupantsWithoutVitals` is omitted under the plain census — the measurement keys (`lensCalls`, `lensCallsServedFromPass`, `lensBytes`, `previewCalls`, `previewBytes`, `previewChangedThreshold`) are ignored by the tick-row change key like `lensDegraded`, so a steady world writes no row per tick; `chainReads`, `chainHp`, `chainBlock` and the gated tick's `outageSec` join the ignored set, so an outage writes no row per tick either — a feed under it writes its decision and tx rows; `chainRested[]` (the kamis an `auto` REST book offered a chain-backed floor stop this tick) is NOT ignored — it moves only when something was signed; `startWaiting[] {kami, readySinceSec}` — the ready kamis held for a batched start — joins the ignored set too (its clock moves every tick; the count rides `kamis.waiting`, which is compared); `nodeReadBytes`, `attackerVitalsCalls` and the clock `censusAgeSec` join it; of the `killScan` note only `kills`, `casts`, `castsOnOthers`, `unattributed`, `gap` and `rpcError` are compared by that key — its cursor and per-pass counters (`head`, `lastScanned`, `logCalls`, `deadWrites`, `ignored`, `overlapSkipped`, `remembered`, `own` and `tsUnread`) move every pass and are carried on the row, not compared, so a quiet node-watching book writes no row per tick; the flag list the same key compares is the tick's `degraded` less the flags that GATE NOTHING (`kamiden-unavailable`, which can flap on and off from tick to tick) — the row written still carries the whole list; a LADDER `harvestAndFeed` tick: the mixin's node lines above plus `ladder {item, heal, floor, need, boundBy, reason}` — the rung chosen this tick, its no-waste floor, `need` = max(bound, rest floor, `ladderMinFloorPct` × hpTotal), what bound it, and WHY that rung (`largest rung the bound allows` | `one rung up` | `held at the last rung` | `bound unknown: smallest rung` | `no rung clears the bound: smallest rung` | `bound above every rung` | `no rung in inventory`; `item` is null on the last two, where the decision is the failsafe stop). A book without `foodLadder` writes none of these keys; the same note carries `dipFrom` — where the dip cap in `need` came from (`flat` | `flat: watchlist stale` | `flat: watchlist not read yet` | `flat: no row for this kami` | `measured` | `measured: no active killer`) — and a book with `dipCapMode` `measured` adds the watchlist read lines `watchKillers` (listed kamis active for this book), `watchCalls` and `watchPending`), `decision` (kind, parameters:
  margin, threshold, class, window, hp; `rest` / `start`: `kamis[]`, `batchSize`, `detail[] {kami, hp, hpTotal, bound, restFloor, boundBy {attackerKami, ownerClass (rendered), threshold, present, thresholdFrom?, hostileBy? (owner | kami | both, on a killed-here attacker only), liftPct? (the measured lift the binding entry carries, absent without one — `threshold` is then already the LIFTED line)} | null}`; `start`: `readySinceSec` on each detail row — how long the kami waited for the batch, 0 with batching off; `rest`: `drainPerHr` on each detail row — the runtime `drainRate` the keeper holds for that kami, recorded from THIS tick's reading when there is one and `null` when it has never read one; `rest`: a JOINER's detail row also carries `pulled: true` and `projectedMinutes` (its projected minutes to its own limit at this tick's drain rate, one decimal) — an ANCHOR's row carries neither, and a joiner of a batch that did not land is not re-offered as a single; `start`: a JOINER's detail row also carries `startJoined: true`, `projectedMinutes` (its projected minutes to `resumeHpPct` at this tick's regen rate, one decimal) and `regenPerHr` (the rate it was projected at), with `readySinceSec` 0 — a READY member's row carries none of the three, and a joiner of a batch that did not land is not re-offered as a single; a LADDER `feed`: `item` is the rung actually fed and `heal` its heal, with `rung`, `ladderReason`, `floor` (`hpMax − heal` of THAT rung), `bound` (`need`) and `boundBy` beside them, plus `emergency: true` when the feed answers a bound that rose above the kami rather than the no-waste point; a LADDER `failsafe-stop`: `reason`, `bound` and `boundBy` beside the failsafe stop's `item` / `inventoryBalance`), `tx` (`txHash`, `nonce`,
  `block`, `status`, `gasUsed`, `effectiveGasPrice`, `account`, `kami`,
  `bookId`, `cycleOpenedBy: keeper|external`, `musuCollected` on a
  collect/stop — **decoded from THIS receipt's logs**: the MUD
  `ComponentValueSet` writes of `component.value` on the kami's
  harvest entity are `[N, 0]` (the system syncs the accrued bounty
  onto the entity, then drains it) and `musuCollected` is the max
  non-zero write when the writes end in 0 — the drain rule of
  kami-harness 3.7.0 `executor/server.py`, checked against a recorded
  receipt fixture; any other shape (no write, a last write that is not 0,
  no logs, no receipt) is `musuCollected: null` plus `decodeError`
  naming what could not be read, never a plausible number; the
  account's own MUSU inventory write is ABSOLUTE and is not the
  source; `batchSize` on every row of a batched collect and
  `batchFallback: true` on a single re-offered after a failed batch
  (P6); `underChain: true` + `chainBlock` on a feed signed from a
  chain reading, and the same two keys on its `decision` row's
  parameters with `hpProjected`; `nonceRetry: true` on a step
  re-signed once at a fresh nonce after a sequence-mismatch refusal of a
  hash the node did not know, with the first refusal kept as
  `broadcastError` and, when the retry was refused too, `status:
  not_sent` and `reason: nonce race: <text>` — P6), `death` (`kami`, `deadBlock`, and then `revived` with
  its tx or `revive-skipped: no-item` — the death is a row of its own
  because an in-tick revive would otherwise hide it from any
  state-sampling read; `revive-refused: <reason>` when the offer
  did not land — the first record's reason, truncated at 120 —
  and `revive-retry` in place of `observed` on the death row of a
  RE-offer, the offer `reviveRetrySec` after an attempt that left
  the kami DEAD), `lifecycle` (`started`, `stopped_by`,
  `paused`, `resumed`, `external-change`, `readopted`, `parking`;
  `parking` is written when `park <book>` is accepted — `reason` `agent:
  park`, with `kamis` (the book's kami count) and `node`; the book stays
  `running` and stops ITSELF later with a `stopped_by` row whose `reason`
  is `agent: park complete` and which carries `parked {kamis, resting,
  dead, elsewhere, lensBlock, lensAppliedThrough}`, the completion facts
  of the read that ended the park; an `auto`
  book's `started` row and a persisted auto book's `resumed` row carry
  `seedFromBattles {status: seeded | already seeded | skipped, reason?,
  kamisRead, deaths, ownersAdded, pending, deathsOutsideWindow?,
  deathsUndated?}`; a death older than the window plus one
  day, or one with neither block nor timestamp, seeds nothing and is
  counted here), `kill`
  (one per kill or KAMI_CAST on the book's node from the chain kill
  scan, written to every running auto book on that node: `event`
  `liquidate|cast`, `victimKami`, `victimAccount`, `attackerKami`,
  `attackerAccount`, `block`, `txHash`, `ownerClassAfter` — the
  RENDERED class (P3a), null with `attackerAccount` null when the kill
  is unattributed (unknown selector, killer not resolvable), which is a
  row and a tick note and changes no class; `attribution` says how the
  attacker was resolved; `item` on a cast; `blockTs` — the kill's
  own block header `timestamp`, null when the header was not read;
  `learned` — what the kill MEASURED about the killer's line
  (P3a): `{hpAtKill, served, gapHp, liftPct, lifted[], owner}` and
  `capped` when the lift hit `LIFT_CAP`, or `{hpAtKill, served, gapHp,
  lift: null, note}` for a kill at or below the served line (a late
  rest), or `{skipped}` when it could not be measured (`unattributed` |
  `no view of the victim from the previous pass` | `no served line for
  the pair`). NULL — the key is always present — for a kill of ANOTHER
  account's kami, which this book measures nothing from), `deviation` (an
  action the chain refused and the book's response: `kami`, `action`
  `start|rest`, `reason` (the node's text), `backoffSec`, `until` —
  `backoffSec` is `startBackoffSec` except on a refusal whose reason says
  `on cooldown`, which waits min(`startBackoffSec`, 300 s); a refusal
  that is NOT the chain's verdict on the kami (the pre-check passed and the
  node refused the broadcast with a non-revert text — a readiness race, a
  missing batch answer, a transport failure, a chunk not offered) waits
  min(`startBackoffSec`, 60 s) and the row reads `transient: true`;
  also each chain-backed floor rest an `auto` REST book offers under a
  lens outage — `kami`, `action` `rest`, `reason`, `hpProjected`,
  `restFloor`, `chainBlock`, and no backoff),
  on a `rest` decision of a PARKING book the parameters carry
  `parking: true` and every member's detail row `parked: true` — the row
  is otherwise a rest's, and a member of a pack that did not land is
  re-offered as a single exactly as an anchor is,
  each with the perception basis it acted on (`lensBlock`,
  `lensReconciledThrough`, and — lens 1.0.0 — `lensAppliedThrough`, the
  applied mark, null on every row taken against a 0.6.3 daemon and on a
  row written before `schemaVersion` 0.1.1) and any `degraded[]`. A
  `kill` row's basis is null: it rests on a chain read, not a lens read.
- `ledger --since <block>` exports rows for a range.
- The ledger is the attribution primitive: every keeper-signed
  transaction is joinable by hash. A batched collect (P6) is ONE hash
  on N rows, one per book, each with its own `kami`, `musuCollected`
  and `batchSize: N`.
- **The two-stage tx row.** A live step
  is TWO `tx` rows, `tx.stage: broadcast | receipt` on every live row
  (a `dry_run` row carries no `stage`). The `broadcast` row is written
  from the signer's broadcast-stage report (P6) BEFORE any receipt is
  awaited: `unconfirmed` with `txHash` and `nonce` (and `reconciled` /
  `broadcastError` / `nonceRetry` where they apply). The `receipt` row is
  written when the receipt read answered: the same `txHash`, `nonce`,
  `account`, `kami`, `system`, `bookId` (and `fn`, `count`, `gasLimit`,
  `precheck`, `batchSize`, `batchFallback`, `underStale` / `underChain`
  — everything the broadcast row stated is repeated, so a reader of the
  receipt row alone never joins), plus `block`, `gasUsed`,
  `effectiveGasPrice`, `musuCollected` / `decodeError` where they apply,
  and the FINAL status. The receipt row says what the receipt read said,
  not that the tx landed: a read that failed or timed out is a receipt
  row `unconfirmed` with the signer's `reconciled` text (P6). A `not_sent` record
  (pre-check refusal, node refusal, refused retry, not offered) is ONE
  row, at the broadcast stage — that is when it is known — and no
  receipt row. A pooled collect is a broadcast row per book, then a
  receipt row per book. `lastTxHash` moves at the broadcast stage. The
  plan application is unchanged: it follows the returned records. Every
  reader counts a step by its FINAL row per hash (`Ledger.tx_hashes()`
  is a set); `Ledger.open_tx()` is
  the broadcast rows whose hash has no FINAL receipt row. A
  `receipt` row written `unconfirmed` (the receipt read failed or timed
  out) is NOT final — the step is still OPEN, probed once per tick like an
  open broadcast row, and closed by the receipt the chain finally answers
  (`reconciled` appended: `receipt read at tick`, and the receipt's
  `block` / `gasUsed` / `effectiveGasPrice`, which also move the freshness
  bar `lastActBlock` (P7)), by the expiry or by the nonce-passed rule; so a hash
  can carry three rows. The plan is not re-applied: the act path already
  counted the `unconfirmed` record as landed. Nor is it
  re-applied for a hash that already carries a `receipt` row of any
  status, whichever pass re-opened it — that hash reached the stage where
  the act path applies the plan. Why two stages: a single `tx` row
  written only after `submit` returned, i.e. after every receipt of the
  burst (up to 120 s a step), would lose the row of a step the chain
  took if the keeper restarted inside that window — a deploy, a crash,
  the unit's `Restart=always`.
- **Open broadcast rows after a restart.** At boot, armed with a
  chain, every book's `broadcast` rows whose hash has no FINAL `receipt`
  row in the same ledger are OPEN (a `receipt` row `unconfirmed`
  does not close a hash, at boot or at a tick). Each open hash is probed ONCE at boot with the
  chain's receipt read (`Chain.receipt`): found → its receipt row
  (`reconciled: receipt read after restart`, the final status from the
  receipt, a collect's drain decoded from its logs); not found → the row
  stays open and is probed again on every tick of that book (one read
  per open hash per pass; a chain read that fails leaves it for the next
  pass) until the receipt is found or the operator's CONFIRMED nonce
  (`latest` count) is above the row's nonce with the hash still unknown
  → a receipt row `status: not_sent`, `reason: nonce passed without a
  receipt after restart`, `reconciled` naming the count read. The set is
  bounded: a row older than `OPEN_TX_MAX_AGE_SEC` = 86,400 s — a module
  constant in `keeper/core.py`, not a config key (a default is MAJOR and
  this is not a policy) — is closed `not_sent`, `reason: open broadcast
  row expired`. These rows carry a null basis (a chain read, like a
  `kill` row). The `resumed` row carries
  `openTx` (what stayed open after the boot probe); `status` carries the
  per-book count (P3). A dry run broadcasts nothing, so nothing is open
  and nothing is probed.
- **The plan advance of a boot-closed landed act.** A boot-opened
  row closed `success` ALSO applies the plan effect of the decision it
  belongs to, exactly as the act path applies `plan_after` once every
  record of a decision landed — the boot close is the one place where
  "landed" is learned late; without the advance the plan would keep the
  pre-act intent, the next valid read would contradict it and the
  external-change guard would pause the book. The decision comes from the
  book's own ledger (`Ledger.decision_of_tx`: the system the kind signs,
  the perception basis, the members the row names or its `count`, in
  ledger order — no row names its decision) and the STRATEGY states the
  effect (`decision_from_ledger` / `plan_state_for`): the core never
  writes `plan` itself and never ADOPTS the world, which would mask a
  real external change made while the daemon was down. Only `success`
  advances (a `reverted` or `not_sent` close leaves the plan as the world
  last confirmed it, the act path's own rule), only a row whose system
  moves a plan entry (`system.harvest.start` / `system.harvest.stop`),
  only a BOOT-opened row, and never a hash that already carries a
  `receipt` row. A decision that cannot be recovered leaves the plan
  untouched and says why on the row (`tx.planAdvance`) and in the log —
  never a guess, never an exception into boot. The plan is saved
  (`store.save`) and the runtime with it, so the advance survives a
  second restart. The same close moves the freshness bar `lastActBlock` for a
  boot-closed `success` row with a block (otherwise, after a restart,
  the book could decide on a read older than an act that landed), and
  the act clocks `lastActAt` — and,
  for `system.kami.use.item`, `lastFeedAt` — to the row's own time,
  monotonically. Those two clocks are report-only and the interval feed
  mode's clock; the HP floor and the feed-idempotence guard read the per
  kami `feedGuard`, which the close never touches, so no feed a floor
  needs can be withheld by it.
- **Runtime persistence.** What a book has done between ticks —
  `lastCollectAt`, `lastFeedAt`, the feed guard (`feedGuard`:
  `lastFeedHp`, `expectedHpAfter`), the craft counters (`crafted`,
  `parked`, and the learned `staminaMin`), the P7 counters (`contradictionTicks`, `contradiction`,
  `matchingSinceBlock`), `lastActAt`, `lastTxHash` — is written to
  `<dataDir>/books/<id>/runtime.json` (schemaVersion, bookId, savedAt,
  runtime) atomically (temp + rename) whenever it changes, and restored
  on boot before the book's `resumed` row, which states it:
  `runtime.restore` = `restored` | `missing` | `unreadable: <why>`, with
  the restored `lastCollectAt` / `lastFeedAt` / `lastActAt`. A missing
  or unreadable file restarts the clocks at the next tick, and the row
  says so. Without this file every restart would reset every collect
  timer to the restart time. Tick bookkeeping (`nextTickAt`, `ticksSinceAct`, the tick-row
  change key), errors, degraded flags and per-tick scratch are not
  persisted: they are re-derived on the first tick. `backoff`
  (per kami: `until`, `kind`, `reason`, `since`) and `drainRate` (the
  last harvesting drain seen per kami, the hold rule's input) join the
  persisted set; so does `startQueue` (per kami: the clock at which
  the keeper first read it ready to start), so a restart mid-wait does not
  restart the `startBatchWaitSec` wait; so does `chainRests` (kami → the clock of its last chain-backed floor-rest attempt; entries older than `chainFeedCooldownSec` are dropped). A `harvestAndFeed` book
  persists its drawn collect due time: `nextCollectAt` (epoch s),
  `collectIntervalSec` (the draw) and `collectDraws` (the draw counter).
  The generator is `random.Random` seeded with `"<bookId>:<collectDraws>"`
  — no wall clock: draw n of a book is the same number in every process,
  and two books never share a sequence. A restart restores the due time
  and draws nothing. A runtime restored WITHOUT `nextCollectAt` (a stored
  runtime that carries no due time; also a missing `runtime.json`) draws
  ONCE on its next valid HARVESTING tick, from `lastCollectAt`: due time =
  `lastCollectAt` + draw, and a due time already past is a collect due
  now. The `collect` decision's parameters carry `drawnIntervalSec` next
  to `sinceLastCollectSec`. Collects are never batched BY due time nor
  re-synchronised across books: the collect pooling (P6) still joins only what is
  due in the same tick pass. A LADDER book persists `ladder`
  `{item, heal, steppedAt}` — the rung in force and the clock
  `tierUpCooldownSec` runs against, which moves only when the ITEM
  changes, so a restart neither resets the step window nor forgets which
  rung the node last justified (the stale and chain branches read it).
- **Not persisted, by design.** The node census (per node and mode,
  shared by the books on the node) and the per-pass attacker cooldown
  reads live in the keeper process only: a restart reads the node again
  on its first pass, and the preview cache below is what keeps a restart
  from re-fetching every preview.
- **The watchlist and its reads.** `<dataDir>/watchlist.json` —
  `{updatedAt, killers[{kami, account|null, lastKillAt}]}` — is written
  by the operator's own tooling and only ever read by the keeper (example:
  `deploy/examples/watchlist.example.json`); it is re-read on a tick pass
  when its mtime moved, and a missing or unreadable file is an empty list
  that measures nothing. What those kamis serve against the node lives in
  its own store, `<dataDir>/watch/<nodeIndex>.json` (per node: `entries
  {killerKami: {fetchedAt, rows {ourKami: threshold, as served}}}`,
  written atomically and restored lazily) — NOT in the preview cache,
  whose entries are presence-managed and would drop or remember an
  attacker that is not on the node.
- **Keeper-level persistence.** The threshold preview cache is
  `<dataDir>/previews/<nodeIndex>.json` (per node: `entries
  {attacker: {fetchedAt, block, attackerLevel, attackerAccount, rows
  {ourKami: {threshold, level, reason}}, cooldownSec, absentSince}}`)
  and the owner ledger is `<dataDir>/owners.json` (`lastScannedBlock`,
  `kills` / `casts` per node per owner, `unattributed` per node,
  `killedAccounts {owner: {victimAccount: lastBlock}}` — the block of
  the owner's LAST kill or cast on a kami of that account —,
  `hostileKamis {attackerKami: {lastBlock,
  node, victimAccount, ownerAtKill, nodes {node: lastBlock},
  victimAccounts {account: lastBlock}}}` written by the same record call
  for EVERY attributed kill the scan sees, whoever the victim is (the
  first four fields are the kami's last kill; the two maps answer "any
  victim on this node, or one of the account's own kamis on any node" once a kami kills twice; an
  unattributed kill and a cast write no kami row), and
  `seeded {ourAccount: {kamis[], at, block}}` — the kamis whose lens
  `battles` were read for the seed, so a restart or a second book of the
  account re-reads none; the seed can only ADD to or RAISE
  `killedAccounts` / `hostileKamis`, takes only deaths inside the window
  plus one day and never touches `kills`); both atomic
  (temp + rename), both restored on boot — a restart is not a refetch
  storm and the kill scan resumes where it stopped (a missing file
  starts the scan at head − `hostileWindowHours` × 1,440 blocks, the
  SPEC D2 block time). Kills, casts, `killedAccounts` and `hostileKamis`
  rows older than the largest running `hostileWindowHours` plus one day
  are pruned (nothing in the ledger is kept for ever). `owners.json`
  carries its OWN `schemaVersion` (`0.2.0`; an older file
  carries the package stamp `0.1.0`): an older file loads and is
  rewritten — its `killedAccounts` blocks are first-kill blocks, kept as
  they are (at or below the true last kill, so they can only forgive
  EARLIER, never bind longer) and raised by new rows; a NEWER file is
  refused — the ledger starts empty, the boot log says so, and the first
  save moves the refused file to `owners.json.v<version>.refused`.
  **Rows dated by the chain.** Every kill / cast row
  carries `blockTs` — its own block header's `timestamp`, read once per
  distinct block per scan pass (`eth_getBlockByNumber`, at most 50 reads
  a pass; the `eth_getLogs` reads are not counted against that cap) and null when the header
  was not read — and `killedAccountsTs {owner: {victimAccount: lastTs}}`
  sits beside `killedAccounts`, a `hostileKamis` row gains `lastTs`,
  `nodesTs {node: ts}` and `victimAccountsTs {victimAccount: ts}` beside
  `lastBlock` / `nodes` / `victimAccounts`. **A ts entry always describes
  the row that holds the block entry beside it**: the row that
  raises a block entry writes its own stamp there, an UNDATED later kill
  CLEARS it — that entry then falls back to the block floor instead of
  being judged by an older row's date — and a row that does not raise the
  block touches neither table, however late its stamp. Every window read (and `prune`) compares WALL-CLOCK
  time for a dated row and does not consult the head at all; an undated
  row — one stored without a stamp, or whose header read failed — keeps the
  block-clock floor (D2). The keys are ADDITIVE and `schemaVersion` does NOT move:
  an older build after a rollback must keep LOADING the file (`load()`
  refuses only a NEWER version, which would empty the owner ledger's kill
  memory) and simply drops the keys it does not know at its next save.

Enforcement: row-schema test; a live gate that replays a recorded day and
asserts every keeper tx hash on chain has a row; the recorded-receipt
decode test (`tests/fixtures/receipts/yominet_32677500.json`: the
drained harvest entity reads 1798, the independently computed amount; the
undrained one reads null with its reason); the restart tests (a restart
mid-window keeps the collect due time; a missing / unreadable
`runtime.json` is stated on the `resumed` row).

## P6 — Signer contract

- Operator keys only. The names manifest lists `*_OPERATOR_KEY` names;
  a test proves an `*_OWNER_KEY` present in the same store is never
  loaded.
- Key store adapter: `env` (file), `gcp-sm` (Secret Manager via the
  host's service account), `keychain` (dev). Selected by
  `KAMI_KEEPER_SECRETS_BACKEND`.
- Key VALUES never in `os.environ`, argv, logs, status, ledger, or
  exception text (the harness D5 rule, same ast-scan test).
- System whitelist enforced at the send path: harvest start, harvest
  stop, harvest collect, kami use-item restricted to the book's
  `foodItem`, every rung of its `foodLadder` (empty without the
  key, so a static book is unchanged) and the
  configured revive items **from inventory only — never a purchase**
  (`listing_buy` is outside the whitelist; buying is the agent's
  economic decision), craft. Everything else — transfer,
  liquidate, move, trade, cast, listing, pool — is refused before
  signing, whatever the caller.
- **Load-on-grant.** The keeper reads `{LABEL}_OPERATOR_KEY` from its
  store when `grant <label>` arrives, never at boot, and re-reads on
  every grant: on lab hosts the harness writes the operator key to the
  shared file mid-run (`create_operator_wallet`), so a boot-time cache
  would be stale or empty. The keys file path is named ONCE,
  `KAMI_KEEPER_KEYS_FILE` (lab: `/srv/keys/.env`).
- One nonce lane per operator; the pending nonce is read once per
  burst; consecutive nonces within a burst; receipts reconciled before
  the next burst (the harness 3.7.0 reconciliation rules, cited).
- **The nonce cursor and the sequence-mismatch retry.** The lane
  keeps a CURSOR across bursts — the nonce after the last step this
  process broadcast on it (never past a step the node refused; reset
  when the operator address changes on a re-grant) — and a burst signs
  from `max(pending count read, cursor)`: the pending read stays (it is
  what catches an external sender on the same operator), the cursor is
  what stops two of the keeper's own bursts colliding inside one tick
  pass, because the node's pending count lags its own admissions by a
  block or so (without the cursor, two books of one account read the
  same pending count, the second burst is refused with `account sequence
  mismatch`, and the reconciliation rule "a nonce below the pending count
  was held" would label its row `unconfirmed` with a hash the chain never
  saw, left to wait out the receipt timeout). A burst refused
  pre-send does not move the cursor. A broadcast refusal whose text is a
  sequence / nonce mismatch (`account sequence mismatch`, `incorrect
  account sequence`, `nonce too low`) is the node saying it did NOT take
  the step, and the pending count cannot prove otherwise: the "sent"
  claim is proved by `eth_getTransactionByHash` (pending or mined)
  after a short grace — one poll at ~2 s, once more at ~5 s. Known → the
  row is `unconfirmed` as any held step (`reconciled: transaction known
  to the node after a sequence mismatch`). Unknown → the step and every
  step of the burst never offered behind it are re-signed ONCE at fresh
  nonces — the largest of a re-read pending count, the nonce the node's
  text said it expected, and the nonce after any step of the same burst
  the node did take (a JSON-RPC batch is admitted item by item, so a
  step behind an off-by-one mismatch lands on the first body) —
  broadcast and reconciled again on the same row set: `nonceRetry:
  true`, the first refusal kept as `broadcastError`; a retry the node
  refuses too is `not_sent` with `reason: nonce race: <text>`, which the
  `auto` strategy never treats as the chain's verdict on the kami (no
  `startBackoffSec`, no `deviation` row — re-decided next tick). A hash
  the node does not know is never waited on. A receipt read the RPC does
  not answer after a broadcast leaves the row `unconfirmed` with its
  hash and nonce (`reconciled: receipt read unavailable (…)`), so a
  broadcast step always gets its ledger row (closing the one keeper-side
  path that could sign without a row).
- **`submit` reports in two phases.** `submit(intents, config,
  on_broadcast=None)` keeps its signature for every existing caller; with
  a callback the LiveSigner calls it ONCE per burst, after step 5
  (reconcile and the nonce-race retry) and before step 6 (receipts), with
  every record of the burst in its post-broadcast state — `unconfirmed`
  with `txHash` and `nonce` (plus `reconciled` / `broadcastError` /
  `nonceRetry` when they apply) or `not_sent` with its `reason` — and
  also when nothing survived the pre-check (every record `not_sent`,
  known in full). The DryRunSigner never calls it: nothing is broadcast.
  The core writes the P5 `broadcast` rows from the callback and the
  `receipt` rows from the returned records.
- Gas ceilings cited from the harness SPEC at a named version (batch
  caps: start 31, stop 15, collect 17, feed 3.5M), never re-measured
  silently; a cap that differs from the cited one is stated here. Three
  are set DOWN here: start 10 (batched starts above 10 were refused by
  the chain's `eth_call` pre-check at the per-transaction gas limit in
  the samples observed; a cap above 10 needs a measurement first), stop
  12 (a batch of 15 reverts the `eth_call` pre-check) and collect 12 (a
  batch of 17 collects reverts the same pre-check, and no collect batch
  above 12 has been observed to land).
- **Batched collects.** A `collect` decision never goes out in its
  book's own burst: it is pooled per ACCOUNT for the tick pass, and at
  the end of the pass (or of a direct single-book tick) every pooled
  collect of one account goes out as ONE `executeBatched` collect of at
  most 12 kamis (the cap above; more is chunked in decision order). One
  `tx` row per book, sharing the hash, each with `batchSize`. A book
  stopped between its tick and the flush leaves the pool (logged, no row).
  Feeds are NEVER batched: `system.kami.use.item` has no batch form.
  The same signer-failure handling (NotGranted / refused → `failed`;
  `rpc-unavailable` → the flag, the book stays running) applies to every
  book of the batch.
- **Batched starts and stops (`auto`).** A book's `rest` decisions
  of one tick go out as `executeBatchedAllowFailure` stops of at most
  12 kamis, its `start` decisions as `executeBatched` starts of at most
  10 (the caps above); a feed is one `use-item` per kami, never batched.
  A batched start or stop that does not land (its pre-check, the node,
  or the chain refused it) is re-offered ONCE in the same tick as one
  single per member with its own pre-check (`batchFallback: true` on
  the rows, the batched-collect fallback pattern below), so one refused
  kami does not hold the others; a SINGLE the chain refuses puts that
  kami on `startBackoffSec` (a `deviation` row, `backoff` in the runtime)
  and it is not offered again until then. Because `AllowFailure` lands
  even when a member reverts, a batched stop's plan marks each member
  RESTING `pending`; the next valid read settles it (P7). **Holding a
  ready kami for company.** With `startBatchWaitSec` above 0 a
  READY kami (RESTING at or above `resumeHpPct`, above its bound plus an
  hour of drain, not held, not in backoff) is not started on its own
  tick: it enters the book's `startQueue` (runtime, persisted) stamped
  with the clock it was first read ready, and every queued kami goes
  out as one batched start (cap 10; chunked above) once `startBatchMin`
  of the book are ready or the OLDEST has waited `startBatchWaitSec`.
  Queue membership is re-derived every valid tick from readiness: a
  kami whose bound rises above its HP, enters backoff, is held, or is
  no longer RESTING leaves; a start that does not land keeps its place
  (it is released again at once). Waiting kamis are the tick note
  `startWaiting[] {kami, readySinceSec}` and the status count
  `kamis.waiting`; with the default 0 nothing is queued and each ready
  kami starts on its own tick. **The stop pack.** With
  `stopJoinWindowMin` above 0, on a tick where the `auto`
  book must rest at least one kami (an ANCHOR: HARVESTING on the node at
  or below max(bound, rest floor) — the rest rule, unchanged), the book
  also rests JOINERS: HARVESTING kamis on the node whose PROJECTED minutes
  to their own limit — (hp − max(bound, restFloor)) ÷ this tick's drain
  rate × 60 — are at most `stopJoinWindowMin`, nearest first, until the
  pack holds `stopPackMax` kamis (anchors first; when the anchors alone
  reach or exceed `stopPackMax` no joiner is added and the anchors are
  chunked at the stop cap 12 as with the key off). A joiner needs a drain
  rate read THIS tick (`hpRatePerHr` on the party read, the runtime
  `drainRate` the `rest` row already records); without one it is not a
  joiner. Never a joiner: a kami in rest backoff, a kami with an UNKNOWN
  bound (`held`: its limit is not known, so its distance is not), a kami
  of a FEED book (`regenMethod` FEED rests nothing — the pack is a
  REST-book rule), a kami under the stale-lens or chain-fallback paths
  (those rest at the floor only and add no joiner). The rule is
  one-directional: a pack only ever stops a kami EARLIER than its limit;
  no kami is ever held past its limit to wait for a pack, and an anchor
  always goes out this tick whatever the pack. With `stopJoinWindowMin`
  0 (the default) nothing is projected and the book rests exactly the
  kamis at or below their limit. The projection uses the SPOT rate (this
  tick's `hpRatePerHr`), never a segment average. **The cross-book stop
  pool.** With `poolStopsAcrossBooks` `true`, a `rest` or `failsafe-stop`
  decided on the NORMAL assess path does not go out in the book's own
  burst: it is held in a pool keyed `(account, nodeIndex)` and flushed at
  the end of the tick pass (or of a direct `tick_book`). The pool packs
  WHOLE decisions, first fit in arrival order, at most 12 kamis to a
  chunk — a decision is already capped at 12 by the book's own batching,
  so it is never split across chunks, and a decision holding more than 12
  is not pooled at all (it stays in the book's own burst, logged) — and
  sends each chunk as ONE `executeBatchedAllowFailure` stop. Each chunk
  writes one `tx` row per BOOK of the chunk, sharing the hash, carrying
  that book's own `kamis`, the CHUNK's `count` and `batchSize`, and `kami`
  as the call set it (the index when the chunk is one kami of one book,
  null otherwise); each book moves its own `lastTxHash`, `lastActAt`,
  `ticksSinceAct` and `lastActBlock` (P7), applies its OWN decision's plan
  when the chunk landed, and takes its own strategy's record rule
  (`on_records`) whether it landed or not. A chunk of more than one kami
  that does not land re-offers each pooled DECISION once as its own stop
  with its own pre-check in the same tick, `batchFallback: true` on the
  rows, through the book's `split_batch` — so the stop-pack and
  start-join rule holds and a JOINER of a pack that did not land is not
  re-offered as a single. A book stopped between its tick and the flush
  leaves the pool (logged, no row). A stop decided under a stale lens or
  from the chain fallback is NEVER pooled: those are degraded-mode acts
  and go out at once. Pooled stops are sent before pooled collects, and
  feeds are not pooled at all: `system.kami.use.item` has no batch form.
  With `poolStopsAcrossBooks` `false` (the default) nothing is held and
  every book's stops go out in its own burst.
  **The kill-forced tick.** With `killForcesTick` `true`, a
  kill the scan records on a book's node whose attacker's OWNER the book
  reads `killed-here` sets that book's `nextTickAt` to the scan's `now`,
  so the book assesses in the SAME pass — `tick_due` scans under the lock
  before the book loop, and the loop admits a book whose clock has passed.
  The clock is only ever brought FORWARD (a book already due earlier keeps
  its earlier clock); an UNATTRIBUTED kill, a `listed` owner and a
  `standing`-only owner force nothing, because a standing owner already
  bound the book before the kill and raised no line. With `killForcesTick`
  `false` (the default) a book assesses a kill at its own next tick.
  **The start join.** With `startJoinWindowMin`
  above 0, on a tick where the `auto` book STARTS at least one kami (the
  start hold releases its queue, or with `startBatchWaitSec` 0 any ready
  kami starts), the book also starts JOINERS: RESTING kamis of the book,
  not ready (below `resumeHpPct`), whose PROJECTED minutes to
  `resumeHpPct` — (ceil(resumeHpPct × hpTotal ÷ 100) − hp) ÷ this tick's
  regen rate × 60 — are at most `startJoinWindowMin`, nearest first,
  filling only the FREE places of the last start chunk (the
  per-transaction cap 10: with 4 starts going out, at most 6 joiners;
  with 10 or 20, none — a joiner never adds a transaction). The regen
  rate is the party read's `hpRatePerHr` of that RESTING kami this tick;
  a kami without a positive rate is not a joiner.
  Every other start gate is unchanged and applies to a joiner as to a ready
  kami: its bound is KNOWN (never `held`), its HP exceeds its bound plus one
  hour of its drain (the same `need` as the ready rule), it is not in
  backoff, not on the chain's action cooldown, not the tick's probe. A
  joiner is only ever started EARLIER than its own readiness — nothing is
  held longer, the start queue is untouched (a joiner was never in it),
  and a joiner's `readySinceSec` is 0. A joiner of a start batch that did
  not land is not re-offered as a single (only the ready members are, as
  the stop-pack fallback does for stop joiners); it is simply read again
  next tick. With `startJoinWindowMin` 0 (the default) nothing is
  projected and the book starts only kamis at `resumeHpPct`.
- **Clocks move on landed records only; a failed batch falls back to
  singles.** `lastCollectAt` (on a `collect` or a
  `start`) and the craft `crafted` counter advance only on a record
  that LANDED — `success` on chain, or a dry run whose pre-check
  passed; a `not_sent`, `reverted` or `unconfirmed` record leaves the
  clock where it was, so the collect is due again next tick. The
  drawn due time `nextCollectAt` follows the same rule and is redrawn in
  that ONE place — with `lastCollectAt`, on a landed `collect` or
  `start`, from the landing tick; a record that did not land keeps its
  due time and its draw. When a
  batched collect of more than one kami does not land (its pre-check,
  the node, or the chain refused it), each running member is re-offered
  ONCE in the same tick as a single collect with its own pre-check:
  survivors land and move their clock, refusals stay due. The batch's
  own `tx` row (per book, `batchSize: N`) is written first; each
  single's row carries `batchSize: 1, batchFallback: true`. A batch of
  one has no fallback.

Enforcement: manifest test; ast-scan test; whitelist unit test that
every non-whitelisted system id is refused; a nonce test on a forked
chain.

## P7 — Interference rule

- **Unconditional, both profiles:** the keeper never resends a plan
  the world has contradicted.
- `external-change` is narrowly an external transaction that changes
  the HARVEST STATE or NODE of a book's kami (start, stop, collect,
  move by another signer). A feed, level-up, skill allocation or item
  use by the operator or the agent is coexistence — an observation in
  the ledger, never a trigger.
- **A kami absent from the account's answer.** No row for a
  book kami on an otherwise VALID read (P4) is a contradiction of that
  kami — `kami N: not in the account's party` (transferred, sacrificed,
  listed) — counted like any other on `externalChangeTicks` consecutive
  reads, each of which is a gated tick meanwhile; the confirmed change
  pauses the book with that reason. `resume` under `lab` answers
  `PLAN_CONTRADICTED` naming the kami while it is absent; the `fleet`
  re-check reads `world does not match plan` (no lens flag: the read is
  valid) and re-adopts only once the row is back for `readoptAfterBlocks`.
- **A pooled stop is the keeper's own act, for every book in the
  chunk.** A stop that left its book's burst for the account+node pool is
  never an external change: its `tx` row carries that book's own `bookId`
  (and its own `kamis`), the book applies its OWN decision's plan from the
  shared receipt, and the act bar `lastActBlock` is set per book from that
  receipt's block — so the next tick compares the world against a plan the
  book itself asserted, on a read no older than the tx, exactly as after a
  stop the book sent alone.
- **Pending rests and the start refusal (`auto`).** A member of an
  `executeBatchedAllowFailure` stop is planned RESTING `pending`: on
  the next valid read RESTING confirms the plan and HARVESTING on the
  book's node means the stop did not apply to it (`restNotApplied`,
  re-decided alone) — neither is an external change; harvesting
  ELSEWHERE still is. `start` on `auto` refuses the whole start
  (`BAD_ARGS`, naming the kami) when a book kami is HARVESTING on
  another node: a plan the world already contradicts is never asserted.
- **Persistence.** A contradiction counts only on a VALID read
  (P4: not gated, not incomplete, not stale). It must be observed on
  `externalChangeTicks` (default 3) CONSECUTIVE valid ticks before it is
  an `external-change`: the first reads are `tick` summary notes, and
  the book asserts nothing against them (the unconditional rule holds
  from the first read). A valid read that no longer contradicts resets
  the count (noted on the tick); a gated tick neither counts nor
  resets.
- On the confirmed `external-change` the book records the lifecycle
  row and PAUSES (`pausedReason: external-change`, `pausedAt`,
  `contradictedAt` = the lens block), under both profiles. Then:
  `lab` → paused until the agent's `resume`, which re-evaluates the
  world first (P8) and never re-asserts; `fleet` → the paused book
  re-checks the world every tick on valid reads only, and once the
  world has matched the book's plan (every owned kami on the node or
  RESTING, none DEAD) for `readoptAfterBlocks` blocks — counted from
  the first matching read, reset by any read that does not match — a
  separate logged `readopted` transition adopts the observed state and
  the book runs in the same tick. It never re-adopts against a
  contradiction, however long the quiet.
- A read older than the book's own last landed tx is not an
  observation of the plan: after a tick that landed a tx at receipt block
  B the book keeps `lastActBlock` = B (runtime, persisted) and decides
  nothing — no pending-settle, no death step, no P7 comparison, no
  assessment — on a read whose lens lower bound is below B (the
  mirror block `meta.blockNumber` is the chain head the daemon knows, not
  an applied cursor — a read at receipt + 1 can still lack the book's own
  act — so it does not count). WHICH bound: on
  lens 1.0.0 the APPLIED mark, `lensAppliedThrough ≥ B` (the 1.0.0 README
  item 4: that is the number which answers "is my transaction reflected
  here?"), and the read is additionally TAKEN with `--at-least B`, so the
  daemon holds it until the mark arrives and refuses `NOT_APPLIED`
  otherwise; on 0.6.3, which serves no applied mark,
  `lensReconciledThrough ≥ B`. Either way the tick row reads
  `awaiting a read past the last act`. Without this bar a read below B
  re-offers an act that already landed, and the plan/world mismatch that
  leaves reads as an `external-change` with nothing external on the chain.

Enforcement: simulation test on a recorded node history (persistence,
the gated-tick rule, the fleet re-check and re-adopt, the lab pause);
profile test.

## P8 — Control

- `grant <label>` / `revoke <label>`: only the account LABEL crosses
  the socket; the keeper loads the key from its own store at grant
  time (P6). The keeper holds no signing capability for an account
  until granted; a grant is visible in `status`. Under `lab` the grant
  arrives only over the socket (the `grant` command; `revoke` is its
  reverse); under `fleet` it may be
  declared in config at deploy. `revoke` first stops every running
  book of the account (`stopped_by: agent`, reason `grant-revoked`)
  and returns the quiescence proof below.
- `start` / `stop` (immediate, or after the current burst — both
  defined) / `pause` / `resume`. `resume` on a book paused by
  `external-change` re-evaluates the world on a VALID read first: an
  invalid read refuses with the lens code; a world that still
  contradicts the plan (a book kami harvesting elsewhere, or DEAD)
  refuses with `PLAN_CONTRADICTED` and the book stays paused; a world
  in a state the book can own is adopted as the plan (the agent's
  explicit re-adopt, `resumed` row with the plan) and the book runs.
- **`park <book>`.** Only a RUNNING `auto` book parks
  (`BAD_ARGS` on any other type, on a stopped or failed book, and on a
  paused one — resume it first); a book already parking answers
  `alreadyParking: true` and changes nothing (idempotent). From the next
  tick the book is PARKING, a persisted mark (`parking {since, by}` on
  the book, null otherwise; it survives a restart and stays on the
  stopped book as history): it offers NO start, feed or collect; on every
  tick with a VALID read (a stale lens, an outage and the chain fallback
  decide nothing — the tick is gated with `parking: true`) it offers
  `harvest stop` for its kamis HARVESTING on its node that are not on the
  chain's action cooldown (deferred, `cooldownDeferred`, as for a rest)
  and not in rest backoff, in packs of at most `PARK_PACK_MAX` = 10 kamis,
  at most `PARK_PACKS_PER_TICK` = 5 packs per tick (one burst, consecutive
  nonces on the account's lane — 50 kamis a tick, a 300-kami book in ~6
  ticks), the plan entry of every member `pending` as a normal rest's; a
  pack that did not land is re-offered as singles once and a refused
  single backs off, exactly as a rest; deaths are recorded and revives
  offered as on any running book. The book stops ITSELF (`stopped_by:
  agent`, reason `park complete`, the `stopped_by` row carrying `parked
  {kamis, resting, dead, elsewhere, lensBlock, lensAppliedThrough}`) on
  the first tick whose valid read — past the book's last landed act, as
  every read is (P7) — shows none of its kamis HARVESTING on the node,
  none DEAD and no plan entry `pending`. A DEAD kami never completes a
  park (it is revived first; with no revive item the book stays parking
  with `parkRemaining 0` and `kamis.dead` above 0 until the operator
  ships one or stops the book by hand). A kami HARVESTING elsewhere is
  P7's business (a contradiction, as on any running book) — counted
  `elsewhere`, never stopped, stated on the completion row. `stop`,
  `pause` and `revoke` act on a parking book as on any running book.
- `stop --all --account <X>` is idempotent and returns the stopped set
  plus a quiescence proof: pending nonce == confirmed nonce, last tx
  hash and block.
- `armed: false` dry-run: full perception and decisions, no signing,
  ledger rows marked `dry_run`.
- Restart semantics: a keeper restart RESUMES `running` books with
  their persisted runtime (P5: the collect and feed clocks, the
  feed guard, the craft counters) and writes a `resumed` row stating
  what was restored; stopped books never restart on their own.

Enforcement: idempotency test; quiescence test on a forked chain;
restart test.

## P9 — Non-goals with enforcement

- No model calls, no network beyond the chain RPC (HTTP + WS) and the
  lens socket — dependency scan and egress test.
- No Kamiden client, no Kamibots API, no oracle token — dependency scan.
- No attack, cast, transfer, trade, move — P6 whitelist.
- No policy language in agent-visible strings: status, logs, errors
  state observation + action + parameters; forbidden-word scrub test
  over every formatted string (`predator`, `enemy`, `danger`, `threat`,
  `safe`, `unsafe`, `trusted`, `saves`, `efficient`, `consider`,
  `harmless`, `armed`, `hostile`, `passive`, MUSU/day rates,
  budgets, deadlines).
- No book-level field that reveals a run window is ever served.

## P10 — Deployment profiles

`profile: fleet | lab`, set at start-up, reported in `meta`. **`lab` is
the default**; `fleet` must be asked for. Every profile-gated feature
is one row of a registry `(feature, fleet, lab)` in code, with a test
that every `profile` branch in the codebase maps to a row and a test
per row that `lab` answers `PROFILE_FORBIDS`. Lab provisioning mounts no
roster file, and under `lab` the keeper refuses to open one even if
present. The lab bring-up asserts `meta.profile == lab` on
its first `status` and refuses to launch otherwise.

| aspect | fleet | lab |
|---|---|---|
| key backend default | `gcp-sm` | `env` (SSH-stdin injected, ro mount) |
| grant | config at deploy | socket only |
| friends inputs | config + optional roster files | config only, default empty |
| external-change (confirmed on `externalChangeTicks` valid reads, both profiles) | pause; re-check every tick; `readopted` after `readoptAfterBlocks` of a matching world | pause; `resume` by the agent only (re-evaluated first) |
| loopback status page | allowed | forbidden (`PROFILE_FORBIDS`) |
| book caps | 0 = unlimited | REQUIRED (`maxBooksPerAccount`); absent = refuse start |
| `readoptAfterBlocks` (common key on all three types) | allowed, default 240 | refused; never defaulted in |

## P11 — Versioning

SemVer. MAJOR = any agent-visible change of behaviour on a default call
(config default, state name, whitelist, description-relevant text).
`schemaVersion` in every response. A rename ships both names for one
release.

# Depends

## D1 — kami-lens daemon

Pin: ≥ 0.6.1. **Tested against 0.6.3 AND 1.0.0 (`0ffc8a7`) —
`LENS_TESTED_VERSIONS` — and the same build serves either.**
The client reads `status.version` on every perception and gates each
1.0.0 feature on it (`keeper/lens.py` `LensFeatures` / `features_for`:
all flags true iff the version parses as SemVer with MAJOR ≥ 1); where a
feature is off nothing is sent, so a request to a 0.6.3 daemon carries
no 1.0.0 option. One belt
covers a wrong version string: an option the daemon answers `BAD_ARGS`
"unknown option" for turns its flag off, is stripped with its value, and
the read is retried once. What 1.0.0 changes here:

- **The applied mark.** `meta.appliedThrough` on every answer and
  `status.sync.appliedThrough` — every write of every block up to it is
  applied to the mirror, so "is my transaction reflected?" is
  `appliedThrough ≥ the receipt's block`. It is the staleness gate's
  lower bound whenever it is served; `reconciledThrough` (now the slower
  chain-proof mark, which starts lower after a boot and lags up to one
  reconcile interval) remains the bound on 0.6.3, which serves no applied
  mark anywhere.
- **`--at-least <block> [--max-wait <ms>]` on every world read — the
  act→verify primitive.** The keeper sends it as the freshness bar of the
  tick after an act (P7): `LensClient.freshness_view(lastActBlock)`
  wraps the client (or the pass cache, where the option is part of the
  cache key, so a post-act read is never served from a pre-act entry) and
  appends it to every world read, never to `status`. A refusal is
  `NOT_APPLIED` with `error.appliedThrough` — the mirror has not applied
  the block yet, not a lens fault.
- **An answer never looks complete when it is not.** A single-entity
  read of a kami the mirror cannot project (`kami`, or `node <n>
  <attacker>` whose ATTACKER is one) is refused `INCOMPLETE`; a list row
  (`party`, `node`, `roster`) keeps identity + `state` + `incomplete:
  true` with no vitals and no `liquidation` block, and is counted in
  `meta.incompleteRows`. The keeper treats BOTH as P4-incomplete: such a
  row is an unknown, never a reading and never a preview LINE.
- **`--targets <k1,…>` / `--account <i>` on `node`, applied before
  projection.** A preview read carries the account's present occupants
  plus the attacker instead of every row of the node; the NODE CENSUS
  carries no selector — it must see every foreign occupant. A list
  outside 1–500 drops the selector rather than truncate it.
- **`status.degraded` can carry `reconcile-stalled:<sec>`**, which gates
  nothing (P4): the chain re-check is behind, the mirror is not known
  incomplete. `stream-stalled` and `unhealed-ranges` gate as before.
- **The three head fields can be ABSENT** (`headBlockNumber`,
  `headSampledAt`, `blockLag`, left out together when the background
  sample is older than 60 s): the staleness reference falls back to the
  mirror block, as it already did for an older daemon's missing head. Assumed: socket protocol and
envelope; `status` (`state`, `sync.reconciledThrough`, `degraded`,
`feedsDegraded`); `node <i> --with-vitals [--stats]` for occupant
projected HP and stat blocks; `node <i> <attacker> --with-vitals` for
the pairwise liquidation preview (`threshold`, `margin`, `eligible`,
`reason`) — ONE attacker per call (the lens serves no multi-attacker
preview, so the keeper makes N calls). Threshold refresh
triggers are two-sided: a level change, skill respec or stat shift on
EITHER the attacker or the occupant, the attacker's arrival, and an
hourly floor. Also assumed: `party`, `kami`,
`account`, `inventory`. `killers` / `battles` are enrichment only —
`battles <kami>` is read once per book kami at `start` (and at boot
for a persisted auto book whose kamis were never read) to seed
`killedAccounts` (and the killer's `hostileKamis` row) from its
`isDeath` rows no older than the window plus one day; `feedsDegraded` or a refusal
skips the seed and says so on the lifecycle row, never a gate.
Every `node` read an `auto` book makes is `--full` (the 50-row cap
hides most of a crowded node); within one tick pass identical
reads are made once (`PassLens`), so N books on one node and account
cost one `node --full`, one `party --full` and one `inventory` per
pass; the measured lines ride each auto tick summary (P5).
The reads of an `auto` book by its read policy: `node <n>` serves at
most 50 rows (capped); `node <n> --full` serves every row as `id,
state, kami {id, name, index}, account` and NO vitals; `node <n>
--with-vitals --full` adds the vitals; one preview `node <n> <attacker>
--with-vitals --full` is about the size of the vitals census, of which
the keeper keeps the rows of its own account. `nodeCensus` `vitals` reads the third
every `nodeCensusEverySec`; `plain` reads the second, and takes what it
lacks — an attacker's `cooldownSec` and `level` — for the attackers that
can bind only: from `attacker.cooldownSec` and the attacker's own row of
a preview answer of the pass, else from one `kami <index>` read
(`cooldownSec`, `level`), at most `attackerVitalsPerTick` per pass and
node. `previewPolicy` `killed-here` previews those attackers and nobody
else. The census is parsed once per pass and node whatever the number of
books (and a kami's entity id — one keccak — once per process). A settled
pass of a `killed-here` + `plain` book is `party --full`, `node --full`
(shared by the node), `inventory`, and one `kami` read per present
killed-here attacker.
Kill evidence: chain logs (D2); the lens serves no `deaths` query.

## D2 — Chain RPC (Yominet)

HTTP JSON-RPC for sends and reads; WS `eth_subscribe(logs)` on the World
contract for triggers, with `eth_getLogs` gap-fill; the World address
and RPC URLs from config, never baked.

Block time: ~2.5 s is the FALLBACK rate only. The chain's block time
varies from day to day, so a window counted in blocks at a fixed rate
is not a fixed number of hours. Every wall-clock window — `hostileWindowHours`, what `prune` keeps,
the first-boot kill-scan span, a battles row placed by its timestamp — is
therefore measured against a block clock (`keeper/blockclock.py`): the
keeper samples (wall-clock time, chain head) once per kill-scan pass from
the `eth_blockNumber` that pass already makes, persists them to
`<dataDir>/blockclock.json`, and answers "which block was the chain at N
hours ago?" by interpolating them (extrapolating at the measured rate
outside the span, ≥ 1 h of samples before the measured rate is used).
Kill rows keep their `block` stamps; with no samples every answer is the
2.5 s arithmetic it replaces, so the constant still binds a cold start.
`status.blockClock` reports samples / span / measured rate — a
measurement, never a decision.

The clock is a model and the chain has the fact, so
every kill / cast row the scan records is STAMPED with its own block
header's `timestamp` (`eth_getBlockByNumber`, one read per distinct block
per pass, cached, at most TS_READS_PER_PASS = 50 a pass, counted
`killScan.tsUnread` when unread) and the owner ledger keeps that stamp
beside the block. A window read — and `prune` — compares wall-clock time
whenever the stamp is there, and the block floor (this clock, then the
2.5 s arithmetic) is the fallback for a row without one. No decision rule
changed: a kill binds for exactly `hostileWindowHours` of wall-clock time
after the block it landed in. Why: for a target time BEFORE its first
sample the clock EXTRAPOLATES at its whole-span rate, and the block rate
also moves within a day, so an extrapolated window can end early. The
clock itself is unchanged: it still feeds the first-boot scan window and
the undated fallback.

## D3 — kami-harness (measurements, not code)

Cited by version: batch caps, feed gas ceiling, nonce-at-pending rule,
reconciliation rules (3.7.0). The keeper imports nothing from the
harness.

## D4 — Deployment host

`fleet`: systemd unit on the deployment host, Python 3.11 (`deploy/`).
`lab`: Docker image on `python:3.11-slim` at an exact patch tag (the
same interpreter as the systemd deployment; hash-pinned dependencies),
built on the deployment host from a `git archive` tarball of the
manifest SHA carrying `KEEPER_PIN.txt`, refused on mismatch, no build
artifacts tracked in the repo, the Dockerfile refusing more than one
artifact; `--network host` (the egress lockdown lives in the OUTPUT
chain), `-v /srv/lens:/srv/lens` (socket), `-v /srv/keys:/srv/keys:ro`
with `KAMI_KEEPER_KEYS_FILE=/srv/keys/.env`, `-v /srv/keeper:/data`,
started after the lens reports LIVE; `NOT_READY` is a wait, never an
error. The whole `/srv/keeper` directory is pulled at close-out and
swept for key material.

# Invariants

| claim | enforcement |
|---|---|
| No transaction is signed for an account without an active grant | P8 test |
| No transaction outside the P6 whitelist is ever signed | P6 whitelist test |
| No key value appears in any output channel | ast-scan + log-scrub test |
| A book never acts on perception older than `maxStalenessBlocks` | P4 fault-injection |
| A keeper read taken after its own act is used only once the lens's applied mark (1.0.0) / verified mark (0.6.3) reaches the receipt block — and on 1.0.0 the read itself is taken with `--at-least <that block>`, so the daemon refuses rather than answer short | `tests/test_lens100.py` §4 (both versions, end to end on the tester chain) + `tests/test_phase2r.py` |
| An `incomplete` row or an `INCOMPLETE` refusal (lens 1.0.0) never becomes a line or a reading: the tick is gated, no preview row is stored, no decision rests on it | `tests/test_lens100.py` §5–6 |
| Every keeper tx hash has a ledger row | P5 live gate |
| Thresholds used for a decision come from the lens preview; the keeper computes only margin-between-refreshes, time-to-cross, and owner class — and the gap the CHAIN measured through a kill of one of the account's kamis above the served line of that pair, added to the lens's number, never a threshold computed from stats (P3a) | code structure; `tests/test_phase2hb.py` |
| Owner class flips to the killed-here class within one tick of a liquidate log on the node | replay test (`tests/test_phase2a.py::test_the_invariant_replay…`, a recorded liquidate replayed through a fake chain: `kill` row, `killed-here-48h`, the bound kami rested in the same pass) |
| An `auto` book's threshold for any (attacker, kami) is a value the lens preview SERVED — clamped at 0, or an attacker's served maximum over the account's occupants for a kami with no row — never one the keeper computed; an attacker never previewed holds the kami on the bound | preview tests (`tests/test_previews.py`), the hold test, code structure (keeper/previews.py computes no threshold) |
| `previewsPerTick` is one cap per tick pass and node, shared by the books on it; presence in the preview cache is a node fact, never a book's friends list; a remembered attacker binds like a present one and is remembered only when its owner OR the kami itself is killed-here at departure | pass-cache test, presence tests, `tests/test_phase2h.py` (the remembered kami under a clean owner) |
| Under a refresh demand above the per-pass cap the entry with the highest line is refreshed first: level-change and aged entries are offered by binding weight, arrivals and no-rows entries still ahead of them | `tests/test_phase2gc.py` |
| The kill scan re-reads `killScanOverlapBlocks` behind its cursor every pass and resolves a DEAD write against the occupants of the last `occupantMemoryBlocks` and every running node-watching book's own kamis, not only the previous census; a kill between the scan head and the census, or in a head block whose logs the node served late, is a kill row on the next pass, and a re-read row is never a second ledger row nor a second transaction read | `tests/test_phase2q.py` (the foreign occupant attributed from memory, `occupantMemoryBlocks: 0` back to `ignored`, the own kami across a restart, the three re-read passes with one `overlapSkipped` and no new transaction read) and `tests/test_kills.py` (`test_overlap_rescans_behind_the_cursor_and_known_rows_skip_the_tx_read`, `test_a_dead_write_the_node_served_late_is_caught_by_the_overlap`, `test_known_skips_casts_too`) |
| A kill row the scan could not attribute is RE-READ from the chain on a later pass — for `REATTRIBUTE_HEAD_BLOCKS` (400) behind the pass head, at most `REATTRIBUTE_MAX_CALLS_PER_PASS` (4) transaction reads a pass, oldest block first, never in the pass that recorded it — and a resolved row is promoted into `kills[node][owner]` with its OWN `block` / `blockTs`, so the window it opens starts at the kill and not at the re-attribution; a row that stays unreadable is left exactly where it is and offered again next pass | `tests/test_phase2qb.py` (the bind recovered with `reattributed: true` and the `; re-attributed N block(s) after the log` line, the kill's own date against a window read 48 h after the KILL, the row outside the head window never retried, the 4-reads-a-pass cap over six rows, the unreadable retry as a no-op offered again, one promotion only, every outcome that names no account, and the fresh attributed kill byte-identical) |
| The kill scan's `rpc-unavailable` / `ws-logs-gap:<N>` ride every auto tick and row of the pass and gate nothing; an unattributed kill changes no class; `lastScannedBlock`, the previews and the owner ledger survive a restart | scan-flag test, unattributed test, restart test |
| A batched start / stop of an `auto` book that does not land is re-offered as singles once; a refused single backs its kami off for `startBackoffSec` with a `deviation` row; an `AllowFailure` stop's members are planned `pending` and settled by the next valid read, never an external change | backoff test, pending-plan test |
| Within one tick pass identical lens reads are made once; every `auto` node read is `--full` | pass-cache test, observe test |
| Under `previewPolicy` `killed-here` an attacker that is not killed-here inside the window (by owner or by kami) and not a remembered one that came back is never fetched, never refreshed and never counted pending | `tests/test_phase2g.py` (the cold node: 45 foreign occupants, zero previews over three passes; the flip: only the killing owner's 22 kamis are ever on file; the scale test: 1,750 foreign, 2 entries) |
| With `passiveOwnersBind` false an attacker never previewed and not killed-here never appears in `unknown`, never holds a start and never binds: no entry → served maximum 0 → `no-threshold` → skipped, and a `threshold-no-kill` owner's unread kami is skipped with its owner | `test_a_cold_node_with_no_kill_reads_no_preview_and_nobody_holds_or_binds` |
| A class flip makes arrivals: the tick pass whose kill scan records the kill offers that owner's / kami's present attackers to the preview queue first, `previewsPerTick` a pass, and until each is fetched it is `unknown` — the book's kamis are held at the floor, never deployed on the bound | `test_the_flip_end_to_end_kill_row_then_arrivals_under_the_cap_then_bound` (kill row → 10 / 10 / 2 fetched under the cap → held → bound 177 → rest) |
| An attacker previewed while the account had no occupant holds at most all-but-one start: one probe start per book per tick is allowed when nothing of the account is on the node | probe tests (`tests/test_phase2gb.py`: the deadlock world → one flagged start and the rest held; a kami of the account on the node → none; an attacker with no entry at all → none; the next tick's refetch and the bound by proxy) |
| A kami without a row of its own is priced at the attacker's highest served ratio times its own max HP, never at the absolute row of a larger kami, and an owner's unread attacker the same way; a cached row that carries no served max HP keeps the absolute stand-in only where neither the row nor the book knows the kami's max HP, else it is scaled by the book's own figure | `tests/test_phase2o.py` |
| A served row is a LINE only with vitals: a row without vitals, or with a non-positive threshold under an `ATTACKER_*` reason while the attacker's own row also lacks vitals, never replaces or lowers a line — the old row is carried — and the entry is due again after `previewRetrySec`, never at 86400; with the attacker's own vitals present that non-positive threshold is stored as served and the entry ages normally | `tests/test_phase2s.py` |
| An attacker whose window expired is not refreshed any more, is kept until it leaves, then dropped | `test_an_attacker_whose_window_expired_is_not_refreshed_kept_until_it_leaves_then_dropped` |
| Under the plain census a killed-here attacker with NO cooldown read this pass — over `attackerVitalsPerTick`, or refused by the lens — binds; the `cooldownHorizonSec` skip needs a served cooldown | `test_the_cooldown_of_a_killed_here_attacker_under_the_plain_census` |
| The census is reused only by a book whose `nodeCensusEverySec` is above its `tickSeconds`, only while younger than that, and never across a kill or cast row on the node; own kamis are read every pass; the census is never persisted; a preview the lens refuses against a reused census holds what was unknown and forces a read; under a reused census the book's own harvesting kamis join the kill scanner's occupant table from the party read, so a kill of one of the book's kamis is attributed whatever the census age | census-interval tests (`tests/test_phase2g.py` §4) |
| A stored `auto` config without the read-policy keys (`previewPolicy`, `nodeCensus`, `nodeCensusEverySec`, `attackerVitalsPerTick`) reads and decides as a book that names `all`, the vitals census and a census every pass: the same lens requests and the same tick summary | `test_a_stored_config_without_the_keys_keeps_todays_reads_byte_for_byte` |
| `previewPolicy` `killed-here` and `nodeCensus` `plain` are refused with `passiveOwnersBind` true, and `nodeCensusEverySec` below `tickSeconds`; conditional defaults are pinned in the lock and its SHA | `test_conditional_defaults_at_start_the_refusals_and_the_lock`, `tests/test_defaults_lock.py` |
| One observe + assess of a 50-kami book on a node of 1,750 foreign occupants with 2 killed-here attackers takes under 1.0 s, three lens reads plus the previews (then plus one `kami` read per killed-here attacker), and previews nobody else | `test_scale_1750_foreign_occupants_50_owned_kamis_2_killed_here_attackers` |
| The default 5 % margin covers `margin_needed(drain, tickSeconds, 90 blocks, 2.5 s)` for every kami of the recorded fixture party (≤ 16.72 HP/h) by more than 8× | margin derivation test |
| `stop --all` returns only after quiescence | forked-chain test |
| `books` under `lab` never lists another account's book | profile test |
| No agent-visible string contains a forbidden word | scrub test |
| The keeper never resends a plan the world has contradicted (both profiles) | P7 simulation test |
| An incomplete lens answer (missing `state` / `hp.current` / `node.index` while HARVESTING) or a lens reporting `stream-stalled` / `unhealed-ranges` is a gated tick, never an external change | P4 validity tests (`tests/test_phase1c.py`) |
| A confirmed `external-change` needs `externalChangeTicks` consecutive valid reads; a fleet re-adopt needs `readoptAfterBlocks` of a matching world; `resume` never re-asserts against a contradiction | P7 simulation test, `resume` test |
| After a feed lands, the same kami is not fed again until the lens shows the feed applied or `feedCooldownSec` elapsed — one feed per cooldown window on a lens that never applies it | feed idempotence tests |
| Under `lens-stale` alone a `harvestAndFeed` book signs feeds only — never start / stop / collect / revive — and only once per cooldown window | stale-feed tests |
| Every owner class renders to one of the five observation-only names (`standing` is the fifth) | P3a formatter test |
| No attacker is killed-here for ever: an owner's / a kami's kill counts only inside `hostileWindowHours` of its LAST kill, a new kill re-binds for a fresh window, and the ledger prunes every table; the window is measured from the kill's block TIMESTAMP when it is known — the chain's own date — and from the block floor only for a row without one | forgiveness tests (`tests/test_phase2h.py`: 47 h binds, 49 h does not, the re-bind; an old-version `owners.json` with 44 ancient `killedAccounts` binds nobody and prunes to empty) and `tests/test_phase2tb.py` (the 47.45-h row binds dated and is forgiven undated; the raise rule, `prune`, the round trip at an unchanged `schemaVersion`) |
| An attacker is killed-here when its owner OR the kami itself killed inside the window — a kami that killed one of the account's kamis under owner A binds under a clean owner B (`hostileBy: kami`), A's new kami binds (`hostileBy: owner`), a listed owner's kami never does | transfer tests (`tests/test_phase2h.py`) |
| `owners.json` of an older `schemaVersion` loads and is rewritten; a newer one is refused, never read and never written over | migration tests (`tests/test_phase2h.py`) |
| A `lab` keeper with no `maxBooksPerAccount` does not start | start-up test |
| No key-shaped value in any socket frame | P1 frame test |
| A revive is never preceded by a purchase; a death always has a ledger row | P6 whitelist test + P5 row test |
| A DEAD kami with a revive item in inventory is offered a revive again `reviveRetrySec` after every attempt until it is alive — a refusal is never final | test_phase2p |
| A default change bumps MAJOR | CI default-diff check |
| A keeper restart keeps every running book's collect and feed clocks and its feed guard (`runtime.json`); a restart never double-feeds and never resets a collect window; a missing or unreadable file is stated on the `resumed` row | restart tests (`tests/test_phase1d.py`) |
| `musuCollected` on a collect row comes from that receipt's own harvest-entity drain, or is `null` with a `decodeError` — never a live read, never a guess | recorded-receipt decode test |
| Collects of one account due in the same tick pass are one batched tx of at most 12 kamis, one row per book sharing the hash; feeds are never batched | batched-collect tests (dry-run and tester chain) |
| The collect clock and the craft counter move only on a landed record; a refused collect is due again next tick; a failed batch is re-offered as singles once, each with its own pre-check | re-offer tests (`tests/test_phase1d.py`: fake signer and tester chain) |
| A `harvestAndFeed` book WITHOUT `foodLadder` makes the same lens reads, takes the same decisions and writes the same rows as a book whose stored config has none of the ladder keys — the ladder keys are defaulted into its stored config and never read — and it enters no node-threat path: no census, no preview, no kill scan, no `killScan` note, no `ladder` | `tests/test_phase2jb2.py::test_a_static_book_reads_and_decides_exactly_as_before` (a book with the keys against one with none), `test_a_static_book_status_carries_only_the_collect_keys` |
| A measured dip cap is used only when the watchlist is fresh (`updatedAt` inside `watchlistMaxAgeSec`) and every kami active for the book has been read for the node; otherwise the flat `ladderMinFloorPct` cap stands, and the tick's `ladder.dipFrom` says which | `tests/test_phase2jb3.py` (the stale list, the missing file, the kami not read yet, the read with no rows) |
| The keeper never writes `watchlist.json` — it reads the operator's file and nothing else; a missing or unreadable one is an empty list, never an error | `tests/test_phase2jb3.py::test_the_watch_store_round_trips_and_the_watchlist_is_never_written`, `test_a_missing_or_unreadable_watchlist_is_an_empty_list` |
| A ladder book's chosen rung never has a no-waste floor under the bound, the rest floor or the DIP CAP (`ladderMinFloorPct` × max HP, or the measured cap of `dipCapMode` `measured`) — unless NO rung does, and then it is the smallest rung in stock and the harvest is stopped only when the bound a KILLED-HERE attacker sets is above every floor (a threshold-no-kill owner binds the rung, never the harvest); it steps at most one rung UP per `tierUpCooldownSec` and any number DOWN at once; at most one feed is in flight, the feed guard included on the emergency top-up | ladder tests (`tests/test_phase2jb2.py`: the dip cap on three max-HP bands, the killed-here tier-down, the threshold-no-kill rule both ways, the tier-up ladder and its restart, the withheld emergency) |
| Thirty-six ladder books on one node cost ONE census read and share ONE `previewsPerTick` cap in a pass, as three `auto` books do | `test_thirty_six_ladder_books_on_one_node_read_one_census_and_share_the_preview_cap` |
| A collect due time is drawn once per landed collect (or start) and survives a restart: uniform in the book's window, seeded from the book id + the persisted draw counter, never redrawn by a restart or by a `not_sent` / `reverted` / `unconfirmed` collect, which stays due; a `collectEvery` set alone is a fixed period exactly; a runtime without a due time draws once from `lastCollectAt` | collect de-sync tests (`tests/test_phase2ja.py`) |
| The keeper-level `status` staleness gate is the loosest running book's `maxStalenessBlocks`, or 90 | status staleness test |
| A craft refused for stamina at reading S is not offered again below S + 1 (nor below a declared `staminaCost`), across restarts; the park lasts the regen time to the need, not a flat window | craft stamina tests (`tests/test_craft_1e.py`) |
| Under a lens outage a feed book signs feeds only, from a chain point read of its own kamis, once per `chainFeedCooldownSec`, and never a start / stop / collect / revive; the branch opens only after `chainFallbackAfterSec` of outage and never under `lens-stale` alone or `lens-incomplete`; the projection equals an independent computation of the game's HP formula | chain-fallback tests (`tests/test_chainhp.py`: the fixture replay against the independently computed expectation, the live lens check, the core branch tests) |
| Under a lens outage an `auto` REST book signs only `harvest stop`, only for its own kamis at or below the rest floor by the chain reading, at most one attempt per kami per `chainFeedCooldownSec` | chain-rest tests (`tests/test_phase2f.py`: the floor rest and its rows, the 12 + 3 batch, the second tick of the same outage, a new outage, the persisted `chainRests`, `chainFallback` false, the wait, the FEED book unmoved) |
| A kami absent from the account's answer on `externalChangeTicks` consecutive otherwise-valid reads is an external change of that kami, never a lens fault for ever; a single absence is a gated tick; `resume` names it with `PLAN_CONTRADICTED`; a fleet re-check cannot match while it is absent | absence tests (`tests/test_phase2b.py`) |
| The lens `battles` seed can only ADD owners to `killedAccounts` or RAISE a block (the last kill), never removes one and never touches `kills`; a death older than the window plus one day seeds nothing; a kami's battles are read once per account; a degraded or refusing lens skips the seed, stated on the lifecycle row, and never gates | seed tests (`tests/test_phase2b.py`), the window seed test (`tests/test_phase2h.py`) |
| `system.getter` is resolved by a view-only resolver outside the send path; `Chain.resolve_system` (what the signer uses) still refuses it and the P6 whitelist is unchanged | `test_view_system_address_resolves_outside_the_send_path_whitelist` |
| An `unconfirmed` row's hash is known to the node at the time of the row; a sequence mismatch on a hash the node does not know is re-signed once at a fresh nonce, never waited on; two bursts of one operator in one pass take consecutive nonces from the lane's cursor whatever the node's pending count says; a broadcast step always gets its row | signer tests (`tests/test_live_signer.py`: the frozen pending count, the external sender, the held-but-mismatched step, the refused retry, the receipt-read failure) and the core tests (`tests/test_phase2c.py`: two auto books in one pass, the refused retry through the book) |
| With `startBatchWaitSec` above 0 a ready `auto` kami is started only in a batch of at least `startBatchMin` or once the oldest ready kami has waited the window; the wait survives a restart and a start that did not land; a kami whose bound rises above its HP leaves the queue; with 0 a ready kami is started on the tick it is ready | start batching tests (`tests/test_phase2c.py`: t / t+60 / t+120 → one batch at t+300; four by t+90 → t+90; wait 0; the bound-rise exit; restart + refused start) |
| With `stopJoinWindowMin` above 0 a rest transaction of an `auto` REST book may carry, after its anchors, harvesting kamis within the window of their own limit at this tick's drain rate, nearest first, up to `stopPackMax`; a joiner is only ever stopped earlier than its limit; an anchor is never held; a joiner of a batch that did not land is not re-offered as a single; with 0 the book rests exactly the kamis at or below their limit | `tests/test_phase2ib.py` |
| With `startJoinWindowMin` above 0 a start transaction of an auto book may carry, in the free places of its last chunk, resting kamis within the window of `resumeHpPct` at this tick's regen rate, nearest first; a joiner is only ever started earlier than its readiness, never held, never adds a transaction; a joiner of a batch that did not land is not re-offered as a single; with 0 a resting kami starts only at `resumeHpPct` | `tests/test_phase2id.py` |
| An `auto` kami the lens reads with `cooldownSec` above 0 is offered neither a start (RESTING; it leaves the batched-start queue) nor a stop (HARVESTING at or below its limit) that tick, is never a stop-pack joiner, and takes no backoff for it; a `kami on cooldown` refusal waits min(`startBackoffSec`, 300 s); a kami read with `cooldownSec` 0 is unaffected | `tests/test_phase2x.py` |
| Every broadcast step has a `broadcast` row before its receipt is awaited; a `not_sent` record is one row at the broadcast stage; a dry-run row carries no `stage` | signer tests (`tests/test_phase2d.py` §A: the callback runs once, after reconcile + retry, before any receipt read) and core tests (§B: the burst, the pre-send refusal, the receipt-read failure, the nonce-race retry, the batch-fallback singles, the pooled collect) |
| A `receipt` row's hash is the hash of a `broadcast` row of the same book, and it repeats what the broadcast row stated | §B tests (`_same` over `txHash`, `nonce`, `account`, `kami`, `system`, `bookId`, `fn`, `count`, `gasLimit`, `precheck`) |
| After a restart no broadcast row stays open unprobed: probed once at boot, then once per tick of the book until the receipt is found, the confirmed nonce passes it, or it expires | boot-probe tests (`tests/test_phase2d.py` §C: the receipt at boot, the passed nonce, the not-passed nonce probed per tick, the failed read, the expiry, the dry run) |
| A boot-closed `success` row applies the plan effect of its own decision (recovered from the ledger, stated by the strategy), moves `lastActBlock` and the act clocks, and never applies one for a `reverted` / `not_sent` close, an act-registered row, a hash that already has a `receipt` row, or a decision it cannot recover — which it states on the row and in the log | `tests/test_phase2dc.py` (§A the join, §B the batched start and stop with `external_change` None, §C the reverted / decision-absent / act-origin / reopened cases, §D the bar and the clocks) |
| A parking auto book offers no start, feed or collect, stops its kamis HARVESTING on its node in packs of at most 10 and at most 5 packs a tick on valid reads only, keeps reviving, and stops itself with the completion facts on the row once a valid read past its last act shows none HARVESTING on the node, none DEAD and nothing pending; the mark survives a restart | `tests/test_phase2park.py` |

# Non-goals

See P9. Additionally: not a history store (the ledger is per book, not a
world index); not a scheduler for anything but books; no sweeps,
guards, bodyguards. Open: caller identity on `books` is the
self-declared `account` argument (a unix socket carries a uid, not a
label); peer-credential mapping or one socket per account is not
implemented.

# Changelog

1.0.0 — first public release.
