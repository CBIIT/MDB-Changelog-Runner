from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree

from mdb_changelog_runner.errors import ChangelogParseError
from mdb_changelog_runner.types import Changeset

NEO4J_NS = "http://www.liquibase.org/xml/ns/dbchangelog-ext"
MDB_NS = "https://cbiit.github.io/mdb/changelog"


def parse(changelog_path: str | Path) -> list[Changeset]:
    """Parse Liquibase-style XML into ordered Cypher changesets."""

    path = Path(changelog_path)
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        msg = f"failed to parse changelog XML {path}: {exc}"
        raise ChangelogParseError(msg) from exc
    except OSError as exc:
        msg = f"failed to read changelog XML {path}: {exc}"
        raise ChangelogParseError(msg) from exc

    if _local_name(root.tag) != "databaseChangeLog":
        msg = f"expected databaseChangeLog root in {path}"
        raise ChangelogParseError(msg)

    changesets: list[Changeset] = []
    seen_ids: set[str] = set()
    for ordinal, element in enumerate(_children_named(root, "changeSet"), start=1):
        changeset_id = (element.attrib.get("id") or "").strip()
        if not changeset_id:
            msg = f"changeSet at position {ordinal} missing required id"
            raise ChangelogParseError(msg)
        if changeset_id in seen_ids:
            msg = f"duplicate changeSet id {changeset_id!r}"
            raise ChangelogParseError(msg)
        seen_ids.add(changeset_id)

        author = (element.attrib.get("author") or "").strip()
        if not author:
            msg = f"changeSet {changeset_id} missing required author"
            raise ChangelogParseError(msg)

        cypher_element = _first_child_named(element, "cypher", namespace=NEO4J_NS)
        if cypher_element is None:
            msg = f"changeSet {changeset_id} missing neo4j:cypher"
            raise ChangelogParseError(msg)

        cypher = _normalize_text(cypher_element.itertext())
        if not cypher:
            msg = f"changeSet {changeset_id} has empty neo4j:cypher"
            raise ChangelogParseError(msg)

        changesets.append(
            Changeset(
                id=changeset_id,
                author=author,
                cypher=cypher,
                params=_parse_params(element, changeset_id),
            ),
        )

    return changesets


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if tag.startswith("{") else tag


def _namespace(tag: str) -> str | None:
    if tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return None


def _children_named(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _first_child_named(
    element: ElementTree.Element,
    name: str,
    namespace: str | None = None,
) -> ElementTree.Element | None:
    for child in list(element):
        if _local_name(child.tag) != name:
            continue
        if namespace is not None and _namespace(child.tag) != namespace:
            continue
        return child
    return None


def _normalize_text(parts: Iterable[str]) -> str:
    return textwrap.dedent("".join(parts)).strip()


def _parse_params(element: ElementTree.Element, changeset_id: str) -> dict[str, Any]:
    params_element = _first_child_named(element, "params", namespace=MDB_NS)
    if params_element is None:
        return {}
    raw = "".join(params_element.itertext()).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"changeSet {changeset_id} has invalid JSON params: {exc.msg}"
        raise ChangelogParseError(msg) from exc
    if not isinstance(parsed, dict):
        msg = f"changeSet {changeset_id} params must be a JSON object"
        raise ChangelogParseError(msg)
    return parsed
