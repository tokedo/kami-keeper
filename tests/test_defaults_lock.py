"""SPEC P2 / P11 — defaults pinned by SHA; the CI default-diff check."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from keeper.books import all_defaults, defaults_digest

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "keeper" / "schemas" / "defaults.lock.json"


def test_lock_matches_the_schemas():
    lock = json.loads(LOCK.read_text())
    assert lock["sha256"] == defaults_digest()
    assert lock["defaults"] == all_defaults()


def test_spec_p2_defaults_are_pinned():
    d = all_defaults()
    assert d["harvestAndFeed"] == {
        "feedMode": "hp", "feedInterval": 3600, "collectEveryMinSec": 108000, "collectEveryMaxSec": 144000, "failsafeRest": True, "resumeHpPct": 100, "feedCooldownSec": 300, "staleFeedCooldownSec": 1800,
        "chainFallback": True, "chainFallbackAfterSec": 300, "chainFeedCooldownSec": 1800, "externalChangeTicks": 3, "readoptAfterBlocks": 240, "tickSeconds": 30, "tickHeartbeat": 20, "maxStalenessBlocks": 90,
        # the food ladder: its own four keys and the node-threat keys `auto` has, same names, same defaults
        "ladderMinFloorPct": 60, "maxOverhealHp": 25, "tierUpCooldownSec": 3600, "foodLowUnits": 100,
        # the measured dip cap is opt-in: `dipCapMode` defaults to `flat` and nothing moves without it
        "dipCapMode": "flat", "dipWindowDays": 7, "dipMeasuredMinPct": 30, "watchRefreshSec": 3600, "watchPreviewsPerTick": 5, "watchlistMaxAgeSec": 86400,
        # a revive attempt that did not land is offered again after this
        "reviveRetrySec": 200,
        "friends": [], "safetyMarginPct": 5, "passiveMarginPct": 2, "passiveOwnersBind": True, "hostileWindowHours": 48, "restFloorPct": 10, "cooldownHorizonSec": 600,
        "previewRefreshSec": 3600, "previewsPerTick": 10, "previewPolicy": "all", "nodeCensus": "vitals", "attackerVitalsPerTick": 10, "logScanBlocks": 40,
        # the kill scan's overlap re-read and occupant memory, the same on both node-watching types
        "killScanOverlapBlocks": 12, "occupantMemoryBlocks": 120,
        # the cross-book stop pool and the kill-forced tick, both OFF by default on both node-watching types
        "poolStopsAcrossBooks": False, "killForcesTick": False,
    }
    # `collectEvery` has no default: set alone it pins the period; unset, the period is the collectEveryMinSec..collectEveryMaxSec window
    assert "collectEvery" not in d["harvestAndFeed"]
    # `foodLadder` and `accountIndex` have NO default: the ladder is opt-in, and a book without the key feeds `foodItem`
    assert "foodLadder" not in d["harvestAndFeed"] and "accountIndex" not in d["harvestAndFeed"]
    # the threat keys a ladder book shares with `auto` carry the SAME default; `nodeCensusEverySec` is not one of them (the census is read every pass)
    for key in ("friends", "safetyMarginPct", "passiveMarginPct", "passiveOwnersBind", "hostileWindowHours", "restFloorPct", "cooldownHorizonSec", "previewRefreshSec", "previewsPerTick", "previewPolicy", "nodeCensus", "attackerVitalsPerTick", "logScanBlocks", "killScanOverlapBlocks", "occupantMemoryBlocks"):
        assert d["harvestAndFeed"][key] == d["auto"][key], key
    assert "nodeCensusEverySec" not in d["harvestAndFeed"]
    # the chain-backed HP fallback keys on both feeding types, never on craft
    for t in ("harvestAndFeed", "auto"):
        assert d[t]["chainFallback"] is True and d[t]["chainFallbackAfterSec"] == 300 and d[t]["chainFeedCooldownSec"] == 1800
    assert not any(k.startswith("chain") for k in d["craft"])
    assert d["auto"]["maxKamisPerBook"] == 50 and d["auto"]["friends"] == [] and d["auto"]["regenMethod"] == "REST"
    assert d["auto"]["safetyMarginPct"] == 5 and d["auto"]["passiveMarginPct"] == 2 and d["auto"]["hostileWindowHours"] == 48  # one hostile window, 48 h
    assert d["auto"]["autoCollect"] is True and d["auto"]["bountyCollectThreshold"] == 10000 and d["auto"]["reviveOnDeath"] is True
    assert d["auto"]["reviveItems"] == [11001] and d["auto"]["resumeHpPct"] == 100 and d["auto"]["readoptAfterBlocks"] == 240
    # the revive retry timer, the same on both feeding types, never on craft
    assert d["auto"]["reviveRetrySec"] == 200 and d["harvestAndFeed"]["reviveRetrySec"] == 200 and "reviveRetrySec" not in d["craft"]
    assert d["craft"] == {"staminaFloor": 0, "staleFeedCooldownSec": 1800, "externalChangeTicks": 3, "readoptAfterBlocks": 240, "tickSeconds": 30, "tickHeartbeat": 20, "maxStalenessBlocks": 90}
    assert d["auto"]["tickHeartbeat"] == 20
    # start batching off by default, four kamis release a held batch
    assert d["auto"]["startBatchWaitSec"] == 0 and d["auto"]["startBatchMin"] == 4
    # the START join window is off by default, so a resting kami starts only at resumeHpPct
    assert d["auto"]["startJoinWindowMin"] == 0
    # both keys default off on both node-watching types, and craft has neither
    for t in ("auto", "harvestAndFeed"):
        assert d[t]["poolStopsAcrossBooks"] is False and d[t]["killForcesTick"] is False
    assert "poolStopsAcrossBooks" not in d["craft"] and "killForcesTick" not in d["craft"]
    assert not any(k.startswith("startBatch") for k in d["harvestAndFeed"]) and not any(k.startswith("startBatch") for k in d["craft"])
    # externalChangeTicks 3 and readoptAfterBlocks 240 on all three; feedCooldownSec 300 on the feeding types
    for t in ("harvestAndFeed", "auto", "craft"):
        assert d[t]["externalChangeTicks"] == 3 and d[t]["readoptAfterBlocks"] == 240 and d[t]["staleFeedCooldownSec"] == 1800
    assert d["auto"]["feedCooldownSec"] == 300 and "feedCooldownSec" not in d["craft"]


def test_check_script_passes_now_and_fails_on_a_default_change(tmp_path):
    ok = subprocess.run([sys.executable, "scripts/check_defaults.py"], cwd=ROOT, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    # a copy of the repo with one default changed must fail
    copy = tmp_path / "repo"
    shutil.copytree(ROOT / "keeper", copy / "keeper")
    shutil.copytree(ROOT / "scripts", copy / "scripts")
    s = copy / "keeper" / "schemas" / "craft.json"
    j = json.loads(s.read_text())
    j["properties"]["staminaFloor"]["default"] = 10
    s.write_text(json.dumps(j))
    bad = subprocess.run([sys.executable, "scripts/check_defaults.py"], cwd=copy, capture_output=True, text=True)
    assert bad.returncode == 1 and "MAJOR" in bad.stderr and "craft.staminaFloor" in bad.stderr
