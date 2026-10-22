"""The chain-backed HP fallback for the feed loop.

SPEC P4 says "on lens failure the keeper never substitutes a mirror of
its own". This module is NOT a mirror: it is a POINT READ of a book's own
kamis from the chain — `GetterSystem.getKamiByIndex` (state, the HP sync,
the stat block, the [body, hand] affinities), the harvest entity's
`component.Time.Last` / `Time.Reset` / `component.id.source` (the node
entity), the bonus totals through the `bonus.type` reverse index, and the
config constants — used for ONE action (a feed) while the lens is down.
Why: when the lens's upstream is down the lens is NOT LIVE for hours, and
a feed book would otherwise let its kamis starve.

What is read, and the arithmetic:

  entity ids       kami / harvest / node / bonus-type / config ids;
                   `unpack_config` (uint32[8] packed high-slot-first, two's
                   complement per slot); `signed` (int256 from a word).
  node affinities  `load_node_affinities` over keeper/data/nodes.csv (the
                   game's node table, described in keeper/data/README.md).
  batched reads    `Chain.batch_calls` in keeper/chain.py (<= 100 calls per
                   JSON-RPC body, retried, serial fallback answering None
                   for a revert).
  the kami shape   GetterSystem.getKamiByIndex (keeper/abi/GetterSystem.json):
                   hp_max = base + shift + boost, hp_sync = sync,
                   affinities [body, hand] padded NORMAL.
  bonus totals     type reverse index -> instances -> (source, level) ->
                   value; total += signed(value) x level.
  sync reads       Time.Last / Time.Reset on the harvest entity.
  the projection   `harvest_efficacy` + `project_hp`: the game's
                   harvest-efficacy and HP-strain formula;
                   tests/test_chainhp.py asserts the projection on a
                   recorded fixture.

What this module does NOT carry, by the CLAUDE.md rule: the liquidation
threshold (`kill_threshold`). Thresholds come from the lens preview and
nothing here computes one; a chain reading feeds a floor comparison only.

`system.getter` is resolved
through `Chain.view_system_address` (a view-only resolver outside the
send-path whitelist, which still refuses it), and nothing is ever signed
against it.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web3 import Web3

from .chain import Chain, ChainError, harvest_entity_id, kami_entity_id, load_abi

DATA_DIR = Path(__file__).with_name("data")
NODES_CSV = DATA_DIR / "nodes.csv"

# the config arrays the projection reads.
CONFIG_NAMES: tuple[str, ...] = (
    "KAMI_LIQ_ANIMOSITY", "KAMI_LIQ_THRESHOLD", "KAMI_LIQ_EFFICACY",
    "KAMI_HARV_FERTILITY", "KAMI_HARV_INTENSITY", "KAMI_HARV_BOUNTY",
    "KAMI_HARV_STRAIN", "KAMI_HARV_EFFICACY_BODY", "KAMI_HARV_EFFICACY_HAND",
)
# the bonus types `project_hp` / `harvest_efficacy` read (the victim-side
# bonus types less the two DEF_THRESHOLD_* types, which only the threshold
# formula uses and this module does not carry)
BONUS_TYPES: tuple[str, ...] = ("HARV_FERTILITY_BOOST", "HARV_INTENSITY_BOOST", "HARV_BOUNTY_BOOST", "STND_STRAIN_BOOST")

HARVESTING = "HARVESTING"

_codec = Web3().codec  # provider-less: ABI encoding / decoding only
_encoder = Web3()


# ---- entity ids --------------------------------------------------------------


def node_entity_id(node_index: int) -> int:
    """keccak256(abi.encodePacked("node", uint32 index))."""
    return int.from_bytes(Web3.solidity_keccak(["string", "uint32"], ["node", int(node_index)]), "big")


def bonus_type_id(bonus_type: str, holder_entity: int) -> int:
    """keccak256("bonus.type", type, holderID)."""
    return int.from_bytes(Web3.solidity_keccak(["string", "string", "uint256"], ["bonus.type", bonus_type, int(holder_entity)]), "big")


def config_id(name: str) -> int:
    """keccak256("is.config", name)."""
    return int.from_bytes(Web3.solidity_keccak(["string", "string"], ["is.config", name]), "big")


def unpack_config(raw: int) -> list[int]:
    """uint32[8] packed high-slot-first (slot 0 in the top 32 bits), each
    slot two's complement."""
    out = []
    for i in range(8):
        v = (int(raw) >> (32 * (7 - i))) & 0xFFFFFFFF
        out.append(v - (1 << 32) if v >= (1 << 31) else v)
    return out


def signed(v: int) -> int:
    """int256 from a uint256 word."""
    return v - (1 << 256) if v >= (1 << 255) else v


def load_node_affinities(path: Path = NODES_CSV) -> dict[int, list[str]]:
    """keeper/data/nodes.csv -> {node_index: [AFFINITY, ...]} (upper-case).
    A node absent from the file projects as NORMAL."""
    out: dict[int, list[str]] = {}
    if not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                idx = int(row["Index"])
            except (KeyError, ValueError):
                continue
            affs = [a.strip().upper() for a in (row.get("Affinity") or "").split(",") if a.strip()]
            if affs:
                out[idx] = affs
    return out


# ---- the kami reading --------------------------------------------------------


@dataclass
class ChainKami:
    """One kami as the chain served it (the fields the projection needs)
    plus what the harvest entity said."""

    index: int
    eid: int
    state: str = ""
    hp_max: int = 0
    hp_sync: int = 0
    power: int = 0
    harmony: int = 0
    violence: int = 0
    body: str = "NORMAL"
    hand: str = "NORMAL"
    level: int | None = None
    account_entity: int | None = None
    bonus: dict[str, int] = field(default_factory=dict)
    t_last: int = 0
    t_reset: int = 0
    harvest_source: int | None = None  # the harvest entity's component.id.source (a node entity)
    node_index: int | None = None  # resolved against the catalog + the requested node; None = unknown
    read: bool = False  # False when getKamiByIndex answered nothing (no such kami / a revert)

    def b(self, key: str) -> int:
        return int(self.bonus.get(key, 0))

    def harvesting_on(self, node_index: int) -> bool:
        return self.read and self.state == HARVESTING and self.harvest_source is not None and self.harvest_source == node_entity_id(int(node_index))


@dataclass
class ChainHpReading:
    """What one fallback read established: the block it rests on, the
    kamis by index, the projected HP per kami at `at`, and what it cost."""

    block: int | None
    at: float
    kamis: dict[int, ChainKami] = field(default_factory=dict)
    projected: dict[int, int] = field(default_factory=dict)
    calls: int = 0
    bytes: int = 0
    config_read: bool = False
    node_affinity_unknown: list[int] = field(default_factory=list)  # kamis whose node is not in the catalog (projected as NORMAL)


# ---- pure math (the game's harvest formulas) ---------------------------------


def harvest_efficacy(k: ChainKami, node_affs: list[str], cfg: dict[str, list[int]]) -> int:
    """The kami's harvest efficacy on a node: base fertility plus the
    body / hand affinity shifts against the node's affinities."""
    body_c = cfg["KAMI_HARV_EFFICACY_BODY"]  # [prec, neutral, strong, weak, special]
    hand_c = cfg["KAMI_HARV_EFFICACY_HAND"]
    hfb = k.b("HARV_FERTILITY_BOOST")
    base = cfg["KAMI_HARV_FERTILITY"][6]

    def shift(trait: str, node: str, c: list[int]) -> int:
        if trait == "NORMAL":
            return hfb // 2
        if node == "NORMAL":
            return c[1]
        if trait == node:
            return c[2] + hfb
        return -c[3]

    affs = node_affs or ["NORMAL"]
    if len(affs) == 1:
        total = shift(k.body, affs[0], body_c) + shift(k.hand, affs[0], hand_c)
    else:
        a, b = affs[0], affs[1]
        total = max(shift(k.body, a, body_c) + shift(k.hand, b, hand_c), shift(k.body, b, body_c) + shift(k.hand, a, hand_c))
    return max(0, base + total)


def project_hp(k: ChainKami, node_affs: list[str], cfg: dict[str, list[int]], at: int) -> int:
    """Victim HP at unix time `at` while it keeps HARVESTING (no feed) —
    the game's strain arithmetic from the last sync:
      fertility = P x fert[2] x harvestEfficacy // 3600
      intensity = 1e6 x (V x int[0] + minutesSinceReset) x (int[6] + HIB) // (int[2] x 3600)
      bounty    = min((fertility + intensity) x dur x (bty[6] + HBB) // 1e9, starveCap)
      strain    = ceil(bounty x str[2] x (str[6] + SB) / (10^(str[3]+str[7]) x (H + str[0])))
      hp        = max(0, syncHP - strain)"""
    if k.state != HARVESTING or k.t_last <= 0:
        return k.hp_sync
    at = int(at)
    dur = max(0, at - k.t_last)
    fert_c, int_c, bty_c, str_c = (cfg["KAMI_HARV_FERTILITY"], cfg["KAMI_HARV_INTENSITY"], cfg["KAMI_HARV_BOUNTY"], cfg["KAMI_HARV_STRAIN"])
    eff = harvest_efficacy(k, node_affs, cfg)
    fertility = k.power * fert_c[2] * eff // 3600
    minutes = max(0, at - (k.t_reset or k.t_last)) // 60
    intensity = 1_000_000 * (k.violence * int_c[0] + minutes) * (int_c[6] + k.b("HARV_INTENSITY_BOOST")) // (int_c[2] * 3600)
    bounty = (fertility + intensity) * dur * (bty_c[6] + k.b("HARV_BOUNTY_BOOST")) // 10 ** 9
    sb = str_c[6] + k.b("STND_STRAIN_BOOST")
    denom = 10 ** (str_c[3] + str_c[7]) * (k.harmony + str_c[0])
    if k.hp_sync <= 0:
        return 0
    cap = k.hp_sync * denom // (str_c[2] * sb) if sb > 0 else bounty
    bounty = min(bounty, cap)
    strain = -(-bounty * str_c[2] * sb // denom)  # ceil
    return max(0, k.hp_sync - strain)


# ---- decoding the getter shape ------------------------------------------------


def _abi_type(o: dict[str, Any]) -> str:
    t = str(o.get("type", ""))
    if t.startswith("tuple"):
        inner = "(" + ",".join(_abi_type(c) for c in o.get("components", [])) + ")"
        return inner + t[len("tuple"):]
    return t


_GETTER_ABI = load_abi("GetterSystem")
_GETTER_FN = next(x for x in _GETTER_ABI if x.get("name") == "getKamiByIndex")
_GETTER_OUT_TYPE = _abi_type(_GETTER_FN["outputs"][0])
_COMPONENT_ABI = load_abi("Component")


def parse_kami_shape(index: int, shape: Any) -> ChainKami:
    """The decoded getKamiByIndex tuple -> ChainKami."""
    k = ChainKami(index=int(index), eid=kami_entity_id(int(index)))
    if shape is None:
        return k
    h, p, hm, v = shape[4]
    k.hp_max = int(h[0]) + int(h[1]) + int(h[2])
    k.hp_sync = int(h[3])
    k.power = int(p[0]) + int(p[1]) + int(p[2])
    k.harmony = int(hm[0]) + int(hm[1]) + int(hm[2])
    k.violence = int(v[0]) + int(v[1]) + int(v[2])
    affs = [str(a) for a in shape[6]] + ["NORMAL", "NORMAL"]
    k.body, k.hand = affs[0].upper(), affs[1].upper()
    k.account_entity = int(shape[7])
    k.level = int(shape[8])
    k.state = str(shape[11])
    k.read = True
    return k


def decode_kami_shape(raw: bytes | None) -> Any:
    if not raw:
        return None
    try:
        return _codec.decode([_GETTER_OUT_TYPE], raw)[0]
    except Exception:  # noqa: BLE001 — not the getter's shape: no reading
        return None


def _word(raw: bytes | None) -> int | None:
    if not raw or len(raw) < 32:
        return None
    return int.from_bytes(raw[:32], "big")


def _words(raw: bytes | None) -> list[int]:
    """A `uint256[]` return."""
    if not raw:
        return []
    try:
        return [int(x) for x in _codec.decode(["uint256[]"], raw)[0]]
    except Exception:  # noqa: BLE001
        return []


# ---- the reader -------------------------------------------------------------------


class ChainHpReader:
    """The point read (the kami shapes and the harvest sync times, in one
    pass): for the book's kamis, four batched round-trips at most —
    the config once per process, then per kami the getter shape, the two
    harvest times, the harvest source and the four bonus reverse-index
    lookups (8 calls a kami, <= 100 a body), then the bonus instances'
    (source, level) and their values. Raises RpcUnavailable / ChainError
    when the chain did not answer; a kami the getter answers nothing for
    is `read: False` (never a plausible zero)."""

    def __init__(self, chain: Chain, affinities: dict[int, list[str]] | None = None):
        self.chain = chain
        self.affinities: dict[int, list[str]] = dict(affinities) if affinities is not None else load_node_affinities()
        self.config: dict[str, list[int]] | None = None
        self._addr: dict[str, str] = {}
        self._node_of_entity: dict[int, int] = {node_entity_id(n): n for n in self.affinities}

    # -- addresses and calldata -------------------------------------------------

    def _component(self, component_id: str) -> str:
        if component_id not in self._addr:
            self._addr[component_id] = self.chain.component_address(component_id)
        return self._addr[component_id]

    def _getter(self) -> str:
        if "system.getter" not in self._addr:
            self._addr["system.getter"] = self.chain.view_system_address("system.getter")
        return self._addr["system.getter"]

    @staticmethod
    def _safe_get(entity: int) -> bytes:
        c = _encoder.eth.contract(abi=_COMPONENT_ABI)
        return bytes.fromhex(c.encode_abi("safeGet", args=[int(entity)])[2:])

    @staticmethod
    def _entities_with(value: int) -> bytes:
        c = _encoder.eth.contract(abi=_COMPONENT_ABI)
        return bytes.fromhex(c.encode_abi("getEntitiesWithValue", args=[int(value)])[2:])

    @staticmethod
    def _get_kami(index: int) -> bytes:
        c = _encoder.eth.contract(abi=_GETTER_ABI)
        return bytes.fromhex(c.encode_abi("getKamiByIndex", args=[int(index)])[2:])

    # -- reads -------------------------------------------------------------------------

    def _read_config(self, reading: ChainHpReading) -> dict[str, list[int]]:
        if self.config is not None:
            return self.config
        value = self._component("component.value")
        raws, nbytes = self.chain.batch_calls([(value, self._safe_get(config_id(n))) for n in CONFIG_NAMES])
        reading.calls += len(CONFIG_NAMES)
        reading.bytes += nbytes
        cfg: dict[str, list[int]] = {}
        for name, raw in zip(CONFIG_NAMES, raws):
            w = _word(raw)
            if w is None:
                raise ChainError(f"config {name} unreadable")
            cfg[name] = unpack_config(w)
        self.config = cfg
        reading.config_read = True
        return cfg

    def read(self, kami_indices: list[int], node_index: int, at: float) -> ChainHpReading:
        """The point read of `kami_indices` projected at unix time `at`;
        `node_index` (the book's node) is added to the node-entity map so
        `harvesting_on(node_index)` resolves even for an uncatalogued node."""
        reading = ChainHpReading(block=None, at=float(at))
        reading.block = int(self.chain.block_number())
        reading.calls += 1
        cfg = self._read_config(reading)
        indices = [int(i) for i in kami_indices]
        kamis: dict[int, ChainKami] = {i: ChainKami(index=i, eid=kami_entity_id(i)) for i in indices}
        if not indices:
            return reading
        getter = self._getter()
        t_last = self._component("component.Time.Last")
        t_reset = self._component("component.Time.Reset")
        source = self._component("component.id.source")
        id_type = self._component("component.id.type")
        level = self._component("component.level")
        value = self._component("component.value")
        per = 4 + len(BONUS_TYPES)
        calls: list[tuple[str, bytes]] = []
        for i in indices:
            hid = harvest_entity_id(i)
            calls.append((getter, self._get_kami(i)))
            calls.append((t_last, self._safe_get(hid)))
            calls.append((t_reset, self._safe_get(hid)))
            calls.append((source, self._safe_get(hid)))
            for t in BONUS_TYPES:
                calls.append((id_type, self._entities_with(bonus_type_id(t, kamis[i].eid))))
        raws, nbytes = self.chain.batch_calls(calls)
        reading.calls += len(calls)
        reading.bytes += nbytes
        node_map = dict(self._node_of_entity)
        node_map[node_entity_id(int(node_index))] = int(node_index)
        inst_keys: list[tuple[int, str, int]] = []
        for n, i in enumerate(indices):
            k = kamis[i]
            shape_raw, tl_raw, tr_raw, src_raw = raws[per * n: per * n + 4]
            parsed = parse_kami_shape(i, decode_kami_shape(shape_raw))
            parsed.t_last = int(_word(tl_raw) or 0)
            parsed.t_reset = int(_word(tr_raw) or 0)
            src = _word(src_raw)
            parsed.harvest_source = int(src) if src else None
            parsed.node_index = node_map.get(parsed.harvest_source) if parsed.harvest_source is not None else None
            for j, t in enumerate(BONUS_TYPES):
                parsed.bonus.setdefault(t, 0)
                for e in _words(raws[per * n + 4 + j]):
                    inst_keys.append((i, t, e))
            kamis[i] = parsed
        if inst_keys:
            raws2, b2 = self.chain.batch_calls([(source, self._safe_get(e)) for _i, _t, e in inst_keys] + [(level, self._safe_get(e)) for _i, _t, e in inst_keys])
            reading.calls += 2 * len(inst_keys)
            reading.bytes += b2
            srcs = [_word(r) for r in raws2[:len(inst_keys)]]
            lvls = [_word(r) for r in raws2[len(inst_keys):]]
            raws3, b3 = self.chain.batch_calls([(value, self._safe_get(s if s is not None else 0)) for s in srcs])
            reading.calls += len(inst_keys)
            reading.bytes += b3
            for (i, t, _e), s, lv, raw in zip(inst_keys, srcs, lvls, raws3):
                v = _word(raw)
                if s is None or v is None:
                    continue
                kamis[i].bonus[t] += signed(int(v)) * (int(lv) if lv else 1)
        reading.kamis = kamis
        for i, k in kamis.items():
            if not k.read:
                continue
            affs = self.affinities.get(k.node_index) if k.node_index is not None else None
            if k.state == HARVESTING and k.node_index is not None and affs is None:
                reading.node_affinity_unknown.append(i)
            reading.projected[i] = int(project_hp(k, affs or [], cfg, int(at)))
        return reading


def projected_view(k: ChainKami, hp: int) -> dict[str, Any]:
    """The chain reading as the feed guard's KamiView needs it (state, hp,
    max, node) — built by the strategies through `kami_view_from_chain`."""
    return {"index": k.index, "state": k.state, "hp": {"current": int(hp), "total": int(k.hp_max)}, **({"node": {"index": k.node_index}} if k.node_index is not None else {}), "level": k.level}


def reads_note(reading: ChainHpReading) -> dict[str, Any]:
    """The tick summary's `chainReads` line."""
    return {"calls": int(reading.calls), "bytes": int(reading.bytes), "block": reading.block, "kamis": len(reading.kamis), "configRead": bool(reading.config_read)}


__all__ = [
    "CONFIG_NAMES", "BONUS_TYPES", "ChainKami", "ChainHpReading", "ChainHpReader",
    "node_entity_id", "bonus_type_id", "config_id", "unpack_config", "signed", "load_node_affinities",
    "harvest_efficacy", "project_hp", "parse_kami_shape", "decode_kami_shape", "projected_view", "reads_note",
]
