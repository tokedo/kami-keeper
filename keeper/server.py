"""SPEC P1 — the control socket.

Unix socket, JSON-lines, ONE request and ONE response per connection
(the harness `_lens_request` shape: connect, send a line, read a line,
close). Every frame goes through the Router, so the socket has no
semantics of its own. An unparsable line answers BAD_ARGS with id null,
exactly as the lens does.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

from .protocol import Router, build_meta

MAX_LINE_BYTES = 1 << 20


class _Handler(socketserver.StreamRequestHandler):
    timeout: float | None = None  # set per server

    def handle(self) -> None:
        server: "KeeperServer" = self.server  # type: ignore[assignment]
        self.request.settimeout(server.request_timeout_sec)
        try:
            line = self.rfile.readline(MAX_LINE_BYTES)
        except (socket.timeout, TimeoutError, OSError):
            return
        if not line:
            return
        try:
            req = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            frame: dict[str, Any] = {"id": None, "ok": False, "error": {"code": "BAD_ARGS", "message": "invalid JSON"}, "meta": build_meta(server.router.profile)}
        else:
            frame = server.router.handle(req)
        try:
            self.wfile.write((json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8"))
        except OSError:
            pass


class KeeperServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: str, router: Router, request_timeout_sec: float = 30.0):
        self.socket_path = socket_path
        self.router = router
        self.request_timeout_sec = request_timeout_sec
        p = Path(socket_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        super().__init__(socket_path, _Handler)
        os.chmod(socket_path, 0o660)
        self._thread: threading.Thread | None = None

    def serve_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.2}, name="keeper-socket", daemon=True)
        t.start()
        self._thread = t
        return t

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def request(socket_path: str, cmd: str, args: dict[str, Any] | None = None, rid: Any = 1, timeout_sec: float = 30.0) -> dict[str, Any]:
    """A client: one request, one response (the same framing the harness
    uses for the lens)."""
    payload = (json.dumps({"id": rid, "cmd": cmd, "args": args or {}}) + "\n").encode("utf-8")
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout_sec)
        conn.connect(socket_path)
        conn.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                raise ConnectionError("the keeper closed the connection before answering")
            buf += chunk
    finally:
        conn.close()
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))


def request_raw(socket_path: str, line: bytes, timeout_sec: float = 30.0) -> dict[str, Any]:
    """Send an arbitrary line (tests: invalid JSON, key-shaped frames)."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(timeout_sec)
        conn.connect(socket_path)
        conn.sendall(line)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        conn.close()
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
