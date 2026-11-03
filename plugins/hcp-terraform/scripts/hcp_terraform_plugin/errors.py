"""The two errors every action turns into a result."""


class RefusalError(Exception):
    """A call the plugin refuses; the message is the reason."""


class ApiError(Exception):
    """A request to HCP Terraform failed; the message is the reason."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        """Hold reason as the message, and the answer's HTTP status if one came."""
        super().__init__(reason)
        self.status = status
