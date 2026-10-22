"""SPEC P6 — the system whitelist, enforced at RESOLUTION (chain.py) and
at SEND (signer.py). One module so both can import it without a cycle;
`keeper.signer` re-exports every name for callers.

Five system ids and nothing else. Everything else — transfer, liquidate,
move, trade, cast, listing, pool, onyx revive, ... — is refused before an
address is resolved, let alone signed, whatever the caller.

use-item restriction: `system.kami.use.item` only with the book's
`foodItem`, one of its `foodLadder` rungs or one of its
`reviveItems`, from inventory only — a purchase (`system.listing.buy`) is
outside the whitelist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

WHITELIST: frozenset[str] = frozenset(
    {
        "system.harvest.start",
        "system.harvest.stop",
        "system.harvest.collect",
        "system.kami.use.item",
        "system.craft",
    }
)

# SPEC P6: start 10, stop 12, collect 12 kamis per transaction. The
# harness derives 31 / 15 / 17 from its per-transaction gas limit; the
# keeper's caps are lower because the node's eth_call pre-check refuses
# larger batches before the lane limit is reached: 15 stops and 17 collects
# revert the pre-check (`Reverted [code -32000]`), and starts are refused
# above a gasLimit of about 25 M. 12 stops / 12 collects land; starts are
# capped at 10. A larger cap needs a measured batch first.
BATCH_CAPS: dict[str, int] = {
    "system.harvest.start": 10,
    "system.harvest.stop": 12,
    "system.harvest.collect": 12,
}


class SignerRefused(PermissionError):
    """A pre-signing refusal. Names the system id and the rule."""


class NotGranted(SignerRefused):
    pass


class WhitelistRefused(SignerRefused):
    pass


@dataclass(frozen=True)
class Intent:
    """What a strategy wants signed. `kami` is the subject kami (None for a
    craft); `params` carries the call's own arguments:
      harvest.start   nodeIndex (+ optional kamiIndices[] for a batch)
      harvest.stop    optional kamiIndices[] (batch) else `kami`
      harvest.collect optional kamiIndices[] (batch) else `kami`
      kami.use.item   itemIndex, source ("inventory")
      craft           recipeIndex, amount
    `count` (kamis in the call) is derived for the cap check."""

    system: str
    account: str
    kami: int | None
    params: dict[str, Any] = field(default_factory=dict)
    # the book the intent came from; the use-item rule reads its config
    book_id: str = ""

    @property
    def kamis(self) -> list[int]:
        ks = self.params.get("kamiIndices")
        if isinstance(ks, list) and ks:
            return [int(k) for k in ks]
        return [int(self.kami)] if self.kami is not None else []

    @property
    def count(self) -> int:
        c = self.params.get("count")
        if isinstance(c, int) and not isinstance(c, bool):
            return c
        return max(1, len(self.kamis))


def check_system(system: str) -> None:
    """The resolution-time gate: raise WhitelistRefused for any id outside
    the five, before any RPC is made."""
    if system not in WHITELIST:
        raise WhitelistRefused(f"{system} is outside the signer whitelist; refused before resolution and before signing")


def check_whitelist(intent: Intent, book_config: dict[str, Any] | None) -> None:
    """The send-time gate: raise WhitelistRefused unless the intent is
    signable for the book it came from."""
    check_system(intent.system)
    if intent.system == "system.kami.use.item":
        item = intent.params.get("itemIndex")
        cfg = book_config or {}
        allowed: set[int] = set()
        if isinstance(cfg.get("foodItem"), int):
            allowed.add(cfg["foodItem"])
        # a ladder book feeds any rung of its `foodLadder`
        for i in cfg.get("foodLadder") or []:
            if isinstance(i, int) and not isinstance(i, bool):
                allowed.add(i)
        for i in cfg.get("reviveItems") or []:
            if isinstance(i, int):
                allowed.add(i)
        if not isinstance(item, int) or isinstance(item, bool) or item not in allowed:
            raise WhitelistRefused(
                f"system.kami.use.item with item {item!r} is outside the book's foodItem / reviveItems / foodLadder; refused before signing"
            )
        if intent.params.get("source", "inventory") != "inventory":
            raise WhitelistRefused("use-item is inventory only; a purchase is never signed")
    cap = BATCH_CAPS.get(intent.system)
    if cap is not None and intent.count > cap:
        raise WhitelistRefused(f"{intent.system}: {intent.count} kamis exceed the batch cap {cap}")
