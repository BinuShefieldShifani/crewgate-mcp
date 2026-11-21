"""One error type for every partner failure, with a category the MCP layer can act on.

Callers (the agent) never see raw HTTP errors from a partner. They see a category and a
safe message, which keeps them insulated from each partner's quirks.
"""

from enum import StrEnum


class ErrorCategory(StrEnum):
    AUTH = "auth"  # our credentials were rejected even after a refresh
    FORBIDDEN = "forbidden"  # token is valid but lacks the scope
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"  # retry budget used up
    VALIDATION = "validation"  # partner rejected the input
    CONFLICT = "conflict"  # e.g. price changed, offer expired
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"  # 5xx or network after retries
    UNKNOWN = "unknown"


_STATUS_MAP = {
    401: ErrorCategory.AUTH,
    403: ErrorCategory.FORBIDDEN,
    404: ErrorCategory.NOT_FOUND,
    409: ErrorCategory.CONFLICT,
    410: ErrorCategory.CONFLICT,
    422: ErrorCategory.VALIDATION,
    400: ErrorCategory.VALIDATION,
    429: ErrorCategory.RATE_LIMITED,
}


class PartnerError(Exception):
    def __init__(self, category: ErrorCategory, message: str, status: int | None = None) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category
        self.message = message
        self.status = status

    @classmethod
    def from_status(cls, status: int, message: str) -> "PartnerError":
        if status >= 500:
            return cls(ErrorCategory.UPSTREAM_UNAVAILABLE, message, status)
        return cls(_STATUS_MAP.get(status, ErrorCategory.UNKNOWN), message, status)

    @property
    def retryable(self) -> bool:
        return self.category in {ErrorCategory.RATE_LIMITED, ErrorCategory.UPSTREAM_UNAVAILABLE}
