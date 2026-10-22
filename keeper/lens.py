"""SPEC D1 — the kami-lens client and the staleness gate.

Written against kami-lens **0.6.3 AND 1.0.0** (`0ffc8a7`), and it runs
unchanged against either: a 0.6.3 daemon serves no `appliedThrough`
anywhere and knows none of the 1.0.0 options, so every 1.0.0 feature is
gated on the version `status` reports (`LensFeatures`, `features_for`)
and the client is SILENT where the feature is off — a 0.6.3 request is
byte-identical to what it was before 1.0.0 existed.

Framing is a copy of the harness `_lens_request` (kami-harness 3.7.0,
executor/server.py): one JSON line `{id, query, args?: string[]}` over a
unix socket, one JSON line back `{id, ok, data, untrusted, meta} |
{id, ok: false, error: {code, message}}`. The keeper imports nothing from
the harness.

Typed errors:
  LensUnavailable   socket absent / refused / closed / timed out
  LensNotReady      the daemon answered NOT_READY (state != LIVE)
  LensQueryError    any other query error (BAD_ARGS, NOT_FOUND, ...)
  LensNotApplied    1.0.0: a read sent `--at-least <block>` and the mark
                    did not reach it in time (a LensQueryError subclass,
                    so every existing `except LensQueryError` catches it)
  LensIncomplete    1.0.0: the mirror cannot project this kami completely

The staleness gate reads the lens's own lower bound against the mirror
block and refuses / flags per SPEC P1 and P4. On 1.0.0 that bound
is `appliedThrough` — every write of every block up to it is APPLIED; on
0.6.3 only `reconciledThrough` is served and it stays the bound. An
unknown bound is unknown and fails closed.

`--at-least <block>` is the act→verify primitive (1.0.0 README item 4):
`freshness_view(bar)` returns a view whose every world read carries it, so
the tick after an act reads a mirror that includes the act's receipt block
or is refused `NOT_APPLIED`.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any

from .protocol import Basis
from . import status as flags

DEFAULT_TIMEOUT_SEC = 30

# The 1.0.0 options this client may send. Each takes a VALUE, and a 0.6.3
# daemon answers BAD_ARGS "unknown option '<opt>' for '<query>'" for every
# one of them — what the belt in `request` reads.
FEATURE_OPTIONS: tuple[str, ...] = ("--at-least", "--targets", "--account")
_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class LensUnavailable(RuntimeError):
    def __init__(self, reason: str, daemon_state: str = "unreachable"):
        self.daemon_state = daemon_state
        super().__init__(f"lens unavailable: {reason} (daemon state: {daemon_state})")


class LensNotReady(LensUnavailable):
    def __init__(self, message: str):
        super().__init__(message, daemon_state="not-live")


class LensQueryError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


class LensNotApplied(LensQueryError):
    """lens 1.0.0: the read carried `--at-least <block>` and the daemon's
    applied mark did not reach it within the wait, so it refused rather
    than answer from a mirror that does not include the block.
    `applied_through` is how far it got (`error.appliedThrough`), None when
    the payload did not say. This is the 3b outcome — "my own tx is not in
    the mirror yet" — not a lens fault."""

    def __init__(self, message: str, applied_through: int | None = None):
        self.applied_through = applied_through
        super().__init__("NOT_APPLIED", message)


class LensIncomplete(LensQueryError):
    """lens 1.0.0: a single-entity read (`kami`, or `node <n> <attacker>`
    whose ATTACKER is such a kami) of a kami the mirror cannot project
    completely. An unknown, never a reading."""

    def __init__(self, message: str):
        super().__init__("INCOMPLETE", message)


@dataclass
class LensFeatures:
    """What the daemon on the other end of the socket can do (1.0.0). All
    False is the 0.6.3 contract — and the default, so a client that has
    not read `status` yet sends nothing a 0.6.3 daemon would refuse."""

    version: str | None = None
    applied_through: bool = False  # `meta.appliedThrough` on every answer
    at_least: bool = False  # `--at-least <block> [--max-wait <ms>]`
    targets: bool = False  # `--targets <k,…>` / `--account <i>` on `node`


def features_for(version: str | None) -> LensFeatures:
    """The capability record for a `status.version` string: every flag True
    iff it parses as SemVer with MAJOR >= 1 (1.0.0 and up), else all False
    — 0.6.x, an unknown version, an unparsable one, or none read yet."""
    if not isinstance(version, str):
        return LensFeatures()
    v = version.strip()
    m = _SEMVER.match(v)
    if m is None or int(m.group(1)) < 1:
        return LensFeatures(version=v or None)
    return LensFeatures(version=v, applied_through=True, at_least=True, targets=True)


def _strip_options(args: list[Any] | None, options: tuple[str, ...] | list[str]) -> list[Any]:
    """`args` without each named option AND its value (every option in
    FEATURE_OPTIONS takes one)."""
    out: list[Any] = []
    skip = False
    for a in args or []:
        if skip:
            skip = False
            continue
        if str(a) in options:
            skip = True
            continue
        out.append(a)
    return out


@dataclass
class LensAnswer:
    data: Any
    meta: dict[str, Any]
    untrusted: list[str] = field(default_factory=list)
    # the answer's size on the wire (bytes of the JSON line), the
    # measured line SPEC D1 asks for; None on an answer built in tests
    size: int | None = None
    # True when a per-pass cache served this answer instead of
    # the daemon (SPEC P5 tick summary `lensCallsServedFromPass`)
    served_from_pass: bool = False

    @property
    def block(self) -> int | None:
        v = self.meta.get("blockNumber")
        return int(v) if isinstance(v, (int, float)) else None

    @property
    def reconciled_through(self) -> int | None:
        v = self.meta.get("reconciledThrough")
        return int(v) if isinstance(v, (int, float)) else None

    @property
    def applied_through(self) -> int | None:
        """1.0.0 `meta.appliedThrough`: every write of every block up to and
        including it is applied to the mirror — the number that answers "is
        my transaction reflected here?". None on 0.6.3, which serves it
        nowhere, and before the daemon is LIVE."""
        v = self.meta.get("appliedThrough")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    @property
    def incomplete_rows(self) -> int:
        """1.0.0 `meta.incompleteRows` — list rows the mirror could not
        project completely (identity + `state` + `incomplete: true`, no
        vitals). A number on `meta`; `status.incompleteRows` is an object,
        so a dict is read through its `total`."""
        v = self.meta.get("incompleteRows")
        if isinstance(v, dict):
            v = v.get("total")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    @property
    def stale(self) -> bool:
        return bool(self.meta.get("stale"))

    def basis(self) -> Basis:
        return Basis(lensBlock=self.block, lensReconciledThrough=self.reconciled_through, lensAppliedThrough=self.applied_through)


class LensClient:
    def __init__(self, socket_path: str, timeout_sec: float = DEFAULT_TIMEOUT_SEC):
        self.socket_path = socket_path
        self.timeout_sec = timeout_sec
        self._id = 0
        # measurement counters (SPEC D1): daemon round-trips
        # made, bytes received, answers served from a pass cache
        self.calls = 0
        self.bytes = 0
        self.served_from_pass = 0
        # what the daemon can do (1.0.0): all False until `note_status` has
        # read a version, so nothing 0.6.3 refuses is ever sent blind
        self.features = LensFeatures()

    def counters(self) -> dict[str, int]:
        return {"calls": self.calls, "bytes": self.bytes, "servedFromPass": self.served_from_pass}

    def note_status(self, data: Any) -> LensFeatures:
        """Set the capability record from a `status` answer's `version`
        (called by `perceive` before anything else reads the answer)."""
        version = data.get("version") if isinstance(data, dict) else None
        self.features = features_for(version if isinstance(version, str) else None)
        return self.features

    # -- argument builders (feature-gated, otherwise silent) ---------------

    def freshness_args(self, at_least: int | None) -> list[str]:
        """`--at-least <block>` when the daemon knows the option and a bar
        was given; nothing otherwise (0.6.3 reads are unchanged)."""
        if at_least is None or not self.features.at_least:
            return []
        return ["--at-least", str(int(at_least))]

    def _selector_args(self, targets: list[int] | None, account: int | None) -> list[Any]:
        """Lens 1.0.0 `--targets` / `--account` on `node`: selection BEFORE
        projection (~1 KB instead of ~1 MB on a crowded node). A list outside
        1..500 is NOT truncated — the selector is dropped and the full read
        made, because a truncated list would silently hide occupants."""
        if not self.features.targets:
            return []
        out: list[Any] = []
        if targets is not None:
            ks = sorted({int(t) for t in targets})
            if 1 <= len(ks) <= 500:
                out += ["--targets", ",".join(str(k) for k in ks)]
        if account is not None:
            out += ["--account", str(int(account))]
        return out

    def request(self, query: str, args: list[Any] | None = None) -> LensAnswer:
        """One round trip. The one belt for a wrong version string: a
        feature option this client sent that the daemon does not know
        (`BAD_ARGS` … `unknown option`) turns the flag off, is stripped
        with its value, and the read is retried ONCE (a second call)."""
        try:
            return self._request(query, args)
        except LensQueryError as e:
            if e.code != "BAD_ARGS" or "unknown option" not in str(e):
                raise
            sent = {str(a) for a in (args or [])}
            offenders = [o for o in FEATURE_OPTIONS if o in sent]
            if not offenders:
                raise
            for o in offenders:
                if o == "--at-least":
                    self.features.at_least = False
                else:
                    self.features.targets = False
            return self._request(query, _strip_options(args, offenders))

    def _request(self, query: str, args: list[Any] | None = None) -> LensAnswer:
        self._id += 1
        req: dict[str, Any] = {"id": self._id, "query": query}
        if args:
            req["args"] = [str(a) for a in args]
        payload = (json.dumps(req) + "\n").encode("utf-8")
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                conn.settimeout(self.timeout_sec)
                conn.connect(self.socket_path)
                conn.sendall(payload)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise LensUnavailable("the daemon closed the connection before answering")
                    buf += chunk
            finally:
                conn.close()
        except LensUnavailable:
            raise
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise LensUnavailable(f"cannot connect to the daemon socket: {type(e).__name__}")
        except (socket.timeout, TimeoutError):
            raise LensUnavailable(f"the daemon did not answer within {self.timeout_sec:g}s", daemon_state="unresponsive")
        except OSError as e:
            raise LensUnavailable(f"socket error: {type(e).__name__}")
        line = buf.split(b"\n", 1)[0]
        self.calls += 1
        self.bytes += len(line)
        resp = json.loads(line.decode("utf-8"))
        if not resp.get("ok"):
            err = resp.get("error") or {}
            code = str(err.get("code") or "INTERNAL")
            message = str(err.get("message") or "")
            if code == "NOT_READY":
                raise LensNotReady(message)
            if code == "NOT_FOUND" and "mirror not initialized" in message:
                raise LensUnavailable(message, daemon_state="starting")
            if code == "NOT_APPLIED":
                at = err.get("appliedThrough")
                raise LensNotApplied(message, int(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None)
            if code == "INCOMPLETE":
                raise LensIncomplete(message)
            raise LensQueryError(code, message)
        return LensAnswer(data=resp.get("data"), meta=resp.get("meta") or {}, untrusted=list(resp.get("untrusted") or []), size=len(line))

    # -- the reads the keeper uses (SPEC D1) -----------------------------

    def status(self) -> LensAnswer:
        """`status` never carries a freshness option (1.0.0: either option
        on `status` is a BAD_ARGS)."""
        return self.request("status")

    def node(self, index: int, attacker: int | None = None, with_vitals: bool = True, stats: bool = False, eligible_only: bool = False, full: bool = False, targets: list[int] | None = None, account: int | None = None, at_least: int | None = None) -> LensAnswer:
        args: list[Any] = [index]
        if attacker is not None:
            args.append(attacker)
        if with_vitals:
            args.append("--with-vitals")
        if stats:
            args.append("--stats")
        if eligible_only:
            args.append("--eligible-only")
        if full:
            args.append("--full")
        args += self._selector_args(targets, account)
        return self.request("node", args + self.freshness_args(at_least))

    def kami(self, index: int, stats: bool = False, at_least: int | None = None) -> LensAnswer:
        return self.request("kami", [index] + (["--stats"] if stats else []) + self.freshness_args(at_least))

    def party(self, account_index: int, full: bool = False, stats: bool = False, at_least: int | None = None) -> LensAnswer:
        return self.request("party", [account_index] + (["--full"] if full else []) + (["--stats"] if stats else []) + self.freshness_args(at_least))

    def account(self, key: int | str, slim: bool = False, at_least: int | None = None) -> LensAnswer:
        return self.request("account", [key] + (["--slim"] if slim else []) + self.freshness_args(at_least))

    def inventory(self, key: int | str, at_least: int | None = None) -> LensAnswer:
        return self.request("inventory", [key] + self.freshness_args(at_least))

    def battles(self, kami_index: int, at_least: int | None = None) -> LensAnswer:
        """`battles <kami>` (the harness `lens_battles`
        query): `{kami, stats {kills, deaths, pnl}, kills[] {timestamp,
        roomIndex, isDeath, killer {index}, victim {index}, account
        {index}, blockNumber, …}}`. Kamiden-sourced — enrichment only,
        never load-bearing (SPEC D1): the keeper reads it once per account
        to seed `killedAccounts`, and a refusal is a skip, never a gate."""
        return self.request("battles", [kami_index] + self.freshness_args(at_least))

    # -- the act->verify view (1.0.0) --------------------------------------

    def freshness_view(self, at_least: int | None) -> "LensClient":
        """A view of this client whose every world read carries
        `--at-least <at_least>`: `self` when there is no bar or the daemon
        does not know the option (0.6.3 — the reads are unchanged and the
        3b `reconciledThrough` check does the work instead)."""
        if at_least is None or not self.features.at_least:
            return self
        return FreshLens(self, int(at_least))


class PassLens(LensClient):
    """The per-pass read cache. Within ONE tick pass
    (the core's loop over due books) identical reads `(query, args)`
    are made once and reused; the cache is dropped with the pass. Three
    `auto` books on one node and account then cost one `node --full`,
    one `party --full` and one `inventory` per pass, not three of each.

    The cache is transparent to a strategy (it keeps calling
    `lens.node(...)`); the same typed error a request raised is raised
    again for the pass, so every book of the pass sees one world. The
    counters on the wrapped client move on daemon round-trips only;
    `served_from_pass` counts the reuses.

    1.0.0: the feature record is the INNER client's (one daemon, one
    capability set), and because a feature option is part of `args` the
    cache key already distinguishes a read taken with `--at-least` or
    `--targets` from one taken without — a fresh read is never served
    from a pre-act cache entry."""

    def __init__(self, inner: LensClient):
        super().__init__(inner.socket_path, inner.timeout_sec)
        self.inner = inner
        self._cache: dict[tuple[str, tuple[str, ...]], LensAnswer | BaseException] = {}

    @property  # type: ignore[override]
    def features(self) -> LensFeatures:
        return self.inner.features

    @features.setter
    def features(self, value: LensFeatures) -> None:
        # LensClient.__init__ sets this before `inner` exists; the default
        # record is the inner one's anyway, so that write is a no-op.
        if getattr(self, "inner", None) is not None:
            self.inner.features = value

    def counters(self) -> dict[str, int]:
        return {"calls": self.inner.calls, "bytes": self.inner.bytes, "servedFromPass": self.inner.served_from_pass}

    def note_status(self, data: Any) -> LensFeatures:
        return self.inner.note_status(data)

    def request(self, query: str, args: list[Any] | None = None) -> LensAnswer:
        key = (query, tuple(str(a) for a in (args or [])))
        hit = self._cache.get(key)
        if hit is not None:
            if isinstance(hit, BaseException):
                raise hit
            self.inner.served_from_pass += 1
            return LensAnswer(data=hit.data, meta=dict(hit.meta), untrusted=list(hit.untrusted), size=hit.size, served_from_pass=True)
        try:
            ans = self.inner.request(query, args)
        except (LensUnavailable, LensQueryError) as e:
            self._cache[key] = e
            raise
        self._cache[key] = ans
        return ans

    @property
    def cached(self) -> int:
        return len(self._cache)


class FreshLens(LensClient):
    """lens 1.0.0: a view that appends `--at-least <bar>` to every WORLD
    read made through it (never to `status`, where the option is a
    BAD_ARGS, and never twice). Built by `LensClient.freshness_view`, and
    only when the daemon knows the option — so on 0.6.3 it is never
    constructed at all.

    `inner` may be a `PassLens`: the option is part of the cache key, so a
    post-act read is never served from the pass's pre-act entry."""

    def __init__(self, inner: LensClient, at_least: int):
        super().__init__(inner.socket_path, inner.timeout_sec)
        self.inner = inner
        self.at_least = int(at_least)

    @property  # type: ignore[override]
    def features(self) -> LensFeatures:
        return self.inner.features

    @features.setter
    def features(self, value: LensFeatures) -> None:
        if getattr(self, "inner", None) is not None:
            self.inner.features = value

    def counters(self) -> dict[str, int]:
        return self.inner.counters()

    def note_status(self, data: Any) -> LensFeatures:
        return self.inner.note_status(data)

    def request(self, query: str, args: list[Any] | None = None) -> LensAnswer:
        if query == "status" or any(str(a) == "--at-least" for a in (args or [])):
            return self.inner.request(query, args)
        return self.inner.request(query, list(args or []) + self.freshness_args(self.at_least))


# ---- staleness gate ------------------------------------------------------


@dataclass
class Perception:
    """What one perception attempt established, before any decision."""

    basis: Basis
    degraded: list[str]
    lens_status: dict[str, Any] | None = None
    staleness_blocks: int | None = None
    # the lens's own gating `status.degraded` entries, verbatim (the flag
    # carries only their names; the tick summary carries this text)
    lens_degraded_text: list[str] = field(default_factory=list)
    # 1.0.0: the applied mark this perception rests on (None on 0.6.3) and
    # the daemon's own version string, both served on the status answer
    applied_through: int | None = None
    lens_version: str | None = None

    @property
    def can_act(self) -> bool:
        return not flags.gates_action(self.degraded)


def staleness_of(status_data: dict[str, Any], meta: dict[str, Any]) -> int | None:
    """Blocks the lens's own lower bound lags the mirror reference.

    The LOWER BOUND, first number of: `meta.appliedThrough`,
    `status.sync.appliedThrough`, `meta.reconciledThrough`,
    `status.sync.reconciledThrough`. Why in that order: 0.6.3 serves only
    `reconciledThrough`, so there it is the bound as before; on 1.0.0 the
    APPLIED mark is the freshness fact (every write of every block up to
    it is in the mirror) while `reconciledThrough` has become the slower
    chain-proof mark, which starts lower after a boot and lags up to one
    reconcile interval — reading that one as freshness would flag a
    healthy 1.0.0 daemon stale for minutes after every restart.

    The REFERENCE is `status.headBlockNumber` (blockLag is measured
    against liveBlockNumber), else the mirror block, else
    `liveBlockNumber` — the head fields can be ABSENT on 1.0.0 (the
    background sample is older than 60 s or has not been taken), which is
    the same handling an older daemon's missing head already got.

    None when no bound or no reference is known (the caller fails closed).
    """
    rt = meta.get("appliedThrough")
    if not isinstance(rt, (int, float)) or isinstance(rt, bool):
        rt = (status_data.get("sync") or {}).get("appliedThrough")
    if not isinstance(rt, (int, float)) or isinstance(rt, bool):
        rt = meta.get("reconciledThrough")
    if rt is None:
        rt = (status_data.get("sync") or {}).get("reconciledThrough")
    if not isinstance(rt, (int, float)):
        return None
    head = status_data.get("headBlockNumber")
    ref = head if isinstance(head, (int, float)) else meta.get("blockNumber", status_data.get("liveBlockNumber"))
    if not isinstance(ref, (int, float)):
        return None
    return max(0, int(ref) - int(rt))


def perceive(client: LensClient, max_staleness_blocks: int) -> Perception:
    """One `status` read -> basis + degraded flags. Fail-closed: any
    condition the keeper cannot rule out is a flag."""
    try:
        ans = client.status()
    except LensNotReady:
        return Perception(Basis(), [flags.lens_not_live()])
    except LensUnavailable:
        return Perception(Basis(), [flags.lens_unavailable()])
    data = ans.data if isinstance(ans.data, dict) else {}
    # FIRST: what this daemon can do, before any read of this pass is built
    features = client.note_status(data)
    degraded: list[str] = []
    if data.get("state") != "LIVE":
        degraded.append(flags.lens_not_live())
    st = staleness_of(data, ans.meta)
    if st is None:
        # unknown lower bound: treat as maximally stale (fail closed)
        degraded.append(flags.lens_stale(max_staleness_blocks + 1))
        st = max_staleness_blocks + 1
    elif st > max_staleness_blocks:
        degraded.append(flags.lens_stale(st))
    lens_text: list[str] = []
    for d in data.get("degraded") or []:
        # SPEC P4: the daemon's own `stream-stalled…` /
        # `unhealed-ranges…` mean the mirror is KNOWN incomplete — every
        # read of this tick is an unknown, carried as `lens-degraded:<name>`.
        # 1.0.0 adds `reconcile-stalled:<sec>`, which is deliberately NOT in
        # LENS_DEGRADED_GATING_PREFIXES: the chain RE-CHECK is behind, the
        # mirror is not known incomplete, so it gates no book.
        if isinstance(d, str) and d.startswith(flags.LENS_DEGRADED_GATING_PREFIXES):
            f = flags.lens_degraded(d)
            if f not in degraded:
                degraded.append(f)
            lens_text.append(d)
    if data.get("feedsDegraded"):
        degraded.append(flags.kamiden_unavailable())
    return Perception(ans.basis(), degraded, lens_status=data, staleness_blocks=st, lens_degraded_text=lens_text, applied_through=ans.applied_through, lens_version=features.version)
