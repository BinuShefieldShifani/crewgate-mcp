"""Gateway auth settings. The client secret is a SecretStr, so it never shows in logs or reprs."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREWGATE_AUTH_", env_file=".env", extra="ignore")

    # The issuer is the public URL users sign in at. jwks_url and token_url may point at an
    # internal hostname (http://keycloak:8080 inside compose) while tokens still say localhost.
    issuer: str = "http://localhost:8080/realms/crewgate"
    jwks_url: str = "http://localhost:8080/realms/crewgate/protocol/openid-connect/certs"
    token_url: str = "http://localhost:8080/realms/crewgate/protocol/openid-connect/token"
    audience: str = "crewgate-gateway"
    resource: str = "http://localhost:8000/mcp"  # this server's URL, published in PRM
    client_id: str = "crewgate-gateway"
    client_secret: SecretStr = SecretStr("dev-only-gateway-secret")
    leeway_seconds: float = 30.0
    jwks_ttl_seconds: float = 300.0
