from __future__ import annotations

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


class FakeDriver:
    def __init__(self, session: FakeSession):
        self._session = session

    def session(self):
        return self._session


def write_changelog(tmp_path):
    path = tmp_path / "changelog.xml"
    path.write_text(CHANGELOG, encoding="utf-8")
    return path


def test_execute_runs_changesets_in_order_without_metadata(tmp_path):
    tx = FakeTx()
    session = FakeSession(tx)
    executor = ChangelogExecutor(FakeDriver(session))

    result = executor.execute(write_changelog(tmp_path), "s3://bucket/changelog.xml")

    assert result.changesets_executed == 2
    assert result.authors == ("Alice", "Bob")
    assert tx.committed is True
    assert tx.rolled_back is False
    assert session.closed is True
    assert tx.runs[0] == ("CREATE (a:test {handle: $handle})", {"handle": "A"})
    assert tx.runs[1] == ("CREATE (b:test {handle: 'B'})", {})
    assert len(tx.runs) == 2
    assert all("_changelog" not in query for query, _ in tx.runs)


def test_execute_rolls_back_and_writes_no_metadata_on_failure(tmp_path):
    tx = FakeTx(fail_on="handle: 'B'")
    executor = ChangelogExecutor(FakeSession(tx))

    with pytest.raises(ChangelogExecutionError, match="changeSet 2"):
        executor.execute(write_changelog(tmp_path), "s3://bucket/changelog.xml")

    assert tx.committed is False
    assert tx.rolled_back is True
    assert len(tx.runs) == 1
    assert all("_changelog" not in query for query, _ in tx.runs)


def test_execute_dry_run_parses_but_does_not_open_transaction(tmp_path):
    tx = FakeTx()
    executor = ChangelogExecutor(FakeSession(tx))

    result = executor.execute(write_changelog(tmp_path), "s3://bucket/changelog.xml", dry_run=True)

    assert result.changesets_executed == 2
    assert tx.runs == []
    assert tx.committed is False
    assert tx.rolled_back is False
