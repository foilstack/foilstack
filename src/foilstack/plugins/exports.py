"""Declarative CSV exporters.

An exporter is a TOML file: the columns a marketplace wants, in order, each
bound to a field on the inventory row or to a small, fixed set of transforms.
No code, therefore no arbitrary execution, therefore a contributor can add
support for a marketplace without anyone auditing a pull request for what it
does to the filesystem.

The transform list is deliberately tiny. Every entry added here is a new thing
a reviewer has to understand, and the moment an exporter needs real logic it
should be a source-plugin-style Python module with the scrutiny that implies —
not a slowly growing expression language nobody meant to design.
"""

from __future__ import annotations

import csv
import io
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

Transform = Callable[[Any], str]

_TRANSFORMS: dict[str, Transform] = {
    "str": lambda v: "" if v is None else str(v),
    "money2": lambda v: "" if v is None else f"{float(v):.2f}",
    "int": lambda v: "" if v is None else str(int(v)),
    "upper": lambda v: "" if v is None else str(v).upper(),
}


@dataclass(frozen=True)
class ExportColumn:
    header: str
    field: str | None = None
    const: str | None = None
    transform: str = "str"


@dataclass(frozen=True)
class ExportSpec:
    name: str
    label: str
    description: str
    filename: str
    columns: list[ExportColumn]

    def render(self, rows: list[dict[str, Any]]) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow([c.header for c in self.columns])
        for row in rows:
            out = []
            for col in self.columns:
                if col.const is not None:
                    out.append(col.const)
                    continue
                value = row.get(col.field) if col.field else None
                fn = _TRANSFORMS.get(col.transform, _TRANSFORMS["str"])
                out.append(fn(value))
            writer.writerow(out)
        return buf.getvalue()


def load_export_specs(directory: Path) -> list[ExportSpec]:
    specs: list[ExportSpec] = []
    if not directory.exists():
        return specs
    for path in sorted(directory.glob("*.toml")):
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        columns = [
            ExportColumn(
                header=c["header"],
                field=c.get("field"),
                const=c.get("const"),
                transform=c.get("transform", "str"),
            )
            for c in data.get("columns", [])
        ]
        unknown = {c.transform for c in columns} - set(_TRANSFORMS)
        if unknown:
            raise ValueError(f"{path.name}: unknown transform(s) {sorted(unknown)}")
        specs.append(
            ExportSpec(
                name=data["name"],
                label=data.get("label", data["name"]),
                description=data.get("description", ""),
                filename=data.get("filename", f"{data['name']}.csv"),
                columns=columns,
            )
        )
    return specs
