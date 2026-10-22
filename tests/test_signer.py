"""SPEC P6 / P8 — grant gate before anything; every non-whitelisted
system id refused; use-item restricted to foodItem/reviveItems from
inventory; DryRunSigner records and keeps pending == confirmed."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import FIXTURE_OPERATOR_KEY
from keeper.secrets import OperatorKey
from keeper.signer import BATCH_CAPS, WHITELIST, DryRunSigner, Intent, NotGranted, WhitelistRefused, check_whitelist

# The 67 documented player-facing system ids of the game, plus the two view
# systems, as the refusal set.
ALL_SYSTEM_IDS = [
    "system.account.register", "system.account.move", "system.account.set.bio", "system.account.set.name", "system.account.set.operator", "system.account.set.pfp", "system.account.use.item",
    "system.chat", "system.echo.kamis", "system.echo.room",
    "system.kami.level", "system.kami.name", "system.kami.equip", "system.kami.unequip", "system.kami.use.item", "system.kami.cast.item", "system.kami.sacrifice.commit", "system.kami.sacrifice.reveal", "system.kami.onyx.rename", "system.kami.onyx.revive", "system.kami.onyx.respec", "system.kami.send",
    "system.skill.upgrade", "system.skill.respec",
    "system.harvest.start", "system.harvest.stop", "system.harvest.collect", "system.harvest.liquidate",
    "system.item.burn", "system.item.transfer", "system.craft", "system.droptable.item.reveal",
    "system.quest.accept", "system.quest.complete", "system.quest.drop",
    "system.trade.create", "system.trade.execute", "system.trade.complete", "system.trade.cancel",
    "system.friend.request", "system.friend.accept", "system.friend.cancel", "system.friend.block",
    "system.listing.buy", "system.listing.sell",
    "system.kami.gacha.mint", "system.kami.gacha.reveal", "system.kami.gacha.reroll", "system.buy.gacha.ticket",
    "system.getter", "system.kami721.stake", "system.kami721.unstake", "system.kami721.transfer", "system.Kami721.IsInWorld", "system.Kami721.Metadata", "system.erc20.portal",
    "system.relationship.advance", "system.goal.contribute", "system.goal.claim", "system.scavenge.claim", "system.auction.buy", "system.pool.swap", "system.pool.add.liquidity", "system.pool.remove.liquidity",
]

CFG = {"foodItem": 11301, "reviveItems": [21301]}


def _key():
    return OperatorKey("LAB_OPERATOR_KEY", FIXTURE_OPERATOR_KEY)


def test_whitelist_is_exactly_the_five():
    assert WHITELIST == {"system.harvest.start", "system.harvest.stop", "system.harvest.collect", "system.kami.use.item", "system.craft"}


@pytest.mark.parametrize("system", sorted(set(ALL_SYSTEM_IDS) - WHITELIST))
def test_every_non_whitelisted_system_is_refused_before_signing(system):
    s = DryRunSigner()
    s.grant("lab", _key())
    with pytest.raises(WhitelistRefused):
        s.submit([Intent(system, "lab", 1, {"itemIndex": 11301})], CFG)
    assert s.recorded == []


@pytest.mark.parametrize("system", sorted(WHITELIST))
def test_every_whitelisted_system_passes_with_a_grant(system):
    s = DryRunSigner()
    s.grant("lab", _key())
    recs = s.submit([Intent(system, "lab", 1, {"itemIndex": 11301, "count": 1})], CFG)
    assert len(recs) == 1 and recs[0].status == "dry_run" and recs[0].system == system


def test_no_grant_means_nothing_is_signed_even_for_a_whitelisted_system():
    s = DryRunSigner()
    with pytest.raises(NotGranted):
        s.submit([Intent("system.harvest.start", "lab", 1, {})], CFG)
    assert s.recorded == []
    s.grant("lab", _key())
    s.revoke("lab")
    with pytest.raises(NotGranted):
        s.submit([Intent("system.harvest.start", "lab", 1, {})], CFG)


def test_use_item_is_restricted_to_food_and_revive_items_from_inventory():
    ok = Intent("system.kami.use.item", "lab", 1, {"itemIndex": 11301})
    check_whitelist(ok, CFG)
    check_whitelist(Intent("system.kami.use.item", "lab", 1, {"itemIndex": 21301}), CFG)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.kami.use.item", "lab", 1, {"itemIndex": 19001}), CFG)  # Spirit Glue
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.kami.use.item", "lab", 1, {"itemIndex": 11301, "source": "purchase"}), CFG)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.listing.buy", "lab", 1, {"itemIndex": 21301}), CFG)  # a revive is never a purchase


def test_batch_caps_are_the_spec_numbers_and_enforced():
    assert BATCH_CAPS == {"system.harvest.start": 10, "system.harvest.stop": 12, "system.harvest.collect": 12}
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.harvest.stop", "lab", None, {"count": 13}), CFG)
    with pytest.raises(WhitelistRefused):
        check_whitelist(Intent("system.harvest.stop", "lab", None, {"kamiIndices": list(range(13))}), CFG)
    check_whitelist(Intent("system.harvest.stop", "lab", None, {"kamiIndices": list(range(12))}), CFG)


def test_dry_run_quiescence_is_trivial():
    s = DryRunSigner()
    s.grant("lab", _key())
    s.submit([Intent("system.craft", "lab", None, {"recipeIndex": 1})], CFG)
    q = s.quiescence("lab")
    assert q.quiescent and q.pendingNonce == q.confirmedNonce


def test_the_send_path_lives_only_in_signer_and_chain():
    """Signing happens in signer.py (the one `sign_transaction`
    call) and broadcasting in chain.py; no other module reaches either."""
    pkg = Path(__file__).resolve().parents[1] / "keeper"
    for p in pkg.rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        if p.name == "signer.py":
            assert len(re.findall(r"sign_transaction\(", src)) == 1 and "send_raw_transaction" not in src
        elif p.name == "chain.py":
            assert "sign_transaction" not in src and "send_raw_transaction" in src
        else:
            assert not re.search(r"send_raw_transaction|sign_transaction|eth_sendRawTransaction", src), p
