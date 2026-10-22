"""SPEC P2 / P3 — books: schema, state machine, persistence.

A book = {id, account, strategyType, config, state, createdAt[, reason]}.
Config is validated against keeper/schemas/<type>.json on `start`, with
schema defaults applied so the stored config is complete. Persistence is
`<dataDir>/books/<id>/book.json`; the ledger lives beside it.

State machine (SPEC P3): created -> running -> paused | stopped | failed;
paused -> running | stopped. Terminal states carry an enumerated reason.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import secrets as _secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema

from . import profiles
from .protocol import STRATEGY_TYPES

SCHEMA_DIR = Path(__file__).with_name("schemas")

STATES: tuple[str, ...] = ("created", "running", "paused", "stopped", "failed")
TERMINAL_STATES: tuple[str, ...] = ("stopped", "failed")
STOPPED_BY: tuple[str, ...] = ("agent", "operator", "keeper")
FAILED_BY: tuple[str, ...] = ("signer", "lens", "rpc", "config")

TRANSITIONS: dict[tuple[str, str], str] = {
    ("created", "running"): "started",
    ("running", "paused"): "paused",
    ("paused", "running"): "resumed",
    ("running", "stopped"): "stopped_by",
    ("paused", "stopped"): "stopped_by",
    ("created", "stopped"): "stopped_by",
    ("running", "failed"): "failed",
    ("paused", "failed"): "failed",
}

# Keys every type's schema carries with identical definitions (asserted by
# tests/test_protocol.py). `readoptAfterBlocks` is fleet-only (x-profile)
# on all three.
_COMMON_KEYS = ("tickSeconds", "tickHeartbeat", "maxStalenessBlocks", "externalChangeTicks", "readoptAfterBlocks")

# Book pause reasons (SPEC P3): why a paused book paused.
PAUSED_REASONS: tuple[str, ...] = ("agent", "external-change", "kami-dead")


class BookError(ValueError):
    pass


class IllegalTransition(BookError):
    def __init__(self, book_id: str, src: str, dst: str):
        super().__init__(f"book {book_id}: no transition {src} -> {dst}")


def load_schema(strategy_type: str) -> dict[str, Any]:
    if strategy_type not in STRATEGY_TYPES:
        raise BookError(f"unknown strategyType {strategy_type!r}; expected one of {', '.join(STRATEGY_TYPES)}")
    return json.loads((SCHEMA_DIR / f"{strategy_type}.json").read_text(encoding="utf-8"))


def schema_defaults(strategy_type: str) -> dict[str, Any]:
    return {k: v["default"] for k, v in load_schema(strategy_type)["properties"].items() if "default" in v}


def all_defaults() -> dict[str, dict[str, Any]]:
    return {t: schema_defaults(t) for t in STRATEGY_TYPES}


def conditional_defaults(strategy_type: str) -> dict[str, list[dict[str, Any]]]:
    """The `x-defaultWhen` rules of a type, by key — a default
    that depends on another key of the same config. A rule is `{when: {key:
    value, …}, default: v}` (every named key equals the value once the
    plain defaults are in) or `{sameAs: key}`; the first rule that holds
    fills the key at `start`, else its plain `default` (when it has one).
    They are defaults: pinned in the lock and its SHA like the plain ones."""
    return {k: copy.deepcopy(v["x-defaultWhen"]) for k, v in load_schema(strategy_type)["properties"].items() if "x-defaultWhen" in v}


def all_conditional_defaults() -> dict[str, dict[str, list[dict[str, Any]]]]:
    return {t: conditional_defaults(t) for t in STRATEGY_TYPES}


def defaults_digest() -> str:
    """SHA-256 over the canonical JSON of every type's defaults (SPEC P2:
    defaults pinned by SHA; a change is MAJOR) — the plain defaults AND
    the conditional ones."""
    canon = json.dumps({"defaults": all_defaults(), "conditionalDefaults": all_conditional_defaults()}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _rule_value(rule: dict[str, Any], config: dict[str, Any]) -> tuple[bool, Any]:
    if "sameAs" in rule:
        return (rule["sameAs"] in config), copy.deepcopy(config.get(rule["sameAs"]))
    when = rule.get("when") or {}
    return all(k in config and config[k] == v for k, v in when.items()), copy.deepcopy(rule.get("default"))


def apply_defaults(strategy_type: str, config: dict[str, Any], profile: str | None = None) -> dict[str, Any]:
    """Schema defaults applied; with a `profile`, a profile-gated key
    (`x-profile`) the profile refuses is NOT defaulted in, so a lab book
    never carries a fleet-only key it could not have asked for."""
    out = copy.deepcopy(config)
    schema = load_schema(strategy_type)
    for k, v in schema["properties"].items():
        if "x-defaultWhen" in v:
            continue  # after the plain ones, below: a rule reads other keys
        if k not in out and "default" in v:
            gate = v.get("x-profile")
            if gate and profile is not None and not profiles.allows(k, profile):
                continue
            # `x-default-unless {present[], absent[]}` withholds a
            # default when the config AS GIVEN sets every `present` key and
            # none of the `absent` ones — a `collectEvery` set alone pins the
            # collect period, so the window defaults stay out of the stored
            # config (and a stored config without the window keys reads the same way)
            unless = v.get("x-default-unless")
            if unless and all(u in config for u in unless.get("present", [])) and not any(u in config for u in unless.get("absent", [])):
                continue
            out[k] = copy.deepcopy(v["default"])
        # nested defaults for craft recipes
        if k == "recipes" and isinstance(out.get(k), list):
            item_props = v.get("items", {}).get("properties", {})
            for r in out[k]:
                if isinstance(r, dict):
                    for rk, rv in item_props.items():
                        if rk not in r and "default" in rv:
                            r[rk] = rv["default"]
    # the conditional defaults, in schema order (a rule may read a
    # key an earlier rule filled: nodeCensus reads previewPolicy)
    for k, v in schema["properties"].items():
        if "x-defaultWhen" not in v or k in out:
            continue
        for rule in v["x-defaultWhen"]:
            holds, value = _rule_value(rule, out)
            if holds:
                out[k] = value
                break
        else:
            if "default" in v:
                out[k] = copy.deepcopy(v["default"])
    return out


def collect_window(config: dict[str, Any]) -> tuple[int, int]:
    """The `harvestAndFeed` collect window (min s, max s) a
    config means. `collectEvery` with NEITHER window key pins it (min = max
    = `collectEvery` — a stored book whose complete config carries
    `collectEvery` and no window key); a config that carries a
    window key ignores `collectEvery`, a side it lacks reading the schema
    default."""
    lo, hi = config.get("collectEveryMinSec"), config.get("collectEveryMaxSec")
    if lo is None and hi is None and config.get("collectEvery") is not None:
        return int(config["collectEvery"]), int(config["collectEvery"])
    if lo is None or hi is None:
        d = schema_defaults("harvestAndFeed")
        lo = d["collectEveryMinSec"] if lo is None else lo
        hi = d["collectEveryMaxSec"] if hi is None else hi
    return int(lo), int(hi)


def validate_config(strategy_type: str, config: Any, profile: str) -> dict[str, Any]:
    """JSON Schema validation + profile-gated keys. Returns the config with
    defaults applied. Raises BookError (schema) or ProfileForbids."""
    if not isinstance(config, dict):
        raise BookError("config must be an object")
    schema = load_schema(strategy_type)
    for k, v in schema["properties"].items():
        gate = v.get("x-profile")
        if gate and k in config and not profiles.allows(k, profile):
            raise ProfileForbids(f"config key {k!r} is not served under this profile")
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        where = "/".join(str(p) for p in e.path) or "<root>"
        raise BookError(f"config invalid at {where}: {e.message}")
    full = apply_defaults(strategy_type, config, profile)
    if strategy_type == "harvestAndFeed":
        lo, hi = collect_window(full)
        if lo > hi:
            raise BookError(f"config invalid at collectEveryMinSec: {lo} is above collectEveryMaxSec {hi}")
        if "foodLadder" in full:
            # ladder mode. The heal table lives with the feed
            # guard; importing it here at call time keeps books.py free of
            # the strategies package (which imports this module).
            from .strategies.base import food_heal

            for item in full["foodLadder"]:
                if food_heal(int(item)) is None:
                    raise BookError(f"config invalid at foodLadder: item {int(item)} has no known heal, so the book cannot size a feed with it")
            if full.get("accountIndex") is None:
                raise BookError("config invalid at accountIndex: foodLadder needs the account's on-chain index (the node and preview reads take it)")
            if int(full["foodItem"]) not in [int(i) for i in full["foodLadder"]]:
                raise BookError(f"config invalid at foodItem: {int(full['foodItem'])} is not one of the foodLadder rungs")
            if full.get("feedMode", "hp") != "hp":
                raise BookError(f"config invalid at feedMode: {full['feedMode']} with foodLadder; ladder mode computes the feed point from the ladder and the bound")
            if "feedBelowHp" in full:
                raise BookError("config invalid at feedBelowHp: foodLadder and feedBelowHp together; ladder mode computes the feed point itself")
        elif full.get("dipCapMode") == "measured":
            # the measured cap sizes a LADDER rung; without the
            # ladder there is no rung to size and no node read to size it from
            raise BookError("config invalid at dipCapMode: measured needs foodLadder (the measured cap sets the lowest floor a ladder rung may sit at, and a book without the ladder chooses no rung)")
    if strategy_type == "auto" and len(full["kamiIndices"]) > full["maxKamisPerBook"]:
        raise BookError(f"config invalid at kamiIndices: {len(full['kamiIndices'])} kamis exceed maxKamisPerBook {full['maxKamisPerBook']}")
    if strategy_type == "auto" or (strategy_type == "harvestAndFeed" and "foodLadder" in full):
        # an owner that binds on its threshold needs the threshold
        # read, and its cooldown from the node rows — the narrow reads are
        # served only where a killed-here attacker alone can bind (a
        # ladder book reads the node the same way and takes the same rule)
        if full.get("passiveOwnersBind", True):
            if full.get("previewPolicy") == "killed-here":
                raise BookError("config invalid at previewPolicy: killed-here needs passiveOwnersBind false (with passiveOwnersBind true an owner with a threshold and no kill binds, and its threshold is read from a preview)")
            if full.get("nodeCensus") == "plain":
                raise BookError("config invalid at nodeCensus: plain needs passiveOwnersBind false (with passiveOwnersBind true the cooldown of every attacker that binds is read from the node rows)")
    if strategy_type == "auto":
        if "nodeCensusEverySec" in full and int(full["nodeCensusEverySec"]) < int(full["tickSeconds"]):
            raise BookError(f"config invalid at nodeCensusEverySec: {full['nodeCensusEverySec']} is below tickSeconds {full['tickSeconds']}")
    return full


class ProfileForbids(BookError):
    pass


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_book_id(strategy_type: str) -> str:
    return f"{strategy_type}_{_secrets.token_hex(4)}"


@dataclass
class Book:
    id: str
    account: str
    strategyType: str
    config: dict[str, Any]
    state: str = "created"
    createdAt: str = field(default_factory=_now_iso)
    reason: dict[str, Any] | None = None
    # SPEC P7: what the book last asserted about the world, per kami:
    # {kamiIndex: {"state": ..., "node": ...}}; None before its first act.
    plan: dict[str, Any] = field(default_factory=dict)
    # external-change bookkeeping (SPEC P7): the lens block at which the
    # CONFIRMED contradiction paused the book, or None
    contradictedAt: int | None = None
    # SPEC P3: why and when a paused book paused; cleared by
    # any transition out of `paused`
    pausedReason: str | None = None
    pausedAt: str | None = None
    # SPEC P8: the parking mark of an `auto` book — {"since": ISO,
    # "by": "agent"} once `park` was asked, null otherwise. It is NOT a
    # state: the book stays `running` while it parks, no transition touches
    # the mark, it survives a restart, and it stays on the stopped book as
    # history of why the book stopped.
    parking: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "account": self.account,
            "strategyType": self.strategyType,
            "config": self.config,
            "state": self.state,
            "createdAt": self.createdAt,
            "reason": self.reason,
            "plan": self.plan,
            "contradictedAt": self.contradictedAt,
            "pausedReason": self.pausedReason,
            "pausedAt": self.pausedAt,
            "parking": self.parking,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Book":
        return cls(
            id=d["id"], account=d["account"], strategyType=d["strategyType"], config=d["config"],
            state=d.get("state", "created"), createdAt=d.get("createdAt", _now_iso()), reason=d.get("reason"),
            plan=d.get("plan") or {}, contradictedAt=d.get("contradictedAt"),
            pausedReason=d.get("pausedReason"), pausedAt=d.get("pausedAt"),
            parking=d.get("parking"),
        )

    # -- state machine ---------------------------------------------------

    def transition(self, dst: str, reason: dict[str, Any] | None = None, paused_reason: str | None = None) -> str:
        """Move to `dst`; returns the lifecycle event name. Terminal states
        require an enumerated reason; `paused` requires one of
        PAUSED_REASONS (recorded with the time)."""
        if dst not in STATES:
            raise BookError(f"unknown state {dst!r}")
        event = TRANSITIONS.get((self.state, dst))
        if event is None:
            raise IllegalTransition(self.id, self.state, dst)
        if dst == "stopped":
            if not reason or reason.get("stopped_by") not in STOPPED_BY:
                raise BookError(f"book {self.id}: stopped needs stopped_by in {STOPPED_BY}")
        if dst == "failed":
            if not reason or reason.get("failed") not in FAILED_BY:
                raise BookError(f"book {self.id}: failed needs failed in {FAILED_BY}")
        if dst == "paused":
            if paused_reason not in PAUSED_REASONS:
                raise BookError(f"book {self.id}: paused needs a reason in {PAUSED_REASONS}")
            self.pausedReason = paused_reason
            self.pausedAt = _now_iso()
        else:
            self.pausedReason = None
            self.pausedAt = None
        self.state = dst
        self.reason = reason if dst in TERMINAL_STATES else None
        return event

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class BookStore:
    """Persistence under <dataDir>/books/<id>/book.json."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir) / "books"
        self._lock = threading.RLock()
        self._books: dict[str, Book] = {}

    def book_dir(self, book_id: str) -> Path:
        return self.root / book_id

    def load_all(self) -> list[Book]:
        with self._lock:
            self._books.clear()
            if self.root.exists():
                for d in sorted(self.root.iterdir()):
                    p = d / "book.json"
                    if p.exists():
                        b = Book.from_dict(json.loads(p.read_text(encoding="utf-8")))
                        self._books[b.id] = b
            return list(self._books.values())

    def save(self, book: Book) -> None:
        with self._lock:
            d = self.book_dir(book.id)
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "book.json.tmp"
            tmp.write_text(json.dumps(book.to_dict(), indent=1, sort_keys=True), encoding="utf-8")
            tmp.replace(d / "book.json")
            self._books[book.id] = book

    def get(self, book_id: str) -> Book | None:
        with self._lock:
            return self._books.get(book_id)

    def all(self) -> list[Book]:
        with self._lock:
            return list(self._books.values())

    def for_account(self, account: str) -> list[Book]:
        return [b for b in self.all() if b.account == account]

    def count_active(self, account: str) -> int:
        return sum(1 for b in self.for_account(account) if not b.terminal)

    def purge(self, book_id: str) -> None:
        """Operator-only removal of a terminal book's record (SPEC P3:
        stopped books stay enumerable until purged)."""
        with self._lock:
            b = self._books.get(book_id)
            if b is None:
                return
            if not b.terminal:
                raise BookError(f"book {book_id} is {b.state}; only a terminal book can be purged")
            del self._books[book_id]
