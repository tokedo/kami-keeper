"""The CLI: `python -m keeper <cmd> ...` is a socket client built from the
SAME command registry the socket serves, so an undeclared `--flag` is
refused with the router's BAD_ARGS wording before anything is dialled.
`python -m keeper daemon ...` starts the keeper process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import KEEPER_VERSION, SCHEMA_VERSION
from .protocol import COMMANDS, KeeperError, build_meta, validate_request
from .profiles import PROFILES

DEFAULT_SOCKET = "/srv/keeper/keeper.sock"

EXIT_OK = 0
EXIT_ERROR_FRAME = 1
EXIT_BAD_ARGS = 2
EXIT_NO_DAEMON = 3


def _add_daemon_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("daemon", help="run the keeper process (serves the control socket)")
    p.add_argument("--profile", choices=PROFILES, default=None, help="deployment profile (default: lab)")
    p.add_argument("--data-dir", dest="dataDir", default=None, help="data directory (books, ledger, socket)")
    p.add_argument("--socket", dest="socketPath", default=None, help="control socket path (default <dataDir>/keeper.sock)")
    p.add_argument("--lens-socket", dest="lensSocket", default=None, help="kami-lens daemon socket")
    p.add_argument("--keys-file", dest="keysFile", default=None, help="operator keys file (env backend)")
    p.add_argument("--secrets-backend", dest="secretsBackend", choices=("env", "gcp-sm", "keychain"), default=None)
    p.add_argument("--gcp-project", dest="gcpProject", default=None)
    p.add_argument("--max-books-per-account", dest="maxBooksPerAccount", type=int, default=None, help="book cap per account (REQUIRED under lab; 0 = unlimited under fleet)")
    p.add_argument("--armed", dest="armed", action="store_const", const=True, default=None, help="sign transactions (default: dry-run)")
    p.add_argument("--rpc-url", dest="rpcUrl", default=None)
    p.add_argument("--ws-rpc-url", dest="wsRpcUrl", default=None)
    p.add_argument("--world-address", dest="worldAddress", default=None)
    p.add_argument("--chain-id", dest="chainId", type=int, default=None)
    p.add_argument("--config", dest="configFile", default=None, help="YAML config file (flag > env > file > default)")
    p.add_argument("--no-engine", dest="noEngine", action="store_true", help="serve the socket without the tick loop (tests)")


def _add_command_parsers(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    for c in COMMANDS.values():
        p = sub.add_parser(c.name, help=c.description, add_help=True)
        p.add_argument("--socket", dest="_socket", default=None, help=f"keeper socket (default {DEFAULT_SOCKET})")
        for a in c.args:
            flag = "--" + a.name
            if a.type == "boolean":
                p.add_argument(flag, dest=a.name, action="store_const", const=True, default=None, help=a.description)
            elif a.type == "integer":
                p.add_argument(flag, dest=a.name, type=int, default=None, help=a.description)
            elif a.type in ("object", "array"):
                p.add_argument(flag, dest=a.name, default=None, help=a.description + " (JSON, or @file)")
            else:
                p.add_argument(flag, dest=a.name, default=None, help=a.description)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keeper", description=f"kami-keeper {KEEPER_VERSION} (schema {SCHEMA_VERSION}) — control CLI and daemon")
    parser.add_argument("--version", action="version", version=f"kami-keeper {KEEPER_VERSION} schema {SCHEMA_VERSION}")
    sub = parser.add_subparsers(dest="cmd", metavar="<cmd>")
    _add_daemon_parser(sub)
    _add_command_parsers(sub)
    return parser


def _json_arg(name: str, raw: str) -> Any:
    text = raw
    if raw.startswith("@"):
        text = Path(raw[1:]).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except ValueError:
        raise KeeperError("BAD_ARGS", f"argument {name!r} must be JSON") from None


def build_request(cmd: str, ns: argparse.Namespace, extras: Sequence[str]) -> dict[str, Any]:
    """Assemble and VALIDATE the request through the router's own rule."""
    spec = COMMANDS[cmd]
    args: dict[str, Any] = {}
    for a in spec.args:
        v = getattr(ns, a.name, None)
        if v is None:
            continue
        if a.type in ("object", "array") and isinstance(v, str):
            v = _json_arg(a.name, v)
        args[a.name] = v
    for tok in extras:
        if tok.startswith("--"):
            args[tok[2:].split("=", 1)[0]] = True  # undeclared: the router names it
            break
    else:
        if extras:
            raise KeeperError("BAD_ARGS", f"{cmd}: unexpected positional argument")
    validate_request(cmd, args)
    return {"id": 1, "cmd": cmd, "args": args}


def _error_frame(e: KeeperError) -> dict[str, Any]:
    return {"id": 1, "ok": False, "error": {"code": e.code, "message": e.message}, "meta": build_meta("unknown")}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    ns, extras = parser.parse_known_args(list(sys.argv[1:] if argv is None else argv))
    if ns.cmd is None:
        parser.print_help()
        return EXIT_BAD_ARGS
    if ns.cmd == "daemon":
        if extras:
            parser.error(f"unrecognized arguments: {' '.join(extras)}")
        from .core import run_daemon

        return run_daemon(vars(ns))
    try:
        req = build_request(ns.cmd, ns, extras)
    except KeeperError as e:
        print(json.dumps(_error_frame(e)))
        return EXIT_BAD_ARGS
    from .server import request

    sock = ns._socket or DEFAULT_SOCKET
    try:
        frame = request(sock, req["cmd"], req["args"], rid=req["id"])
    except (OSError, ConnectionError) as e:
        print(json.dumps({"id": 1, "ok": False, "error": {"code": "NOT_READY", "message": f"no keeper at {sock}: {type(e).__name__}"}, "meta": build_meta("unknown")}))
        return EXIT_NO_DAEMON
    print(json.dumps(frame, indent=1, sort_keys=True))
    return EXIT_OK if frame.get("ok") else EXIT_ERROR_FRAME
