"""Client-side auth for MCP clients: discovery and authorization code + PKCE sign-in."""

from .pkce import (
    AuthServerMetadata,
    DiscoveryError,
    PkcePair,
    TokenRequestError,
    build_authorization_url,
    discover_authorization_server,
    discovery_urls,
    exchange_code,
    fetch_protected_resource_metadata,
)

__all__ = [
    "AuthServerMetadata",
    "DiscoveryError",
    "PkcePair",
    "TokenRequestError",
    "build_authorization_url",
    "discover_authorization_server",
    "discovery_urls",
    "exchange_code",
    "fetch_protected_resource_metadata",
]
