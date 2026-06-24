from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable

from mdb_changelog_runner.errors import ChangelogExecutionError
from mdb_changelog_runner.parser import parse
from mdb_changelog_runner.types import ChangelogRunResult, Changeset

LOGGER_NAME = "mdb_changelog_runner"


class ChangelogExecutor:
    """Execute parsed Cypher changesets in one transaction."""

    def __init__(
        self,
        driver_or_session: Any,
        logger: logging.Logger | None = None,
    ) -> None:
        self._driver_or_session = driver_or_session
        self._logger = logger or logging.getLogger(LOGGER_NAME)

    def parse(self, changelog_path: str | Path) -> list[Changeset]:
        """Parse a changelog XML file."""

        return parse(changelog_path)

    def execute(
        self,
        changelog_path: str | Path,
        changelog_location: str,
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


def _unique_authors(authors: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for author in authors:
        if author in seen:
            continue
        seen.add(author)
        unique.append(author)
    return unique
