"""Plugin discovery.

Sources are Python modules under `plugins/sources/`. Exports are TOML files
under `plugins/exports/`. Neither is fetched from a registry and nothing is
installed automatically: a source plugin runs code with network access against
your inventory, and auto-installing that from the internet is how a card
scanner becomes someone else's botnet.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from foilstack.plugins.base import CardRecord, SourcePlugin
from foilstack.plugins.exports import ExportSpec, load_export_specs

_EXPORTS_DIR = Path(__file__).parent / "exports"


def source_plugins() -> dict[str, SourcePlugin]:
    found: dict[str, SourcePlugin] = {}
    package = importlib.import_module("foilstack.plugins.sources")
    for mod in pkgutil.iter_modules(package.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"foilstack.plugins.sources.{mod.name}")
        plugin = getattr(module, "PLUGIN", None)
        if plugin is not None:
            found[plugin.name] = plugin
    return found


def export_plugins() -> dict[str, ExportSpec]:
    return {spec.name: spec for spec in load_export_specs(_EXPORTS_DIR)}


__all__ = [
    "CardRecord",
    "ExportSpec",
    "SourcePlugin",
    "export_plugins",
    "source_plugins",
]
