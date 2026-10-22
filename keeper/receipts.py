"""SPEC P5 — `musuCollected` out of a collect receipt.

MUSU is not an ERC-20: it is inventory item 1 tracked on the MUD
`component.value` component, and a harvest entity holds its unclaimed
accrued bounty on its own slot of that component. There is no Transfer
event to read. What a collect (or a stop) leaves in the receipt is the
component write log: the system SYNCS the accrued bounty onto the harvest
entity and then DRAINS it, so the entity's `component.value` writes in
log order are `[N, 0]` and the amount collected is the MAX non-zero
write. The account's own MUSU inventory write is ABSOLUTE (the new
balance, not the delta), so it is deliberately not the source.

Cited from kami-harness 3.7.0 (submodule a2c22c1) executor/server.py —
the keeper imports nothing from it (SPEC D3):

  L9056       `_STORE_SET_RECORD_EVENT` = 6ac31c38…6073, the MUD
              component-write topic0; L9341-9345 names it
              `_COMPONENT_VALUE_SET_TOPIC0` (StoreSetRecord and
              `ComponentValueSet(uint256,address,uint256,bytes)` are
              the same signature) and reads it for its VALUE.
  L9333-9339  `_VALUE_COMPONENT_ID` = uint256(keccak256("component.value")).
  L9365-9379  `_component_write_word`: the `bytes` payload is offset 32,
              length 32, one 32-byte word; anything longer is skipped,
              never misread.
  L9387-9419  `_component_write_words` / `_component_value_writes`:
              topic[1] is the component id, topic[3] the entity id, one
              pass in log order.
  L9483-9487  the drain rule: "the system writes the accrued bounty to
              the victim's harvest entity and then drains it, so the
              writes are [N, 0] and the gross is the MAX non-zero
              write. This is musu.py's drain rule exactly, and its
              result equals the oracle's kami_action.amount" — verified
              on four receipts; L9512-9514: "the harvest_stop at
              32677564 drained exactly 3,217", i.e. the stop / collect
              path is the same drain.
  L9524-9526  the decode never fails a landed transaction: the field is
  + L9609     None and `decode_error` names what could not be read.

The keeper's rule is the drain rule with its shape asserted: a harvest
entity whose writes carry at least one non-zero value AND end in 0 was
drained of the max non-zero value; any other shape (no write, a last
write that is not 0) is `musuCollected: null` with a `decodeError`, so
a batched receipt whose kami was not actually collected never reads as
a collect of a plausible number.
"""

from __future__ import annotations

from typing import Any

from web3 import Web3

from .chain import harvest_entity_id

# server.py L9056 / L9345: the MUD component-write event.
COMPONENT_VALUE_SET_TOPIC0: bytes = Web3.keccak(text="ComponentValueSet(uint256,address,uint256,bytes)")
# server.py L9337: uint256(keccak256("component.value")).
VALUE_COMPONENT_ID: int = int.from_bytes(Web3.keccak(text="component.value"), "big")


def _bytes(v: Any) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    s = str(v)
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    return bytes.fromhex(s) if s else b""


def component_write_word(data: bytes) -> bytes | None:
    """server.py L9365-9379: the single 32-byte word inside a
    ComponentValueSet `bytes` payload (offset 32, length 32, one word);
    None for any other layout."""
    if len(data) < 96:
        return None
    if int.from_bytes(data[32:64], "big") != 32:
        return None
    return data[64:96]


def component_writes(logs: list[Any], component_id: int = VALUE_COMPONENT_ID) -> dict[int, list[int]]:
    """{entity_id: [values written, in log order]} for ONE component over
    a receipt's logs (server.py L9387-9419). Each log is a
    mapping (or object) with `topics` and `data`, hex strings or bytes."""
    out: dict[int, list[int]] = {}
    for log in logs or []:
        topics = log.get("topics") if isinstance(log, dict) else getattr(log, "topics", None)
        data = log.get("data") if isinstance(log, dict) else getattr(log, "data", None)
        if not topics or len(topics) < 4:
            continue
        if _bytes(topics[0]) != COMPONENT_VALUE_SET_TOPIC0:
            continue
        if int.from_bytes(_bytes(topics[1]), "big") != component_id:
            continue
        word = component_write_word(_bytes(data))
        if word is None:
            continue
        out.setdefault(int.from_bytes(_bytes(topics[3]), "big"), []).append(int.from_bytes(word, "big"))
    return out


def drained(writes: list[int]) -> tuple[int | None, str | None]:
    """The drain rule (server.py L9483-9487) with its shape asserted:
    (amount, None) when the writes carry a non-zero value and end in 0;
    (None, why) otherwise."""
    if not writes:
        return None, "no component.value write to the harvest entity in this receipt"
    non_zero = [v for v in writes if v > 0]
    if not non_zero:
        return None, "only zero component.value writes to the harvest entity in this receipt"
    if writes[-1] != 0:
        return None, f"the harvest entity's component.value writes did not end in a drain (last write {writes[-1]})"
    return max(non_zero), None


def decode_harvest_drains(logs: list[Any] | None, harvest_entities: list[int]) -> dict[int, tuple[int | None, str | None]]:
    """{harvest_entity: (amount, decodeError)} for each entity asked for."""
    if logs is None:
        return {e: (None, "no receipt logs") for e in harvest_entities}
    try:
        writes = component_writes(logs)
    except (ValueError, TypeError) as e:  # a malformed log: never a raise on a landed tx
        return {e_: (None, f"receipt log walk failed: {type(e).__name__}") for e_ in harvest_entities}
    if not logs:
        return {e: (None, "the receipt carried no logs") for e in harvest_entities}
    return {e: drained(writes.get(e, [])) for e in harvest_entities}


def decode_musu_collected(logs: list[Any] | None, kami_indices: list[int]) -> dict[int, dict[str, Any]]:
    """{kami: {"musuCollected": int | None, "decodeError": str | None}}
    for each kami of a collect (one kami, or a batched receipt's set)."""
    entities = {int(k): harvest_entity_id(int(k)) for k in kami_indices}
    drains = decode_harvest_drains(logs, list(entities.values()))
    out: dict[int, dict[str, Any]] = {}
    for k, e in entities.items():
        amount, err = drains[e]
        out[k] = {"musuCollected": amount, "decodeError": err}
    return out
