from __future__ import annotations


class BotError(Exception):
    """Base class for errors whose message is safe to display to users."""

    def __init__(self, user_message: str, *, title: str = "Error") -> None:
        """Create the error.

        Args:
            user_message: Human readable explanation shown in the error embed.
            title: Embed title.
        """
        super().__init__(user_message)
        self.user_message = user_message
        self.title = title


class ConfigurationError(BotError):
    """Raised when settings or guild configuration is missing or invalid."""


class TicketNotFound(BotError):
    """Raised when a channel or id does not map to a known ticket."""

    def __init__(self, user_message: str = "This is not a ticket channel.") -> None:
        """Create the error with a default message."""
        super().__init__(user_message, title="Ticket not found")


class PanelNotFound(BotError):
    """Raised when a panel no longer exists in the database."""

    def __init__(self, user_message: str = "This panel no longer exists.") -> None:
        """Create the error with a default message."""
        super().__init__(user_message, title="Panel not found")


class PermissionDenied(BotError):
    """Raised when a member is not allowed to perform an action."""

    def __init__(self, user_message: str = "You are not allowed to do that.") -> None:
        """Create the error with a default message."""
        super().__init__(user_message, title="Permission denied")


class TicketStateError(BotError):
    """Raised when an action is invalid for the ticket's current state."""

    def __init__(self, user_message: str) -> None:
        """Create the error."""
        super().__init__(user_message, title="Invalid action")


class TicketLimitReached(BotError):
    """Raised when a user already owns the maximum number of open tickets."""

    def __init__(self, jump_url: str, limit: int) -> None:
        """Create the error.

        Args:
            jump_url: Link to the user's existing open ticket.
            limit: The configured per-user limit.
        """
        super().__init__(
            f"You already have {limit} open ticket(s): {jump_url}",
            title="Ticket limit reached",
        )
        self.jump_url = jump_url
        self.limit = limit


class TargetBlocked(BotError):
    """Raised when adding a user or role that was previously revoked from a ticket."""

    def __init__(self) -> None:
        """Create the error."""
        super().__init__(
            "That user or role was revoked from this ticket. "
            "A member with Manage Server must add them.",
            title="Access revoked",
        )


class DatabaseError(Exception):
    """Raised when the storage layer cannot read or write data."""
