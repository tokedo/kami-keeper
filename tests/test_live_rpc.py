"""READ-ONLY checks against the public Yominet RPC. Marked `live`; skipped
cleanly when the RPC is unreachable. Nothing here signs or sends.

  pytest -m live -q          # run them
  pytest -q                  # they self-skip without network
"""

from __future__ import annotations

import pytest
from web3 import Web3

from keeper.chain import Chain, ChainError, RpcUnavailable, encode
from keeper.whitelist import WHITELIST, Intent

# Chain facts (as in kami-harness 3.7.0 executor/server.py L70-75); the
# keeper's config carries them, never code.
RPC = "https://jsonrpc-yominet-1.anvil.asia-southeast.initia.xyz"
WORLD = "0x2729174c265dbBd8416C6449E0E813E88f43D0E7"
CHAIN_ID = 428962654539583

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def chain():
    c = Chain(RPC, WORLD, CHAIN_ID, request_timeout_sec=8)
    try:
        c.verify_chain_id()
    except (RpcUnavailable, ChainError) as e:
        pytest.skip(f"public RPC unreachable or not Yominet: {e}")
    return c


def test_the_five_whitelisted_systems_resolve_to_contracts(chain):
    seen = {}
    for sid in sorted(WHITELIST):
        addr = chain.resolve_system(sid)
        assert Web3.is_checksum_address(addr)
        assert chain.code_size(addr) > 0, sid
        seen[sid] = addr
    assert len(set(seen.values())) == 5, seen
    assert set(chain.describe()["resolvedSystems"]) == WHITELIST


def test_a_non_whitelisted_system_is_never_resolved_on_the_live_chain(chain):
    from keeper.whitelist import WhitelistRefused

    with pytest.raises(WhitelistRefused):
        chain.resolve_system("system.harvest.liquidate")
    assert "system.harvest.liquidate" not in chain.describe()["resolvedSystems"]


def test_precheck_from_the_zero_address_reverts_with_a_reason_for_each_keeper_selector(chain):
    """Informational evidence for the ABI shapes: an eth_call from the
    zero address with a dummy subject must be REFUSED by the system's own
    checks (a reason string), never by a missing selector (empty revert
    data). Records the reason per system."""
    from keeper.chain import PrecheckReverted

    intents = {
        "system.harvest.start": Intent("system.harvest.start", "x", 1, {"nodeIndex": 1}),
        "system.harvest.stop": Intent("system.harvest.stop", "x", 1, {}),
        "system.harvest.collect": Intent("system.harvest.collect", "x", 1, {}),
        "system.kami.use.item": Intent("system.kami.use.item", "x", 1, {"itemIndex": 11301}),
        "system.craft": Intent("system.craft", "x", None, {"recipeIndex": 29, "amount": 1}),
    }
    reasons = {}
    for sid, intent in intents.items():
        try:
            chain.precheck(encode(intent), "0x" + "00" * 20)
            reasons[sid] = "(no revert)"
        except PrecheckReverted as e:
            reasons[sid] = e.reason
    print("\nlive pre-check reasons:", reasons)
    for sid, reason in reasons.items():
        assert reason and reason != "execution reverted", (sid, reason)
