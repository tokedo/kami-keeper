"""SPEC P10 — deployment profiles as ONE registry.

Every profile-gated feature is a row here; code asks `allows(feature,
profile)` / `require(feature, profile)` and never compares the profile
string itself (a test scans the package for `== "lab"` / `== "fleet"`
outside this module and for feature names that are not rows).

`lab` is the default; `fleet` must be asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PROFILES: tuple[str, ...] = ("fleet", "lab")
DEFAULT_PROFILE = "lab"


@dataclass(frozen=True)
class FeatureRow:
    feature: str
    fleet: Any
    lab: Any
    note: str

    def value(self, profile: str) -> Any:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
        return getattr(self, profile)


# (feature, fleet, lab). A boolean row is a permission; a string row is the
# profile's value for that aspect (SPEC P10 table).
REGISTRY: tuple[FeatureRow, ...] = (
    FeatureRow("key-backend-default", "gcp-sm", "env", "SPEC P10: key backend default"),
    FeatureRow("grant-in-config", True, False, "SPEC P8/P10: grants declared in config at deploy"),
    FeatureRow("friends-files", True, False, "SPEC P10: roster files as friends input"),
    FeatureRow("external-change-readopt", True, False, "SPEC P7: re-adopt after quiet (lab pauses)"),
    FeatureRow("loopback-status-page", True, False, "SPEC P10: loopback status page"),
    FeatureRow("book-cap-required", False, True, "SPEC P2: maxBooksPerAccount REQUIRED under lab"),
    FeatureRow("readoptAfterBlocks", True, False, "SPEC P2: auto config key allowed only under fleet"),
    FeatureRow("books-list-all", True, False, "SPEC P3: list every account's books (operator profile)"),
    FeatureRow("books-in-config", True, False, "SPEC P8: books declared in config at deploy (lab starts books over the socket only)"),
)

_BY_NAME: dict[str, FeatureRow] = {r.feature: r for r in REGISTRY}
FEATURES: frozenset[str] = frozenset(_BY_NAME)


class UnknownFeature(KeyError):
    pass


def row(feature: str) -> FeatureRow:
    try:
        return _BY_NAME[feature]
    except KeyError:
        raise UnknownFeature(feature) from None


def value(feature: str, profile: str) -> Any:
    return row(feature).value(profile)


def allows(feature: str, profile: str) -> bool:
    """True when the registry row permits `feature` under `profile`. A
    non-boolean row (a default) counts as permitted."""
    v = value(feature, profile)
    return bool(v) if isinstance(v, bool) else True


def validate_profile(profile: str | None) -> str:
    if profile is None:
        return DEFAULT_PROFILE
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; expected one of {PROFILES}")
    return profile


def registry_table() -> list[dict[str, Any]]:
    """The registry as served by `describe` (SPEC P10: every gated feature
    is one visible row)."""
    return [
        {"feature": r.feature, "fleet": r.fleet, "lab": r.lab, "note": r.note}
        for r in REGISTRY
    ]
