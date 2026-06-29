from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from mdb_changelog_runner.errors import ChangelogExecutionError
from mdb_changelog_runner.parser import parse
from mdb_changelog_runner.types import ChangelogRunResult, Changeset

LOGGER_NAME = "mdb_changelog_runner"
DEFAULT_DEPRECATE_AFTER = timedelta(days=180)
METADATA_QUERY = """
OPTIONAL MATCH (previous:_changelog)
WHERE ($scope IS NOT NULL OR $scope_path IS NOT NULL OR previous.location = $location)
  AND ($scope IS NULL OR previous.scope = $scope)
  AND ($scope_path IS NULL OR previous.scope_path = $scope_path)
WITH previous
ORDER BY previous.timestamp DESC
LIMIT 1
CREATE (current:_changelog {
  timestamp: $timestamp,
  location: $location,
  scope: $scope,
  scope_path: $scope_path,
  changesets_executed: $changesets_executed,
  authors: $authors,
  deprecate_after: $deprecate_after
})
FOREACH (_ IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
  CREATE (current)-[:prev_changelog]->(previous)
)
RETURN current
""".strip()


class ChangelogExecutor:
    """Execute parsed Cypher changesets in one transaction."""

    def __init__(
        self,
        driver_or_session: Any,
        logger: logging.Logger | None = None,
        deprecate_after: timedelta = DEFAULT_DEPRECATE_AFTER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._driver_or_session = driver_or_session
        self._logger = logger or logging.getLogger(LOGGER_NAME)
        self._deprecate_after = deprecate_after
        self._clock = clock or _utc_now

    def parse(self, changelog_path: str | Path) -> list[Changeset]:
        """Parse a changelog XML file."""

        return parse(changelog_path)

    def execute(
        self,
        changelog_path: str | Path,
        changelog_location: str,
        changelog_scope: str | None = None,
        changelog_scope_path: str | None = None,
        *,
        dry_run: bool = False,
    ) -> ChangelogRunResult:
        """Execute a changelog atomically."""

        total_start = time.perf_counter()
        changesets = self.parse(changelog_path)
        self._logger.info("Found %d changesets in changelog file", len(changesets))
        authors = _unique_authors(changeset.author for changeset in changesets)
        result = ChangelogRunResult(
            changelog_location=changelog_location,
            changelog_scope=changelog_scope,
            changelog_scope_path=changelog_scope_path,
            changesets_executed=len(changesets),
            authors=tuple(authors),
            dry_run=dry_run,
        )

        if dry_run:
            self._logger.warning("Dry run requested; no Cypher will be executed")
            self._log_finished(total_start)
            return result

        session, should_close = self._open_session()
        tx = None
        current_changeset: Changeset | None = None
        current_index = 0
        try:
            tx = session.begin_transaction()
            for current_index, current_changeset in enumerate(changesets, start=1):
                changeset_start = time.perf_counter()
                self._logger.info(
                    "Executing changeSet %s by %s (%d/%d)",
                    current_changeset.id,
                    current_changeset.author,
                    current_index,
                    len(changesets),
                )
                tx.run(current_changeset.cypher, parameters=current_changeset.params)
                changeset_elapsed = time.perf_counter() - changeset_start
                self._logger.info(
                    "Changelog %d took %.2f seconds",
                    current_index - 1,
                    changeset_elapsed,
                )
                self._logger.info("Completed changelog update %d", current_index)

            current_changeset = None
            self._record_metadata(
                tx,
                changelog_location,
                changelog_scope,
                changelog_scope_path,
                len(changesets),
                authors,
            )
            tx.commit()
            self._logger.info("Changelog runner finished.")
        except Exception as exc:
            if tx is not None:
                tx.rollback()
            self._logger.exception("Error in changelog update")
            failing = (
                f" at changeSet {current_changeset.id} ({current_index}/{len(changesets)})"
                if current_changeset is not None
                else ""
            )
            msg = f"failed to execute changelog {changelog_location}{failing}"
            raise ChangelogExecutionError(msg) from exc
        finally:
            if should_close and hasattr(session, "close"):
                session.close()
            self._logger.info("TOTAL RUN TIME: %.2f seconds", time.perf_counter() - total_start)

        return result

    def _open_session(self) -> tuple[Any, bool]:
        if hasattr(self._driver_or_session, "session") and callable(
            self._driver_or_session.session,
        ):
            return self._driver_or_session.session(), True
        return self._driver_or_session, False

    def _log_finished(self, total_start: float) -> None:
        self._logger.info("Changelog runner finished.")
        self._logger.info("TOTAL RUN TIME: %.2f seconds", time.perf_counter() - total_start)

    def _record_metadata(
        self,
        tx: Any,
        changelog_location: str,
        changelog_scope: str | None,
        changelog_scope_path: str | None,
        changesets_executed: int,
        authors: list[str],
    ) -> None:
        timestamp = _as_utc(self._clock())
        deprecate_after = timestamp + self._deprecate_after
        self._logger.info("Recording _changelog metadata for %s", changelog_location)
        tx.run(
            METADATA_QUERY,
            parameters={
                "timestamp": timestamp,
                "location": changelog_location,
                "scope": changelog_scope,
                "scope_path": changelog_scope_path,
                "changesets_executed": changesets_executed,
                "authors": authors,
                "deprecate_after": deprecate_after,
            },
        )


def _unique_authors(authors: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for author in authors:
        if author in seen:
            continue
        seen.add(author)
        unique.append(author)
    return unique


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
