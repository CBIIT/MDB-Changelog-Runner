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
        schema_mode: bool = False,
    ) -> ChangelogRunResult:
        """Execute a changelog."""

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
        try:
            transaction_mode = _TransactionMode(
                is_schema_mode=schema_mode,
                logger=self._logger,
                clock=self._clock,
                deprecate_after=self._deprecate_after,
            )
            transaction_mode.run(
                session=session,
                changesets=changesets,
                changelog_location=changelog_location,
                changelog_scope=changelog_scope,
                changelog_scope_path=changelog_scope_path,
                authors=authors,
            )
            self._logger.info("Changelog runner finished.")
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


class _TransactionMode:
    def __init__(
        self,
        *,
        is_schema_mode: bool,
        logger: logging.Logger,
        clock: Callable[[], datetime],
        deprecate_after: timedelta,
    ) -> None:
        self._is_schema_mode = is_schema_mode
        self._logger = logger
        self._clock = clock
        self._deprecate_after = deprecate_after

    def run(
        self,
        *,
        session: Any,
        changesets: list[Changeset],
        changelog_location: str,
        changelog_scope: str | None,
        changelog_scope_path: str | None,
        authors: list[str],
    ) -> None:
        if self._is_schema_mode:
            self._run_schema_mode(
                session,
                changesets,
                changelog_location,
                changelog_scope,
                changelog_scope_path,
                authors,
            )
            return
        self._run_general_mode(
            session,
            changesets,
            changelog_location,
            changelog_scope,
            changelog_scope_path,
            authors,
        )

    def _run_general_mode(
        self,
        session: Any,
        changesets: list[Changeset],
        changelog_location: str,
        changelog_scope: str | None,
        changelog_scope_path: str | None,
        authors: list[str],
    ) -> None:
        tx = None
        failed_changeset: Changeset | None = None
        current_index = 0

        def fail(exc: Exception) -> None:
            self._logger.exception("Error in changelog update")
            failing = (
                f" at changeSet {failed_changeset.id} ({current_index}/{len(changesets)})"
                if failed_changeset is not None
                else ""
            )
            msg = f"failed to execute changelog {changelog_location}{failing}"
            raise ChangelogExecutionError(msg) from exc

        try:
            self._logger.info("Transaction mode: single transaction")
            tx = session.begin_transaction()
            for current_index, changeset in enumerate(changesets, start=1):
                failed_changeset = changeset
                self._run_changeset(
                    tx,
                    changeset,
                    current_index,
                    len(changesets),
                )

            failed_changeset = None
            if changesets:
                self._record_metadata(
                    tx,
                    changelog_location,
                    changelog_scope,
                    changelog_scope_path,
                    len(changesets),
                    authors,
                )
            else:
                self._logger.warning(
                    "Changelog file contains no changesets; no metadata will be recorded",
                )
            tx.commit()
        except Exception as exc:
            if tx is not None:
                tx.rollback()
            fail(exc)

    def _run_schema_mode(
        self,
        session: Any,
        changesets: list[Changeset],
        changelog_location: str,
        changelog_scope: str | None,
        changelog_scope_path: str | None,
        authors: list[str],
    ) -> None:
        if not changesets:
            self._logger.warning(
                "Changelog file contains no changesets; no metadata will be recorded",
            )
            return

        self._logger.info("Transaction mode: schema mode; one transaction per changeSet")
        tx = None
        failed_changeset: Changeset | None = None
        current_index = 0

        def fail(exc: Exception) -> None:
            self._logger.exception("Error in changelog update")
            failing = (
                f" at changeSet {failed_changeset.id} ({current_index}/{len(changesets)})"
                if failed_changeset is not None
                else ""
            )
            msg = f"failed to execute changelog {changelog_location}{failing}"
            raise ChangelogExecutionError(msg) from exc

        try:
            for current_index, changeset in enumerate(changesets, start=1):
                failed_changeset = changeset
                tx = session.begin_transaction()
                self._run_changeset(
                    tx,
                    changeset,
                    current_index,
                    len(changesets),
                )
                tx.commit()
                tx = None

            failed_changeset = None
            tx = session.begin_transaction()
            self._record_metadata(
                tx,
                changelog_location,
                changelog_scope,
                changelog_scope_path,
                len(changesets),
                authors,
            )
            tx.commit()
        except Exception as exc:
            if tx is not None:
                tx.rollback()
            fail(exc)

    def _run_changeset(
        self,
        tx: Any,
        changeset: Changeset,
        current_index: int,
        total_changesets: int,
    ) -> None:
        changeset_start = time.perf_counter()
        self._logger.info(
            "Executing changeSet %s by %s (%d/%d)",
            changeset.id,
            changeset.author,
            current_index,
            total_changesets,
        )
        tx.run(changeset.cypher, parameters=changeset.params)
        changeset_elapsed = time.perf_counter() - changeset_start
        self._logger.info(
            "Changelog %d took %.2f seconds",
            current_index - 1,
            changeset_elapsed,
        )
        self._logger.info("Completed changelog update %d", current_index)

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
