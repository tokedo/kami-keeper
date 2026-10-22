"""`craft` — an account and a recipe list (SPEC P2).

Per tick: read the account's stamina; for recipes in priority order with
amount remaining (0 = until inputs run out), sign ONE craft of the first
recipe whose remaining count is > 0 while stamina > staminaFloor.

The chain is the input/stamina oracle: the signer's eth_call pre-check
refuses a craft the chain would revert, and `on_records` PARKS that
recipe (`exhausted`) — retried after
EXHAUSTED_RETRY_SEC through the same free pre-check; a stamina refusal
backs off STAMINA_RETRY_SEC only. A crafted count moves only on a landed
(or dry-run) record.
"""

from __future__ import annotations

from typing import Any

from ..lens import LensClient
from ..signer import Intent
from .base import Decision, Observation, Strategy

EXHAUSTED_RETRY_SEC = 3600  # an exhausted recipe is retried hourly through the free eth_call pre-check
STAMINA_REGEN_SEC = 60  # the game regenerates 1 stamina per minute, cap 100
STAMINA_RETRY_MIN_SEC = 60  # a stamina park is at least one regen step


class Craft(Strategy):
    type_name = "craft"

    def observe(self, lens: LensClient) -> Observation:
        # The account is resolved through the lens `account` query (index,
        # name or address). The key is the book's label unless `accountKey`
        # names the lens key — when the account's in-game name differs from
        # its label (label `alice`, in-game name `alice-1`); the label still names the
        # grant, the signer lane and the ledger. The lens answers NOT_FOUND
        # for an unknown key.
        key = self.book.config.get("accountKey")
        ans = lens.account(self.book.account if key is None else key, slim=False)
        data = ans.data or {}
        stamina = (data.get("stamina") or {}).get("current")
        obs = Observation(basis=ans.basis(), stamina=int(stamina) if isinstance(stamina, (int, float)) else None, answers=[ans])
        return obs

    @staticmethod
    def _need(recipe: dict[str, Any], stamina_min: dict[str, int]) -> int:
        """Stamina the recipe needs before it is offered: the larger of the
        declared `staminaCost` and the learned floor; 0 when neither."""
        idx = str(recipe["recipeIndex"])
        declared = int(recipe.get("staminaCost") or 0)
        return max(declared, int(stamina_min.get(idx, 0)))

    def assess(self, obs: Observation, now_sec: float, runtime: dict[str, Any]) -> list[Decision]:
        cfg = self.book.config
        runtime.pop("staminaWait", None)
        if obs.stamina is None or obs.stamina <= int(cfg["staminaFloor"]):
            return []
        done: dict[str, int] = runtime.setdefault("crafted", {})
        parked: dict[str, dict[str, Any]] = runtime.setdefault("parked", {})
        stamina_min: dict[str, int] = runtime.setdefault("staminaMin", {})
        floor = int(cfg["staminaFloor"])
        waits: list[dict[str, Any]] = []
        for r in sorted(cfg["recipes"], key=lambda r: (int(r.get("priority", 0)), int(r["recipeIndex"]))):
            idx = str(r["recipeIndex"])
            amount = int(r.get("amount", 0))
            park = parked.get(idx)
            if park and now_sec < float(park.get("retryAt", 0)):
                continue
            if amount and done.get(idx, 0) >= amount:
                continue
            need = self._need(r, stamina_min)
            if need and obs.stamina - floor < need:
                # not enough stamina for this recipe yet: a note, no offer,
                # no pre-check; the next recipe in priority order may fit
                short = need + floor - obs.stamina
                waits.append({"recipe": int(idx), "stamina": obs.stamina, "need": need, "staminaFloor": floor, "retryInSec": max(STAMINA_RETRY_MIN_SEC, short * STAMINA_REGEN_SEC)})
                continue
            if waits:
                runtime["staminaWait"] = waits  # higher-priority recipes still short of their need
            return [Decision(
                "craft",
                {"recipe": int(idx), "amount": 1, "crafted": done.get(idx, 0), "target": amount, "stamina": obs.stamina, "need": need, "retryAfterPark": bool(park)},
                [Intent("system.craft", self.book.account, None, {"recipeIndex": int(idx), "amount": 1}, self.book.id)],
            )]
        if waits:
            runtime["staminaWait"] = waits
        return []

    def on_records(self, decisions: list[Decision], records: list[Any], now_sec: float, runtime: dict[str, Any]) -> None:
        done: dict[str, int] = runtime.setdefault("crafted", {})
        parked: dict[str, dict[str, Any]] = runtime.setdefault("parked", {})
        stamina_min: dict[str, int] = runtime.setdefault("staminaMin", {})
        pos = 0
        for d in decisions:
            mine = records[pos:pos + len(d.intents)]
            pos += len(d.intents)
            if d.kind != "craft" or not mine:
                continue
            idx = str(d.parameters["recipe"])
            rec = mine[0]
            if rec.status in ("success", "dry_run") and getattr(rec, "precheck", None) != "reverted":
                done[idx] = done.get(idx, 0) + int(d.parameters.get("amount", 1))
                parked.pop(idx, None)
            elif getattr(rec, "precheck", None) == "reverted" or rec.status == "reverted":
                reason = str(getattr(rec, "reason", "") or "reverted on chain")
                if "stamina" in reason.lower():
                    # cost > the reading the decision used: raise the learned
                    # floor past it and park until the regen reaches it
                    seen = int(d.parameters.get("stamina") or 0)
                    stamina_min[idx] = max(int(stamina_min.get(idx, 0)), seen + 1)
                    short = stamina_min[idx] - seen
                    wait = max(STAMINA_RETRY_MIN_SEC, short * STAMINA_REGEN_SEC)
                    parked[idx] = {"retryAt": now_sec + wait, "reason": reason[:120], "parkedAt": now_sec, "stamina": seen, "staminaMin": stamina_min[idx]}
                else:
                    parked[idx] = {"retryAt": now_sec + EXHAUSTED_RETRY_SEC, "reason": reason[:120], "parkedAt": now_sec}
