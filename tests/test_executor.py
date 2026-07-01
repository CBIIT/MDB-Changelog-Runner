from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from mdb_changelog_runner import ChangelogExecutionError, ChangelogExecutor


CHANGELOG = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext"
  xmlns:mdb="https://cbiit.github.io/mdb/changelog">
  <changeSet id="1" author="Alice">
    <neo4j:cypher>CREATE (a:test {handle: $handle})</neo4j:cypher>
    <mdb:params>{"handle": "A"}</mdb:params>
  </changeSet>
  <changeSet id="2" author="Bob">
    <neo4j:cypher>CREATE (b:test {handle: 'B'})</neo4j:cypher>
  </changeSet>
</databaseChangeLog>
"""

EMPTY_CHANGELOG = """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext">
</databaseChangeLog>
"""


class FakeTx:
    def __init__(self, fail_on: str | None = None):
        self.fail_on = fail_on
        self.runs: list[tuple[str, dict]] = []
        self.committed = False
        self.rolled_back = False

    def run(self, query: str, parameters: dict | None = None, **kwargs):
        params = parameters if parameters is not None else kwargs
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("boom")
        self.runs.append((query, params))
        return []

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class FakeSession:
    def __init__(self, tx: FakeTx):
        self.tx = tx
        self.closed = False

    def begin_transaction(self):
        return self.tx

    def close(self):
        self.closed = True


class RecordingSession:
    def __init__(self):
        self.transactions: list[FakeTx] = []
        self.closed = False

    def begin_transaction(self):
        tx = FakeTx()
        self.transactions.append(tx)
        return tx

    def close(self):
        self.closed = True


class FailingBeginSession(FakeSession):
    def begin_transaction(self):
        raise RuntimeError("cannot begin transaction")


class FakeDriver:
    def __init__(self, session: FakeSession):
        self._session = session

    def session(self):
        return self._session


def write_changelog(tmp_path):
    path = tmp_path / "changelog.xml"
    path.write_text(CHANGELOG, encoding="utf-8")
    return path


def write_empty_changelog(tmp_path):
    path = tmp_path / "empty_changelog.xml"
    path.write_text(EMPTY_CHANGELOG, encoding="utf-8")
    return path


def execute_changelog(executor, tmp_path, *, dry_run=False):
    return executor.execute(
        write_changelog(tmp_path),
        "s3://bucket/model_changelogs/CTDC/changelog.xml",
        changelog_scope="model",
        changelog_scope_path="model_changelogs/CTDC",
        dry_run=dry_run,
    )


def test_execute_runs_changesets_in_order_and_records_metadata(tmp_path):
    tx = FakeTx()
    session = FakeSession(tx)
    timestamp = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    executor = ChangelogExecutor(
        FakeDriver(session),
        deprecate_after=timedelta(days=30),
        clock=lambda: timestamp,
    )

    result = execute_changelog(executor, tmp_path)

    assert result.changelog_scope == "model"
    assert result.changelog_scope_path == "model_changelogs/CTDC"
    assert result.changesets_executed == 2
    assert result.authors == ("Alice", "Bob")
    assert tx.committed is True
    assert tx.rolled_back is False
    assert session.closed is True
    assert tx.runs[0] == ("CREATE (a:test {handle: $handle})", {"handle": "A"})
    assert tx.runs[1] == ("CREATE (b:test {handle: 'B'})", {})
    assert len(tx.runs) == 3
    metadata_query, metadata_params = tx.runs[2]
    assert "OPTIONAL MATCH (previous:_changelog)" in metadata_query
    assert "previous.scope = $scope" in metadata_query
    assert "previous.scope_path = $scope_path" in metadata_query
    assert "scope: $scope" in metadata_query
    assert "scope_path: $scope_path" in metadata_query
    assert "CREATE (current:_changelog" in metadata_query
    assert "CREATE (current)-[:prev_changelog]->(previous)" in metadata_query
    assert metadata_params == {
        "timestamp": timestamp,
        "location": "s3://bucket/model_changelogs/CTDC/changelog.xml",
        "scope": "model",
        "scope_path": "model_changelogs/CTDC",
        "changesets_executed": 2,
        "authors": ["Alice", "Bob"],
        "deprecate_after": datetime(2026, 2, 14, 12, 30, tzinfo=UTC),
    }


def test_execute_allows_metadata_without_scope(tmp_path):
    tx = FakeTx()
    timestamp = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    executor = ChangelogExecutor(FakeSession(tx), clock=lambda: timestamp)

    result = executor.execute(write_changelog(tmp_path), "s3://bucket/changelog.xml")

    assert result.changelog_scope is None
    assert result.changelog_scope_path is None
    metadata_query, metadata_params = tx.runs[2]
    assert "$scope IS NULL" in metadata_query
    assert "previous.location = $location" in metadata_query
    assert "scope: $scope" in metadata_query
    assert "scope_path: $scope_path" in metadata_query
    assert metadata_params == {
        "timestamp": timestamp,
        "location": "s3://bucket/changelog.xml",
        "scope": None,
        "scope_path": None,
        "changesets_executed": 2,
        "authors": ["Alice", "Bob"],
        "deprecate_after": datetime(2026, 7, 14, 12, 30, tzinfo=UTC),
    }


def test_execute_schema_mode_runs_each_changeset_in_its_own_transaction(tmp_path, caplog):
    path = tmp_path / "schema_changelog.xml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext">
  <changeSet id="0" author="Alice">
    <neo4j:cypher>CREATE INDEX term_origin_idx IF NOT EXISTS FOR (t:term) ON (t.origin_name)</neo4j:cypher>
  </changeSet>
  <changeSet id="1" author="Alice">
    <neo4j:cypher>CREATE CONSTRAINT term_nanoid_unique IF NOT EXISTS FOR (t:term) REQUIRE t.nanoid IS UNIQUE</neo4j:cypher>
  </changeSet>
</databaseChangeLog>
""",
        encoding="utf-8",
    )
    session = RecordingSession()
    timestamp = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
    executor = ChangelogExecutor(session, clock=lambda: timestamp)

    with caplog.at_level(logging.INFO, logger="mdb_changelog_runner"):
        result = executor.execute(path, "s3://bucket/schema_changelog.xml", schema_mode=True)

    assert result.changesets_executed == 2
    assert len(session.transactions) == 3
    assert [tx.committed for tx in session.transactions] == [True, True, True]
    assert "CREATE INDEX term_origin_idx" in session.transactions[0].runs[0][0]
    assert "CREATE CONSTRAINT term_nanoid_unique" in session.transactions[1].runs[0][0]
    assert "_changelog" in session.transactions[2].runs[0][0]
    assert "Transaction mode: schema mode; one transaction per changeSet" in [
        record.getMessage() for record in caplog.records
    ]


def test_execute_skips_metadata_for_empty_changelog(tmp_path, caplog):
    tx = FakeTx()
    executor = ChangelogExecutor(FakeSession(tx))

    with caplog.at_level(logging.WARNING, logger="mdb_changelog_runner"):
        result = executor.execute(
            write_empty_changelog(tmp_path),
            "s3://bucket/empty_changelog.xml",
            changelog_scope="term",
            changelog_scope_path="term_changelogs",
        )

    assert result.changesets_executed == 0
    assert result.authors == ()
    assert tx.runs == []
    assert tx.committed is True
    assert tx.rolled_back is False
    assert "Changelog file contains no changesets; no metadata will be recorded" in [
        record.getMessage() for record in caplog.records
    ]


def test_execute_logs_changeset_and_total_runtime(tmp_path, caplog):
    tx = FakeTx()
    executor = ChangelogExecutor(FakeSession(tx))

    with caplog.at_level(logging.INFO, logger="mdb_changelog_runner"):
        execute_changelog(executor, tmp_path)

    messages = [record.getMessage() for record in caplog.records]
    assert "Found 2 changesets in changelog file" in messages
    assert "Transaction mode: single transaction" in messages
    assert any(message.startswith("Changelog 0 took ") for message in messages)
    assert any(message.startswith("Changelog 1 took ") for message in messages)
    assert "Completed changelog update 1" in messages
    assert "Completed changelog update 2" in messages
    assert "Changelog runner finished." in messages
    assert any(message.startswith("TOTAL RUN TIME: ") for message in messages)


def test_execute_rolls_back_and_writes_no_metadata_on_failure(tmp_path):
    tx = FakeTx(fail_on="handle: 'B'")
    executor = ChangelogExecutor(FakeSession(tx))

    with pytest.raises(ChangelogExecutionError, match="changeSet 2"):
        execute_changelog(executor, tmp_path)

    assert tx.committed is False
    assert tx.rolled_back is True
    assert len(tx.runs) == 1
    assert all("_changelog" not in query for query, _ in tx.runs)


def test_execute_does_not_attribute_metadata_failure_to_last_changeset(tmp_path):
    tx = FakeTx(fail_on="_changelog")
    executor = ChangelogExecutor(FakeSession(tx))

    with pytest.raises(ChangelogExecutionError) as exc_info:
        execute_changelog(executor, tmp_path)

    assert "changeSet" not in str(exc_info.value)
    assert tx.committed is False
    assert tx.rolled_back is True


def test_execute_closes_driver_session_when_begin_transaction_fails(tmp_path):
    tx = FakeTx()
    session = FailingBeginSession(tx)
    executor = ChangelogExecutor(FakeDriver(session))

    with pytest.raises(ChangelogExecutionError, match="failed to execute changelog"):
        execute_changelog(executor, tmp_path)

    assert session.closed is True
    assert tx.rolled_back is False


def test_execute_dry_run_parses_but_does_not_open_transaction(tmp_path):
    tx = FakeTx()
    executor = ChangelogExecutor(FakeSession(tx))

    result = execute_changelog(executor, tmp_path, dry_run=True)

    assert result.changesets_executed == 2
    assert tx.runs == []
    assert tx.committed is False
    assert tx.rolled_back is False
