"""SPEC P1 — the socket: JSON-lines, one request per connection, invalid
JSON -> BAD_ARGS, the frame rule in both directions, and the daemon
entry run as a subprocess."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import FIXTURE_KEY_HEX, FIXTURE_OPERATOR_KEY
from keeper.protocol import META_KEYS
from keeper.scrub import key_shaped_paths
from keeper.server import KeeperServer, request, request_raw

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def served(env, sock_dir):
    sock = str(sock_dir / "keeper.sock")
    srv = KeeperServer(sock, env.keeper.router, request_timeout_sec=5)
    srv.serve_in_thread()
    yield env, sock
    srv.close()


def test_status_and_describe_over_the_socket(served):
    env, sock = served
    st = request(sock, "status")
    assert st["ok"] and st["meta"]["profile"] == "lab" and set(st["meta"]) == META_KEYS
    d = request(sock, "describe", {"type": "craft"})
    assert d["ok"] and d["data"]["strategyType"] == "craft"


def test_invalid_json_answers_bad_args_with_null_id(served):
    _, sock = served
    r = request_raw(sock, b"{not json\n")
    assert r == {"id": None, "ok": False, "error": {"code": "BAD_ARGS", "message": "invalid JSON"}, "meta": r["meta"]}


def test_one_request_per_connection(served):
    _, sock = served
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(5)
    conn.connect(sock)
    conn.sendall(b'{"id":1,"cmd":"describe","args":{}}\n{"id":2,"cmd":"describe","args":{}}\n')
    buf = b""
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    conn.close()
    lines = [l for l in buf.split(b"\n") if l]
    assert len(lines) == 1 and json.loads(lines[0])["id"] == 1


def test_key_shaped_value_in_a_request_is_refused_and_never_echoed(served):
    _, sock = served
    r = request(sock, "grant", {"account": FIXTURE_OPERATOR_KEY})
    assert r["ok"] is False and r["error"]["code"] == "BAD_ARGS"
    assert FIXTURE_KEY_HEX not in json.dumps(r)
    r = request(sock, "start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [], "note": FIXTURE_OPERATOR_KEY}})
    assert r["error"]["code"] == "BAD_ARGS" and FIXTURE_KEY_HEX not in json.dumps(r)


def test_no_frame_carries_a_key_shaped_value_after_a_grant(served):
    env, sock = served
    g = request(sock, "grant", {"account": "lab"})
    assert g["ok"], g
    frames = [g, request(sock, "status"), request(sock, "describe"), request(sock, "books", {"account": "lab"}), request(sock, "logs")]
    s = request(sock, "start", {"account": "lab", "strategyType": "harvestAndFeed", "config": {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}})
    assert s["ok"], s
    frames.append(s)
    frames.append(request(sock, "ledger", {"book": s["data"]["book"]["id"]}))
    for f in frames:
        assert FIXTURE_KEY_HEX not in json.dumps(f)
        assert key_shaped_paths(f) == []


def test_daemon_entry_point_serves_and_refuses_start_without_cap_under_lab(sock_dir):
    tmp_path = sock_dir
    """The brief's done-when, run as a subprocess: `python -m keeper daemon
    --profile lab --data-dir X` starts, answers status and describe over
    its socket, and refuses `start` naming maxBooksPerAccount."""
    data = tmp_path / "d"
    proc = subprocess.Popen([sys.executable, "-m", "keeper", "daemon", "--profile", "lab", "--data-dir", str(data), "--lens-socket", str(tmp_path / "no-lens.sock")], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sock = str(data / "keeper.sock")
    try:
        for _ in range(100):
            if os.path.exists(sock):
                break
            time.sleep(0.05)
        st = request(sock, "status")
        assert st["ok"] and st["meta"]["profile"] == "lab" and st["data"]["engine"]["started"] is False
        assert any("maxBooksPerAccount" in r for r in st["data"]["engine"]["refusals"])
        d = request(sock, "describe", {"type": "auto"})
        assert d["ok"]
        s = request(sock, "start", {"account": "lab", "strategyType": "craft", "config": {"recipes": [{"recipeIndex": 1}]}})
        assert s["ok"] is False and s["error"]["code"] == "BAD_ARGS" and "maxBooksPerAccount" in s["error"]["message"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
