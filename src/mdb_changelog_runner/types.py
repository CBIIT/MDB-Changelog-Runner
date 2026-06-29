from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Changeset:
    """A parsed Liquibase-style Cypher changeset."""

    id: str
    author: str
    cypher: str
    params: dict[str, Any]


@dataclass(frozen=True)
class ChangelogRunResult:
    """Summary of a changelog execution attempt."""

    changelog_location: str
    changesets_executed: int
    authors: tuple[str, ...]
    changelog_scope: str | None = None
    changelog_scope_path: str | None = None
    dry_run: bool = False
