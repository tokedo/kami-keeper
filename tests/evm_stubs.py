"""Hand-assembled EVM stubs for the tester chain (no compiler needed).

  returner(data)  runtime code that RETURNs `data` for any call
  reverter(msg)   runtime code that REVERTs with Error(string msg)
  deploy(w3, runtime) -> address

The World stub returns abi.encode(address systemsComponent); the
SystemsComponent stub returns abi.encode(uint256[] [systemAddress]) for
ANY input, so every system id resolves to the one stub address — enough
to exercise keccak -> systems() -> getEntitiesWithValue -> low-160-bits
on a real EVM. A provider wrapper adds JSON-RPC batching (ids = nonces)
on top of the tester provider, which has none.
"""

from __future__ import annotations

import itertools
import re
from typing import Any

from eth_abi import encode as abi_encode
from web3 import EthereumTesterProvider, Web3

ERROR_SELECTOR = bytes.fromhex("08c379a0")


def _words(data: bytes) -> list[bytes]:
    padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
    return [padded[i:i + 32] for i in range(0, len(padded), 32)]


def _store_and_end(data: bytes, opcode: int) -> bytes:
    code = b""
    for i, w in enumerate(_words(data)):
        code += b"\x7f" + w  # PUSH32 word
        code += b"\x61" + (i * 32).to_bytes(2, "big")  # PUSH2 offset
        code += b"\x52"  # MSTORE
    code += b"\x61" + len(data).to_bytes(2, "big")  # PUSH2 len
    code += b"\x60\x00"  # PUSH1 0
    code += bytes([opcode])  # RETURN / REVERT
    return code


def returner(data: bytes) -> bytes:
    return _store_and_end(data, 0xF3)


def reverter(message: str) -> bytes:
    return _store_and_end(ERROR_SELECTOR + abi_encode(["string"], [message]), 0xFD)


def initcode(runtime: bytes) -> bytes:
    # PUSH2 len, DUP1, PUSH1 12, PUSH1 0, CODECOPY, PUSH1 0, RETURN  (12 bytes)
    return b"\x61" + len(runtime).to_bytes(2, "big") + b"\x80\x60\x0c\x60\x00\x39\x60\x00\xf3" + runtime


def deploy(w3: Web3, runtime: bytes) -> str:
    h = w3.eth.send_transaction({"from": w3.eth.accounts[0], "data": initcode(runtime), "gas": 2_000_000})
    r = w3.eth.get_transaction_receipt(h)
    assert r.status == 1 and r.contractAddress
    return r.contractAddress


def deploy_world(w3: Web3, system_address: str) -> str:
    """World -> SystemsComponent -> [uint(system_address)]."""
    sc = deploy(w3, returner(abi_encode(["uint256[]"], [[int(system_address, 16)]])))
    return deploy(w3, returner(abi_encode(["address"], [sc])))


class BatchingTesterProvider(EthereumTesterProvider):
    """The tester provider plus `make_batch_request` (ids = the caller's
    request_counter, as web3's HTTPProvider) and a request log. Fault
    injection: `fail_batch_after_forward` raises AFTER forwarding each
    item (the node holds them, the client saw an error); `fail_batch_times`
    raises BEFORE forwarding; `refuse_nonce` answers an error for that
    nonce and forwards nothing after it (a node refusing mid-body);
    `reverse` answers the list in reverse order.

    The tester RAISES a ValidationError on a nonce it will not
    take where Yominet answers an error item — the wrapper translates it
    to the node's text (`account sequence mismatch, expected N, got M:
    incorrect account sequence`); `mutate(method, params, response)` may
    rewrite any answer (a stale pending count); `mismatch_after_forward`
    is a set of nonces the node TAKES but answers a sequence mismatch for
    (a body the node admitted on a delivery the client never saw)."""

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.request_counter = itertools.count()
        self.log: list[tuple[str, Any]] = []
        self.fail_batch_after_forward = 0
        self.fail_batch_times = 0
        self.refuse_nonce: int | None = None
        self.reverse = False
        self.batch_calls = 0
        # fault injection read at CALL time (web3 caches the bound
        # make_request, so tests hook here instead of rebinding it)
        self.inject: Any = None
        self.mutate: Any = None
        self.mismatch_after_forward: set[int] = set()

    def make_request(self, method: str, params: Any) -> Any:
        self.log.append((method, params))
        if self.inject is not None:
            self.inject(method, params)
        resp = super().make_request(method, params)
        if self.mutate is not None:
            resp = self.mutate(method, params, resp) or resp
        return resp

    @staticmethod
    def _yominet_nonce_error(exc: BaseException) -> dict[str, Any] | None:
        text = str(exc)
        m = re.search(r"Expected (\d+), but got (\d+)", text)
        if "nonce" in text.lower() and m:
            return {"code": -32000, "message": f"account sequence mismatch, expected {m.group(1)}, got {m.group(2)}: incorrect account sequence"}
        return None

    def make_batch_request(self, requests: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        self.batch_calls += 1
        if self.fail_batch_times > 0:
            self.fail_batch_times -= 1
            raise ConnectionError("simulated: request failed before the node saw it")
        out = []
        refused = False
        for method, params in requests:
            rid = next(self.request_counter)
            if refused or (self.refuse_nonce is not None and rid == self.refuse_nonce):
                refused = True
                out.append({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "account sequence mismatch"} if rid != self.refuse_nonce else {"code": -32000, "message": ""}})
                continue
            try:
                resp = self.make_request(method, params)
            except Exception as e:  # noqa: BLE001 — the tester raises where a node answers an error item
                err = self._yominet_nonce_error(e)
                if err is None:
                    raise
                out.append({"jsonrpc": "2.0", "id": rid, "error": err})
                continue
            if rid in self.mismatch_after_forward:
                out.append({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": f"account sequence mismatch, expected {rid + 1}, got {rid}: incorrect account sequence"}})
            elif "error" in resp:
                out.append({"jsonrpc": "2.0", "id": rid, "error": resp["error"]})
            else:
                out.append({"jsonrpc": "2.0", "id": rid, "result": resp["result"]})
        if self.fail_batch_after_forward > 0:
            self.fail_batch_after_forward -= 1
            raise TimeoutError("simulated: the node admitted the body but the client timed out")
        return list(reversed(out)) if self.reverse else out

    def count(self, method: str, params_prefix: tuple[Any, ...] | None = None) -> int:
        n = 0
        for m, p in self.log:
            if m == method and (params_prefix is None or tuple(p[:len(params_prefix)]) == params_prefix):
                n += 1
        return n


def make_tester_chain(rpc_url: str = "http://tester.invalid", fund: str | None = None):
    """A Chain on a fresh tester EVM with the stub World; every whitelisted
    system resolves to accounts[1] (no code: calls return empty, txs land).
    Returns (provider, w3, chain)."""
    from keeper.chain import Chain

    provider = BatchingTesterProvider()
    w3 = Web3(provider)
    sink = w3.eth.accounts[1]
    world = deploy_world(w3, sink)
    if fund:
        w3.eth.send_transaction({"from": w3.eth.accounts[0], "to": fund, "value": 10**18})
    return provider, w3, Chain(rpc_url, world, w3.eth.chain_id, web3=w3)
