class ChangelogParseError(ValueError):
    """Raised when a changelog XML file cannot be parsed."""


class ChangelogExecutionError(RuntimeError):
    """Raised when changelog execution fails and the transaction is rolled back."""
