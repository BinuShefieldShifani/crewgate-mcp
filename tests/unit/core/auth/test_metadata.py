from crewgate.core.auth import (
    AuthError,
    metadata_path,
    metadata_url,
    protected_resource_metadata,
    www_authenticate,
)


def test_well_known_is_inserted_before_the_path() -> None:
    assert (
        metadata_url("https://gw.example/mcp")
        == "https://gw.example/.well-known/oauth-protected-resource/mcp"
    )
    assert metadata_path("https://gw.example/mcp/") == "/.well-known/oauth-protected-resource/mcp"
    assert metadata_url("https://gw.example") == "https://gw.example/.well-known/oauth-protected-resource"


def test_document_fields() -> None:
    doc = protected_resource_metadata(
        "https://gw.example/mcp",
        ["https://idp.example/realms/crewgate"],
        ["swap.write", "roster.read", "roster.read"],
        resource_name="crewgate",
        resource_documentation="https://github.com/x/crewgate-mcp",
    )
    assert doc == {
        "resource": "https://gw.example/mcp",
        "authorization_servers": ["https://idp.example/realms/crewgate"],
        "scopes_supported": ["roster.read", "swap.write"],
        "bearer_methods_supported": ["header"],
        "resource_name": "crewgate",
        "resource_documentation": "https://github.com/x/crewgate-mcp",
    }
    assert "resource_name" not in protected_resource_metadata("r", ["a"], [])


def test_challenge_without_error_points_at_metadata() -> None:
    header = www_authenticate("https://gw.example/.well-known/oauth-protected-resource/mcp")
    assert header == 'Bearer resource_metadata="https://gw.example/.well-known/oauth-protected-resource/mcp"'


def test_insufficient_scope_challenge_names_the_scope() -> None:
    header = www_authenticate("https://m", AuthError.insufficient_scope("swap.write"))
    assert 'error="insufficient_scope"' in header
    assert 'scope="swap.write"' in header


def test_quotes_in_descriptions_are_escaped() -> None:
    header = www_authenticate("https://m", AuthError.invalid_token('bad "token"'))
    assert 'error_description="bad \\"token\\""' in header


def test_unavailable_is_not_reported_as_a_token_error() -> None:
    assert "error=" not in www_authenticate("https://m", AuthError.unavailable("idp down"))
