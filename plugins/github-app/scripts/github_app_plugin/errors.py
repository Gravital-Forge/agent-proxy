"""The one error a run turns into its stderr line."""


class ProviderError(Exception):
    """The run gives no token; the message is the reason, and holds no secret."""
