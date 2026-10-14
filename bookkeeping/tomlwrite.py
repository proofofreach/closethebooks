"""Write the small TOML subset used by workspace configuration files.

Supports strings, integers, booleans, lists of scalars, and nested tables.
Writes are atomic so a failed save never leaves a half-written config.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(name: str) -> str:
    return name if BARE_KEY.match(name) else json.dumps(name)


def _scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(item) for item in value) + "]"
    raise TypeError(f"Unsupported TOML value: {value!r}")


def dumps(payload: dict, header: str = "") -> str:
    lines: list[str] = [line for line in header.splitlines()]
    if lines:
        lines.append("")

    def table(path: list[str], data: dict) -> None:
        scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and v is not None}
        tables = {k: v for k, v in data.items() if isinstance(v, dict)}
        if path and (scalars or not tables):
            lines.append("[" + ".".join(_key(part) for part in path) + "]")
        for name, value in scalars.items():
            lines.append(f"{_key(name)} = {_scalar(value)}")
        if path and (scalars or not tables):
            lines.append("")
        for name, value in tables.items():
            table([*path, name], value)

    table([], payload)
    return "\n".join(lines).rstrip() + "\n"


def write(path: Path, payload: dict, header: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(dumps(payload, header), encoding="utf-8")
    temporary.replace(path)
