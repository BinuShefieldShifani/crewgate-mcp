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
    # A non-idempotent call (e.g. creating an order) was sent but no answer came back. It may or
    # may not have taken effect, so it is never retried blindly and never reported as done.
    OUTCOME_UNKNOWN = "outcome_unknown"
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
    def __init__(
        self, category: ErrorCategory, message: str, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(f"{category}: {message}")
        self.category = category
        self.message = message
        self.status = status
        self.code = code  # the partner's own machine-readable code, e.g. Duffel's "price_changed"

    @classmethod
    def from_status(cls, status: int, message: str, code: str | None = None) -> "PartnerError":
        if status >= 500:
            return cls(ErrorCategory.UPSTREAM_UNAVAILABLE, message, status, code)
        return cls(_STATUS_MAP.get(status, ErrorCategory.UNKNOWN), message, status, code)

    @property
    def retryable(self) -> bool:
        return self.category in {ErrorCategory.RATE_LIMITED, ErrorCategory.UPSTREAM_UNAVAILABLE}
