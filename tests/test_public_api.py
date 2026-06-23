from __future__ import annotations

import mdb_changelog_runner
from mdb_changelog_runner.errors import ChangelogExecutionError, ChangelogParseError
from mdb_changelog_runner.executor import ChangelogExecutor
from mdb_changelog_runner.parser import parse
from mdb_changelog_runner.types import ChangelogRunResult, Changeset


def test_package_root_re_exports_public_api():
    assert mdb_changelog_runner.ChangelogExecutionError is ChangelogExecutionError
    assert mdb_changelog_runner.ChangelogExecutor is ChangelogExecutor
    assert mdb_changelog_runner.ChangelogParseError is ChangelogParseError
    assert mdb_changelog_runner.ChangelogRunResult is ChangelogRunResult
    assert mdb_changelog_runner.Changeset is Changeset
    assert mdb_changelog_runner.parse is parse
    assert set(mdb_changelog_runner.__all__) == {
        "ChangelogExecutionError",
        "ChangelogExecutor",
        "ChangelogParseError",
        "ChangelogRunResult",
        "Changeset",
        "parse",
    }
