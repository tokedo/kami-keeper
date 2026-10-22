"""SPEC P6 / P8 — the operator-key store adapter.

Backends, selected by `KAMI_KEEPER_SECRETS_BACKEND` (config
`secretsBackend`; default per profile row `key-backend-default`):

  env       a dotenv-style keys file (`KAMI_KEEPER_KEYS_FILE`; lab:
            /srv/keys/.env, a read-only mount). Re-read on EVERY grant —
            never cached at boot — because the lab harness writes the
            operator key mid-run (`create_operator_wallet`).
  gcp-sm    Google Secret Manager via the host's service account: secret
            name = the manifest name, project from config. Optional
            dependency (requirements-gcp.txt); the client is injectable.
  keychain  macOS login Keychain, generic-password `<prefix><NAME>` with
            the prefix from config `keychainServicePrefix` (default
            `kami-mcp/`, the kami-harness convention, so the keeper and the
            harness read the same items without a second copy). Dev
            only, never on a server host. The `security` runner is
            injectable.

Operator keys ONLY: `get_operator_key(label)` resolves `{LABEL}_OPERATOR_KEY`
and nothing else. A name that does not end in `_OPERATOR_KEY` is refused
before any store is consulted, so an `*_OWNER_KEY` sitting in the same file
is never read. The optional names manifest (`<keysfile>.secrets.names`,
one name per line) restricts which operator names may be granted; owner
names in it are ignored, not honoured.

A key value is held only inside `OperatorKey`, whose repr/str never show
it. It never reaches os.environ, argv, logs, status, the ledger or an
exception message (the ast-scan test and the runtime log-scrub test).
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

BACKENDS: tuple[str, ...] = ("env", "gcp-sm", "keychain")
OPERATOR_SUFFIX = "_OPERATOR_KEY"
OWNER_SUFFIX = "_OWNER_KEY"
KEYCHAIN_SERVICE_PREFIX = "kami-mcp/"  # default; config keychainServicePrefix overrides

_HEX32 = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class SecretError(RuntimeError):
    """Names only, never values."""


class MissingSecret(SecretError):
    def __init__(self, name: str, where: str):
        super().__init__(f"secret {name} not found in {where}")
        self.name = name


class NotAnOperatorKey(SecretError):
    def __init__(self, name: str):
        super().__init__(f"refusing to load {name}: only *_OPERATOR_KEY names are ever loaded")
        self.name = name


def operator_key_name(label: str) -> str:
    """`alice` -> `ALICE_OPERATOR_KEY`. Labels are [a-z0-9_-]; a dash
    becomes an underscore."""
    if not label or not re.match(r"^[A-Za-z0-9_-]+$", label):
        raise SecretError("account label must be [A-Za-z0-9_-]+")
    return label.upper().replace("-", "_") + OPERATOR_SUFFIX


def is_operator_name(name: str) -> bool:
    return name.endswith(OPERATOR_SUFFIX) and len(name) > len(OPERATOR_SUFFIX)


class OperatorKey:
    """A loaded operator key. The value is reachable only through
    `reveal_bytes()` (for the signer) and is absent from repr/str/dir-
    visible attributes."""

    __slots__ = ("name", "address", "_b")

    def __init__(self, name: str, raw: str):
        if not is_operator_name(name):
            raise NotAnOperatorKey(name)
        if not isinstance(raw, str) or not _HEX32.match(raw.strip()):
            # The message names the NAME, never the value.
            raise SecretError(f"secret {name} is not a 32-byte hex private key")
        self.name = name
        self._b = bytes.fromhex(raw.strip().removeprefix("0x"))
        self.address = _address_of(self._b)

    def reveal_bytes(self) -> bytes:
        return self._b

    def __repr__(self) -> str:
        return f"OperatorKey({self.name}, address={self.address})"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, OperatorKey) and other.name == self.name and other._b == self._b

    def __hash__(self) -> int:
        return hash((self.name, self.address))


def _address_of(key_bytes: bytes) -> str:
    """Public address of the key (public information). eth_account is a
    web3 dependency; imported lazily so the package imports without it."""
    from eth_account import Account  # type: ignore

    return Account.from_key(key_bytes).address


# ---- backends ---------------------------------------------------------------


def parse_env_file(path: Path) -> dict[str, str]:
    """dotenv-style: `NAME=value`, optional `export`, quotes, `# comment`."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export").strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].rstrip()
        out[name] = value
    return out


def default_manifest_path(keys_file: Path) -> Path:
    """`/srv/keys/.env` -> `/srv/keys/.secrets.names`; `x/hybrid.env` ->
    `x/hybrid.secrets.names` (the harness rule, mirrored)."""
    return keys_file.parent / (keys_file.name.removesuffix(".env") + ".secrets.names")


def read_manifest(path: Path) -> set[str] | None:
    """None when there is no manifest (every operator name is grantable);
    otherwise the OPERATOR names it lists. Owner names are dropped."""
    if not path.exists():
        return None
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and is_operator_name(line):
            names.add(line)
    return names


SecurityRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_security_runner(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=15)


@dataclass
class SecretStore:
    backend: str
    keys_file: Path
    manifest_path: Path | None = None
    gcp_project: str = ""
    gcp_client: Any = None  # injectable; None -> lazy google client
    security_runner: SecurityRunner | None = None  # injectable (tests)
    keychain_account: str = ""
    keychain_prefix: str = KEYCHAIN_SERVICE_PREFIX

    def __post_init__(self) -> None:
        backend = (self.backend or "").strip().lower()
        if backend not in BACKENDS:
            # A typo must not resolve to a backend by accident.
            raise SecretError(f"unknown secrets backend {backend!r}; expected one of {BACKENDS}")
        self.backend = backend
        self.keys_file = Path(self.keys_file).expanduser()
        if self.manifest_path is None:
            self.manifest_path = default_manifest_path(self.keys_file)

    # -- public ---------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Names and sources only."""
        return {
            "backend": self.backend,
            "keysFile": str(self.keys_file) if self.backend == "env" else None,
            "manifest": str(self.manifest_path),
            "manifestPresent": bool(self.manifest_path and self.manifest_path.exists()),
            "keychainServicePrefix": self.keychain_prefix if self.backend == "keychain" else None,
        }

    def grantable(self, name: str) -> bool:
        """Manifest rule: if a manifest exists, only its operator names."""
        if not is_operator_name(name):
            return False
        allowed = read_manifest(self.manifest_path) if self.manifest_path else None
        return True if allowed is None else name in allowed

    def get_operator_key(self, label: str) -> OperatorKey:
        """Load-on-grant: reads the store NOW, never a boot-time cache."""
        name = operator_key_name(label)
        if not is_operator_name(name):  # structurally impossible; belt and braces
            raise NotAnOperatorKey(name)
        if not self.grantable(name):
            raise MissingSecret(name, f"manifest {self.manifest_path}")
        raw = self._read(name)
        if raw is None or raw == "":
            raise MissingSecret(name, self._where())
        return OperatorKey(name, raw)

    # -- backends -------------------------------------------------------

    def _where(self) -> str:
        if self.backend == "env":
            return str(self.keys_file)
        if self.backend == "gcp-sm":
            return f"gcp-sm project {self.gcp_project or '<unset>'}"
        return f"macOS Keychain ({self.keychain_prefix}<NAME>)"

    def _read(self, name: str) -> str | None:
        if not is_operator_name(name):
            raise NotAnOperatorKey(name)
        if self.backend == "env":
            return parse_env_file(self.keys_file).get(name)
        if self.backend == "gcp-sm":
            return self._read_gcp(name)
        return self._read_keychain(name)

    def _read_gcp(self, name: str) -> str | None:
        client = self.gcp_client
        if client is None:
            try:
                from google.cloud import secretmanager  # type: ignore
            except ImportError:
                raise SecretError("gcp-sm backend needs google-cloud-secret-manager (requirements-gcp.txt)") from None
            client = secretmanager.SecretManagerServiceClient()
            self.gcp_client = client
        if not self.gcp_project:
            raise SecretError("gcp-sm backend needs gcpProject")
        path = f"projects/{self.gcp_project}/secrets/{name}/versions/latest"
        try:
            resp = client.access_secret_version(name=path)
        except Exception as e:  # noqa: BLE001 — the client's text may carry the path, never a value
            raise MissingSecret(name, f"gcp-sm ({type(e).__name__})") from None
        data = resp.payload.data
        return data.decode("utf-8").strip() if isinstance(data, (bytes, bytearray)) else str(data).strip()

    def _read_keychain(self, name: str) -> str | None:
        if sys.platform != "darwin" and self.security_runner is None:
            raise SecretError("keychain backend is macOS-only (dev)")
        runner = self.security_runner or _default_security_runner
        argv = ["security", "find-generic-password", "-s", self.keychain_prefix + name, "-w"]
        if self.keychain_account:
            argv[2:2] = ["-a", self.keychain_account]
        try:
            proc = runner(argv)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        value = proc.stdout.rstrip("\n")
        return value or None


def store_from_config(cfg: Mapping[str, Any] | Any, **overrides: Any) -> SecretStore:
    """Build the store from a KeeperConfig (or a mapping with the same keys)."""
    get = (lambda k: cfg[k]) if isinstance(cfg, Mapping) else (lambda k: getattr(cfg, k))
    kwargs: dict[str, Any] = dict(backend=get("secretsBackend"), keys_file=Path(get("keysFile")), gcp_project=get("gcpProject") or "")
    try:
        prefix = get("keychainServicePrefix")
    except (KeyError, AttributeError):
        prefix = None
    if prefix:
        kwargs["keychain_prefix"] = str(prefix)
    kwargs.update(overrides)
    return SecretStore(**kwargs)
