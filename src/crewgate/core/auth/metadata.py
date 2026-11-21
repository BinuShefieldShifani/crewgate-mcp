"""OAuth 2.0 Protected Resource Metadata (RFC 9728) and RFC 6750 challenges.

The MCP authorization spec requires the server to publish this document and to point clients
at it from the WWW-Authenticate header on a 401, so a client can find the right identity
provider without any out-of-band config.
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import AuthError, AuthErrorCode

WELL_KNOWN = "/.well-known/oauth-protected-resource"


def metadata_path(resource: str) -> str:
    """RFC 9728 section 3.1: insert the well-known segment between host and path."""
    path = urlsplit(resource).path.rstrip("/")
    return WELL_KNOWN + path


def metadata_url(resource: str) -> str:
    parts = urlsplit(resource)
    return urlunsplit((parts.scheme, parts.netloc, metadata_path(resource), "", ""))


def protected_resource_metadata(
    resource: str,
    authorization_servers: Sequence[str],
    scopes_supported: Sequence[str],
    resource_name: str | None = None,
    resource_documentation: str | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "resource": resource,
        "authorization_servers": list(authorization_servers),
        "scopes_supported": sorted(set(scopes_supported)),
        "bearer_methods_supported": ["header"],
    }
    if resource_name:
        doc["resource_name"] = resource_name
    if resource_documentation:
        doc["resource_documentation"] = resource_documentation
    return doc


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def www_authenticate(resource_metadata_url: str, error: AuthError | None = None) -> str:
    params = [f"resource_metadata={_quote(resource_metadata_url)}"]
    if error is not None and error.code is not AuthErrorCode.TEMPORARILY_UNAVAILABLE:
        params.append(f"error={_quote(error.code.value)}")
        params.append(f"error_description={_quote(error.description)}")
        if error.scope:
            params.append(f"scope={_quote(error.scope)}")
    return "Bearer " + ", ".join(params)
