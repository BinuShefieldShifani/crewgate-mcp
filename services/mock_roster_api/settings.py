"""Mock API settings. Values come from env vars or .env; the defaults are for local and CI only."""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCKAPI_", env_file=".env", extra="ignore")

    # "local": the API issues its own HS256 tokens (unit tests, Day 1 demo).
    # "oidc": the API trusts Keycloak, like a real partner. It then checks both the scope
    # (what the app may ask for) and the user's role (what this person may do).
    auth_mode: Literal["local", "oidc"] = "local"
    oidc_issuer: str = "http://localhost:8080/realms/crewgate"
    oidc_jwks_url: str = "http://localhost:8080/realms/crewgate/protocol/openid-connect/certs"
    role_for_scope: dict[str, str] = {"roster.read": "roster-reader", "swap.write": "swap-requester"}
    audience: str = "crew-roster-api"
    # When set, every call except /health must carry a valid X-Crewgate-Signature.
    # Several secrets may be listed during a key rotation.
    hmac_secrets: list[SecretStr] = []
    signature_tolerance_seconds: float = 300.0
    jwt_signing_key: SecretStr = SecretStr("dev-only-signing-key-change-me-0123456789")
    token_ttl_seconds: int = 300
    rate_limit_per_minute: int = 60
    page_size_max: int = 50
    # client_id -> (secret, scopes that client may request)
    clients: dict[str, tuple[str, list[str]]] = {
        "crewgate-agent": ("dev-only-secret", ["roster.read", "swap.write"]),
        "readonly-agent": ("dev-only-readonly", ["roster.read"]),
    }
