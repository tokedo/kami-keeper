"""SPEC D2 / P6 — the chain: provider, system resolution (whitelist
enforced BEFORE any RPC), entity ids, ABI loading and call encoding, the
read-only pre-check, raw broadcast (batch or serial) and receipts.

Everything measured or derived here is cited from kami-harness 3.7.0
(submodule a2c22c1) executor/server.py by line; the keeper imports
nothing from the harness (SPEC D3).

  resolution   keccak(text=system_id) -> world.systems() ->
               SystemsComponent.getEntitiesWithValue(h)[0] -> low 160
               bits as the address, cached (server.py L359-372
               _resolve_system).
  entity ids   kami: keccak256(abi.encodePacked("kami.id", uint32 index))
               (L393-403); harvest: keccak256(abi.encodePacked("harvest",
               uint256 kamiID)) (L413-423); account: uint256(owner
               address); inventory instance: keccak256("inventory.instance",
               uint256 holderID, uint32 itemIndex) (integration/
               entity-ids.md).
  ABIs         keeper/abi/<Contract>.json — the shapes the harness sends
               live, each file naming its source line (three of the five
               integration/abi copies are stale; see the files).
  pre-check    eth_call of the exact calldata from the operator address
               (L1783-1826 _dry_run); a revert refuses the item with the
               node's message; a transient RPC failure is retried once.
  broadcast    a JSON-RPC batch of eth_sendRawTransaction whose ids ARE
               the nonces, so answers map back by nonce and never by
               position (L10268-10300 _seq_batch_send); a provider that
               cannot batch (the tester) answers serially, marked so.

The World address, RPC URLs and chain id come from config, never baked
(SPEC D2). No key value enters this module: signing happens in signer.py
and this module only ever sees signed bytes.
"""

from __future__ import annotations

import ast
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import Web3
from web3.exceptions import ContractLogicError, Web3Exception

from .whitelist import Intent, check_system

ABI_DIR = Path(__file__).with_name("abi")

# system id -> ABI file and the function names the keeper may encode.
SYSTEM_ABI: dict[str, str] = {
    "system.harvest.start": "HarvestStartSystem",
    "system.harvest.stop": "HarvestStopSystem",
    "system.harvest.collect": "HarvestCollectSystem",
    "system.kami.use.item": "KamiUseItemSystem",
    "system.craft": "CraftSystem",
}
# (single-subject function, batch function or None). Stop prefers
# executeBatchedAllowFailure so one reverted kami does not fail the tx
# (kami-harness executor/server.py L7937 / L8020-8028).
SYSTEM_FUNCTIONS: dict[str, tuple[str, str | None]] = {
    "system.harvest.start": ("executeTyped", "executeBatched"),
    "system.harvest.stop": ("executeTyped", "executeBatchedAllowFailure"),
    "system.harvest.collect": ("executeTyped", "executeBatched"),
    "system.kami.use.item": ("executeTyped", None),
    "system.craft": ("executeTyped", None),
}

_abi_cache: dict[str, list[dict[str, Any]]] = {}


class ChainError(RuntimeError):
    """A chain-side failure. Messages carry a class name, never RPC text
    verbatim (RPC text is external and unscrubbed)."""


class RpcUnavailable(ChainError):
    """The RPC did not answer a READ (nonce, receipt, call). Maps to the
    `rpc-unavailable` flag / RPC_UNAVAILABLE code."""


class TransportError(ChainError):
    """A broadcast request failed as a whole. Different from a refused
    transaction: the node may or may not hold the items, so the caller
    reconciles against the node before labelling anything."""


class PrecheckReverted(ChainError):
    """The eth_call of the exact calldata reverted. `reason` is the node's
    message (bounded, hash-stripped by the signer before it is recorded)."""

    def __init__(self, reason: str):
        super().__init__("pre-check reverted")
        self.reason = reason


def load_abi(contract: str) -> list[dict[str, Any]]:
    if contract not in _abi_cache:
        doc = json.loads((ABI_DIR / f"{contract}.json").read_text(encoding="utf-8"))
        _abi_cache[contract] = doc["abi"]
    return _abi_cache[contract]


def abi_source(contract: str) -> str:
    return str(json.loads((ABI_DIR / f"{contract}.json").read_text(encoding="utf-8")).get("source", ""))


# ---- entity ids --------------------------------------------------------------


def kami_entity_id(kami_index: int) -> int:
    """keccak256(abi.encodePacked("kami.id", uint32 index)) — server.py L393."""
    return int.from_bytes(Web3.solidity_keccak(["string", "uint32"], ["kami.id", int(kami_index)]), "big")


def harvest_entity_id(kami_index: int) -> int:
    """keccak256(abi.encodePacked("harvest", uint256 kamiID)) — server.py L413."""
    return int.from_bytes(Web3.solidity_keccak(["string", "uint256"], ["harvest", kami_entity_id(kami_index)]), "big")


def account_entity_id(owner_address: str) -> int:
    """uint256(uint160(owner)) — integration/entity-ids.md."""
    return int(owner_address, 16)


def inventory_entity_id(holder_id: int, item_index: int) -> int:
    """keccak256(abi.encodePacked("inventory.instance", uint256 holderID, uint32 itemIndex))."""
    return int.from_bytes(Web3.solidity_keccak(["string", "uint256", "uint32"], ["inventory.instance", int(holder_id), int(item_index)]), "big")


def system_hash(system_id: str) -> int:
    """The SystemsComponent value of a system id: keccak(text=id) — server.py L362."""
    return int.from_bytes(Web3.keccak(text=system_id), "big")


# ---- encoding ---------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One encoded transaction: the system, the ABI function, its args and
    the calldata. `count` is the number of kamis in it (gas + caps)."""

    system: str
    contract: str
    fn: str
    args: tuple[Any, ...]
    calldata: bytes
    count: int
    kamis: tuple[int, ...] = field(default_factory=tuple)

    @property
    def selector(self) -> str:
        return "0x" + self.calldata[:4].hex()


_encoder = Web3()  # provider-less: encoding only


def encode(intent: Intent) -> Call:
    """Encode a whitelisted intent against keeper/abi. Refuses (before any
    ABI is opened) an id outside the whitelist."""
    check_system(intent.system)
    contract = SYSTEM_ABI[intent.system]
    single, batch = SYSTEM_FUNCTIONS[intent.system]
    p = intent.params
    kamis = tuple(intent.kamis)
    if intent.system == "system.harvest.start":
        node = int(p["nodeIndex"])
        if len(kamis) == 1:
            fn, args = single, (kami_entity_id(kamis[0]), node, 0, 0)
        else:
            fn, args = batch or single, ([kami_entity_id(k) for k in kamis], node, 0, 0)
    elif intent.system in ("system.harvest.stop", "system.harvest.collect"):
        ids = [harvest_entity_id(k) for k in kamis]
        if len(ids) == 1:
            fn, args = single, (ids[0],)
        else:
            fn, args = batch or single, (ids,)
    elif intent.system == "system.kami.use.item":
        if len(kamis) != 1:
            raise ValueError("use-item takes exactly one kami")
        fn, args = single, (kami_entity_id(kamis[0]), int(p["itemIndex"]))
    else:  # system.craft
        fn, args = single, (int(p["recipeIndex"]), int(p.get("amount", 1)))
    if not kamis and intent.system != "system.craft":
        raise ValueError(f"{intent.system} needs a kami")
    c = _encoder.eth.contract(abi=load_abi(contract))
    data = c.encode_abi(fn, args=list(args))
    return Call(intent.system, contract, fn, args, bytes.fromhex(data[2:]), max(1, len(kamis)), kamis)


def decode(contract: str, calldata: bytes) -> tuple[str, dict[str, Any]]:
    """Decode calldata against a keeper ABI (tests: the round-trip)."""
    c = _encoder.eth.contract(abi=load_abi(contract))
    fn, args = c.decode_function_input("0x" + calldata.hex())
    return fn.fn_name, dict(args)


# ---- the chain --------------------------------------------------------------


def _hex(h: Any) -> str:
    if isinstance(h, (bytes, bytearray)):
        return "0x" + bytes(h).hex()
    s = str(h)
    return s if s.startswith("0x") else "0x" + s


class Chain:
    """One provider, one World. Every read that fails raises RpcUnavailable
    (class name only); every broadcast that fails as a request raises
    TransportError. The whitelist is checked at resolution."""

    def __init__(self, rpc_url: str, world_address: str, chain_id: int, ws_url: str = "", web3: Web3 | None = None, request_timeout_sec: float = 30.0):
        self.rpc_url = rpc_url
        self.ws_url = ws_url
        self.world_address = Web3.to_checksum_address(world_address) if world_address else ""
        self.chain_id = int(chain_id)
        self.request_timeout_sec = request_timeout_sec
        if web3 is None:
            if not rpc_url:
                raise ChainError("rpcUrl is required")
            web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": request_timeout_sec}))
        self.w3 = web3
        self._systems: dict[str, str] = {}
        self._verified_chain_id: int | None = None
        self._batch_providers: dict[float, Any] = {}

    # -- description --------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "rpcUrl": self.rpc_url,
            "wsUrl": self.ws_url or None,
            "worldAddress": self.world_address,
            "chainId": self.chain_id,
            "chainIdVerified": self._verified_chain_id,
            # the SEND-path resolutions only: component / view / emitter
            # addresses sit under prefixed keys and are read-only
            "resolvedSystems": {k: v for k, v in self._systems.items() if ":" not in k},
            "batchBroadcast": self.supports_batch,
        }

    @property
    def supports_batch(self) -> bool:
        return callable(getattr(self.w3.provider, "make_batch_request", None))

    # -- reads (RpcUnavailable on failure) -----------------------------------

    def _read(self, what: str, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except (ContractLogicError, PrecheckReverted):
            raise
        except Exception as e:  # noqa: BLE001 — RPC text is external; class name only
            raise RpcUnavailable(f"{what}: {type(e).__name__}") from None

    def verify_chain_id(self) -> int:
        """The node's chain id must equal the configured one; checked once."""
        if self._verified_chain_id is None:
            cid = int(self._read("eth_chainId", lambda: self.w3.eth.chain_id))
            if cid != self.chain_id:
                raise ChainError(f"node chain id {cid} != configured {self.chain_id}")
            self._verified_chain_id = cid
        return self._verified_chain_id

    def block_number(self) -> int:
        return int(self._read("eth_blockNumber", lambda: self.w3.eth.block_number))

    def block_timestamp(self, block: int) -> int:
        """The block header's own `timestamp`: what dates a kill / cast row."""
        return int(self._read("eth_getBlockByNumber", self.w3.eth.get_block, int(block), False)["timestamp"])

    def pending_nonce(self, address: str) -> int:
        """Read at the PENDING block, never latest (server.py L1864 _NONCE_BLOCK)."""
        return int(self._read("eth_getTransactionCount(pending)", self.w3.eth.get_transaction_count, address, "pending"))

    def latest_nonce(self, address: str) -> int:
        return int(self._read("eth_getTransactionCount(latest)", self.w3.eth.get_transaction_count, address, "latest"))

    def balance(self, address: str) -> int:
        return int(self._read("eth_getBalance", self.w3.eth.get_balance, address))

    def resolve_system(self, system_id: str) -> str:
        """Whitelist first — an id outside it never reaches the RPC."""
        check_system(system_id)
        if system_id in self._systems:
            return self._systems[system_id]
        if not self.world_address:
            raise ChainError("worldAddress is required")
        world = self.w3.eth.contract(address=self.world_address, abi=load_abi("World"))
        sc_addr = self._read("world.systems()", world.functions.systems().call)
        sc = self.w3.eth.contract(address=Web3.to_checksum_address(sc_addr), abi=load_abi("SystemsComponent"))
        entities = self._read("systems.getEntitiesWithValue", sc.functions.getEntitiesWithValue(system_hash(system_id)).call)
        if not entities:
            raise ChainError(f"system not found on chain: {system_id}")
        addr = Web3.to_checksum_address("0x" + hex(int(entities[0]))[2:].zfill(40)[-40:])
        self._systems[system_id] = addr
        return addr

    def code_size(self, address: str) -> int:
        return len(self._read("eth_getCode", self.w3.eth.get_code, Web3.to_checksum_address(address)))

    def precheck(self, call: Call, from_address: str) -> None:
        """eth_call of the exact calldata from the signing address (server.py
        L1783 _dry_run). Raises PrecheckReverted(reason) on a revert,
        RpcUnavailable when the node did not answer twice."""
        to = self.resolve_system(call.system)
        params = {"from": from_address, "to": to, "data": _hex(call.calldata)}
        for attempt in (1, 2):
            try:
                self.w3.eth.call(params)
                return
            except ContractLogicError as e:
                raise PrecheckReverted(_revert_text(e)) from None
            except Exception as e:  # noqa: BLE001
                if _is_revert(e):
                    # a node (or the tester) that reports the revert through
                    # another exception class: still the node's verdict
                    raise PrecheckReverted(_revert_text(e)) from None
                # A refused eth_call is not a reverted one (L1809-1816): retry
                # transient failures once, then report unavailability.
                if attempt == 1:
                    time.sleep(0.5)
                    continue
                raise RpcUnavailable(f"eth_call: {type(e).__name__}") from None

    # -- broadcast --------------------------------------------------------------

    def send_raw(self, rawtx: bytes) -> str:
        """One eth_sendRawTransaction. A node refusal is a ValueError with
        the node's text; a request failure is TransportError."""
        try:
            return _hex(self.w3.eth.send_raw_transaction(rawtx))
        except ValueError:
            raise
        except Web3Exception as e:
            raise ValueError(str(e)[:300]) from None
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"{type(e).__name__}") from None

    def _batch_provider(self, timeout_s: float | None) -> Any:
        """A dedicated HTTP provider per timeout so a batch body carries a
        timeout sized to what it offers while every other call keeps the
        default (server.py L10188-10214 _seq_batch_provider)."""
        provider = self.w3.provider
        if timeout_s is None or not isinstance(provider, Web3.HTTPProvider):
            return provider
        key = float(timeout_s)
        if key not in self._batch_providers:
            self._batch_providers[key] = Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": key}, exception_retry_configuration=None)
        return self._batch_providers[key]

    def send_raw_batch(self, items: list[tuple[int, bytes]], timeout_s: float | None = None) -> dict[int, dict[str, Any]]:
        """ONE round-trip: a JSON-RPC batch of eth_sendRawTransaction whose
        request ids are the NONCES (server.py L10268-10300). Returns
        {nonce: response}; raises TransportError when the request itself
        failed or the node refused the batch as a whole."""
        if not items:
            return {}
        provider = self._batch_provider(timeout_s)
        make = getattr(provider, "make_batch_request", None)
        if not callable(make):
            raise TransportError("provider cannot batch")
        body = [("eth_sendRawTransaction", [_hex(rawtx)]) for _n, rawtx in items]
        saved = getattr(provider, "request_counter", None)
        import itertools

        if saved is not None:
            provider.request_counter = itertools.count(items[0][0])
        try:
            responses = make(body)
        except Exception as e:  # noqa: BLE001
            raise TransportError(f"{type(e).__name__}") from None
        finally:
            if saved is not None:
                provider.request_counter = saved
        if not isinstance(responses, list):
            raise TransportError("batch refused as a whole")
        out: dict[int, dict[str, Any]] = {}
        for resp in responses:
            if isinstance(resp, dict) and isinstance(resp.get("id"), int):
                out[resp["id"]] = resp
        return out

    # -- receipts -----------------------------------------------------------------

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """The receipt, or None when the chain has none yet."""
        try:
            r = self.w3.eth.get_transaction_receipt(tx_hash)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "NotFound" in name:
                return None
            raise RpcUnavailable(f"eth_getTransactionReceipt: {name}") from None
        if r is None:
            return None
        return {
            "txHash": _hex(r["transactionHash"]),
            "status": int(r["status"]),
            "block": int(r["blockNumber"]),
            "gasUsed": int(r["gasUsed"]),
            "effectiveGasPrice": int(r["effectiveGasPrice"]) if r.get("effectiveGasPrice") is not None else None,
            # the component-write logs, normalised to hex, so a
            # collect's `musuCollected` is decoded from THIS receipt
            # (keeper/receipts.py) and never from a live read
            "logs": _receipt_logs(r.get("logs")),
        }

    def wait_receipt(self, tx_hash: str, timeout_s: float, poll_s: float = 0.5) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout_s
        while True:
            r = self.receipt(tx_hash)
            if r is not None or time.monotonic() >= deadline:
                return r
            time.sleep(poll_s)

    # -- logs (gap-fill) ---------------------------------------------------------

    def get_logs(self, from_block: int, to_block: int, address: str | None = None, topics: list[Any] | None = None) -> list[dict[str, Any]]:
        """eth_getLogs on the World (or `address`) over [from, to], with an
        optional topics filter (the kill scan filters on
        `[ComponentValueSet, component.state]`). Every log comes back with
        `topics` / `data` normalised to hex plus `blockNumber`,
        `transactionHash`, `logIndex`, `address`."""
        params: dict[str, Any] = {"fromBlock": int(from_block), "toBlock": int(to_block), "address": Web3.to_checksum_address(address or self.world_address)}
        if topics:
            params["topics"] = [_hex(t) if t is not None else None for t in topics]
        logs = self._read("eth_getLogs", self.w3.eth.get_logs, params)
        out: list[dict[str, Any]] = []
        for l in logs:
            d = dict(l)
            d["topics"] = [_hex(t) for t in (d.get("topics") or [])]
            d["data"] = _hex(d.get("data")) if d.get("data") else "0x"
            if d.get("transactionHash") is not None:
                d["transactionHash"] = _hex(d["transactionHash"])
            if d.get("address"):
                d["address"] = str(d["address"])
            out.append(d)
        return out

    # -- kill-attribution reads (the attacker is named by the liquidate calldata)

    def get_transaction(self, tx_hash: str) -> dict[str, Any] | None:
        """eth_getTransactionByHash: `{hash, from, to, input, blockNumber}`
        with `input` as hex, or None when the node holds no such tx. The
        liquidate's calldata is what names the killer (the victim's DEAD
        write carries no attacker)."""
        try:
            t = self.w3.eth.get_transaction(tx_hash)
        except Exception as e:  # noqa: BLE001
            name = type(e).__name__
            if "NotFound" in name:
                return None
            raise RpcUnavailable(f"eth_getTransactionByHash: {name}") from None
        if t is None:
            return None
        raw_in = t.get("input")
        return {
            "hash": _hex(t["hash"]),
            "from": str(t.get("from") or ""),
            "to": str(t.get("to") or ""),
            "input": _hex(raw_in) if raw_in else "0x",
            "blockNumber": int(t["blockNumber"]) if t.get("blockNumber") is not None else None,
        }

    def component_address(self, component_id: str) -> str:
        """A component's contract address: keccak(text=id) ->
        world.components() -> registry.getEntitiesWithValue(h)[0] -> low
        160 bits. Cached."""
        key = "component:" + component_id
        if key in self._systems:
            return self._systems[key]
        if not self.world_address:
            raise ChainError("worldAddress is required")
        world = self.w3.eth.contract(address=self.world_address, abi=load_abi("World"))
        reg_addr = self._read("world.components()", world.functions.components().call)
        reg = self.w3.eth.contract(address=Web3.to_checksum_address(reg_addr), abi=load_abi("Component"))
        entities = self._read("components.getEntitiesWithValue", reg.functions.getEntitiesWithValue(system_hash(component_id)).call)
        if not entities:
            raise ChainError(f"component not found on chain: {component_id}")
        addr = Web3.to_checksum_address("0x" + hex(int(entities[0]))[2:].zfill(40)[-40:])
        self._systems[key] = addr
        return addr

    def component_value(self, component_id: str, entity: int) -> int | None:
        """`safeGet(entity)` on a value component (`component.index.kami`
        is read this way). None when the call reverted
        (no such entity); RpcUnavailable when the node did not answer."""
        addr = self.component_address(component_id)
        comp = self.w3.eth.contract(address=addr, abi=load_abi("Component"))
        try:
            return int(self._read(f"{component_id}.safeGet", comp.functions.safeGet(int(entity)).call))
        except (ContractLogicError, PrecheckReverted):
            return None

    def kami_index_of(self, kami_entity: int) -> int | None:
        """The kami index behind a kami entity id, from `component.index.kami`;
        None for an entity that is not a kami."""
        v = self.component_value("component.index.kami", kami_entity)
        return int(v) if v else None

    # -- the chain-backed HP fallback reads (SPEC P4)

    def view_system_address(self, system_id: str) -> str:
        """The address of a system the keeper only ever CALLS (eth_call of a
        `view`), resolved through the same registry read as
        `resolve_system` (the systems registry) but OUTSIDE the send-path
        resolver: the
        whitelist is not consulted, the address is cached under a `view:`
        key the signer never reads, and `resolve_system` keeps refusing
        the same id. The HP fallback uses it for `system.getter`
        (GetterSystem.getKamiByIndex); nothing is ever signed against it."""
        key = "view:" + system_id
        if key in self._systems:
            return self._systems[key]
        if not self.world_address:
            raise ChainError("worldAddress is required")
        world = self.w3.eth.contract(address=self.world_address, abi=load_abi("World"))
        sc_addr = self._read("world.systems()", world.functions.systems().call)
        sc = self.w3.eth.contract(address=Web3.to_checksum_address(sc_addr), abi=load_abi("SystemsComponent"))
        entities = self._read("systems.getEntitiesWithValue", sc.functions.getEntitiesWithValue(system_hash(system_id)).call)
        if not entities:
            raise ChainError(f"system not found on chain: {system_id}")
        addr = Web3.to_checksum_address("0x" + hex(int(entities[0]))[2:].zfill(40)[-40:])
        self._systems[key] = addr
        return addr

    def batch_calls(self, calls: list[tuple[str, bytes]], chunk: int = 100, attempts: int = 3, sleep: Any = time.sleep) -> tuple[list[bytes | None], int]:
        """Read-only `eth_call`s in JSON-RPC batches of at most `chunk`
        (one body per <= 100 calls, a failing body retried, then a serial
        fallback that answers
        None for a revert). Returns (results in call order — bytes, or
        None for a revert / no result — , bytes received). Raises
        RpcUnavailable when a call's transport failed on every road; a
        revert never raises (an entity with no value is None)."""
        import itertools

        out: list[bytes | None] = []
        received = 0
        provider = self.w3.provider
        make = getattr(provider, "make_batch_request", None)
        for i in range(0, len(calls), max(1, int(chunk))):
            part = calls[i:i + chunk]
            body = [("eth_call", [{"to": Web3.to_checksum_address(to), "data": _hex(data)}, "latest"]) for to, data in part]
            done = False
            if callable(make):
                for attempt in range(max(1, int(attempts))):
                    saved = getattr(provider, "request_counter", None)
                    if saved is not None:
                        provider.request_counter = itertools.count(0)
                    try:
                        responses = make(body)
                    except Exception:  # noqa: BLE001 — retried, then serial
                        responses = None
                    finally:
                        if saved is not None:
                            provider.request_counter = saved
                    if isinstance(responses, list) and len(responses) == len(part):
                        by_id: dict[int, Any] = {}
                        for j, resp in enumerate(responses):
                            rid = resp.get("id") if isinstance(resp, dict) else None
                            by_id[int(rid) if isinstance(rid, int) else j] = resp
                        for j in range(len(part)):
                            resp = by_id.get(j) or {}
                            result = resp.get("result") if isinstance(resp, dict) else None
                            if isinstance(result, str) and result.startswith("0x") and len(result) > 2:
                                raw = bytes.fromhex(result[2:])
                                received += len(raw)
                                out.append(raw)
                            else:
                                out.append(None)  # a revert (error) or an empty answer
                        done = True
                        break
                    if attempt + 1 < attempts:
                        sleep(0.4 * (attempt + 1))
            if done:
                continue
            for to, data in part:
                try:
                    raw = bytes(self.w3.eth.call({"to": Web3.to_checksum_address(to), "data": _hex(data)}))
                    received += len(raw)
                    out.append(raw if raw else None)
                except (ContractLogicError, PrecheckReverted):
                    out.append(None)
                except Exception as e:  # noqa: BLE001
                    if _is_revert(e):
                        out.append(None)
                        continue
                    raise RpcUnavailable(f"eth_call: {type(e).__name__}") from None
        return out, received

    def emitter_address(self) -> str:
        """`world._emitter()` — the contract that logs `WorldEvent` (KAMI_CAST
        among them). Cached."""
        key = "world:_emitter"
        if key in self._systems:
            return self._systems[key]
        if not self.world_address:
            raise ChainError("worldAddress is required")
        world = self.w3.eth.contract(address=self.world_address, abi=load_abi("WorldEmitter"))
        addr = Web3.to_checksum_address(self._read("world._emitter()", world.functions._emitter().call))
        self._systems[key] = addr
        return addr


def _receipt_logs(logs: Any) -> list[dict[str, Any]]:
    """[{topics: [hex...], data: hex}] in log order; a log the node shaped
    unexpectedly is skipped rather than raised on (the receipt is the
    landed fact, the logs are its evidence)."""
    out: list[dict[str, Any]] = []
    for log in logs or []:
        try:
            topics = log["topics"] if isinstance(log, dict) else getattr(log, "topics")
            data = log["data"] if isinstance(log, dict) else getattr(log, "data")
        except (KeyError, AttributeError, TypeError):
            continue
        out.append({"topics": [_hex(t) for t in (topics or [])], "data": _hex(data) if data else "0x"})
    return out


def _is_revert(e: Exception) -> bool:
    text = (getattr(e, "message", None) or str(e) or "").lower()
    return "revert" in text or "transactionfailed" in type(e).__name__.lower()


def _revert_text(e: Exception) -> str:
    """The node's revert reason as text, bounded. Yominet answers an
    eth_call revert as a JSON-RPC error payload ({code, message}) that
    web3 hands over as the exception's first argument; the message is the
    reason, the code is appended."""
    payload: Any = e.args[0] if e.args else None
    resp = getattr(e, "rpc_response", None)
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict):
        payload = resp["error"]  # web3 7 Web3RPCError keeps the whole response
    msg: Any = getattr(e, "message", None) or payload or str(e)
    if isinstance(msg, dict):
        payload = msg
    elif isinstance(msg, str) and msg.startswith("{") and not isinstance(payload, dict):
        try:
            parsed = ast.literal_eval(msg)  # the repr of the error dict (Web3RPCError.args[0])
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    if isinstance(payload, dict):
        text = str(payload.get("message") or "")
        if payload.get("code") is not None:
            text = f"{text} [code {payload['code']}]"
        if payload.get("data") not in (None, "", "no data"):
            text = f"{text} [data {str(payload['data'])[:80]}]"
        msg = text or str(payload)
    data = getattr(e, "data", None)
    if isinstance(data, str) and data and data != "no data" and str(msg) in ("execution reverted", ""):
        msg = f"{msg} [data {data[:80]}]"
    return str(msg)[:300]
