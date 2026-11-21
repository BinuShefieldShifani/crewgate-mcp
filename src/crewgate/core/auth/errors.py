"""Auth failures, shaped so the gateway can turn them straight into RFC 6750 responses."""

from enum import StrEnum


class AuthErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"  # 400: malformed Authorization header
    INVALID_TOKEN = "invalid_token"  # 401: missing, expired, wrong audience, bad signature
    INSUFFICIENT_SCOPE = "insufficient_scope"  # 403: valid token, not enough scope
    TEMPORARILY_UNAVAILABLE = "temporarily_unavailable"  # 503: cannot reach the identity provider


_STATUS = {
    AuthErrorCode.INVALID_REQUEST: 400,
    AuthErrorCode.INVALID_TOKEN: 401,
    AuthErrorCode.INSUFFICIENT_SCOPE: 403,
    AuthErrorCode.TEMPORARILY_UNAVAILABLE: 503,
}


class AuthError(Exception):
    """The description is safe to return to the caller: it never contains token contents."""

    def __init__(self, code: AuthErrorCode, description: str, scope: str | None = None) -> None:
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description
        self.scope = scope  # the scope that would satisfy the request, for insufficient_scope

    @property
    def status(self) -> int:
        return _STATUS[self.code]

    @classmethod
    def invalid_token(cls, description: str) -> "AuthError":
        return cls(AuthErrorCode.INVALID_TOKEN, description)

    @classmethod
    def insufficient_scope(cls, scope: str, description: str = "token lacks required scope") -> "AuthError":
        return cls(AuthErrorCode.INSUFFICIENT_SCOPE, description, scope=scope)

    @classmethod
    def unavailable(cls, description: str) -> "AuthError":
        return cls(AuthErrorCode.TEMPORARILY_UNAVAILABLE, description)
