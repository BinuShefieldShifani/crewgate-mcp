"""Gateway authentication: JWT verification via JWKS, PRM (RFC 9728) and OBO (RFC 8693)."""

from .errors import AuthError, AuthErrorCode
from .jwks import JwksCache, SigningKey
from .metadata import metadata_path, metadata_url, protected_resource_metadata, www_authenticate
from .principal import Principal
from .settings import AuthSettings
from .token_exchange import OnBehalfOf, TokenExchanger
from .verifier import TokenVerifier

__all__ = [
    "AuthError",
    "AuthErrorCode",
    "AuthSettings",
    "JwksCache",
    "OnBehalfOf",
    "Principal",
    "SigningKey",
    "TokenExchanger",
    "TokenVerifier",
    "metadata_path",
    "metadata_url",
    "protected_resource_metadata",
    "www_authenticate",
]
