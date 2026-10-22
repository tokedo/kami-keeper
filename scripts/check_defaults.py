#!/usr/bin/env python3
"""SPEC P2 / P11 — the default-diff CI check.

Defaults are pinned in keeper/schemas/defaults.lock.json (the per-type
defaults) and its SHA-256. This script:

  --write     regenerate the lock from the schemas (run when a MAJOR bump
              is intended; the diff is the changelog entry)
  (no flag)   exit 1 if the schemas' defaults differ from the lock, or —
              when a git tag exists — if the lock at the last tag differs
              and KEEPER_VERSION's MAJOR did not move.

Run from the repo root: `python scripts/check_defaults.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from keeper import KEEPER_VERSION  # noqa: E402
from keeper.books import all_conditional_defaults, all_defaults, defaults_digest  # noqa: E402

LOCK = ROOT / "keeper" / "schemas" / "defaults.lock.json"


def current() -> dict:
    # `conditionalDefaults` — the `x-defaultWhen` rules (a default that
    # depends on another key of the same config) are defaults too
    return {"sha256": defaults_digest(), "defaults": all_defaults(), "conditionalDefaults": all_conditional_defaults()}


def main(argv: list[str]) -> int:
    if "--write" in argv:
        LOCK.write_text(json.dumps(current(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {LOCK} sha256={defaults_digest()}")
        return 0
    if not LOCK.exists():
        print(f"no lock at {LOCK}; run with --write", file=sys.stderr)
        return 1
    locked = json.loads(LOCK.read_text(encoding="utf-8"))
    now = current()
    if locked["sha256"] != now["sha256"] or locked["defaults"] != now["defaults"] or locked.get("conditionalDefaults", {}) != now["conditionalDefaults"]:
        print("DEFAULTS CHANGED against keeper/schemas/defaults.lock.json — a default change is MAJOR (SPEC P2/P11).", file=sys.stderr)
        for t, d in now["defaults"].items():
            old = locked["defaults"].get(t, {})
            for k in sorted(set(d) | set(old)):
                if d.get(k) != old.get(k):
                    print(f"  {t}.{k}: {old.get(k)!r} -> {d.get(k)!r}", file=sys.stderr)
        for t, d in now["conditionalDefaults"].items():
            old = (locked.get("conditionalDefaults") or {}).get(t, {})
            for k in sorted(set(d) | set(old)):
                if d.get(k) != old.get(k):
                    print(f"  {t}.{k} (conditional): {old.get(k)!r} -> {d.get(k)!r}", file=sys.stderr)
        print("If intended: bump MAJOR in keeper/__init__.py, then `python scripts/check_defaults.py --write`.", file=sys.stderr)
        return 1
    # compare against the last tag, when there is one
    try:
        tag = subprocess.run(["git", "describe", "--tags", "--abbrev=0"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except subprocess.CalledProcessError:
        tag = ""
    if tag:
        try:
            at_tag = subprocess.run(["git", "show", f"{tag}:keeper/schemas/defaults.lock.json"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        except subprocess.CalledProcessError:
            at_tag = ""
        if at_tag:
            tagged = json.loads(at_tag)
            if tagged["sha256"] != now["sha256"]:
                tag_major = tag.lstrip("v").split(".")[0]
                cur_major = KEEPER_VERSION.split(".")[0]
                if tag_major == cur_major:
                    print(f"defaults differ from tag {tag} but MAJOR did not move ({KEEPER_VERSION})", file=sys.stderr)
                    return 1
    print(f"defaults ok sha256={now['sha256']}" + (f" (tag {tag})" if tag else " (no tag yet)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
