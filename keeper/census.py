"""The node census cache and the per-pass attacker reads.

`node <i> --with-vitals --full` — the census every `auto` book reads every
pass — serves every occupant's vitals; `node <i> --full` (rows `id, state,
kami {id, name, index}, account`) serves none. The read limit is kept on
the keeper's side.

A CENSUS is one parsed `node <i> [--with-vitals] --full` answer: who is on
the node (presence, account indices, the kill scanner's OccupantRefs). It
is kept per (node, mode) in the keeper context, shared by every book on
the node, and NEVER persisted — a restart reads again.

  mode     `vitals` (the default read) | `plain` (no
           `--with-vitals`: rows carry no HP, cooldown or level)
  reuse    a book whose `nodeCensusEverySec` is above its `tickSeconds`
           reuses a census younger than that, read in an EARLIER pass
           (`censusAgeSec` on the tick note); every other book reads every
           pass — within one pass the PassLens serves
           the same answer to every book and the parse is shared
  forced   a kill or cast row on the node drops its censuses: the next
           observe reads again (a hostility flip needs present attackers)
  own      a REUSED census may predate a deploy of the book's own: the
           book's kamis the party read shows HARVESTING on the node are
           added to the occupant table every pass, so a kill of the book's own kami is
           never missed for the age of a census (a foreign victim that
           arrived since the census is: a lag of the class, bounded by
           `nodeCensusEverySec`, none at the default)

What the plain census cannot say — an attacker's liquidation cooldown and
level — is read for the attackers that can BIND only (`attacker_vitals`:
one `kami <index>` read per such attacker per pass and node, shared by the
books on it, capped by `attackerVitalsPerTick`; the strategy treats an
attacker without a read as cooldown 0, i.e. it binds).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .chain import kami_entity_id
from .kills import OccupantRef
from .lens import LensAnswer

if TYPE_CHECKING:  # strategies/auto.py imports this module: the row parser is imported at call time
    from .strategies.base import NodeOccupant

VITALS = "vitals"
PLAIN = "plain"

# A kami's entity id is a keccak of its index and never changes; web3's
# `solidity_keccak` costs ~0.35 ms a call, which on a node of a few thousand
# harvests is a large share of each census parse. Computed once
# per kami per process (131,072 entries is every kami minted, twice over).
entity_id = functools.lru_cache(maxsize=131072)(kami_entity_id)


@dataclass
class NodeCensus:
    node: int
    mode: str
    read_at: float
    pass_seq: int
    answer: LensAnswer
    occupants: dict[int, "NodeOccupant"] = field(default_factory=dict)
    # kami entity id -> OccupantRef: what the kill scanner resolves DEAD
    # writes against next pass (one keccak per occupant, computed once per
    # census instead of once per book per pass)
    refs: dict[int, OccupantRef] = field(default_factory=dict)


def parse_census(node: int, mode: str, answer: LensAnswer, read_at: float, pass_seq: int) -> NodeCensus:
    from .strategies.base import node_occupant

    occupants: dict[int, NodeOccupant] = {}
    for raw in (answer.data or {}).get("harvests", []) if isinstance(answer.data, dict) else []:
        if isinstance(raw, dict):
            occ = node_occupant(raw, vitals_expected=(mode != PLAIN))
            if occ is not None:
                occupants[occ.kami_index] = occ
    refs = {entity_id(int(i)): OccupantRef(int(node), i, o.account_index) for i, o in occupants.items()}
    return NodeCensus(int(node), mode, float(read_at), int(pass_seq), answer, occupants, refs)


class CensusCache:
    def __init__(self) -> None:
        self.pass_seq = 0
        self._held: dict[tuple[int, str], NodeCensus] = {}
        # this PASS only, per node: attacker kami -> {cooldownSec, level} from
        # one `kami <index>` read ({} when the lens refused it), and the count of
        # such reads (the `attackerVitalsPerTick` cap is per pass and node,
        # shared by the books on it, like `previewsPerTick`)
        self.attacker_vitals: dict[int, dict[int, dict[str, Any]]] = {}
        self.vitals_calls: dict[int, int] = {}

    def begin_pass(self) -> None:
        self.pass_seq += 1
        self.attacker_vitals = {}
        self.vitals_calls = {}

    def held(self, node: int, mode: str) -> NodeCensus | None:
        return self._held.get((int(node), mode))

    def keep(self, census: NodeCensus) -> NodeCensus:
        self._held[(census.node, census.mode)] = census
        return census

    def force(self, node: int) -> None:
        """Drop every census of `node`: the next observe reads again."""
        for key in [k for k in self._held if k[0] == int(node)]:
            del self._held[key]
