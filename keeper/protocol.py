"""SPEC P1 — the control protocol: command registry, error codes, meta,
and the ONE router both the socket and the CLI use.

Request:  {id, cmd, args}
Response: {id, ok: true, data, meta} | {id, ok: false, error: {code, message}, meta}

`meta` carries the same key set on every answer (asserted in both
directions by a test): keeperVersion, schemaVersion, servedAt, profile,
lensBlock, lensReconciledThrough, lensAppliedThrough. The THREE lens keys
are null on an answer that rests on no lens read — and
`lensAppliedThrough` is null on EVERY answer taken against a lens 0.6.3
daemon, which serves no applied mark at all; on 1.0.0 it is the mark that
says every write of every block up to it is in the mirror.

An undeclared argument is refused with BAD_ARGS here, so the CLI and the
socket refuse the same tokens with the same wording (the lens 0.5.2 rule).
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Callable

from . import KEEPER_VERSION, SCHEMA_VERSION
from .scrub import ScrubError, assert_clean, assert_no_key_shaped

# SPEC P1: closed set.
ERROR_CODES: tuple[str, ...] = (
    "NOT_READY",
    "NOT_FOUND",
    "BAD_ARGS",
    "NOT_GRANTED",
    "LENS_UNAVAILABLE",
    "LENS_STALE",
    "RPC_UNAVAILABLE",
    "SIGNER_BUSY",
    "BOOK_CAP",
    "PROFILE_FORBIDS",
    "PLAN_CONTRADICTED",  # `resume` on an external-change pause while the world still contradicts the plan
    "INTERNAL",
)

META_KEYS: frozenset[str] = frozenset(
    {"keeperVersion", "schemaVersion", "servedAt", "profile", "lensBlock", "lensReconciledThrough", "lensAppliedThrough"}
)

# SPEC P1: request timeout stated in `describe`.
REQUEST_TIMEOUT_SEC = 30

STRATEGY_TYPES: tuple[str, ...] = ("harvestAndFeed", "auto", "craft")


class KeeperError(Exception):
    """A protocol-level refusal. `code` is one of ERROR_CODES; `message` is
    agent-visible and therefore scrubbed at construction."""

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = assert_clean(message, f"error message ({code})")
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ArgSpec:
    name: str
    type: str  # "string" | "integer" | "boolean" | "object" | "array"
    required: bool = False
    description: str = ""
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True)
class CommandSpec:
    name: str
    description: str
    args: tuple[ArgSpec, ...] = ()
    lens_backed: bool = False  # answer may rest on a lens read (meta basis filled)

    def arg(self, name: str) -> ArgSpec | None:
        for a in self.args:
            if a.name == name:
                return a
        return None


_ACCOUNT = ArgSpec("account", "string", description="account label (the caller's account)")
_BOOK = ArgSpec("book", "string", description="book id")

# SPEC P1 command set. Descriptions are observation + action + parameters.
COMMANDS: dict[str, CommandSpec] = {
    c.name: c
    for c in (
        CommandSpec(
            "describe",
            "protocol description; with `type`, that strategy type's config JSON Schema and the description generated from it",
            (ArgSpec("type", "string", description="strategy type", enum=STRATEGY_TYPES),),
        ),
        CommandSpec(
            "books",
            "books held for the caller's account; `all` lists every account (profile-gated)",
            (_ACCOUNT, ArgSpec("all", "boolean", description="list every account's books")),
        ),
        CommandSpec(
            "start",
            "create and start a book: {account, strategyType, config}; adopts HARVESTING kamis on `auto`",
            (
                ArgSpec("account", "string", required=True, description="account label; must hold a grant"),
                ArgSpec("strategyType", "string", required=True, description="strategy type", enum=STRATEGY_TYPES),
                ArgSpec("config", "object", required=True, description="per-type config (describe <type>)"),
            ),
            lens_backed=True,
        ),
        CommandSpec(
            "stop",
            "stop one book (`book`) or every book of an account (`all` + `account`); idempotent; returns the stopped set and a quiescence proof",
            (
                _BOOK,
                _ACCOUNT,
                ArgSpec("all", "boolean", description="stop every book of `account`"),
                ArgSpec("afterBurst", "boolean", description="let the current burst finish first"),
            ),
        ),
        CommandSpec(
            "park",
            "park a running auto book: from the next tick it starts, feeds and collects nothing, stops its kamis HARVESTING on its node in packs of at most 10 (at most 5 packs a tick, consecutive nonces on the account's lane), keeps reviving, and stops itself once a valid read past its last stop shows none of them HARVESTING on the node and none DEAD; idempotent on a book already parking; BAD_ARGS on any other strategy type or a book that is not running",
            (ArgSpec("book", "string", required=True, description="book id"),),
        ),
        CommandSpec("pause", "pause a running book", (ArgSpec("book", "string", required=True, description="book id"),)),
        CommandSpec(
            "resume",
            "resume a paused book (refused against a stale or incomplete lens read; a book paused by external-change is re-evaluated against the world first and refused with PLAN_CONTRADICTED while the contradiction stands)",
            (ArgSpec("book", "string", required=True, description="book id"),),
            lens_backed=True,
        ),
        CommandSpec(
            "grant",
            "load the account's operator key from the keeper's own store; only the label crosses the socket",
            (ArgSpec("account", "string", required=True, description="account label"),),
        ),
        CommandSpec(
            "revoke",
            "stop every book of the account, drop its signing capability, return the quiescence proof",
            (ArgSpec("account", "string", required=True, description="account label"),),
        ),
        CommandSpec(
            "status",
            "keeper state: profile, engine, grants, degraded flags, and every book's status",
            (_BOOK,),
            lens_backed=True,
        ),
        CommandSpec(
            "logs",
            "formatted log lines, newest last (`book` narrows to one book; `tail` caps the count)",
            (_BOOK, ArgSpec("tail", "integer", description="max lines (default 200)")),
        ),
        CommandSpec(
            "ledger",
            "ledger rows of a book; `since` exports rows whose lens block is at or above it",
            (
                ArgSpec("book", "string", required=True, description="book id"),
                ArgSpec("since", "integer", description="lens block lower bound"),
                ArgSpec("kind", "string", description="row kind filter", enum=("tick", "decision", "tx", "death", "lifecycle", "kill", "deviation")),
            ),
        ),
    )
}

_TYPE_CHECK: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


def validate_request(cmd: Any, args: Any) -> dict[str, Any]:
    """Refuse an unknown command, an undeclared argument, a missing required
    argument, a mistyped argument, or a key-shaped value. Returns the
    validated args dict."""
    if not isinstance(cmd, str) or not cmd:
        raise KeeperError("BAD_ARGS", "request needs a cmd name")
    spec = COMMANDS.get(cmd)
    if spec is None:
        raise KeeperError("BAD_ARGS", f"unknown cmd {cmd!r} (have: {', '.join(COMMANDS)})")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise KeeperError("BAD_ARGS", "args must be an object")
    for name in args:
        if spec.arg(name) is None:
            declared = ", ".join(a.name for a in spec.args) or "none"
            raise KeeperError("BAD_ARGS", f"{cmd}: undeclared argument {name!r} (declared: {declared})")
    for a in spec.args:
        if a.required and name_missing(args, a.name):
            raise KeeperError("BAD_ARGS", f"{cmd}: missing required argument {a.name!r}")
        if a.name in args:
            v = args[a.name]
            if not _TYPE_CHECK[a.type](v):
                raise KeeperError("BAD_ARGS", f"{cmd}: argument {a.name!r} must be {a.type}")
            if a.enum is not None and v not in a.enum:
                raise KeeperError("BAD_ARGS", f"{cmd}: argument {a.name!r} must be one of {', '.join(a.enum)}")
    try:
        assert_no_key_shaped(args, "request")
    except ScrubError as e:
        raise KeeperError("BAD_ARGS", str(e)) from None
    return dict(args)


def name_missing(args: dict[str, Any], name: str) -> bool:
    return name not in args or args[name] is None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Basis:
    """The perception basis an answer rests on (SPEC P1 meta).

    The three lens block numbers, weakest claim first: `lensBlock`
    (`meta.blockNumber` — the highest block any applied write came from, a
    hint and no bound), `lensAppliedThrough` (1.0.0: every write of every
    block up to it is APPLIED — what answers "is my tx reflected?"; None
    on 0.6.3) and `lensReconciledThrough` (re-read from the chain under the
    proof — the strongest claim and the slowest mark)."""

    lensBlock: int | None = None
    lensReconciledThrough: int | None = None
    lensAppliedThrough: int | None = None


def build_meta(profile: str, basis: Basis | None = None) -> dict[str, Any]:
    b = basis or Basis()
    return {
        "keeperVersion": KEEPER_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "servedAt": _now_iso(),
        "profile": profile,
        "lensBlock": b.lensBlock,
        "lensReconciledThrough": b.lensReconciledThrough,
        "lensAppliedThrough": b.lensAppliedThrough,
    }


@dataclass
class Answer:
    """What a handler returns: data plus the basis it rests on."""

    data: Any
    basis: Basis = field(default_factory=Basis)


Handler = Callable[[dict[str, Any]], Answer]


class Router:
    """One router for both entry points. `handlers[cmd]` takes validated
    args and returns an Answer; every response is frame-scrubbed."""

    def __init__(self, profile: str, handlers: dict[str, Handler]):
        self.profile = profile
        self.handlers = handlers
        missing = set(COMMANDS) - set(handlers)
        if missing:
            raise ValueError(f"router is missing handlers for: {sorted(missing)}")

    def handle(self, request: Any) -> dict[str, Any]:
        rid = request.get("id") if isinstance(request, dict) else None
        try:
            if not isinstance(request, dict):
                raise KeeperError("BAD_ARGS", "request must be an object")
            cmd = request.get("cmd")
            args = validate_request(cmd, request.get("args"))
            answer = self.handlers[cmd](args)
            frame = {"id": rid, "ok": True, "data": answer.data, "meta": build_meta(self.profile, answer.basis)}
        except KeeperError as e:
            frame = {"id": rid, "ok": False, "error": {"code": e.code, "message": e.message}, "meta": build_meta(self.profile)}
        except Exception as e:  # noqa: BLE001 — the socket must always answer
            # Exception text is agent-visible: it is scrubbed, and a scrub
            # failure of the text itself falls back to the class name only.
            text = f"{type(e).__name__}: {e}"
            try:
                assert_clean(text, "internal error text")
                assert_no_key_shaped(text, "internal error text")
            except ScrubError:
                text = type(e).__name__
            frame = {"id": rid, "ok": False, "error": {"code": "INTERNAL", "message": text}, "meta": build_meta(self.profile)}
        # SPEC P1: no key-shaped value in any frame, either direction.
        try:
            assert_no_key_shaped(frame, "response")
        except ScrubError as e:
            frame = {"id": rid, "ok": False, "error": {"code": "INTERNAL", "message": str(e)}, "meta": build_meta(self.profile)}
        return frame


def describe_protocol(profile: str) -> dict[str, Any]:
    """The `describe` answer with no type: commands, args, error codes, meta
    keys, timeout, strategy types. Generated from the registry."""
    from .profiles import registry_table

    return {
        "keeperVersion": KEEPER_VERSION,
        "schemaVersion": SCHEMA_VERSION,
        "profile": profile,
        "requestTimeoutSec": REQUEST_TIMEOUT_SEC,
        "framing": "unix socket; JSON-lines; one request, one response per connection",
        "request": {"id": "any", "cmd": "string", "args": "object"},
        "response": {"ok": {"id": "any", "ok": True, "data": "any", "meta": "object"}, "error": {"id": "any", "ok": False, "error": {"code": "string", "message": "string"}, "meta": "object"}},
        "metaKeys": sorted(META_KEYS),
        "errorCodes": list(ERROR_CODES),
        "strategyTypes": list(STRATEGY_TYPES),
        "commands": [
            {
                "name": c.name,
                "description": c.description,
                "args": [
                    {"name": a.name, "type": a.type, "required": a.required, "description": a.description, **({"enum": list(a.enum)} if a.enum else {})}
                    for a in c.args
                ],
            }
            for c in COMMANDS.values()
        ],
        "profiles": registry_table(),
    }
