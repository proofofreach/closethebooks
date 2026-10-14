"""Build throwaway workspaces for tests."""

from __future__ import annotations

from pathlib import Path

from bookkeeping import tomlwrite
from bookkeeping.workspace import Workspace, load_workspace


def make_workspace(
    root: Path,
    *,
    ledger: str = "ledger/main.beancount",
    sources: str = "sources.toml",
    name: str = "Test Company",
    currency: str = "USD",
    chart: dict | None = None,
    **paths: str,
) -> Workspace:
    tomlwrite.write(
        root / "books.toml",
        {
            "company": {"name": name, "currency": currency},
            "chart": chart or {},
            "paths": {"ledger": ledger, "sources": sources, **paths},
        },
    )
    return load_workspace(root)
