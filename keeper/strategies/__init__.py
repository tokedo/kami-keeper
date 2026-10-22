"""Strategy types (SPEC P2): exactly three. `registry()` maps the type
name to its class."""

from __future__ import annotations

from typing import Type

from .base import Strategy
from .harvest_and_feed import HarvestAndFeed
from .craft import Craft
from .auto import Auto

REGISTRY: dict[str, Type[Strategy]] = {
    HarvestAndFeed.type_name: HarvestAndFeed,
    Auto.type_name: Auto,
    Craft.type_name: Craft,
}

__all__ = ["REGISTRY", "Strategy", "HarvestAndFeed", "Craft", "Auto"]
