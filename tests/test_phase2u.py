"""The preview cache is scoped per NODE AND ACCOUNT.

With a node-scoped cache, a second account's book arriving on a node
another account's book already held finds only the first account's rows:
every one of its kamis rests at the `no row for this kami` stand-in, and
the two books thrash (each refresh replacing the other account's rows).

The rows, entries, presence and due lists of one account are its own; the
per-pass fetch cap and the cooldowns served this pass stay NODE facts.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from fakelens import FakeLens
from keeper import SCHEMA_VERSION
from keeper.lens import LensClient
from keeper.previews import PreviewCache, PreviewEntry
from keeper.strategies.base import HARVESTING
from test_phase2a import CAROL, FRIENDS, NODE, T, _config, _preview_threshold, _seed
from test_phase2h import _bounds
from test_previews import ATT92, NODE79, OURS, _foreign

A = OURS  # the account the recorded preview's occupants belong to
B = 4242  # a second account with kamis on the same node
C = 5151  # an account with nothing on the node
MOVED = 40  # how many of A's occupants the fixture hands to B


def _split_preview() -> tuple[dict[str, Any], list[int], list[int]]:
    """The recorded attacker preview with `MOVED` of its 107 account-A
    occupants relabelled to account B: one answer, two accounts' rows."""
    doc = copy.deepcopy(ATT92)
    for h in [h for h in doc["harvests"] if h["account"]["index"] == A][:MOVED]:
        h["account"] = {"index": B, "name": "acct-b"}
    a_kamis = sorted(h["kami"]["index"] for h in doc["harvests"] if h["account"]["index"] == A)
    b_kamis = sorted(h["kami"]["index"] for h in doc["harvests"] if h["account"]["index"] == B)
    return doc, a_kamis, b_kamis


def _lens(sock_dir):
    doc, a_kamis, b_kamis = _split_preview()
    fake = FakeLens(str(sock_dir / "p2u.sock")).start()
    fake.nodes[79] = copy.deepcopy(NODE79)
    fake.previews[79] = {360: doc}
    return fake, LensClient(fake.socket_path, 5), a_kamis, b_kamis


def _entry(threshold: int = 100, fetched_at: float = 0.0) -> PreviewEntry:
    return PreviewEntry(79, 360, fetched_at, 1, 38, 806, {263: {"threshold": threshold, "level": 38, "hpTotal": 240}}, 0, None)


def _doc(entry: PreviewEntry, **extra: Any) -> str:
    d = {"schemaVersion": SCHEMA_VERSION, "node": 79, "savedAt": "2026-09-25T21:05:00.000Z", "entries": {"360": entry.to_dict()}}
    d.update(extra)
    return json.dumps(d, indent=1, sort_keys=True)


# ------------------------------------------------------------ the scope


def test_two_accounts_on_one_node_keep_their_own_rows(sock_dir, tmp_path):
    fake, lens, a_kamis, b_kamis = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 1000.0)
    att = _foreign()[360]
    ea, _ch, _ans = cache.fetch(lens, 79, 360, att, A, 1000.0)
    assert len(a_kamis) == 107 - MOVED and len(b_kamis) == MOVED and set(a_kamis).isdisjoint(b_kamis)
    assert sorted(ea.rows) == a_kamis
    eb, _ch, _ans = cache.fetch(lens, 79, 360, att, B, 2000.0)
    assert sorted(eb.rows) == b_kamis
    # B's fetch left A's entry exactly as it was — rows AND fetchedAt
    kept = cache.entries(79, A)[360]
    assert kept is ea and sorted(kept.rows) == a_kamis and kept.fetched_at == 1000.0
    assert cache.entries(79, B)[360].fetched_at == 2000.0
    # the cap is a NODE fact: both fetches counted against the one node
    assert cache.pass_fetches[79] == 2 and list(cache.pass_fetches) == [79]
    fake.stop()


def test_other_accounts_entry_never_suppresses_a_fetch(sock_dir, tmp_path):
    fake, lens, _a, _b = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 1000.0)
    att = _foreign()[360]
    cache.fetch(lens, 79, 360, att, A, 1000.0)
    assert cache.due(79, A, {360: att}, {}, 3600, 1000.0) == []  # A is served
    assert cache.due(79, B, {360: att}, {}, 3600, 1000.0) == [(360, "arrival")]
    assert cache.next_due_at(79, B, 3600) is None  # B has no entry at all yet
    fake.stop()


# ------------------------------------------------------------ persistence


def test_scopes_persist_to_their_own_files_and_legacy_file_is_ignored(sock_dir, tmp_path):
    fake, lens, a_kamis, b_kamis = _lens(sock_dir)
    cache = PreviewCache(tmp_path, lambda: 5.0)
    att = _foreign()[360]
    cache.fetch(lens, 79, 360, att, A, 5.0)
    cache.fetch(lens, 79, 360, att, B, 6.0)
    assert cache.save(79, A) is True and cache.save(79, B) is True and cache.save(79, A) is False
    d = tmp_path / "previews"
    assert sorted(p.name for p in d.glob("*.json")) == [f"79-{A}.json", f"79-{B}.json"]
    assert json.loads((d / f"79-{B}.json").read_text())["account"] == B
    # the legacy node-scoped file is neither read nor written
    legacy = d / "79.json"
    legacy.write_text(_doc(_entry(threshold=999, fetched_at=1.0)), encoding="utf-8")
    before, mtime = legacy.read_text(), legacy.stat().st_mtime
    again = PreviewCache(tmp_path, lambda: 7.0)
    assert again.entries(79, C) == {} and again.restore[f"79-{C}"] == "missing"
    assert again.save(79, C) is False
    # and each scope restores from its own file
    assert sorted(again.entries(79, A)[360].rows) == a_kamis and again.restore[f"79-{A}"] == "restored"
    assert sorted(again.entries(79, B)[360].rows) == b_kamis and again.restore[f"79-{B}"] == "restored"
    assert again.entries(79, A)[360].fetched_at == 5.0 and again.entries(79, B)[360].fetched_at == 6.0
    assert legacy.read_text() == before and legacy.stat().st_mtime == mtime
    fake.stop()


def test_seed_file_from_another_instance_is_readable(tmp_path):
    """A legacy archive: the `save()` doc shape with `node` and no
    `account` key — only `entries` matters."""
    d = tmp_path / "previews"
    d.mkdir(parents=True)
    (d / f"79-{B}.json").write_text(_doc(_entry(threshold=7, fetched_at=4.0)), encoding="utf-8")
    cache = PreviewCache(tmp_path, lambda: 100.0)
    ents = cache.entries(79, B)
    assert cache.restore[f"79-{B}"] == "restored" and set(ents) == {360}
    assert ents[360].clamped(263) == 7 and ents[360].fetched_at == 4.0 and ents[360].attacker_account == 806
    assert cache.entries(79, A) == {} and cache.restore[f"79-{A}"] == "missing"


# ------------------------------------------------------------ presence


def test_presence_and_forget_are_scoped(tmp_path):
    cache = PreviewCache(tmp_path, lambda: 0.0)
    for acct in (A, B):
        cache.entries(79, acct)[360] = _entry()
    out = cache.reconcile_presence(79, B, {}, set(), 10.0, 24 * 3600.0)
    assert out == {"remembered": [], "dropped": [360], "expired": []}
    assert cache.entries(79, B) == {} and 360 in cache.entries(79, A)  # A's entry untouched
    cache.entries(79, B)[360] = _entry()
    cache.forget(79, A, 360)
    assert cache.entries(79, A) == {} and 360 in cache.entries(79, B)


# ------------------------------------------------------------ through the engine


def _two_account_world(e) -> tuple[list[int], list[int]]:
    """The recorded-node world with a SECOND account on the same node: five
    of the recorded account's occupants are relabelled account B on the
    node, in the recorded preview of the attacker and into a party of their
    own. The node is pruned to that attacker and the two books, and each
    book lists the other account as a friend, so one pass fetches the one
    attacker for each book inside the shared per-node cap."""
    fake = e.lens
    party_rows = {r["index"]: r for r in fake.party[CAROL]["kamis"]}
    node_rows = {h["kami"]["index"]: h for h in fake.nodes[NODE]["harvests"]}
    prev_rows = {h["kami"]["index"]: h for h in fake.previews[NODE][360]["harvests"]}
    cands = [k for k in sorted(prev_rows) if k in party_rows and k in node_rows and node_rows[k]["account"]["index"] == CAROL and party_rows[k]["state"] == HARVESTING]
    b_kamis, a_kamis = cands[:5], cands[5:10]
    assert len(b_kamis) == 5 and len(a_kamis) == 5
    for k in b_kamis:
        node_rows[k]["account"] = {"index": B, "name": "acct-b"}
        prev_rows[k]["account"] = {"index": B, "name": "acct-b"}
    fake.party[B] = {"account": {"index": B, "name": "acct-b"}, "kamis": [dict(copy.deepcopy(party_rows[k]), account={"index": B, "name": "acct-b"}) for k in b_kamis]}
    fake.party[CAROL]["kamis"] = [r for r in fake.party[CAROL]["kamis"] if r["index"] not in b_kamis]
    fake.inventory[B] = copy.deepcopy(fake.inventory[CAROL])
    fake.inventory[B]["account"] = {"index": B, "name": "acct-b"}
    keep = set(a_kamis) | set(b_kamis) | {360}
    fake.nodes[NODE]["harvests"] = [h for h in fake.nodes[NODE]["harvests"] if h["kami"]["index"] in keep]
    # a distinct positive line per kami: a row read from the other account's
    # entry, or a stand-in, could not produce this kami's own number
    served = {k: 41 + i for i, k in enumerate(a_kamis)}
    served.update({k: 71 + i for i, k in enumerate(b_kamis)})
    for k, thr in served.items():
        _preview_threshold(fake, 360, k, thr)
    return a_kamis, b_kamis, served


def _stand_ins(lim: dict[int, dict[str, Any]]) -> list[int]:
    return sorted(k for k, v in lim.items() if (v.get("boundBy") or {}).get("thresholdFrom", "").endswith("(no row for this kami)"))


def test_two_books_of_different_accounts_on_one_node_are_each_bound_by_their_own_rows(make_keeper):
    e = make_keeper(cap=4)
    clock = {"now": T}
    e.keeper.clock = lambda: clock["now"]
    e.clock = clock  # type: ignore[attr-defined]
    _seed(e.lens)
    e.grant()
    a_kamis, b_kamis, served = _two_account_world(e)
    book_a = e.keeper.store.get(e.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": _config(a_kamis, friends=FRIENDS + [B])})["data"]["book"]["id"])
    book_b = e.keeper.store.get(e.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": _config(b_kamis, accountIndex=B, friends=FRIENDS + [CAROL])})["data"]["book"]["id"])
    clock["now"] += 30.0
    e.keeper.tick_due(now=clock["now"])
    # one pass, one preview read each: each book's own account's rows
    ents_a = e.keeper.previews.entries(NODE, CAROL)
    ents_b = e.keeper.previews.entries(NODE, B)
    assert set(ents_a) == {360} and set(ents_b) == {360}
    assert set(a_kamis) <= set(ents_a[360].rows) and set(ents_a[360].rows).isdisjoint(b_kamis)
    assert set(b_kamis) <= set(ents_b[360].rows) and set(ents_b[360].rows).isdisjoint(a_kamis)
    # and no kami of either book is priced at the other account's stand-in
    for book, kamis in ((book_a, a_kamis), (book_b, b_kamis)):
        _s, _obs, lim, _c, _a = _bounds(e, book)
        assert _stand_ins(lim) == []
        # every kami is bound by the attacker on ITS OWN served line
        assert sorted(lim) == sorted(kamis)
        assert {k: lim[k]["boundBy"]["threshold"] for k in kamis} == {k: served[k] for k in kamis}
        assert all(lim[k]["boundBy"]["attackerKami"] == 360 for k in kamis)
