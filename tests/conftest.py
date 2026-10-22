"""Shared fixtures. Every test is chain-free: the lens is tests/fakelens.py,
the signer is DryRunSigner, the keys are fixture files under tmp_path."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fakelens import FakeLens  # noqa: E402
from keeper.config import load as load_config  # noqa: E402
from keeper.core import Keeper  # noqa: E402
from keeper.lens import LensClient  # noqa: E402

# Public, well-known TEST keys (Hardhat accounts #0 and #1). Never real.
FIXTURE_OPERATOR_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
FIXTURE_OPERATOR_ADDRESS = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
FIXTURE_OWNER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
FIXTURE_KEY_HEX = FIXTURE_OPERATOR_KEY[2:]

HAF_CONFIG: dict[str, Any] = {"kamiIndex": 1001, "nodeIndex": 62, "foodItem": 11301, "feedBelowHp": 100}
CRAFT_CONFIG: dict[str, Any] = {"recipes": [{"recipeIndex": 23, "amount": 2, "priority": 1}]}


def write_keys_file(path: Path, operator: str = FIXTURE_OPERATOR_KEY, owner: str | None = FIXTURE_OWNER_KEY, label: str = "LAB") -> Path:
    lines = [f"{label}_OPERATOR_KEY={operator}"]
    if owner:
        lines.append(f"{label}_OWNER_KEY={owner}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@dataclass
class Env:
    keeper: Keeper
    lens: FakeLens
    data_dir: Path
    keys_file: Path

    def grant(self, label: str = "lab") -> dict[str, Any]:
        return self.keeper.handle("grant", {"account": label})

    def start_haf(self, account: str = "lab", **over: Any) -> dict[str, Any]:
        cfg = dict(HAF_CONFIG, **over)
        return self.keeper.handle("start", {"account": account, "strategyType": "harvestAndFeed", "config": cfg})


@pytest.fixture
def sock_dir():
    """AF_UNIX paths are capped at 104 bytes on macOS and pytest's tmp_path
    is longer, so sockets live in a short temp dir (data dirs stay in
    tmp_path). Override with KEEPER_TEST_SOCK_DIR."""
    d = tempfile.mkdtemp(prefix="kk-", dir=os.environ.get("KEEPER_TEST_SOCK_DIR", "/tmp"))
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def make_keeper(tmp_path: Path, sock_dir: Path):
    envs: list[Env] = []

    def _make(profile: str = "lab", cap: int | None = 4, lens_mode: str = "live", lag: int = 0, armed: bool = False, grants: list[str] | None = None, flags: dict[str, Any] | None = None, env: dict[str, str] | None = None, config_file: str | None = None, keys_file: Path | None = None, lens_version: str = "0.6.3") -> Env:
        n = len(envs)
        data_dir = tmp_path / f"keeper{n}"
        data_dir.mkdir()
        lens_sock = str(sock_dir / f"lens{n}.sock")
        # `lens_version` (SPEC D1 LENS_TESTED_VERSIONS): the default is the
        # 0.6.3 contract; a test that wants the 1.0.0 contract asks
        # for it here, or flips `e.lens.lens_version` on a built env
        lens = FakeLens(lens_sock, mode=lens_mode, lag=lag, lens_version=lens_version).start()
        kf = keys_file or write_keys_file(tmp_path / f"keys{n}.env")
        f: dict[str, Any] = {"profile": profile, "dataDir": str(data_dir), "lensSocket": lens_sock, "keysFile": str(kf), "secretsBackend": "env", "maxBooksPerAccount": cap, "armed": armed or None, "requestTimeoutSec": 5}
        if grants is not None:
            f["grants"] = grants
        if flags:
            f.update(flags)
        cfg = load_config(flags=f, env=env or {}, config_file=config_file)
        keeper = Keeper(cfg, lens=LensClient(lens_sock, 5))
        keeper.boot()
        e = Env(keeper=keeper, lens=lens, data_dir=data_dir, keys_file=kf)
        envs.append(e)
        return e

    yield _make
    for e in envs:
        e.keeper.stop_engine()
        e.lens.stop()


@pytest.fixture
def env(make_keeper) -> Env:
    return make_keeper()
