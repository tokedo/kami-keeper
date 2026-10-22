"""Keeper configuration: flag > env > file > default, with per-key
provenance reported like the lens `configSources` block.

Env names are `KAMI_KEEPER_*`. The keys file path is named ONCE,
`KAMI_KEEPER_KEYS_FILE` (SPEC P6); the secrets backend by
`KAMI_KEEPER_SECRETS_BACKEND`; the socket by `KAMI_KEEPER_SOCKET`.

No key VALUE is a config key: `grants` under fleet is a list of LABELS.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

import yaml

from . import profiles

# (config key, env name, default, type). `None` default = derived below.
_KEYS: tuple[tuple[str, str, Any, str], ...] = (
    ("profile", "KAMI_KEEPER_PROFILE", profiles.DEFAULT_PROFILE, "string"),
    ("dataDir", "KAMI_KEEPER_DATA_DIR", "/srv/keeper", "string"),
    ("socketPath", "KAMI_KEEPER_SOCKET", None, "string"),  # <dataDir>/keeper.sock
    ("lensSocket", "KAMI_KEEPER_LENS_SOCKET", "/srv/lens/kami-lens.sock", "string"),
    ("keysFile", "KAMI_KEEPER_KEYS_FILE", "/srv/keys/.env", "string"),
    ("secretsBackend", "KAMI_KEEPER_SECRETS_BACKEND", None, "string"),  # profile row
    ("gcpProject", "KAMI_KEEPER_GCP_PROJECT", "", "string"),
    ("keychainServicePrefix", "KAMI_KEEPER_KEYCHAIN_SERVICE_PREFIX", "kami-mcp/", "string"),  # keychain backend (dev) only
    ("rpcUrl", "KAMI_KEEPER_RPC_URL", "", "string"),
    ("wsRpcUrl", "KAMI_KEEPER_WS_RPC_URL", "", "string"),
    ("worldAddress", "KAMI_KEEPER_WORLD_ADDRESS", "", "string"),
    ("chainId", "KAMI_KEEPER_CHAIN_ID", 0, "integer"),
    ("maxBooksPerAccount", "KAMI_KEEPER_MAX_BOOKS_PER_ACCOUNT", None, "integer"),  # lab: REQUIRED
    ("armed", "KAMI_KEEPER_ARMED", False, "boolean"),
    ("grants", "KAMI_KEEPER_GRANTS", [], "list"),  # fleet only: labels
    ("statusPage", "KAMI_KEEPER_STATUS_PAGE", "", "string"),  # fleet only: loopback host:port
    ("friendsFiles", "KAMI_KEEPER_FRIENDS_FILES", [], "list"),  # fleet only
    ("standingOwners", "KAMI_KEEPER_STANDING_OWNERS", [], "list"),  # account indices whose attackers always bind
    ("requestTimeoutSec", "KAMI_KEEPER_REQUEST_TIMEOUT_SEC", 30, "integer"),
    # fleet only: books declared at deploy — [{account, strategyType, config}], file only
    ("books", "", [], "objects"),
)


class ConfigError(ValueError):
    """A configuration refusal. The message names the KEY, never a value
    that could be secret (no config key holds a secret)."""


@dataclass
class KeeperConfig:
    profile: str = profiles.DEFAULT_PROFILE
    dataDir: str = "/srv/keeper"
    socketPath: str = ""
    lensSocket: str = "/srv/lens/kami-lens.sock"
    keysFile: str = "/srv/keys/.env"
    secretsBackend: str = "env"
    gcpProject: str = ""
    keychainServicePrefix: str = "kami-mcp/"
    rpcUrl: str = ""
    wsRpcUrl: str = ""
    worldAddress: str = ""
    chainId: int = 0
    maxBooksPerAccount: int | None = None
    armed: bool = False
    grants: list[str] = field(default_factory=list)
    statusPage: str = ""
    friendsFiles: list[str] = field(default_factory=list)
    # the operator's standing list — account INDICES (never names)
    # whose attackers are classed `standing`: previewed, bound with
    # `safetyMarginPct`, arrivals held, remembered on departure for
    # `hostileWindowHours`. Keeper-level, because a live book is matched to
    # its declaration by (type, account, kami set, node): a book key would
    # reach a running book only through a new book id and a new ledger.
    standingOwners: list[int] = field(default_factory=list)
    requestTimeoutSec: int = 30
    books: list[dict[str, Any]] = field(default_factory=list)
    sources: dict[str, str] = field(default_factory=dict)
    configFile: str | None = None
    # Problems found at load that do not stop the process from SERVING
    # (describe/status) but do stop the engine (SPEC P2 reading, see
    # core.Keeper): each names the key.
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in ("sources", "configFile", "errors")}
        return d


def _coerce(name: str, raw: Any, typ: str) -> Any:
    if typ == "integer":
        if isinstance(raw, bool):
            raise ConfigError(f"{name}: expected an integer")
        if isinstance(raw, int):
            return raw
        try:
            return int(str(raw).strip())
        except ValueError:
            raise ConfigError(f"{name}: expected an integer") from None
    if typ == "boolean":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off", ""):
            return False
        raise ConfigError(f"{name}: expected a boolean")
    if typ == "list":
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    if typ == "objects":
        if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
            raise ConfigError(f"{name}: expected a list of mappings")
        return [dict(x) for x in raw]
    return str(raw)


def load(
    flags: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    config_file: str | os.PathLike[str] | None = None,
) -> KeeperConfig:
    """Resolve every key with provenance. `env` defaults to os.environ (the
    ONLY place the package reads the environment besides secrets.py)."""
    flags = dict(flags or {})
    env = os.environ if env is None else env
    file_values: dict[str, Any] = {}
    cfg_path = config_file or env.get("KAMI_KEEPER_CONFIG")
    if cfg_path:
        p = Path(cfg_path)
        if p.exists():
            loaded = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ConfigError("config file: top level must be a mapping")
            file_values = loaded
        else:
            raise ConfigError(f"config file not found: {p}")

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name, env_name, default, typ in _KEYS:
        if name in flags and flags[name] is not None:
            values[name], sources[name] = _coerce(name, flags[name], typ), "flag"
        elif env_name and env_name in env and env[env_name] != "":
            values[name], sources[name] = _coerce(name, env[env_name], typ), "env"
        elif name in file_values and file_values[name] is not None:
            values[name], sources[name] = _coerce(name, file_values[name], typ), "file"
        else:
            values[name], sources[name] = default, "default"
    unknown = sorted(set(file_values) - {k[0] for k in _KEYS})
    if unknown:
        raise ConfigError(f"config file: unknown key(s) {unknown}")

    try:
        profile = profiles.validate_profile(values["profile"])
    except ValueError as e:
        raise ConfigError(str(e)) from None
    values["profile"] = profile
    if values["socketPath"] is None:
        values["socketPath"] = str(Path(values["dataDir"]) / "keeper.sock")
        sources["socketPath"] = "default"
    if values["secretsBackend"] is None:
        values["secretsBackend"] = profiles.value("key-backend-default", profile)
        sources["secretsBackend"] = "default"

    # the "list" kind produces strings — the standing list is account
    # INDICES, so it is converted here and the dataclass holds ints.
    # Duplicates collapse, order kept; an entry that is not a non-negative
    # integer is ONE error naming the key (no value: a config key holds no
    # secret, but the message names keys by house rule). No profile gate:
    # the key is allowed under `lab` and `fleet` alike (no registry row —
    # it is not a gated feature).
    values["standingOwners"], standing_error = _standing_owners(values.get("standingOwners") or [])

    cfg = KeeperConfig(**values, sources=sources, configFile=str(cfg_path) if cfg_path else None)
    cfg.errors = validate(cfg)
    if standing_error:
        cfg.errors.append(standing_error)
    return cfg


def _standing_owners(raw: Any) -> tuple[list[int], str | None]:
    """(the account indices, the one error or None). An empty list is the
    default and means "no standing owner"."""
    out: list[int] = []
    bad = False
    for x in raw if isinstance(raw, list) else [raw]:
        try:
            n = int(str(x).strip())
        except (TypeError, ValueError):
            bad = True
            continue
        if n < 0:
            bad = True
            continue
        if n not in out:
            out.append(n)
    return out, ("standingOwners: expected account indices (non-negative integers)" if bad else None)


def validate(cfg: KeeperConfig) -> list[str]:
    """Profile-gated refusals (SPEC P10 registry) and the lab cap rule
    (SPEC P2). Returns messages naming the key; the caller decides
    what to do with them (core.Keeper refuses the engine)."""
    errs: list[str] = []
    if cfg.maxBooksPerAccount is None and profiles.value("book-cap-required", cfg.profile):
        errs.append("maxBooksPerAccount is required under this profile and is absent; it never reads as 0 or unlimited by omission")
    if cfg.maxBooksPerAccount is not None and cfg.maxBooksPerAccount < 0:
        errs.append("maxBooksPerAccount must be >= 0")
    if cfg.grants and not profiles.allows("grant-in-config", cfg.profile):
        errs.append("grants: declared in config, which this profile forbids (grant over the socket)")
    if cfg.statusPage and not profiles.allows("loopback-status-page", cfg.profile):
        errs.append("statusPage: this profile forbids the loopback status page")
    if cfg.friendsFiles and not profiles.allows("friends-files", cfg.profile):
        errs.append("friendsFiles: this profile forbids roster files")
    if cfg.requestTimeoutSec <= 0:
        errs.append("requestTimeoutSec must be > 0")
    if cfg.books and not profiles.allows("books-in-config", cfg.profile):
        errs.append("books: declared in config, which this profile forbids (start over the socket)")
    if cfg.armed and not (cfg.rpcUrl and cfg.worldAddress and cfg.chainId):
        errs.append("signing requested, but rpcUrl / worldAddress / chainId are not all set; nothing runs until they are")
    return errs
