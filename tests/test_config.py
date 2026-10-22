"""Config provenance flag > env > file > default, like the lens."""

from __future__ import annotations

import pytest

from keeper.config import ConfigError, load


def test_precedence_and_sources(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text("profile: fleet\nmaxBooksPerAccount: 3\nrpcUrl: file-url\n")
    cfg = load(flags={"maxBooksPerAccount": 7}, env={"KAMI_KEEPER_RPC_URL": "env-url"}, config_file=str(f))
    assert cfg.profile == "fleet" and cfg.sources["profile"] == "file"
    assert cfg.maxBooksPerAccount == 7 and cfg.sources["maxBooksPerAccount"] == "flag"
    assert cfg.rpcUrl == "env-url" and cfg.sources["rpcUrl"] == "env"
    assert cfg.secretsBackend == "gcp-sm" and cfg.sources["secretsBackend"] == "default"
    assert cfg.socketPath.endswith("/keeper.sock")


def test_lab_default_backend_and_cap_rule():
    cfg = load(flags={}, env={})
    assert cfg.profile == "lab" and cfg.secretsBackend == "env"
    assert any("maxBooksPerAccount" in e for e in cfg.errors)
    ok = load(flags={"maxBooksPerAccount": 2}, env={})
    assert ok.errors == []


def test_unknown_file_key_and_bad_types_refused(tmp_path):
    f = tmp_path / "k.yaml"
    f.write_text("nope: 1\n")
    with pytest.raises(ConfigError):
        load(flags={}, env={}, config_file=str(f))
    with pytest.raises(ConfigError):
        load(flags={}, env={"KAMI_KEEPER_MAX_BOOKS_PER_ACCOUNT": "many"})
    with pytest.raises(ConfigError):
        load(flags={"profile": "prod"}, env={})


def test_no_config_key_is_a_secret():
    from keeper.config import _KEYS
    for name, env_name, _, _ in _KEYS:
        assert not name.lower().endswith("key") and not env_name.endswith("_KEY")
