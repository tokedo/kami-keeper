"""SPEC P3a — owner classes and the ONE formatter that renders them.

Internal policy identifiers stay internal. Every formatted string uses the
observation-only names below; the formatter is the boundary and a test
renders every class through it and matches the closed set. The forbidden
word list in scrub.py contains the internal names, so an internal name
leaking into a formatted string fails the scrub test as well.

`standing` is the fifth observation-only name — "the attacker's
OWNER is on the keeper's `standingOwners` list". It is an observation of
the CONFIGURATION, like `listed` is, and it needs no kill and no window.
"""

from __future__ import annotations

import enum


class OwnerClass(enum.Enum):
    """Internal policy identifiers (never formatted)."""

    FRIEND = "friend"
    HARMLESS = "harmless"
    ARMED_PASSIVE = "armed-passive"
    STANDING = "standing"
    HOSTILE = "hostile"


# The closed set of observation-only names (SPEC P3a). `killed-here-<window>`
# carries the BOOK's window it was observed over, e.g. `killed-here-48h`.
OBSERVATION_NAMES: tuple[str, ...] = ("listed", "no-threshold", "threshold-no-kill", "standing", "killed-here-<window>")


def classify(*, listed: bool, max_threshold: int, owner_hostile_in_window: bool, kami_hostile_in_window: bool, standing: bool = False) -> OwnerClass:
    """The definition, in observation terms, evaluated PER ATTACKER (an
    attacker is a kami on, or remembered for, the node, under its
    current owner):
      listed                                   -> FRIEND
      owner OR kami hostile in the window      -> HOSTILE
      owner on the standing list               -> STANDING
      max threshold 0 against every book kami  -> HARMLESS
      otherwise (threshold > 0, no kill)       -> ARMED_PASSIVE
    `owner_hostile_in_window`: the owner liquidated / cast on this node, or
    on one of this account's kamis on any node, inside the window (measured
    from its LAST such kill — there is no permanent class).
    `kami_hostile_in_window`: the attacker KAMI itself did, whoever owns it
    now. `standing`: the attacker's OWNER is on the keeper's
    `standingOwners` list — configuration, not memory, so it binds without
    a kill and without a window. `max_threshold` is the served lens preview
    threshold (never computed here). Order matters: a listed owner is
    FRIEND whatever it did, whatever kami it holds and whatever other list
    it is on (the two lists are the operator's to keep disjoint)."""
    if listed:
        return OwnerClass.FRIEND
    if owner_hostile_in_window or kami_hostile_in_window:
        return OwnerClass.HOSTILE
    if standing:
        return OwnerClass.STANDING
    if max_threshold <= 0:
        return OwnerClass.HARMLESS
    return OwnerClass.ARMED_PASSIVE


def hostile_by(*, owner_hostile_in_window: bool, kami_hostile_in_window: bool, standing: bool = False) -> str | None:
    """What made an attacker bind with the safety margin: `owner`, `kami`
    or `both` from the window flags; `standing` when neither flag is
    set and the owner is on the keeper's `standingOwners`; None when
    nothing does (`boundBy.hostileBy`, the tick / measure lines)."""
    if owner_hostile_in_window and kami_hostile_in_window:
        return "both"
    if owner_hostile_in_window:
        return "owner"
    if kami_hostile_in_window:
        return "kami"
    if standing:
        return "standing"
    return None


# severity order, for "the class an OWNER is counted under" when its
# attackers differ (a clean owner holding a kami that killed): the worst.
# a standing owner counted under the worst of its attackers shows
# `killed-here-<window>` when any attacker of it killed, `standing` otherwise
_SEVERITY = {OwnerClass.FRIEND: 0, OwnerClass.HARMLESS: 1, OwnerClass.ARMED_PASSIVE: 2, OwnerClass.STANDING: 3, OwnerClass.HOSTILE: 4}


def worst(classes: "list[OwnerClass]") -> OwnerClass:
    return max(classes, key=lambda c: _SEVERITY[c])


def render(cls: OwnerClass, window_hours: int) -> str:
    """The formatter. Returns one of the five observation-only names."""
    if cls is OwnerClass.FRIEND:
        return "listed"
    if cls is OwnerClass.HARMLESS:
        return "no-threshold"
    if cls is OwnerClass.ARMED_PASSIVE:
        return "threshold-no-kill"
    if cls is OwnerClass.STANDING:
        return "standing"
    if cls is OwnerClass.HOSTILE:
        return f"killed-here-{int(window_hours)}h"
    raise ValueError(f"unknown owner class {cls!r}")


def is_observation_name(name: str) -> bool:
    if name in ("listed", "no-threshold", "threshold-no-kill", "standing"):
        return True
    return name.startswith("killed-here-") and name.endswith("h") and name[len("killed-here-"):-1].isdigit()
