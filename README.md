# mdb-changelog-runner

Lightweight Python execution for Liquibase-style Neo4j/MDB changelog XML.

This package parses ordered `<changeSet>` entries and executes their
`<neo4j:cypher>` queries in a single Neo4j transaction. It does not require
Java, the JDK, Liquibase, or the Liquibase Neo4j extension at runtime.

## Install

```bash
uv add mdb-changelog-runner
```

For local development:

```bash
uv sync
uv run pytest
```

## XML format

Existing Liquibase Neo4j changelogs are supported:

```xml
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext"
  xmlns:mdb="https://cbiit.github.io/mdb/changelog">
  <changeSet id="1" author="MDB-runner">
    <neo4j:cypher>CREATE (n:test {handle:'TEST'})</neo4j:cypher>
  </changeSet>
</databaseChangeLog>
```

Parameterized Cypher can use MDB JSON params:

```xml
<changeSet id="2" author="MDB-runner">
  <neo4j:cypher>
    MERGE (n:term {handle: $handle, value: $value})
  </neo4j:cypher>
  <mdb:params>{"handle": "C123", "value": "Example"}</mdb:params>
</changeSet>
```

## Usage

```python
import logging

from neo4j import GraphDatabase
from mdb_changelog_runner import ChangelogExecutor

driver = GraphDatabase.driver(uri, auth=(user, password))
logger = logging.getLogger("mdb_changelog_runner")

executor = ChangelogExecutor(driver, logger=logger)
result = executor.execute(
    "local_changelog.xml",
    "s3://my-bucket/model_changelogs/CTDC/local_changelog.xml",
    # Optional changelog_scope
    changelog_scope="model",
    # Optional changelog_scope_path
    changelog_scope_path="model_changelogs/CTDC",
    # Optional schema_mode=True for schema-only changelogs
)

print(result.changesets_executed)
```

`execute()` opens a session when given a Neo4j driver. It can also accept an
already-open session-like object that provides `begin_transaction()`.

## Behavior

- Each `<changeSet>` must have an `id`, `author`, and `<neo4j:cypher>` element.
- Changesets run in XML order.
- All changesets run inside one transaction unless `schema_mode=True` is used.
- In schema mode, each changeSet is committed separately.
- After all changesets succeed, one `_changelog` metadata node is written in
  the same transaction. It records the run timestamp, changelog S3 location,
  optional scope values, number of executed changesets, unique authors, and a
  `deprecate_after` timestamp.
- The new `_changelog` node links to the previous matching run with
  `:prev_changelog`. Matching uses scope values when provided, otherwise
  `location`.
- Empty changelogs do not write `_changelog` metadata.
- If any changeset fails, the transaction is rolled back and
  `ChangelogExecutionError` is raised. No metadata is written for failed runs.
- `dry_run=True` parses the changelog and returns a summary without executing
  Cypher.
- By default, `deprecate_after` is 6 months after the run timestamp. Pass a
  `datetime.timedelta` as `ChangelogExecutor(..., deprecate_after=...)` to
  override it.
