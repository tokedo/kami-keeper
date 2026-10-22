#!/usr/bin/env python3
"""Record one kami's chain point read as a test fixture.

    python scripts/record_kami_chain.py <kami_index> <out.json> [rpc_url] [--node N]

READ-ONLY (eth_call / eth_blockNumber only); nothing is signed or sent.
Run it on a host with RPC access, against one harvesting kami; the test
suite itself never touches the RPC outside `pytest -m live`.

The file shape is what tests/test_chainhp.py replays through the fake
chain (`tests/fixtures/chain/*.json`): every call the reader makes, keyed
`<component id or system.getter>:<function>:<entity or index>` (a plain
`safeGet` is keyed `<component id>:<entity>`), with the RAW hex answer
the chain gave (None for a revert), plus `expected`: what
keeper/chainhp.py decoded and projected at `at` (the wall clock of the
read). A synthetic file of the same shape
(`kami-1001-synthetic.json`, `_synthetic: true`) stands in until one is
recorded; its `expected` was computed independently of the reader.

Parity check right after recording, with a lens up:
    KEEPER_LENS_SOCKET=<lens socket> pytest -m live tests/test_chainhp.py -k lens
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_RPC = "https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz"
WORLD = "0x2729174c265dbBd8416C6449E0E813E88f43D0E7"
CHAIN_ID = 428962654539583


class RecordingChain:
    """Wraps keeper.chain.Chain and records every batched eth_call by the
    id it was made against (the reader resolves ids to addresses through
    this wrapper, so the reverse map is exact)."""

    def __init__(self, chain):
        from web3 import Web3

        from keeper import chainhp

        self.chain = chain
        self.by_address: dict[str, str] = {}
        self.calls: dict[str, str | None] = {}
        self._gew = Web3().eth.contract(abi=chainhp._COMPONENT_ABI).encode_abi("getEntitiesWithValue", args=[0])[2:10]

    def block_number(self) -> int:
        return self.chain.block_number()

    def component_address(self, component_id: str) -> str:
        addr = self.chain.component_address(component_id)
        self.by_address[addr.lower()] = component_id
        return addr

    def view_system_address(self, system_id: str) -> str:
        addr = self.chain.view_system_address(system_id)
        self.by_address[addr.lower()] = system_id
        return addr

    def batch_calls(self, calls, **kw):
        out, nbytes = self.chain.batch_calls(calls, **kw)
        for (to, data), raw in zip(calls, out):
            ident = self.by_address.get(to.lower(), to)
            arg = int.from_bytes(data[4:36], "big") if len(data) >= 36 else 0
            if ident == "system.getter":
                key = f"system.getter:getKamiByIndex:{arg}"
            elif data[:4].hex() == self._gew:
                key = f"{ident}:getEntitiesWithValue:{arg}"
            else:
                key = f"{ident}:{arg}"
            self.calls[key] = ("0x" + raw.hex()) if raw is not None else None
        return out, nbytes


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        return 2
    index, out = int(args[0]), Path(args[1])
    rpc = args[2] if len(args) > 2 else DEFAULT_RPC
    node = None
    if "--node" in argv:
        node = int(argv[argv.index("--node") + 1])
    from keeper import chainhp
    from keeper.chain import Chain

    chain = Chain(rpc, WORLD, CHAIN_ID, request_timeout_sec=20)
    chain.verify_chain_id()
    rec = RecordingChain(chain)
    reader = chainhp.ChainHpReader(rec)  # type: ignore[arg-type]
    at = int(time.time())
    reading = reader.read([index], node if node is not None else 0, at)
    k = reading.kamis[index]
    expected = {
        str(index): {
            "state": k.state, "hpMax": k.hp_max, "hpSync": k.hp_sync, "power": k.power, "harmony": k.harmony, "violence": k.violence,
            "body": k.body, "hand": k.hand, "tLast": k.t_last, "tReset": k.t_reset, "nodeIndex": k.node_index,
            "nodeAffinities": reader.affinities.get(k.node_index, []) if k.node_index is not None else [],
            "bonus": dict(k.bonus), "projectedHp": reading.projected.get(index), "read": k.read,
        }
    }
    doc = {
        "_synthetic": False,
        "note": "recorded by scripts/record_kami_chain.py (read-only eth_call); `expected` is what keeper/chainhp.py decoded and projected at `at`",
        "recorded_from": rpc,
        "block": reading.block,
        "at": at,
        "node": k.node_index if node is None else node,
        "kamis": [index],
        "config": reader.config,
        "calls": rec.calls,
        "expected": expected,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"recorded kami {index} at block {reading.block} ({reading.calls} calls, {reading.bytes} bytes) -> {out}")
    print(f"state {k.state} hpSync {k.hp_sync} / {k.hp_max} node {k.node_index} projected {reading.projected.get(index)} at {at}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
