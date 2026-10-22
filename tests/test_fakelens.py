"""The fake lens invents values, never fields: every fixture answer is
valid against the copied kami-lens schema for its query — in BOTH the
versions the keeper is tested against (SPEC D1 `LENS_TESTED_VERSIONS`),
each against its own schema set: `0.6.1/` for the 0.6.3 answers (0.6.2
and 0.6.3 changed no schema the keeper reads) and `1.0.0/` for the 1.0.0
ones, where an `incomplete` row and the `--targets` selector have to be
valid too."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from fakelens import FakeLens
from keeper.lens import LensClient, LensIncomplete, LensNotReady, LensUnavailable

SCHEMAS = Path(__file__).with_name("fixtures") / "lens-schemas"
# the schema directory each tested lens version is validated against
SCHEMA_SET = {"0.6.3": "0.6.1", "1.0.0": "1.0.0"}


def _validator(version: str, name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / SCHEMA_SET[version] / f"{name}.json").read_text()))


CASES = [
    ("status", [], "status"),
    ("kami", [1001], "kami"),
    ("inventory", [4242], "inventory"),
    ("account", [4242], "account"),
    ("party", [4242], "party"),
    ("node", [62], "node"),
    ("node", [62, "--with-vitals"], "node"),
    ("node", [62, 1001, "--with-vitals"], "node"),
]


@pytest.mark.parametrize("version", ["0.6.3", "1.0.0"])
@pytest.mark.parametrize("query,args,schema", CASES)
def test_fixture_answers_are_schema_valid(sock_dir, version, query, args, schema):
    with FakeLens(str(sock_dir / "l.sock"), lens_version=version) as fake:
        ans = LensClient(fake.socket_path, 5).request(query, args)
    _validator(version, schema).validate(ans.data)
    assert set(ans.meta) >= {"servedAt", "blockNumber", "reconciledThrough", "stale", "mode", "asOf"}
    # the deprecated `observed*` clock names are served by 0.6.3 and gone in 1.0.0
    as_of = ans.meta["asOf"]
    assert {"clockSampleBlock", "clockSampleBlockTime", "clockSampleAgoMs"} <= set(as_of)
    assert ("observedBlock" in as_of) is (version == "0.6.3")
    assert ("appliedThrough" in ans.meta) is (version == "1.0.0")


def test_1_0_0_selected_node_answer_is_schema_valid(sock_dir):
    """`--targets` keeps the named occupants (plus the attacker) and the
    selected answer carries `harvestsSelected` / `targetsAbsent`."""
    with FakeLens(str(sock_dir / "l.sock"), lens_version="1.0.0") as fake:
        other = next(h["kami"]["index"] for h in fake.node["harvests"] if h["kami"]["index"] != 1001)
        ans = LensClient(fake.socket_path, 5).request("node", [62, 1001, "--with-vitals", "--targets", f"1001,{other},999999"])
    _validator("1.0.0", "node").validate(ans.data)
    assert sorted(h["kami"]["index"] for h in ans.data["harvests"]) == sorted({1001, other})
    assert ans.data["harvestsSelected"] == len(ans.data["harvests"]) and ans.data["targetsAbsent"] == [999999]
    assert ans.data["harvestsTotal"] == len(fake.node["harvests"])  # the WHOLE node, still


def test_1_0_0_incomplete_rows_are_schema_valid_and_counted(sock_dir):
    """A list row of a kami the mirror cannot project keeps identity +
    `state` + `incomplete: true` and no vitals (the schema's if/then); the
    single-entity read of it is refused."""
    with FakeLens(str(sock_dir / "l.sock"), lens_version="1.0.0") as fake:
        fake.set_incomplete(1001)
        c = LensClient(fake.socket_path, 5)
        party = c.request("party", [4242])
        node = c.request("node", [62, "--with-vitals"])
        with pytest.raises(LensIncomplete):
            c.request("kami", [1001])
    _validator("1.0.0", "party").validate(party.data)
    _validator("1.0.0", "node").validate(node.data)
    row = next(r for r in party.data["kamis"] if r["index"] == 1001)
    assert row["incomplete"] is True and "hp" not in row and "node" not in row and row["state"]
    assert party.incomplete_rows == 1 and node.incomplete_rows >= 1
    hrow = next(h for h in node.data["harvests"] if h["kami"]["index"] == 1001)
    assert hrow["incomplete"] is True and "vitals" not in hrow and "liquidation" not in hrow


def test_0_6_3_refuses_the_1_0_0_options_as_unknown(sock_dir):
    with FakeLens(str(sock_dir / "l.sock"), lens_version="0.6.3") as fake:
        c = LensClient(fake.socket_path, 5)
        for args in ([62, "--with-vitals", "--targets", "1001"], [62, "--with-vitals", "--account", "4242"], [1001, "--at-least", "5"]):
            query = "kami" if args[0] == 1001 else "node"
            # the raw request: the client's own belt is tested in test_lens100
            with pytest.raises(ValueError) as e:
                c._request(query, args)
            assert "unknown option" in str(e.value) and "BAD_ARGS" in str(e.value)


def test_not_ready_mode_refuses_world_reads_and_still_answers_status(sock_dir):
    with FakeLens(str(sock_dir / "l.sock"), mode="not-ready") as fake:
        c = LensClient(fake.socket_path, 5)
        assert c.status().data["state"] == "SETUP"
        with pytest.raises(LensNotReady):
            c.kami(1001)


def test_absent_mode_is_unavailable(sock_dir):
    fake = FakeLens(str(sock_dir / "l.sock"), mode="absent").start()
    with pytest.raises(LensUnavailable):
        LensClient(fake.socket_path, 5).status()


def test_stale_mode_lags_reconciled_through(sock_dir):
    with FakeLens(str(sock_dir / "l.sock"), mode="stale", lag=50) as fake:
        ans = LensClient(fake.socket_path, 5).status()
    assert ans.block - ans.reconciled_through == 50
