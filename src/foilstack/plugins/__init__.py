"""Plugin discovery.

Sources are Python modules under `plugins/sources/`, enrichers under
`plugins/enrichers/`. Exports are TOML files under `plugins/exports/`. None of
them is fetched from a registry and nothing is installed automatically: a
source plugin runs code with network access against your inventory, and
auto-installing that from the internet is how a card scanner becomes someone
else's botnet.
"""

from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
from typing import Any

from foilstack.plugins.base import CardRecord, EnrichmentPlugin, SourcePlugin
from foilstack.plugins.exports import ExportSpec, load_export_specs

_EXPORTS_DIR = Path(__file__).parent / "exports"


def _discover(package_name: str) -> dict[str, Any]:
    found: dict[str, Any] = {}
    package = importlib.import_module(package_name)
    for mod in pkgutil.iter_modules(package.__path__):
        if mod.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package_name}.{mod.name}")
        plugin = getattr(module, "PLUGIN", None)
        if plugin is not None:
            found[plugin.name] = plugin
    return found


def source_plugins() -> dict[str, SourcePlugin]:
    return _discover("foilstack.plugins.sources")


def enrichment_plugins() -> dict[str, EnrichmentPlugin]:
    """Plugins that add to a catalogue rather than supply one.

    A separate registry, not a flag on the source list. An enricher cannot be
    passed to `ingest` — it has no images and would write cards nothing can
    match — and one list holding both kinds is one `hasattr` away from that
    happening.
    """
    return _discover("foilstack.plugins.enrichers")


def export_plugins() -> dict[str, ExportSpec]:
    return {spec.name: spec for spec in load_export_specs(_EXPORTS_DIR)}


def supported_games() -> list[str]:
    """Every game any installed source plugin can fetch, in human words.

    The landing page's answer to the first question a visitor has, which is
    "does it do mine". Derived from the plugins rather than written into the
    page, because a hardcoded list is a promise that goes stale the first time
    somebody adds a source and does not think to edit the marketing copy — and
    a landing page naming a game the install cannot actually ingest is worse
    than one naming none.

    Deduplicated, because two plugins may cover the same game, and sorted by
    the displayed name so the order is the one a reader would expect.
    """
    names: set[str] = set()
    for plugin in source_plugins().values():
        labels = getattr(plugin, "labels", {}) or {}
        for game in plugin.games:
            names.add(labels.get(game) or game.title())
    return sorted(names)


__all__ = [
    "CardRecord",
    "EnrichmentPlugin",
    "ExportSpec",
    "SourcePlugin",
    "enrichment_plugins",
    "export_plugins",
    "source_plugins",
    "supported_games",
]
