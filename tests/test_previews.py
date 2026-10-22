"""keeper/previews.py on the two recorded node previews (one per
attacker): fetch on arrival / level change / age,
the per-tick cap and ordering, negative thresholds clamped to 0, listed
owners never fetched, persist + restore, absent-but-remembered only for a
HOSTILE owner."""

from __future__ import annotations

import json
from pathlib import Path

from fakelens import FakeLens
from keeper.lens import LensClient
from keeper.previews import REFRESH_NO_THRESHOLD_SEC, PreviewCache, PreviewEntry, clamp
from keeper.strategies.base import node_occupant

FIX = Path(__file__).with_name("fixtures") / "lens"
NODE79 = json.loads((FIX / "node-79-full-2026-09-15.json").read_text())["data"]
ATT92 = json.loads((FIX / "node-79-preview-att360-2026-09-15.json").read_text())["data"]
ATT157 = json.loads((FIX / "node-79-preview-att448-2026-09-15.json").read_text())["data"]
OURS = 2849


def _occupants():
    return {o.kami_index: o for o in (node_occupant(h) for h in NODE79["harvests"]) if o is not None}


def _foreign():
    return {i: o for i, o in _occupants().items() if o.account_index != OURS}


def _lens(sock_dir):
    fake = FakeLens(str(sock_dir / "p.sock")).start()
    fake.nodes[79] = NODE79
    fake.previews[79] = {360: ATT92, 448: ATT157}
    return fake, LensClient(fake.socket_path, 5)


def test_clamp_and_the_fixture_numbers():
    assert clamp(-18) == 0 and clamp(0) == 0 and clamp(6) == 6 and clamp(None) is None
    ours = [h for h in ATT92["harvests"] if h["account"]["index"] == OURS]
    assert len(ours) == 107
    thr = [h["liquidation"]["threshold"] for h in ours]
    assert min(thr) == -27 and max(thr) == 6
    assert sum(1 for h in ours if h["liquidation"]["reason"] == "THRESHOLD_ZERO") == 89 and sum(1 for h in ours if h["liquidation"]["reason"] == "TARGET_HP_ABOVE_THRESHOLD") == 18
    assert all(h["liquidation"]["eligible"] is False for h in ours)


def test_fetch_stores_our_rows_with_clamped_thresholds_and_reports_a_change(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 1000.0)
    foreign = _foreign()
    entry, changed, ans = cache.fetch(lens, 79, 360, foreign[360], OURS, 1000.0)
    assert changed is False and ans.size and ans.size > 100_000  # a --full preview is ~136 KB
    assert fake.requests[-1] == {"query": "node", "args": ["79", "360", "--with-vitals", "--full"]}
    assert len(entry.rows) == 107 and entry.attacker_account == 806 and entry.attacker_level == 38 and entry.cooldown_sec == 0
    assert entry.rows[263]["threshold"] == -18 and entry.clamped(263) == 0
    positive = {k: r["threshold"] for k, r in entry.rows.items() if r["threshold"] > 0}
    assert len(positive) == 18 and max(positive.values()) == 6 and entry.max_clamped() == 6
    assert entry.max_clamped([263]) == 0
    # the same read again: nothing changed; a moved threshold is reported
    _e, changed, _a = cache.fetch(lens, 79, 360, foreign[360], OURS, 1001.0)
    assert changed is False
    moved = json.loads(json.dumps(ATT92))
    for h in moved["harvests"]:
        if h["kami"]["index"] == 263:
            h["liquidation"]["threshold"] = 30
    fake.previews[79][360] = moved
    e2, changed, _a = cache.fetch(lens, 79, 360, foreign[360], OURS, 1002.0)
    assert changed is True and e2.clamped(263) == 30
    fake.stop()


def test_due_orders_arrivals_then_level_changes_then_age(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign = _foreign()
    assert len(foreign) == 45
    due = cache.due(79, OURS, foreign, {}, 3600, 0.0)
    assert [r for _a, r in due] == ["arrival"] * 45 and [a for a, _r in due] == sorted(foreign)
    ents = cache.entries(79, OURS)
    for a, occ in foreign.items():
        ents[a] = PreviewEntry(79, a, 0.0, 1, occ.level, occ.account_index, {263: {"threshold": 5 if a == 360 else -3, "level": 38}}, 0, None)
    assert cache.due(79, OURS, foreign, {263: 38}, 3600, 100.0) == []
    # the second attacker levels up; the book's kami levels up against another attacker's row; a positive threshold ages at 3600, a non-positive one only at 86400
    foreign[448].level = 38
    due = cache.due(79, OURS, foreign, {263: 38}, 3600, 3600.0)
    assert due[0] == (448, "attacker level 37 -> 38") and (360, "older than 3600 s") in due and not any(a == 601 for a, _r in due)
    due2 = cache.due(79, OURS, foreign, {263: 39}, 3600, 100.0)
    assert due2 and all("level" in r for _a, r in due2)
    due3 = cache.due(79, OURS, {a: o for a, o in foreign.items() if a in (601,)}, {263: 38}, 3600, float(REFRESH_NO_THRESHOLD_SEC))
    assert due3 == [(601, f"older than {REFRESH_NO_THRESHOLD_SEC} s")]
    assert cache.next_due_at(79, OURS, 3600) == 3600.0


def test_persist_and_restore(sock_dir, tmp_path):
    fake, lens = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 5.0)
    cache.fetch(lens, 79, 360, _foreign()[360], OURS, 5.0)
    cache.fetch(lens, 79, 448, _foreign()[448], OURS, 6.0)
    assert cache.save(79, OURS) is True and cache.save(79, OURS) is False
    doc = json.loads((tmp_path / "previews" / f"79-{OURS}.json").read_text())
    assert set(doc["entries"]) == {"360", "448"} and doc["entries"]["360"]["rows"]["263"]["threshold"] == -18
    again = PreviewCache(tmp_path, lambda: 7.0)
    ents = again.entries(79, OURS)
    assert again.restore[f"79-{OURS}"] == "restored" and set(ents) == {360, 448} and ents[448].clamped(263) == 0 and ents[360].fetched_at == 5.0
    assert again.due(79, OURS, {a: o for a, o in _foreign().items() if a in (360, 448)}, {}, 3600, 100.0) == []  # a restart is not a refetch storm
    fake.stop()


def test_absent_but_remembered_only_for_a_hostile_owner(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign = _foreign()
    ents = cache.entries(79, OURS)
    ents[360] = PreviewEntry(79, 360, 0.0, 1, 38, 806, {263: {"threshold": 100, "level": 38}}, 0, None)
    ents[448] = PreviewEntry(79, 448, 0.0, 1, 37, 997, {263: {"threshold": 100, "level": 37}}, 0, None)
    present = {a: o for a, o in foreign.items() if a not in (360, 448)}
    out = cache.reconcile_presence(79, OURS, present, {806}, 10.0, 24 * 3600.0)
    assert out == {"remembered": [360], "dropped": [448], "expired": []}
    assert ents[360].remembered and ents[360].absent_since == 10.0 and ents[360].cooldown_sec == 0 and 448 not in ents
    assert cache.next_due_at(79, OURS, 3600) is None  # a remembered entry is not due
    # back on the node: present again; gone past the window: expired
    out = cache.reconcile_presence(79, OURS, {360: foreign[360]}, {806}, 20.0, 24 * 3600.0)
    assert out["remembered"] == [] and ents[360].absent_since is None
    cache.reconcile_presence(79, OURS, {}, {806}, 30.0, 24 * 3600.0)
    out = cache.reconcile_presence(79, OURS, {}, {806}, 30.0 + 24 * 3600.0, 24 * 3600.0)
    assert out["expired"] == [360] and 360 not in ents
    assert cache.save(79, OURS) is True
