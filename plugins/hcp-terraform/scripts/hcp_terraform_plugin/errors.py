"""The two errors every action turns into a result."""


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


class ApiError(Exception):
    """A request to HCP Terraform failed; the message is the reason."""
