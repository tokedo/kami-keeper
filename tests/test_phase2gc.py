"""The preview refresh order is BINDING WEIGHT, on the recorded node
occupants:

  1. the aged bucket is offered highest clamped line first, the oldest
     fetch first among equals, attacker index among equals still — not in
     attacker-index order, which can starve the entry that actually binds
     when a crowded node has more positive lines than fetches an hour;
  2. the bucket ORDER does not move: weight never lifts an aged entry
     above an arrival, a no-rows entry or a level change;
  3. level-change entries are ordered by weight too (one of the book's
     kamis levelling up makes hundreds of entries due in one pass);
  4. a degraded read's retry sits in the aged bucket and takes its place
     by weight like any other;
  5. what is DUE is independent of the order: `next_due_at` and the
     size of the due list are as without the weight, and an entry whose every
     threshold is <= 0 is still due only at REFRESH_NO_THRESHOLD_SEC and
     then sorts LAST.
"""

from __future__ import annotations

from keeper.previews import REFRESH_NO_THRESHOLD_SEC, PreviewCache, PreviewEntry
from test_previews import OURS, _foreign

# a distinct POSITIVE threshold per attacker whose order is NOT the index
# order: the first 30 indices descend from 99,908 (so among them the lowest
# index binds hardest), the last 15 carry their own index (so among those the
# HIGHEST index binds hardest)
def _thresholds(attackers: list[int]) -> dict[int, int]:
    return {a: (100_000 - a if i < 30 else a) for i, a in enumerate(attackers)}


def _world(cache: PreviewCache, account: int = OURS, fetched_at: float = 0.0):
    """Every foreign occupant of the recorded node on file, fetched at `fetched_at`,
    one row for the book's kami at level 38 with the `_thresholds` line."""
    foreign = _foreign()
    thr = _thresholds(sorted(foreign))
    ents = cache.entries(79, account)
    for a, occ in foreign.items():
        ents[a] = PreviewEntry(79, a, float(fetched_at), 1, occ.level, occ.account_index, {263: {"threshold": thr[a], "level": 38}}, 0, None)
    return foreign, thr, ents


def _expect(thr: dict[int, int], ents: dict[int, PreviewEntry]) -> list[int]:
    return sorted(thr, key=lambda a: (-max(0, thr[a]), ents[a].fetched_at, a))


def test_aged_entries_are_offered_highest_line_first_then_oldest(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, thr, ents = _world(cache)
    assert len(foreign) == 45 and len(set(thr.values())) == 45 and all(v > 0 for v in thr.values())
    want = _expect(thr, ents)
    assert sorted(foreign) != want  # the fix is observable: index order != weight order
    due = cache.due(79, OURS, foreign, {263: 38}, 3600, 3600.0)
    assert [r for _a, r in due] == ["older than 3600 s"] * 45
    assert [a for a, _r in due] == want

    # two equal lines: the OLDER fetch first
    cache2 = PreviewCache(tmp_path / "b", lambda: 0.0)
    foreign, thr, ents = _world(cache2)
    hi, lo = want[0], want[1]
    thr[hi] = thr[lo] = 100_000
    ents[hi].rows[263]["threshold"] = ents[lo].rows[263]["threshold"] = 100_000
    ents[hi].fetched_at = 10.0
    ents[lo].fetched_at = 0.0
    due = cache2.due(79, OURS, foreign, {263: 38}, 3600, 4000.0)
    assert [a for a, _r in due][:2] == [lo, hi] and [a for a, _r in due] == _expect(thr, ents)
    # equal lines AND equal fetch times: the lower attacker index first
    ents[hi].fetched_at = ents[lo].fetched_at = 0.0
    due = cache2.due(79, OURS, foreign, {263: 38}, 3600, 4000.0)
    assert [a for a, _r in due][:2] == sorted((hi, lo))


def test_bucket_order_is_unchanged_arrivals_no_rows_levels_aged(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, thr, ents = _world(cache)
    ordered = sorted(foreign)
    arrivals = ordered[-3:]  # the three HIGHEST indices, never fetched
    for a in arrivals:
        del ents[a]
    no_rows, level = ordered[0], ordered[1]
    ents[no_rows].rows = {}
    ents[level].rows[263]["threshold"] = 1  # a LOW line
    foreign[level].level = (ents[level].attacker_level or 0) + 1
    aged = ordered[2:6]
    for i, a in enumerate(aged):
        ents[a].rows[263]["threshold"] = 900 - i  # HIGH lines, descending
    for a in ordered[6:-3]:
        del ents[a]  # keep the world small and the expectation exact
        del foreign[a]
    due = cache.due(79, OURS, foreign, {263: 38}, 3600, 3600.0, own_on_node=True)
    assert [a for a, _r in due] == arrivals + [no_rows, level] + aged
    assert [r for _a, r in due][:3] == ["arrival"] * 3
    assert due[3][1] == "no rows for the account; a kami is on the node now"
    assert due[4][1].startswith("attacker level ")
    assert [r for _a, r in due][5:] == ["older than 3600 s"] * 4


def test_level_change_entries_are_ordered_by_weight_too(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, _thr, ents = _world(cache)
    five = sorted(foreign)[:5]
    lines = {five[0]: 10, five[1]: 50, five[2]: 30, five[3]: 20, five[4]: 40}
    for a in list(foreign):
        if a in five:
            ents[a].rows[263]["threshold"] = lines[a]
        else:
            del ents[a]
            del foreign[a]
    due = cache.due(79, OURS, foreign, {263: 39}, 3600, 100.0)
    assert [r for _a, r in due] == ["kami 263 level changed"] * 5
    assert [a for a, _r in due] == [five[1], five[4], five[2], five[3], five[0]]


def test_degraded_read_retries_sit_in_the_aged_bucket_by_weight(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, _thr, ents = _world(cache)
    hi, lo, clean = sorted(foreign)[:3]
    for a in list(foreign):
        if a not in (hi, lo, clean):
            del ents[a]
            del foreign[a]
    ents[hi].rows[263]["threshold"] = 500
    ents[lo].rows[263]["threshold"] = 100
    ents[clean].rows[263]["threshold"] = 300
    for a in (hi, lo):
        ents[a].fetched_at = 1000.0  # age 3000: past `retry_sec`, short of 3600
        ents[a].invalid_rows = {263: "no vitals: hp"}
    due = cache.due(79, OURS, foreign, {263: 38}, 3600, 4000.0, retry_sec=300)
    assert [a for a, _r in due] == [hi, clean, lo]
    assert dict(due) == {hi: "degraded read: 1 invalid rows; retry", lo: "degraded read: 1 invalid rows; retry", clean: "older than 3600 s"}


def test_next_due_at_and_pending_count_are_unchanged(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, _thr, _ents = _world(cache)
    assert cache.next_due_at(79, OURS, 3600) == 3600.0
    assert len(cache.due(79, OURS, foreign, {263: 38}, 3600, 3600.0)) == 45
    assert cache.due(79, OURS, foreign, {263: 38}, 3600, 100.0) == []  # nothing is due early


def test_a_zero_line_entry_is_due_only_at_86400_and_then_sorts_last(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    foreign, thr, ents = _world(cache)
    zero = sorted(foreign)[7]
    ents[zero].rows[263]["threshold"] = -18  # every threshold <= 0: max_clamped 0
    thr[zero] = -18
    assert ents[zero].max_clamped() == 0
    at_refresh = cache.due(79, OURS, foreign, {263: 38}, 3600, 3600.0)
    assert zero not in [a for a, _r in at_refresh] and len(at_refresh) == 44
    at_86400 = cache.due(79, OURS, foreign, {263: 38}, 3600, float(REFRESH_NO_THRESHOLD_SEC))
    assert len(at_86400) == 45 and at_86400[-1] == (zero, f"older than {REFRESH_NO_THRESHOLD_SEC} s")
    assert [a for a, _r in at_86400] == _expect(thr, ents)
