#!/usr/bin/env python3
"""Record a Yominet transaction receipt as a test fixture.

    python scripts/record_receipt.py <tx_hash> <out.json> [rpc_url]

READ-ONLY (eth_getTransactionReceipt); nothing is signed or sent. The
receipt hashes come from the keeper's own ledger `tx` rows; the test
suite itself never touches the RPC outside `pytest -m live`. The file shape is the one under
tests/fixtures/receipts/: tx_hash, block, status, gas_used, logs[]
{topics[] hex, data hex} in log order, plus `decoded`: what
keeper/receipts.py reads out of it (every harvest-entity drain, so the
fixture carries its own expected values).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_RPC = "https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    tx_hash, out = argv[1], Path(argv[2])
    rpc = argv[3] if len(argv) > 3 else DEFAULT_RPC
    from web3 import Web3

    from keeper.receipts import VALUE_COMPONENT_ID, component_writes, drained

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
    r = w3.eth.get_transaction_receipt(tx_hash)
    logs = [{"topics": ["0x" + bytes(t).hex() for t in log["topics"]], "data": "0x" + bytes(log["data"]).hex()} for log in r["logs"]]
    writes = component_writes(logs, VALUE_COMPONENT_ID)
    decoded = {}
    for entity, values in writes.items():
        amount, err = drained(values)
        decoded[str(entity)] = {"writes": values, "drained": amount, "decodeError": err}
    doc = {
        "tx_hash": "0x" + bytes(r["transactionHash"]).hex(),
        "block": int(r["blockNumber"]),
        "status": int(r["status"]),
        "gas_used": int(r["gasUsed"]),
        "from": r.get("from"),
        "to": r.get("to"),
        "recorded_from": rpc,
        "logs": logs,
        "decoded": {"component.value": decoded},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1))
    drains = {e: d["drained"] for e, d in decoded.items() if d["drained"] is not None}
    print(f"recorded {doc['tx_hash']} block {doc['block']} status {doc['status']} logs {len(logs)} -> {out}")
    print(f"harvest-entity drains: {drains or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
