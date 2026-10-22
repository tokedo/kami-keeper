# Recorded receipts

Chain data, not code: every file here is a Yominet transaction receipt
(`tx_hash`, `block`, `status`, `gas_used`, `logs[] {topics[], data}`) in
the shape the RPC serves it. The keeper imports nothing from kami-harness.
Account addresses and kami / harvest entity ids in these files are
synthetic replacements, applied consistently (an address keeps its form
in every topic and data word; an entity id is the keeper's own derivation
from the synthetic kami index), so every decode the tests run still holds.

| file | what | source |
|---|---|---|
| `yominet_32677500.json` | a liquidation at block 32677500: the victim's harvest entity `689272690007724837243479850868931864664682577897694345337885744847683293322` is written `[1798, 0]` — the sync-then-drain shape a collect / stop leaves — and 1798 is the drained amount; the killer's harvest entity `38986004861930616840179368661838477756132336890865011080109669343625179232305` is written `[0, 1191]` (spoils added, not drained) | derived from kami-harness 3.7.0 `executor/tests/fixtures/liquidation_32677500/32677500.json`, the killer's ids replaced |
| `liquidate_12143977.json` | a liquidation: the DEAD write on the victim's kami entity, the calldata naming the victim's harvest and the killer's kami, the gas-token and emitter logs | a recorded receipt; addresses, entity ids, the transaction hash and the block number replaced (the transaction hash and the block number are synthetic) |

New receipts are recorded with
`scripts/record_receipt.py <tx_hash> tests/fixtures/receipts/<name>.json`.
