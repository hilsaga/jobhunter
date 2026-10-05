"""Run-control exceptions shared by the CLI, browser bridge, and pipeline."""


class DocsMissing(Exception):
    """Raised when docs/ has no usable base documents and the run cannot continue."""


class SkipJob(Exception):
    """Raised to abandon the current job and continue with the next one."""


class AbortRun(Exception):
    """Raised when the human stops the whole run."""


class HumanInputRequired(Exception):
    """Raised when a non-interactive run reaches a question only a person can answer."""


class ChromeInteractionError(Exception):
    """Raised when a Chrome MCP action times out or cannot be completed."""


class ChromeNotConfigured(Exception):
    """Raised when no Chrome MCP server command is available."""
