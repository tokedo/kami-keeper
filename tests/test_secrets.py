"""SPEC P6 / P8 — operator keys only; load-on-grant; manifest; backends
selected by name; the value never in repr/str/exception text."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import FIXTURE_KEY_HEX, FIXTURE_OPERATOR_ADDRESS, FIXTURE_OPERATOR_KEY, FIXTURE_OWNER_KEY, write_keys_file
from keeper.secrets import MissingSecret, NotAnOperatorKey, OperatorKey, SecretError, SecretStore, operator_key_name


def test_operator_key_name_rule():
    assert operator_key_name("alice") == "ALICE_OPERATOR_KEY"
    assert operator_key_name("grace-grace") == "GRACE_GRACE_OPERATOR_KEY"
    with pytest.raises(SecretError):
        operator_key_name("bad label!")


def test_env_backend_loads_the_operator_key_and_derives_its_address(tmp_path):
    kf = write_keys_file(tmp_path / "keys.env")
    store = SecretStore("env", kf)
    k = store.get_operator_key("lab")
    assert k.name == "LAB_OPERATOR_KEY" and k.address == FIXTURE_OPERATOR_ADDRESS
    assert k.reveal_bytes() == bytes.fromhex(FIXTURE_KEY_HEX)


def test_owner_key_in_the_same_file_is_never_loaded(tmp_path):
    kf = write_keys_file(tmp_path / "keys.env")
    store = SecretStore("env", kf)
    with pytest.raises(NotAnOperatorKey):
        store._read("LAB_OWNER_KEY")
    with pytest.raises(NotAnOperatorKey):
        OperatorKey("LAB_OWNER_KEY", FIXTURE_OWNER_KEY)
    assert store.grantable("LAB_OWNER_KEY") is False
    # a manifest that lists the owner name does not make it loadable
    (tmp_path / "keys.secrets.names").write_text("LAB_OWNER_KEY\nLAB_OPERATOR_KEY\n")
    assert store.grantable("LAB_OWNER_KEY") is False and store.grantable("LAB_OPERATOR_KEY") is True


def test_manifest_restricts_grantable_operator_names(tmp_path):
    kf = write_keys_file(tmp_path / "keys.env")
    (tmp_path / "keys.secrets.names").write_text("OTHER_OPERATOR_KEY\n")
    store = SecretStore("env", kf)
    with pytest.raises(MissingSecret):
        store.get_operator_key("lab")


def test_load_on_grant_rereads_the_file_every_time(tmp_path):
    kf = write_keys_file(tmp_path / "keys.env", operator="0x" + "11" * 32)
    store = SecretStore("env", kf)
    first = store.get_operator_key("lab")
    write_keys_file(kf, operator=FIXTURE_OPERATOR_KEY)  # the harness wrote a new key mid-run
    second = store.get_operator_key("lab")
    assert first.address != second.address and second.address == FIXTURE_OPERATOR_ADDRESS


def test_missing_and_malformed_secrets_name_the_name_never_a_value(tmp_path):
    kf = tmp_path / "keys.env"
    kf.write_text("LAB_OPERATOR_KEY=notakey\n")
    store = SecretStore("env", kf)
    with pytest.raises(SecretError) as ei:
        store.get_operator_key("lab")
    assert "LAB_OPERATOR_KEY" in str(ei.value) and "notakey" not in str(ei.value)
    with pytest.raises(MissingSecret) as ei2:
        store.get_operator_key("nobody")
    assert "NOBODY_OPERATOR_KEY" in str(ei2.value)


def test_key_value_never_in_repr_str_or_hash(tmp_path):
    k = OperatorKey("LAB_OPERATOR_KEY", FIXTURE_OPERATOR_KEY)
    for text in (repr(k), str(k), f"{k}", repr(vars(SecretStore("env", tmp_path / "x")))):
        assert FIXTURE_KEY_HEX not in text
    assert not hasattr(k, "__dict__")


def test_unknown_backend_fails_loudly(tmp_path):
    with pytest.raises(SecretError):
        SecretStore("envfile", tmp_path / "keys.env")


def test_keychain_backend_with_injected_runner(tmp_path):
    calls = []

    def runner(argv):
        calls.append(argv)
        assert "-w" in argv and "kami-mcp/LAB_OPERATOR_KEY" in argv  # default prefix = the kami-harness item convention
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE_OPERATOR_KEY + "\n", stderr="")

    store = SecretStore("keychain", tmp_path / "keys.env", security_runner=runner)
    assert store.get_operator_key("lab").address == FIXTURE_OPERATOR_ADDRESS
    assert calls and all(FIXTURE_KEY_HEX not in " ".join(c) for c in calls)

    def missing(argv):
        return subprocess.CompletedProcess(argv, 44, stdout="", stderr="not found")

    with pytest.raises(MissingSecret):
        SecretStore("keychain", tmp_path / "keys.env", security_runner=missing).get_operator_key("lab")


def test_keychain_prefix_is_a_config_key(tmp_path):
    """`keychainServicePrefix` (default kami-mcp/)
    reaches the `security` lookup; the store never formats the value."""
    from keeper.config import load as load_config
    from keeper.secrets import store_from_config
    seen = []

    def runner(argv):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=FIXTURE_OPERATOR_KEY + "\n", stderr="")

    cfg = load_config(flags={"secretsBackend": "keychain", "maxBooksPerAccount": 1}, env={"KAMI_KEEPER_KEYCHAIN_SERVICE_PREFIX": "kami-dev/"})
    assert cfg.keychainServicePrefix == "kami-dev/" and cfg.sources["keychainServicePrefix"] == "env"
    store = store_from_config(cfg, security_runner=runner)
    assert store.get_operator_key("lab").address == FIXTURE_OPERATOR_ADDRESS
    assert "kami-dev/LAB_OPERATOR_KEY" in seen[0]
    assert store.describe()["keychainServicePrefix"] == "kami-dev/"
    default = store_from_config(load_config(flags={"secretsBackend": "keychain", "maxBooksPerAccount": 1}, env={}), security_runner=runner)
    assert default.keychain_prefix == "kami-mcp/"


def test_gcp_backend_with_injected_client(tmp_path):
    class Resp:
        class payload:  # noqa: N801
            data = FIXTURE_OPERATOR_KEY.encode()

    class Client:
        def __init__(self):
            self.names = []

        def access_secret_version(self, name):
            self.names.append(name)
            return Resp()

    c = Client()
    store = SecretStore("gcp-sm", tmp_path / "unused.env", gcp_project="proj", gcp_client=c)
    assert store.get_operator_key("lab").address == FIXTURE_OPERATOR_ADDRESS
    assert c.names == ["projects/proj/secrets/LAB_OPERATOR_KEY/versions/latest"]
    with pytest.raises(SecretError):
        SecretStore("gcp-sm", tmp_path / "unused.env", gcp_client=c).get_operator_key("lab")  # no project


def test_grant_over_the_router_returns_only_public_facts(env):
    g = env.grant()
    assert g["ok"], g
    assert g["data"]["grant"] == {"account": "lab", "operatorAddress": FIXTURE_OPERATOR_ADDRESS, "grantedAt": g["data"]["grant"]["grantedAt"]}
    st = env.keeper.handle("status")["data"]
    assert st["grants"][0]["operatorAddress"] == FIXTURE_OPERATOR_ADDRESS
    missing = env.keeper.handle("grant", {"account": "nobody"})
    assert missing["error"]["code"] == "NOT_FOUND" and "NOBODY_OPERATOR_KEY" in missing["error"]["message"]
