"""The keeper's lens client on the kami-lens 1.0.0 contract, working
against BOTH a 0.6.3 daemon and a 1.0.0 one:

  1. the capability record — every 1.0.0 feature is gated on
     `status.version`, and `perceive` is what sets it;
  2. the staleness lower bound — the APPLIED mark when the daemon serves
     one (so a 1.0.0 boot, whose `reconciledThrough` starts far behind, is
     not read as stale), `reconciledThrough` on 0.6.3; an ABSENT head
     sample falls back to the mirror block;
  3. the read-past-last-act rule on `Basis`;
  4. end to end, answered by the daemon: the book's own stop is not applied yet, the read is refused
     `NOT_APPLIED`, the tick decides nothing and says so, and the next
     read past the bar settles the plan — and the same scenario on 0.6.3,
     where no option is sent and the `reconciledThrough` bar does it;
  5. an `incomplete` row is an unknown: the tick is gated, nothing is
     decided, nothing pauses;
  6. an `incomplete` row never becomes a preview LINE, and an
     `INCOMPLETE` attacker is a refused preview, never a reading;
  7. `--targets`: the preview selects the account's occupants and the attacker
     before projection, and serves the same lines the full read did;
  8. `reconcile-stalled:<sec>` never gates; `unhealed-ranges` still does;
  9. the belt: a client that believes it talks to 1.0.0 and does not;
 10. the protocol meta and the ledger key;
 11. the absent head fields.
"""

from __future__ import annotations

import json

import pytest

from fakelens import FakeLens
from keeper import SCHEMA_VERSION
from keeper.core import Keeper
from keeper.lens import LensClient, LensFeatures, features_for, perceive, staleness_of
from keeper.previews import PreviewCache
from keeper.protocol import META_KEYS, Basis
from keeper.strategies.base import RESTING
from test_phase2a import BOOK_43, CAROL, NODE, _add_occupant, _apply_plans_to_world, _last_tick, _preview_threshold, _rows, _set_party, _start, _tick_all
from test_phase2c import _live_env
from test_phase2g import _book, _env, _tick
from test_previews import ATT92, NODE79, OURS, _foreign

V1 = "1.0.0"


def _one_lens(sock_dir, version: str = V1, name: str = "x.sock") -> tuple[FakeLens, LensClient]:
    fake = FakeLens(str(sock_dir / name), lens_version=version).start()
    return fake, LensClient(fake.socket_path, 5)


def _args(fake: FakeLens, query: str) -> list[list[str]]:
    return [r["args"] for r in fake.requests if r["query"] == query]


# ------------------------------------------------------ 1. the capability record


def test_features_are_the_version_and_perceive_sets_them(make_keeper):
    for v in ("0.6.3", "0.6.1", None, "garbage", ""):
        f = features_for(v)
        assert (f.applied_through, f.at_least, f.targets) == (False, False, False), v
    for v in ("1.0.0", "1.2.3", "2.0.0-rc.1"):
        f = features_for(v)
        assert (f.applied_through, f.at_least, f.targets) == (True, True, True), v
        assert f.version == v
    assert LensFeatures() == features_for(None)

    e = make_keeper()  # the 0.6.3 default
    per = perceive(e.keeper.lens, 90)
    assert per.lens_version == "0.6.3" and e.keeper.lens.features.at_least is False
    assert per.applied_through is None and per.basis.lensAppliedThrough is None
    e.lens.lens_version = V1
    per = perceive(e.keeper.lens, 90)
    assert per.lens_version == V1 and e.keeper.lens.features == features_for(V1)
    assert per.applied_through == e.lens.block and per.basis.lensAppliedThrough == e.lens.block


# ------------------------------------------------------------- 2. the staleness bound


def test_the_lower_bound_is_the_applied_mark_when_the_daemon_serves_one(sock_dir):
    # 0.6.3: reconciledThrough against the sampled head, exactly as before
    fake, c = _one_lens(sock_dir, "0.6.3", "a.sock")
    ans = c.status()
    assert staleness_of(ans.data, ans.meta) == 5  # head = block + 3, reconciled = block - 2
    assert ans.applied_through is None
    fake.stop()
    # 1.0.0 with an ABSENT head sample: the reference falls back to the
    # mirror block and the bound is the applied mark
    fake, c = _one_lens(sock_dir, V1, "b.sock")
    fake.head_absent = True
    fake.applied_lag = 4
    ans = c.status()
    assert "headBlockNumber" not in ans.data and "blockLag" not in ans.data and "headSampledAt" not in ans.data
    assert ans.data["sync"]["appliedThrough"] == fake.block - 4
    assert staleness_of(ans.data, ans.meta) == 4  # ref = meta.blockNumber
    fake.stop()


def test_a_1_0_0_boot_whose_reconciled_mark_lags_is_not_stale(sock_dir):
    """The 1.0.0 reconciled mark starts far below the applied one (it is
    seeded at the boot block and re-read pass by pass). Reading THAT as
    freshness would flag a healthy daemon stale for minutes."""
    fake = FakeLens(str(sock_dir / "c.sock"), mode="stale", lag=300, lens_version=V1).start()
    fake.applied_lag = 2
    c = LensClient(fake.socket_path, 5)
    per = perceive(c, 90)
    assert per.degraded == [] and per.can_act is True
    assert per.staleness_blocks == 5  # head = block + 3 against appliedThrough = block - 2
    assert per.basis.lensReconciledThrough == fake.block - 300
    fake.stop()
    # the same status on 0.6.3 (no applied mark at all) IS stale
    fake = FakeLens(str(sock_dir / "d.sock"), mode="stale", lag=300).start()
    per = perceive(LensClient(fake.socket_path, 5), 90)
    assert per.degraded == ["lens-stale:303"]
    fake.stop()


# ----------------------------------------------------------------- 3. the read-past-last-act rule


def test_read_past_last_act_takes_the_applied_mark_when_there_is_one():
    assert Keeper._read_past_last_act(Basis(lensBlock=99, lensReconciledThrough=90, lensAppliedThrough=100), 95) is True
    assert Keeper._read_past_last_act(Basis(lensBlock=99, lensReconciledThrough=90, lensAppliedThrough=None), 95) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=99, lensReconciledThrough=96, lensAppliedThrough=None), 95) is True
    # the applied mark DECIDES: a reconciled mark past the bar does not
    # rescue an applied mark short of it (the writes are not in yet)
    assert Keeper._read_past_last_act(Basis(lensBlock=99, lensReconciledThrough=96, lensAppliedThrough=94), 95) is False
    assert Keeper._read_past_last_act(Basis(lensBlock=200, lensReconciledThrough=None, lensAppliedThrough=95), 95) is True


# ------------------------------------------- 4. end to end: "my stop did not apply"


def _events(e, book) -> list[str]:
    return [r["event"] for r in _rows(e, book, "lifecycle")]


def test_a_landed_stop_the_mirror_has_not_applied_is_refused_and_then_settles(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)
    e.lens.lens_version = V1
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    k = BOOK_43[0]
    _set_party(e.lens, k, hp=(20, 240))  # below the floor: one stop
    _tick_all(e)
    receipts = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["stage"] == "receipt"]
    assert len(receipts) == 1 and receipts[0]["system"] == "system.harvest.stop" and receipts[0]["status"] == "success"
    B = receipts[0]["block"]
    assert e.keeper.runtime[book.id]["lastActBlock"] == B
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}

    # the mirror is PAST the receipt block and has not applied it: on 0.6.3
    # that read comes back and the Basis rule has to catch it; on 1.0.0 the
    # daemon holds it and refuses, and says how far it got
    e.lens.block = B + 5
    e.lens.applied_lag = 10  # appliedThrough = B - 5
    e.lens.requests.clear()
    sent = provider.count("eth_sendRawTransaction")
    before = (len(_rows(e, book, "decision")), len(_rows(e, book, "tx")), len(_rows(e, book, "deviation")))
    _tick_all(e)
    asked = [a for a in _args(e.lens, "party") + _args(e.lens, "node") if "--at-least" in a]
    assert asked and all(a[a.index("--at-least") + 1] == str(B) for a in asked)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lastActBlock"] == B and summary["lensAppliedThrough"] == B - 5
    assert (len(_rows(e, book, "decision")), len(_rows(e, book, "tx")), len(_rows(e, book, "deviation"))) == before
    assert provider.count("eth_sendRawTransaction") == sent
    assert book.state == "running" and e.keeper.runtime[book.id].get("lastError") is None
    assert "external-change" not in _events(e, book) and "paused" not in _events(e, book)
    assert book.plan[str(k)] == {"state": RESTING, "node": None, "pending": True}
    st = e.keeper.handle("status", {"book": book.id})["data"]
    assert st["books"][0]["lastError"] is None and st["lens"]["appliedThrough"] is not None and st["lens"]["version"] == V1

    # the mark reaches the bar: the read is served, the pending entry
    # settles, and nothing contradicts anything
    e.lens.applied_lag = 0
    e.lens.block = B + 2
    _apply_plans_to_world(e)
    _tick_all(e)
    assert book.plan[str(k)] == {"state": RESTING, "node": None}
    assert _last_tick(e, book).get("reason") != "awaiting a read past the last act"
    assert "external-change" not in _events(e, book) and book.state == "running"
    assert e.keeper.runtime[book.id].get("contradictionTicks", 0) == 0


def test_the_same_scenario_on_0_6_3_sends_no_option_and_behaves_as_today(make_keeper, tmp_path):
    e, provider, w3, chain = _live_env(make_keeper, tmp_path)  # the 0.6.3 default
    book = _start(e, BOOK_43[:3], previewsPerTick=50)
    e.books = [book]  # type: ignore[attr-defined]
    k = BOOK_43[0]
    _set_party(e.lens, k, hp=(20, 240))
    _tick_all(e)
    B = [r["tx"] for r in _rows(e, book, "tx") if r["tx"]["stage"] == "receipt"][0]["block"]
    e.lens.block = B + 1  # reconciledThrough = B - 1: the read short of the bar
    e.lens.requests.clear()
    _tick_all(e)
    assert not any("--at-least" in r["args"] or "--targets" in r["args"] or "--account" in r["args"] for r in e.lens.requests)
    summary = _last_tick(e, book)
    assert summary["acted"] is False and summary["reason"] == "awaiting a read past the last act"
    assert summary["lensReconciledThrough"] == B - 1 and summary.get("lensAppliedThrough") is None
    assert book.state == "running"


# -------------------------------------------------------- 5. an incomplete row gates


def test_an_incomplete_party_row_gates_the_tick_and_decides_nothing(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    e.lens.lens_version = V1
    a = _book(e, BOOK_43[:4], previewsPerTick=50)
    b = _book(e, [25814, 27850, 28532], previewsPerTick=50)
    _tick(e)
    assert _last_tick(e, a).get("reason") != "perception gated"
    decided = {bk.id: len(_rows(e, bk, "decision")) for bk in (a, b)}
    # one kami of each book cannot be projected; ONE party read serves both
    e.lens.set_incomplete(BOOK_43[0])
    e.lens.set_incomplete(27850)
    _tick(e)
    assert len([r for r in e.lens.requests if r["query"] == "party"]) == 1  # the shared read
    for book, kami in ((a, BOOK_43[0]), (b, 27850)):
        s = _last_tick(e, book)
        assert s["reason"] == "perception gated" and "lens-incomplete" in s["degraded"]
        assert any(line.startswith(f"kami {kami}: missing incomplete") for line in s["incomplete"])
        assert book.state == "running" and len(_rows(e, book, "decision")) == decided[book.id]
    assert all("paused" not in _events(e, bk) for bk in (a, b))
    # cleared: the next tick is a normal observation again
    e.lens.set_incomplete(BOOK_43[0], False)
    e.lens.set_incomplete(27850, False)
    _tick(e)
    for book in (a, b):
        assert _last_tick(e, book).get("reason") != "perception gated"


# ------------------------------------- 6. an incomplete row is never a preview line


def _fake79(sock_dir, name: str, version: str = V1) -> tuple[FakeLens, LensClient]:
    fake = FakeLens(str(sock_dir / name), lens_version=version).start()
    fake.nodes[79] = json.loads(json.dumps(NODE79))
    fake.previews[79] = {360: json.loads(json.dumps(ATT92))}
    c = LensClient(fake.socket_path, 5)
    c.features = features_for(version)
    return fake, c


def test_an_incomplete_own_row_carries_the_old_line_and_retries(sock_dir, tmp_path):
    fake, lens = _fake79(sock_dir, "p1.sock")
    cache = PreviewCache(tmp_path, lambda: 100.0)
    foreign = _foreign()
    first, _c, _a = cache.fetch(lens, 79, 360, foreign[360], OURS, 100.0, targets=None)
    assert first.invalid_rows == {} and first.rows[263]["threshold"] == -18 and len(first.rows) == 107
    # the kami cannot be projected: its row keeps identity + state and nothing else
    fake.set_incomplete(263)
    second, changed, ans = cache.fetch(lens, 79, 360, foreign[360], OURS, 200.0)
    assert second.invalid_rows[263] == "incomplete row" and second.degraded_read is True and changed is False
    assert second.rows[263] == dict(first.rows[263], carriedFrom=100.0)  # carried, never replaced
    assert ans.incomplete_rows >= 1
    only = {360: foreign[360]}
    assert cache.due(79, OURS, only, {}, 3600, 499.0) == []
    assert cache.due(79, OURS, only, {}, 3600, 500.0) == [(360, "degraded read: 1 invalid rows; retry")]
    assert cache.next_due_at(79, OURS, 3600, {360}) == 500.0
    # a kami with no old row gets NO row at all, and the stand-in covers it
    fake.set_incomplete(263, False)
    fake.set_incomplete(508)
    cache2 = PreviewCache(tmp_path / "two", lambda: 300.0)
    third, _c3, _a3 = cache2.fetch(lens, 79, 360, foreign[360], OURS, 300.0)
    assert 508 not in third.rows and third.invalid_rows[508] == "incomplete row"
    assert third.clamped(508) is None and third.stand_in(220)[0] > 0
    fake.stop()


def test_an_incomplete_attacker_is_a_refused_preview_never_a_reading(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    e.lens.lens_version = V1
    # one hostile attacker on the node, serving a line against the book's kami
    e.lens.nodes[NODE]["harvests"] = [h for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] == CAROL or h["kami"]["index"] == 360]
    _preview_threshold(e.lens, 360, 263, 100)
    book = _book(e, [263], previewsPerTick=50)
    _tick(e)
    assert _last_tick(e, book)["previewCalls"] == 1
    bound = e.keeper.previews.entries(NODE, CAROL)[360].clamped(263)
    assert bound == 100
    # the attacker itself cannot be projected: the pairing read is refused
    e.lens.set_incomplete(360)
    _tick(e, 3601.0)  # the entry is due again
    s = _last_tick(e, book)
    assert s["previewsRefused"] == [360] and s["previewsIncomplete"] == [360]
    assert s["previewCalls"] == 0 and book.state == "running"
    assert e.keeper.previews.entries(NODE, CAROL)[360].clamped(263) == 100  # the old line stands


# -------------------------------------------------------------- 7. the selector


def test_a_preview_selects_our_occupants_and_the_attacker(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    e.lens.lens_version = V1
    book = _book(e, BOOK_43[:3], previewsPerTick=1)
    _tick(e)
    reads = [a for a in _args(e.lens, "node") if "--targets" in a]
    assert len(reads) == 1
    attacker = int(reads[0][1])
    sent = [int(x) for x in reads[0][reads[0].index("--targets") + 1].split(",")]
    own_present = sorted(h["kami"]["index"] for h in e.lens.nodes[NODE]["harvests"] if h["account"]["index"] == CAROL)
    assert sent == sorted(set(own_present) | {attacker}) and 1 <= len(sent) <= 500
    assert "--account" not in reads[0]  # the preview needs no account selector: the account's rows are what it keeps
    # the selected answer serves the same lines the full read did
    selected = {k: dict(v) for k, v in e.keeper.previews.entries(NODE, CAROL)[attacker].rows.items()}

    f = _env(make_keeper, tmp_path / "063")  # the same world on 0.6.3
    _book(f, BOOK_43[:3], previewsPerTick=1)
    _tick(f)
    assert not any("--targets" in r["args"] or "--account" in r["args"] for r in f.lens.requests)
    assert {k: dict(v) for k, v in f.keeper.previews.entries(NODE, CAROL)[attacker].rows.items()} == selected


def test_above_500_own_occupants_the_selector_is_dropped_not_truncated(make_keeper, tmp_path):
    e = _env(make_keeper, tmp_path)
    e.lens.lens_version = V1
    for i in range(400):  # 107 own occupants in the fixture + 400 = 507
        _add_occupant(e.lens, 900000 + i, CAROL)
    _book(e, BOOK_43[:3], previewsPerTick=1)
    _tick(e)
    previews = [a for a in _args(e.lens, "node") if len([x for x in a if not x.startswith("--")]) >= 2]
    assert previews and not any("--targets" in a for a in previews)


# --------------------------------------------------- 8. reconcile-stalled never gates


def test_reconcile_stalled_does_not_gate_but_unhealed_ranges_still_does(make_keeper):
    e = make_keeper(lens_version=V1)
    e.grant()
    book = e.keeper.store.get(e.start_haf()["data"]["book"]["id"])
    e.lens.extra_degraded = ["reconcile-stalled:45"]
    per = perceive(e.keeper.lens, 90)
    assert per.can_act is True and per.degraded == [] and "reconcile-stalled:45" in per.lens_status["degraded"]
    e.lens.set_kami(1001, hp=(40, 200))  # below feedBelowHp 100
    summary = e.keeper.tick_book(book)
    assert summary["acted"] is True and summary["decisions"] == ["feed"]
    assert not any(f.startswith("lens-degraded") for f in summary.get("degraded", []))
    assert not any(f.startswith("lens-degraded") for f in _rows(e, book, "tick")[-1]["degraded"])
    # the chain-correctness one still gates every book of the tick
    e.lens.extra_degraded = ["unhealed-ranges:1"]
    e.lens.set_kami(1001, hp=(40, 200))
    summary = e.keeper.tick_book(book)
    assert summary["acted"] is False and "lens-degraded:unhealed-ranges" in summary["degraded"]


# ------------------------------------------------------------------- 9. the belt


def test_a_client_that_believes_in_1_0_0_against_a_0_6_3_daemon(sock_dir):
    fake, c = _one_lens(sock_dir, "0.6.3", "belt.sock")
    c.features = features_for(V1)
    ans = c.node(62, with_vitals=True, targets=[1001], account=4242)
    assert ans.data["index"] == 62 and c.features.targets is False and c.features.at_least is True
    assert [r["args"] for r in fake.requests] == [
        ["62", "--with-vitals", "--targets", "1001", "--account", "4242"],
        ["62", "--with-vitals"],
    ]
    fake.requests.clear()
    c.node(62, with_vitals=True, targets=[1001])  # the flag is off: nothing is sent
    assert [r["args"] for r in fake.requests] == [["62", "--with-vitals"]]
    # the freshness option has its own flag
    fake.requests.clear()
    ans = c.kami(1001, at_least=500)
    assert ans.data["index"] == 1001 and c.features.at_least is False
    assert [r["args"] for r in fake.requests] == [["1001", "--at-least", "500"], ["1001"]]
    assert c.calls == 5  # 2 + 1 + 2: every retry is a second round trip, counted
    fake.stop()


# ------------------------------------------------------- 10. the protocol and ledger


@pytest.mark.parametrize("version,applied", [("0.6.3", False), (V1, True)])
def test_the_meta_and_the_ledger_carry_the_applied_mark(make_keeper, version, applied):
    assert SCHEMA_VERSION == "0.1.1"
    assert "lensAppliedThrough" in META_KEYS
    e = make_keeper(lens_version=version)
    e.grant()
    book = e.keeper.store.get(e.start_haf()["data"]["book"]["id"])
    st = e.keeper.handle("status", {})
    assert set(st["meta"]) == META_KEYS
    assert (st["meta"]["lensAppliedThrough"] is not None) is applied
    e.keeper.tick_book(book)
    rows = e.keeper.handle("ledger", {"book": book.id})["data"]["rows"]
    assert rows and all("lensAppliedThrough" in r for r in rows)
    assert (rows[-1]["lensAppliedThrough"] is not None) is applied


# -------------------------------------------------------- 11. the absent head sample


def test_an_absent_head_sample_is_not_a_fault(make_keeper):
    e = make_keeper(lens_version=V1)
    e.grant()
    book = e.keeper.store.get(e.start_haf()["data"]["book"]["id"])
    e.lens.head_absent = True
    e.lens.applied_lag = 7
    per = perceive(e.keeper.lens, 90)
    assert per.degraded == [] and per.staleness_blocks == 7  # from meta.blockNumber
    lens_block = e.keeper.handle("status", {})["data"]["lens"]
    assert lens_block["appliedThrough"] == e.lens.block - 7 and lens_block["stalenessBlocks"] == 7
    assert lens_block["version"] == V1 and lens_block.get("blockLag") is None
    e.lens.set_kami(1001, hp=(40, 200))
    assert e.keeper.tick_book(book)["acted"] is True  # it acts: nothing is unknown
