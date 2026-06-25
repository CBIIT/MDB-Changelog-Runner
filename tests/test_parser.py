from __future__ import annotations

import pytest

from mdb_changelog_runner import ChangelogParseError, parse


def write_changelog(tmp_path, body: str):
    path = tmp_path / "changelog.xml"
    path.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext"
  xmlns:mdb="https://cbiit.github.io/mdb/changelog">
{body}
</databaseChangeLog>
""",
        encoding="utf-8",
    )
    return path


def test_parse_existing_liquibase_cypher_changesets_with_json_params(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <neo4j:cypher><![CDATA[
      MERGE (n:term {handle: $handle, value: $value}) SET n.note = 'a > b'
    ]]></neo4j:cypher>
    <mdb:params>{"handle": "C123", "value": "Alpha &amp; Beta"}</mdb:params>
  </changeSet>
  <changeSet id="2" author="Bob">
    <neo4j:cypher>CREATE (n:test {handle:'TEST'})</neo4j:cypher>
  </changeSet>""",
    )

    changesets = parse(path)

    assert [changeset.id for changeset in changesets] == ["1", "2"]
    assert [changeset.author for changeset in changesets] == ["Alice", "Bob"]
    assert changesets[0].cypher == (
        "MERGE (n:term {handle: $handle, value: $value}) SET n.note = 'a > b'"
    )
    assert changesets[0].params == {"handle": "C123", "value": "Alpha & Beta"}
    assert changesets[1].cypher == "CREATE (n:test {handle:'TEST'})"
    assert changesets[1].params == {}


def test_parse_rejects_missing_cypher(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <comment>Nothing to execute</comment>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="missing neo4j:cypher"):
        parse(path)


def test_parse_rejects_duplicate_changeset_ids(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <neo4j:cypher>RETURN 1</neo4j:cypher>
  </changeSet>
  <changeSet id="1" author="Bob">
    <neo4j:cypher>RETURN 2</neo4j:cypher>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="duplicate changeSet id"):
        parse(path)


def test_parse_rejects_missing_author(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1">
    <neo4j:cypher>RETURN 1</neo4j:cypher>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="missing required author"):
        parse(path)


def test_parse_rejects_empty_cypher(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <neo4j:cypher>
    </neo4j:cypher>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="empty neo4j:cypher"):
        parse(path)


def test_parse_rejects_invalid_json_params(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <neo4j:cypher>RETURN $value</neo4j:cypher>
    <mdb:params>{"value":</mdb:params>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="invalid JSON params"):
        parse(path)


def test_parse_rejects_non_object_json_params(tmp_path):
    path = write_changelog(
        tmp_path,
        """  <changeSet id="1" author="Alice">
    <neo4j:cypher>RETURN $value</neo4j:cypher>
    <mdb:params>["not", "an", "object"]</mdb:params>
  </changeSet>""",
    )

    with pytest.raises(ChangelogParseError, match="params must be a JSON object"):
        parse(path)


def test_parse_rejects_xml_entities(tmp_path):
    path = tmp_path / "malicious_changelog.xml"
    path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE databaseChangeLog [
  <!ENTITY payload "expanded">
]>
<databaseChangeLog
  xmlns="http://www.liquibase.org/xml/ns/dbchangelog"
  xmlns:neo4j="http://www.liquibase.org/xml/ns/dbchangelog-ext">
  <changeSet id="1" author="Alice">
    <neo4j:cypher>RETURN "&payload;"</neo4j:cypher>
  </changeSet>
</databaseChangeLog>
""",
        encoding="utf-8",
    )

    with pytest.raises(ChangelogParseError, match="unsafe XML"):
        parse(path)
