"""A fake kami-lens daemon for tests: a unix socket server
speaking the lens protocol from recorded fixture SHAPES.

  request:  {id?, query, args?: string[]}
  response: {id, ok: true, data, untrusted, meta} | {id, ok: false, error}

Modes:
  live       every fixture answers; status is LIVE
  not-ready  status answers state SETUP; every world read -> NOT_READY
  stale      LIVE, but reconciledThrough lags the mirror by `lag` blocks
  absent     no socket at all (LensUnavailable at the client)

Versions (`lens_version`, SPEC D1 `LENS_TESTED_VERSIONS`):
  0.6.3  the default: no `appliedThrough`
         anywhere, `meta.asOf` carries BOTH the `clockSample*` and the
         deprecated `observed*` names, and `--at-least` / `--targets` /
         `--account` are refused `BAD_ARGS` "unknown option".
  1.0.0  `meta.appliedThrough` on every answer and `sync.appliedThrough`
         on status (`applied_lag` blocks behind the mirror block);
         `status.incompleteRows` the object; `meta.asOf` without the
         `observed*` names; `head_absent` drops `headBlockNumber`,
         `headSampledAt` and `blockLag` together; `extra_degraded` adds
         entries to `status.degraded` (`reconcile-stalled:45`);
         `--at-least <n>` holds or refuses `NOT_APPLIED`; `--targets` /
         `--account` select node rows; `set_incomplete(kami)` serves that
         kami as an incomplete row / an `INCOMPLETE` refusal.

Fixture data lives in tests/fixtures/lens/*.json and is validated against
the copied lens schemas (per version) by tests/test_fakelens.py, so the
fake invents values, never fields — the status files on disk stay
0.6.1-shaped and the 1.0.0 shape is DERIVED in `_status()` / `_meta()`.
`set_kami(...)` mutates the kami / node / party fixtures in place so a
test can script a world history (SPEC P7 test).
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

FIXTURES = Path(__file__).with_name("fixtures") / "lens"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The 1.0.0 options. Every one of them takes a VALUE (so the parser must
# not read that value as a positional), and 0.6.3 knows none of them (so
# it refuses each with "unknown option") — one list serves both facts.
VALUE_OPTIONS: tuple[str, ...] = ("--at-least", "--max-wait", "--targets", "--account")


class FakeLens:
    def __init__(self, socket_path: str, mode: str = "live", lag: int = 0, lens_version: str = "0.6.3"):
        self.socket_path = socket_path
        self.mode = mode
        self.lag = lag  # stale mode: blocks reconciledThrough lags the mirror
        self.lens_version = lens_version
        # 1.0.0 only: blocks `appliedThrough` lags the mirror block (like
        # `lag` for the reconciled mark), the absent background head
        # sample, extra `status.degraded` entries, and the kamis the
        # mirror "cannot project completely"
        self.applied_lag = 0
        self.head_absent = False
        self.extra_degraded: list[str] = []
        self.incomplete: set[int] = set()
        self.block = 11878491
        self.status_data = _load("status-live")
        self.kamis: dict[int, dict[str, Any]] = {1001: _load("kami-1001")}
        self.node = _load("node-62")
        # more nodes by index, and per-attacker preview answers
        # {node: {attacker: data}} (a `node <i> <attacker>` read)
        self.nodes: dict[int, dict[str, Any]] = {}
        self.previews: dict[int, dict[int, dict[str, Any]]] = {}
        # an attacker with no recorded preview is synthesized from the node
        # data: every non-attacker row gets `liquidation.threshold` =
        # synth_thresholds[(attacker, kami)] or synth_default (0)
        self.synth_thresholds: dict[tuple[int, int], int] = {}
        self.synth_default: int | None = 0
        # {node: {attacker | None}} — a preview answer served in the
        # DEGRADED shape (see `set_degraded_preview`); empty = every
        # answer is the normal one
        self.degraded_previews: dict[int, set[int | None]] = {}
        self.inventory: dict[int, dict[str, Any]] = {4242: _load("inventory-4242")}
        self.accounts: dict[str, dict[str, Any]] = {"4242": _load("account-4242"), "lab": _load("account-4242")}
        self.party: dict[int, dict[str, Any]] = {4242: _load("party-4242")}
        # `battles <kami>` answers by kami index; a kami in any
        # party without a recorded answer gets an empty one; `battles_mode`
        # "bad-args" simulates a lens that does not serve the query
        self.battles: dict[int, dict[str, Any]] = {}
        self.battles_mode = "ok"
        self.requests: list[dict[str, Any]] = []
        self.hooks: dict[str, Callable[[list[str]], Any]] = {}
        self._server: socketserver.ThreadingUnixStreamServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- world scripting -------------------------------------------------

    @property
    def v1(self) -> bool:
        """True when this fake serves the 1.0.0 contract."""
        try:
            return int(str(self.lens_version).split(".")[0]) >= 1
        except ValueError:
            return False

    def reconciled_through(self) -> int | None:
        if self.mode == "stale":
            return self.block - self.lag
        return self.block - 2

    def applied_through(self) -> int | None:
        """1.0.0: every write of every block up to this one is applied.
        None on 0.6.3, which serves no applied mark anywhere."""
        if not self.v1:
            return None
        return self.block - int(self.applied_lag)

    def set_incomplete(self, kami: int, on: bool = True) -> None:
        """1.0.0: the mirror cannot project this kami completely. Its
        `kami <i>` read and any pairing read with it as the ATTACKER are
        refused `INCOMPLETE`; its `party` / `node` rows keep identity +
        `state` + `incomplete: true` and NOTHING else, and count in
        `meta.incompleteRows`."""
        with self._lock:
            if on:
                self.incomplete.add(int(kami))
            else:
                self.incomplete.discard(int(kami))

    def advance(self, blocks: int = 1) -> None:
        with self._lock:
            self.block += blocks

    def set_degraded_preview(self, node: int, attacker: int | None, on: bool) -> None:
        """Serve `node <node> <attacker>` in a degraded shape: every harvest
        row without `vitals` and with `liquidation` = eligible false, reason
        ATTACKER_STARVING, threshold 0. `attacker` None = every attacker on
        the node. Off (the default) the answers are the normal ones."""
        s = self.degraded_previews.setdefault(int(node), set())
        key = None if attacker is None else int(attacker)
        if on:
            s.add(key)
        else:
            s.discard(key)
            if key is None:
                s.clear()

    def _degraded_preview(self, node: int, attacker: int) -> bool:
        s = self.degraded_previews.get(int(node)) or set()
        return None in s or int(attacker) in s

    def set_kami(self, index: int, **fields: Any) -> None:
        """state=, hp=(current,total), node=index|None, musu=accrued"""
        with self._lock:
            k = self.kamis.setdefault(index, copy.deepcopy(_load("kami-1001")))
            k["index"] = index
            if "state" in fields:
                k["state"] = fields["state"]
            if "hp" in fields:
                cur, tot = fields["hp"]
                k["hp"] = {"current": cur, "total": tot, "percent": round(100 * cur / tot) if tot else 0}
            if "node" in fields:
                if fields["node"] is None:
                    k.pop("node", None)
                else:
                    k["node"] = {"index": fields["node"], "name": f"node-{fields['node']}"}
            if "musu" in fields:
                k["musu"]["accrued"] = fields["musu"]
            if "account" in fields:
                k["account"] = {"index": fields["account"], "name": f"acct-{fields['account']}"}
            for row in self.party.get(4242, {}).get("kamis", []):
                if row["index"] == index:
                    row["state"] = k["state"]
                    row["hp"] = dict(k["hp"])
                    if "node" in k:
                        row["node"] = dict(k["node"])
                    else:
                        row.pop("node", None)

    def set_inventory(self, account: int, item: int, balance: int) -> None:
        with self._lock:
            inv = self.inventory.setdefault(account, copy.deepcopy(_load("inventory-4242")))
            for row in inv["items"]:
                if row["item"]["index"] == item:
                    row["balance"] = balance
                    return
            inv["items"].append({"balance": balance, "item": {"id": f"{item}001", "index": item, "name": f"item-{item}", "type": "FOOD", "rarity": 1}})

    def set_stamina(self, account: int, current: int) -> None:
        with self._lock:
            for key in (str(account), "lab"):
                a = self.accounts.get(key)
                if a:
                    a["stamina"]["current"] = current

    # -- protocol ---------------------------------------------------------

    def _meta(self, incomplete_rows: int = 0) -> dict[str, Any]:
        now = int(_dt.datetime.now().timestamp())
        # 0.5.2 named these `observed*`; 0.6.1 renamed them `clockSample*`
        # and kept both for one release; 1.0.0 dropped the old names (a
        # 0.6.3 daemon still serves both).
        as_of: dict[str, Any] = {"block": self.block, "projectedAtSec": now, "clockSampleBlock": self.block, "clockSampleBlockTime": now, "clockOffsetMs": 0, "clockSampleAgoMs": 0}
        if not self.v1:
            as_of.update({"observedBlock": self.block, "observedBlockTime": now, "observedAgoMs": 0})
        meta: dict[str, Any] = {
            "servedAt": _now_iso(),
            "blockNumber": self.block,
            "reconciledThrough": self.reconciled_through(),
            "stale": self.mode != "live" and self.mode != "stale",
            "mode": "daemon",
            "asOf": as_of,
        }
        if self.v1:
            meta["appliedThrough"] = self.applied_through()
            if incomplete_rows:
                # SPEC line 113: served only when a row of THIS answer is one
                meta["incompleteRows"] = int(incomplete_rows)
        return meta

    def _status(self) -> dict[str, Any]:
        s = copy.deepcopy(self.status_data)
        s["liveBlockNumber"] = self.block
        s["headBlockNumber"] = self.block + 3
        s["headSampledAt"] = _now_iso()
        s["blockLag"] = 3
        s["version"] = str(self.lens_version)
        s["sync"]["reconciledThrough"] = self.reconciled_through()
        if self.v1:
            s["sync"].update({"appliedThrough": self.applied_through(), "shortReads": 0, "olderWritesSkipped": 0, "lastReconcileAdvanceAt": _now_iso()})
            n = len(self.incomplete)
            s["incompleteRows"] = {"total": n, "refused": 0, "flagged": n, "lastBlock": self.block if n else None, "lastAt": _now_iso() if n else None}
            # 0.6.2 / 0.6.3 keys the 0.6.1 fixture predates; the 1.0.0
            # status schema requires both (null lastFullLoad = a warm boot)
            s.setdefault("lastFullLoad", None)
            s.setdefault("heap", {"limitMb": 8192, "source": "self-configured"})
            if self.head_absent:
                # 1.0.0: the three head fields are left out TOGETHER when the
                # background sample is absent or older than 60 s
                for k in ("headBlockNumber", "headSampledAt", "blockLag"):
                    s.pop(k, None)
            if self.extra_degraded:
                s["degraded"] = list(s.get("degraded") or []) + [str(d) for d in self.extra_degraded]
        if self.mode == "not-ready":
            s["state"] = "SETUP"
            s["percentage"] = 0
            s["liveAt"] = None
        return s

    def _error(self, rid: Any, code: str, message: str, **extra: Any) -> dict[str, Any]:
        return {"id": rid, "ok": False, "error": {"code": code, "message": message, **extra}}

    @staticmethod
    def _parse(args: list[str]) -> tuple[list[str], dict[str, Any]]:
        """(positional, options). A value option's VALUE is not a
        positional — `node 62 --targets 1001,1002` has one positional."""
        positional: list[str] = []
        opts: dict[str, Any] = {}
        i = 0
        while i < len(args):
            a = args[i]
            if a in VALUE_OPTIONS:
                opts[a] = args[i + 1] if i + 1 < len(args) else None
                i += 2
                continue
            if a.startswith("--"):
                opts[a] = True
                i += 1
                continue
            positional.append(a)
            i += 1
        return positional, opts

    # -- 1.0.0: the incomplete row ------------------------------------

    def _party_row_1_0(self, row: dict[str, Any]) -> dict[str, Any]:
        """A party row of a kami the mirror cannot project: identity +
        `state` + `incomplete: true`, and nothing else (the 1.0.0 party
        schema forbids every other key on such a row)."""
        return {k: row[k] for k in ("id", "index", "name", "state") if k in row} | {"incomplete": True}

    def _harvest_row_1_0(self, row: dict[str, Any]) -> dict[str, Any]:
        """A node harvest row of such a kami: `id`, `state`, `kami`,
        `account`, `incomplete: true` — no vitals, no liquidation."""
        return {k: row[k] for k in ("id", "state", "kami", "account") if k in row} | {"incomplete": True}

    def answer(self, req: dict[str, Any]) -> dict[str, Any]:
        rid = req.get("id")
        query = req.get("query")
        args = [str(a) for a in (req.get("args") or [])]
        with self._lock:
            self.requests.append({"query": query, "args": args})
            if not query:
                return self._error(rid, "BAD_ARGS", "request needs a query name")
            if query in self.hooks:
                data = self.hooks[query](args)
                if isinstance(data, dict) and data.get("__error__"):
                    return self._error(rid, data["__error__"], data.get("message", ""))
                return {"id": rid, "ok": True, "data": data, "untrusted": [], "meta": self._meta()}
            positional, opts = self._parse(args)
            if not self.v1:
                # 0.6.3 knows none of the 1.0.0 options; over the socket the
                # CLI's refusal reads BAD_ARGS (
                # "unknown option '--targets' for 'node' — accepts: …")
                unknown = [o for o in VALUE_OPTIONS if o in opts]
                if unknown:
                    return self._error(rid, "BAD_ARGS", f"unknown option '{unknown[0]}' for '{query}'")
            if query == "status":
                if "--at-least" in opts or "--max-wait" in opts:
                    # 1.0.0: either freshness option on `status` is a
                    # usage error — and NOT an "unknown option", so a client
                    # must not read it as "this daemon is older"
                    return self._error(rid, "BAD_ARGS", "the freshness options are not accepted on 'status'")
                return {"id": rid, "ok": True, "data": self._status(), "untrusted": [], "meta": self._meta()}
            if self.mode == "not-ready":
                return self._error(rid, "NOT_READY", "daemon not LIVE (SETUP 0%): mirror empty")
            if self.v1 and "--at-least" in opts:
                # 1.0.0: the read is held until the mark reaches the
                # block; this fake does not wait — it answers or refuses
                try:
                    want = int(opts["--at-least"])
                except (TypeError, ValueError):
                    return self._error(rid, "BAD_ARGS", f"--at-least: not a block number: {opts['--at-least']!r}")
                got = self.applied_through()
                if got is None or want > got:
                    return self._error(rid, "NOT_APPLIED", f"not applied through {want} within 5000 ms", appliedThrough=got)
            try:
                if query == "kami":
                    idx = int(positional[0])
                    if idx not in self.kamis:
                        return self._error(rid, "NOT_FOUND", f"kami {idx} not in mirror")
                    if self.v1 and idx in self.incomplete:
                        # 1.0.0: a single-entity read of a kami the
                        # mirror cannot project completely is REFUSED
                        return self._error(rid, "INCOMPLETE", f"kami {idx} cannot be projected completely: missing joins")
                    return self._ok(rid, copy.deepcopy(self.kamis[idx]))
                if query == "inventory":
                    key = positional[0]
                    idx = int(key) if key.isdigit() else 4242
                    if idx not in self.inventory:
                        return self._error(rid, "NOT_FOUND", f"account {key} not in mirror")
                    return self._ok(rid, copy.deepcopy(self.inventory[idx]))
                if query == "account":
                    key = positional[0]
                    if key not in self.accounts:
                        return self._error(rid, "NOT_FOUND", f"account {key} not in mirror")
                    return self._ok(rid, copy.deepcopy(self.accounts[key]))
                if query == "party":
                    idx = int(positional[0])
                    if idx not in self.party:
                        return self._error(rid, "NOT_FOUND", f"account {idx} not in mirror")
                    data = copy.deepcopy(self.party[idx])
                    flagged = 0
                    if self.v1 and self.incomplete:
                        rows = []
                        for r in data.get("kamis") or []:
                            if int(r.get("index", -1)) in self.incomplete:
                                rows.append(self._party_row_1_0(r))
                                flagged += 1
                            else:
                                rows.append(r)
                        data["kamis"] = rows
                    return self._ok(rid, data, flagged)
                if query == "battles":
                    if self.battles_mode == "bad-args":
                        return self._error(rid, "BAD_ARGS", "unknown query 'battles'")
                    idx = int(positional[0])
                    if idx in self.battles:
                        return self._ok(rid, copy.deepcopy(self.battles[idx]))
                    known = any(r.get("index") == idx for p in self.party.values() for r in p.get("kamis", [])) or idx in self.kamis
                    if not known:
                        return self._error(rid, "NOT_FOUND", f"kami {idx} not in mirror")
                    return self._ok(rid, {"kami": {"index": idx, "name": f"kami-{idx}"}, "stats": {"kills": 0, "deaths": 0, "pnl": 0}, "kills": []})
                if query == "node":
                    idx = int(positional[0])
                    src = self.nodes.get(idx) if idx in self.nodes else (self.node if idx == self.node["index"] else None)
                    if src is None:
                        return self._error(rid, "NOT_FOUND", f"node {idx} not in mirror")
                    if len(positional) >= 2 and (idx in self.previews or idx in self.nodes):
                        att = int(positional[1])
                        if self.v1 and att in self.incomplete:
                            # 1.0.0: a hollow ATTACKER refuses the whole
                            # pairing read — it would otherwise read starving
                            return self._error(rid, "INCOMPLETE", f"attacker {att} cannot be projected completely: missing joins")
                        if att in self.previews.get(idx, {}):
                            src = self.previews[idx][att]
                            data = copy.deepcopy(src)
                        elif self.synth_default is None:
                            return self._error(rid, "NOT_FOUND", f"attacker {att} not on node {idx}")
                        else:
                            data = copy.deepcopy(src)
                            att_row = next((h for h in data["harvests"] if h.get("kami", {}).get("index") == att), None)
                            if att_row is None:
                                return self._error(rid, "NOT_FOUND", f"attacker {att} not on node {idx}")
                            data["attacker"] = {"id": att_row["kami"].get("id", str(att)), "index": att, "name": att_row["kami"].get("name", f"kami-{att}"), "cooldownSec": (att_row.get("vitals") or {}).get("cooldownSec", 0), "cooldownUntil": 0, "blocked": None}
                            for h in data["harvests"]:
                                k = h.get("kami", {}).get("index")
                                if k == att:
                                    continue
                                thr = self.synth_thresholds.get((att, k), self.synth_default)
                                hp = ((h.get("vitals") or {}).get("hp") or {}).get("current", 0)
                                h["liquidation"] = {"eligible": thr > 0 and hp < thr, "reason": "THRESHOLD_ZERO" if thr <= 0 else ("TARGET_HP_ABOVE_THRESHOLD" if hp >= thr else None), "threshold": thr, "margin": hp - thr, "spoils": 0, "salvage": 0, "recoil": 0}
                        if self._degraded_preview(idx, att):
                            # the degraded shape: no vitals, an attacker-side reason, threshold 0
                            for h in data["harvests"]:
                                h.pop("vitals", None)
                                h["liquidation"] = {"eligible": False, "reason": "ATTACKER_STARVING", "threshold": 0, "margin": 0, "spoils": 0, "salvage": 0, "recoil": 0}
                    else:
                        data = copy.deepcopy(src)
                    if "--with-vitals" not in args:
                        data.pop("attacker", None)
                        for h in data["harvests"]:
                            h.pop("vitals", None)
                            h.pop("liquidation", None)
                    elif len(positional) < 2:
                        data.pop("attacker", None)
                        for h in data["harvests"]:
                            h.pop("liquidation", None)
                    if self.v1 and ("--targets" in opts or "--account" in opts):
                        # 1.0.0: both selectors apply BEFORE projection
                        # and intersect; `harvestsTotal` still reports the
                        # whole node, `targetsAbsent` is independent of
                        # `--account` and reads the whole node too
                        keep: set[int] | None = None
                        present = {int(h["kami"]["index"]) for h in data["harvests"]}
                        if "--targets" in opts:
                            want = {int(x) for x in str(opts["--targets"] or "").split(",") if x.strip()}
                            if not 1 <= len(want) <= 500:
                                return self._error(rid, "BAD_ARGS", f"--targets: 1-500 kami indices, got {len(want)}")
                            data["targetsAbsent"] = sorted(want - present)
                            keep = want
                        if "--account" in opts:
                            owned = {int(h["kami"]["index"]) for h in data["harvests"] if int((h.get("account") or {}).get("index", -1)) == int(opts["--account"])}
                            keep = owned if keep is None else (keep & owned)
                        data["harvests"] = [h for h in data["harvests"] if int(h["kami"]["index"]) in (keep or set())]
                        data["harvestsSelected"] = len(data["harvests"])
                        data["harvestsServed"] = len(data["harvests"])
                    flagged = 0
                    if self.v1 and self.incomplete:
                        rows = []
                        for h in data["harvests"]:
                            if int(h.get("kami", {}).get("index", -1)) in self.incomplete:
                                rows.append(self._harvest_row_1_0(h))
                                flagged += 1
                            else:
                                rows.append(h)
                        data["harvests"] = rows
                    return self._ok(rid, data, flagged)
            except (IndexError, ValueError):
                return self._error(rid, "BAD_ARGS", f"{query}: bad arguments")
            return self._error(rid, "BAD_ARGS", f"unknown query '{query}'")

    def _ok(self, rid: Any, data: Any, incomplete_rows: int = 0) -> dict[str, Any]:
        return {"id": rid, "ok": True, "data": data, "untrusted": [], "meta": self._meta(incomplete_rows)}

    # -- server -------------------------------------------------------------

    def start(self) -> "FakeLens":
        if self.mode == "absent":
            return self
        fake = self

        class H(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                buf = b""
                while True:
                    chunk = self.request.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        if not line.strip():
                            continue
                        try:
                            req = json.loads(line.decode("utf-8"))
                        except ValueError:
                            resp = fake._error(None, "BAD_ARGS", "invalid JSON")
                        else:
                            resp = fake.answer(req)
                        self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))

        class S(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        p = Path(self.socket_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            p.unlink()
        self._server = S(self.socket_path, H)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def __enter__(self) -> "FakeLens":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


# ---- standalone: `python tests/fakelens.py <socket> [seed.yaml]` ------------
# Serves the fixtures as a process so `python -m keeper daemon` can be run
# against it. With a keeper config file as the
# second argument, its harvestAndFeed books' kamis are seeded HARVESTING on
# their node above their feed floor and the account is named `alice`.

if __name__ == "__main__":
    import signal
    import sys
    import time

    sock = sys.argv[1]
    fake = FakeLens(sock).start()
    if len(sys.argv) > 2:
        import yaml

        doc = yaml.safe_load(Path(sys.argv[2]).read_text())
        acct = copy.deepcopy(_load("account-4242"))
        acct["name"] = "alice"
        fake.accounts["alice"] = acct
        fake.accounts["4242"] = acct
        for b in doc.get("books", []):
            if b.get("strategyType") == "harvestAndFeed":
                c = b["config"]
                fake.set_kami(int(c["kamiIndex"]), state="HARVESTING", node=int(c["nodeIndex"]), hp=(int(c["feedBelowHp"]) + 30, int(c["feedBelowHp"]) + 74), account=4242)
                fake.set_inventory(4242, int(c["foodItem"]), 50)
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("now", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("now", True))
    print(f"fake lens LIVE at {sock} (kamis: {len(fake.kamis)})", flush=True)
    while not stop["now"]:
        time.sleep(0.2)
    fake.stop()
