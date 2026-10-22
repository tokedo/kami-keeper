"""SPEC P8 — stop --all idempotent with a quiescence proof; revoke stops
books (grant-revoked) and returns the proof; restart resumes running
books with a `resumed` row and never restarts a stopped one; pause /
resume. (`armed` selection: tests/test_core_1b.py.)"""

from __future__ import annotations

from keeper.config import load as load_config
from keeper.core import Keeper
from keeper.lens import LensClient


def test_stop_all_is_idempotent_and_returns_quiescence(env):
    env.grant()
    ids = [env.start_haf()["data"]["book"]["id"] for _ in range(2)]
    first = env.keeper.handle("stop", {"all": True, "account": "lab"})["data"]
    assert sorted(first["stopped"]) == sorted(ids)
    assert first["quiescence"]["quiescent"] is True and first["quiescence"]["pendingNonce"] == first["quiescence"]["confirmedNonce"]
    second = env.keeper.handle("stop", {"all": True, "account": "lab"})["data"]
    assert second["stopped"] == [] and second["quiescence"]["quiescent"] is True
    for b in env.keeper.store.all():
        assert b.state == "stopped" and b.reason == {"stopped_by": "agent", "reason": "stop --all"}
    single = env.keeper.handle("stop", {"book": ids[0]})["data"]
    assert single["stopped"] == [] and single["alreadyStopped"] is True


def test_revoke_stops_every_book_with_grant_revoked_and_drops_the_key(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    r = env.keeper.handle("revoke", {"account": "lab"})["data"]
    assert r["revoked"] is True and r["stopped"] == [bid] and r["quiescence"]["quiescent"]
    assert env.keeper.store.get(bid).reason == {"stopped_by": "agent", "reason": "grant-revoked"}
    assert env.keeper.signer.has_grant("lab") is False
    assert env.keeper.handle("status")["data"]["grants"] == []
    assert env.start_haf()["error"]["code"] == "NOT_GRANTED"
    rows = env.keeper.handle("ledger", {"book": bid})["data"]["rows"]
    assert rows[-1]["event"] == "stopped_by" and "grant-revoked" in rows[-1]["reason"]


def test_restart_resumes_running_books_and_never_a_stopped_one(env):
    env.grant()
    running = env.start_haf()["data"]["book"]["id"]
    stopped = env.start_haf()["data"]["book"]["id"]
    env.keeper.handle("stop", {"book": stopped})
    cfg = load_config(flags={"profile": "lab", "dataDir": str(env.data_dir), "lensSocket": env.lens.socket_path, "keysFile": str(env.keys_file), "secretsBackend": "env", "maxBooksPerAccount": 4}, env={})
    k2 = Keeper(cfg, lens=LensClient(env.lens.socket_path, 5))
    k2.boot()
    assert k2.store.get(running).state == "running" and k2.store.get(stopped).state == "stopped"
    assert [r["event"] for r in k2.handle("ledger", {"book": running})["data"]["rows"] if r["kind"] == "lifecycle"][-1] == "resumed"
    assert not any(r.get("event") == "resumed" for r in k2.handle("ledger", {"book": stopped})["data"]["rows"])
    # grants are not persisted: the restarted keeper holds no key until granted again
    assert k2.signer.grants() == []


def test_pause_and_resume(env):
    env.grant()
    bid = env.start_haf()["data"]["book"]["id"]
    assert env.keeper.handle("pause", {"book": bid})["data"]["book"]["state"] == "paused"
    assert env.keeper.handle("pause", {"book": bid})["error"]["code"] == "BAD_ARGS"
    assert env.keeper.handle("resume", {"book": bid})["data"]["book"]["state"] == "running"
    assert env.keeper.handle("resume", {"book": bid})["error"]["code"] == "BAD_ARGS"
    events = [r["event"] for r in env.keeper.handle("ledger", {"book": bid})["data"]["rows"] if r["kind"] == "lifecycle"]
    assert events == ["started", "paused", "resumed"]


def test_auto_without_account_index_is_refused_by_the_schema_and_described(env):
    """`auto` is implemented; `accountIndex` is required (the
    party / inventory reads take the on-chain index, never the label)."""
    env.grant()
    r = env.keeper.handle("start", {"account": "lab", "strategyType": "auto", "config": {"kamiIndices": [1001], "nodeIndex": 62}})
    assert r["error"]["code"] == "BAD_ARGS" and "accountIndex" in r["error"]["message"]
    assert env.keeper.handle("describe", {"type": "auto"})["ok"]


def test_engine_loop_ticks_due_books(env):
    env.grant()
    bid = env.start_haf(tickSeconds=5)["data"]["book"]["id"]
    book = env.keeper.store.get(bid)
    assert env.keeper.tick_due() == [bid]
    assert env.keeper.tick_due() == []  # not due for another 5 s
    env.keeper.runtime[bid]["nextTickAt"] = 0
    assert env.keeper.tick_due() == [bid]
    assert book.state == "running"
