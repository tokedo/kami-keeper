"""A fake `Chain` for the kill-scan tests: scripted logs by
block, transactions by hash, a kami-index table for the chain fallback,
an eth_call pre-check the tests can make refuse, and fault injection
(`fail_reads`). It is what `Keeper(chain=...)` sees; nothing dials out."""

from __future__ import annotations

from typing import Any, Callable

from keeper.chain import PrecheckReverted, RpcUnavailable

# `getLogs` filter shapes the scanner uses (keeper/kills.py)
_LOG_KEYS = ("topics", "data", "blockNumber", "transactionHash", "logIndex", "address")


class FakeChain:
    world_address = "0x2729174c265dbBd8416C6449E0E813E88f43D0E7"
    emitter = "0xA1A1a1a1A1A1A1A1A1a1a1a1a1a1A1A1a1A1a1a1"
    chain_id = 428962654539583
    rpc_url = "http://fake.invalid"

    def __init__(self, head: int = 1000):
        self.head = head
        self.logs: list[dict[str, Any]] = []  # every log, with blockNumber / address
        self.txs: dict[str, dict[str, Any]] = {}
        self.kami_index: dict[int, int | None] = {}  # kami entity -> index
        self.fail_reads = False  # every read raises RpcUnavailable
        # the RPC node's log index behind its own eth_blockNumber —
        # `get_logs` omits every log above head - log_lag, so a DEAD write
        # in the head block is invisible until the head moves on
        self.log_lag = 0
        # block -> header timestamp; a block that is not here is one the
        # node does not have (RpcUnavailable), and `fail_block_ts` fails them all
        self.block_ts: dict[int, int] = {}
        self.fail_block_ts = False
        self.block_ts_calls = 0
        self.refuse: Callable[[Any], str | None] = lambda call: None  # pre-check refusal by Call
        self.calls: list[tuple[str, Any]] = []

    # -- what the core / signer / scanner call --------------------------------

    def describe(self) -> dict[str, Any]:
        return {"rpcUrl": self.rpc_url, "wsUrl": None, "worldAddress": self.world_address, "chainId": self.chain_id, "chainIdVerified": self.chain_id, "resolvedSystems": {}, "batchBroadcast": False}

    def _guard(self, what: str) -> None:
        if self.fail_reads:
            raise RpcUnavailable(f"{what}: ConnectionError")

    def block_number(self) -> int:
        self.calls.append(("eth_blockNumber", None))
        self._guard("eth_blockNumber")
        return self.head

    def block_timestamp(self, block: int) -> int:
        self.calls.append(("eth_getBlockByNumber", int(block)))
        self.block_ts_calls += 1
        self._guard("eth_getBlockByNumber")
        if self.fail_block_ts:
            raise RpcUnavailable("eth_getBlockByNumber: test")
        try:
            return int(self.block_ts[int(block)])
        except KeyError:
            raise RpcUnavailable("eth_getBlockByNumber: BlockNotFound") from None

    def get_logs(self, from_block: int, to_block: int, address: str | None = None, topics: list[Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("eth_getLogs", (from_block, to_block, address, topics)))
        self._guard("eth_getLogs")
        addr = (address or self.world_address).lower()
        out = []
        served_to = to_block if not self.log_lag else min(to_block, self.head - int(self.log_lag))
        for l in self.logs:
            if not (from_block <= int(l["blockNumber"]) <= served_to):
                continue
            if str(l.get("address", self.world_address)).lower() != addr:
                continue
            ok = True
            for i, t in enumerate(topics or []):
                if t is not None and (len(l["topics"]) <= i or l["topics"][i].lower() != str(t).lower()):
                    ok = False
                    break
            if ok:
                out.append({k: l.get(k) for k in _LOG_KEYS})
        return out

    def get_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        self.calls.append(("eth_getTransactionByHash", tx_hash))
        self._guard("eth_getTransactionByHash")
        return self.txs.get(tx_hash)

    def kami_index_of(self, kami_entity: int) -> int | None:
        self.calls.append(("component.index.kami.safeGet", kami_entity))
        self._guard("component.index.kami.safeGet")
        return self.kami_index.get(int(kami_entity))

    def emitter_address(self) -> str:
        self._guard("world._emitter()")
        return self.emitter

    def precheck(self, call: Any, from_address: str) -> None:
        self.calls.append(("eth_call", call.fn))
        why = self.refuse(call)
        if why:
            raise PrecheckReverted(why)

    def verify_chain_id(self) -> int:
        return self.chain_id


def liquidate_log(block: int, tx_hash: str, victim_entity: int, log_index: int = 0, address: str = FakeChain.world_address) -> dict[str, Any]:
    """A `component.state = DEAD` write in the recorded receipt's shape."""
    from keeper.kills import COMPONENT_VALUE_SET_TOPIC, STATE_COMPONENT_TOPIC, abi_decode  # noqa: F401
    from web3 import Web3

    inner = Web3().codec.encode(["string"], ["DEAD"])
    data = "0x" + Web3().codec.encode(["bytes"], [inner]).hex()
    return {"topics": [COMPONENT_VALUE_SET_TOPIC, STATE_COMPONENT_TOPIC, "0x" + (0xfe8f).to_bytes(32, "big").hex(), "0x" + int(victim_entity).to_bytes(32, "big").hex()], "data": data, "blockNumber": block, "transactionHash": tx_hash, "logIndex": log_index, "address": address}


def cast_log(block: int, tx_hash: str, account_entity: int, target_kami_entity: int, item: int, node: int, log_index: int = 0, address: str = FakeChain.emitter) -> dict[str, Any]:
    from keeper.kills import KAMI_CAST_TOPIC, WORLD_EVENT_TOPIC
    from web3 import Web3

    value = Web3().codec.encode(["uint256", "uint256", "uint256", "uint32", "uint32"], [int(account_entity), 1789442775, int(target_kami_entity), int(item), int(node)])
    data = "0x" + Web3().codec.encode(["uint8[]", "bytes"], [[0, 0, 0, 1, 1], value]).hex()
    return {"topics": [WORLD_EVENT_TOPIC, KAMI_CAST_TOPIC], "data": data, "blockNumber": block, "transactionHash": tx_hash, "logIndex": log_index, "address": address}


def liquidate_tx(tx_hash: str, victim_harvest: int, killer_kami_entity: int, form: str = "typed") -> dict[str, Any]:
    from keeper.kills import EXECUTE_BYTES_SELECTOR, LIQUIDATE_SELECTOR
    from web3 import Web3

    words = Web3().codec.encode(["uint256", "uint256"], [int(victim_harvest), int(killer_kami_entity)])
    if form == "typed":
        data = LIQUIDATE_SELECTOR + words.hex()
    elif form == "bytes":
        data = EXECUTE_BYTES_SELECTOR + Web3().codec.encode(["bytes"], [words]).hex()
    else:
        data = "0xdeadbeef" + words.hex()
    return {"hash": tx_hash, "from": "0xa2A2a2A2A2A2A2a2a2a2a2A2A2A2A2a2A2A2a2a2", "to": "0xA3A3A3a3a3A3a3A3a3A3A3A3A3a3a3A3A3A3A3A3", "input": data, "blockNumber": None}
