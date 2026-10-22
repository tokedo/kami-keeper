"""SPEC P2 — a lab keeper with no maxBooksPerAccount does not run
books; BOOK_CAP; fleet 0 = unlimited."""

from __future__ import annotations


def test_lab_without_cap_serves_but_never_starts_a_book(make_keeper):
    e = make_keeper(cap=None)
    assert e.keeper.engine_started is False
    assert any("maxBooksPerAccount" in r for r in e.keeper.engine_refusals)
    e.keeper.start_engine()
    assert e.keeper.engine_started is False
    assert e.keeper.handle("status")["ok"] and e.keeper.handle("describe")["ok"]
    e.grant()
    s = e.start_haf()
    assert s["error"]["code"] == "BAD_ARGS" and "maxBooksPerAccount" in s["error"]["message"]
    assert e.keeper.store.all() == []


def test_lab_with_cap_enforces_book_cap(make_keeper):
    e = make_keeper(cap=2)
    e.grant()
    assert e.start_haf()["ok"] and e.start_haf(kamiIndex=1001)["ok"]
    third = e.start_haf()
    assert third["error"]["code"] == "BOOK_CAP"
    # a stopped book frees its slot
    bid = e.keeper.store.all()[0].id
    assert e.keeper.handle("stop", {"book": bid})["ok"]
    assert e.start_haf()["ok"]


def test_fleet_zero_is_unlimited_and_absent_is_fine(make_keeper):
    e = make_keeper(profile="fleet", cap=0)
    assert e.keeper.engine_refusals == []
    e.grant()
    for _ in range(5):
        assert e.start_haf()["ok"]
    f = make_keeper(profile="fleet", cap=None)
    assert f.keeper.engine_refusals == [] and f.keeper.cfg.maxBooksPerAccount is None


def test_lab_running_books_do_not_resume_when_the_cap_is_missing(make_keeper):
    e = make_keeper(cap=2)
    e.grant()
    bid = e.start_haf()["data"]["book"]["id"]
    e.keeper.stop_engine()
    # restart the keeper on the same data dir WITHOUT a cap
    from keeper.config import load as load_config
    from keeper.core import Keeper
    from keeper.lens import LensClient
    cfg = load_config(flags={"profile": "lab", "dataDir": str(e.data_dir), "lensSocket": e.lens.socket_path, "keysFile": str(e.keys_file), "secretsBackend": "env", "maxBooksPerAccount": None}, env={})
    k2 = Keeper(cfg, lens=LensClient(e.lens.socket_path, 5))
    k2.boot()
    assert k2.engine_started is False
    rows = k2.handle("ledger", {"book": bid})["data"]["rows"]
    assert not any(r.get("event") == "resumed" for r in rows)
