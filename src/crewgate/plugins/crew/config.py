from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class CrewSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREW_", env_file=".env", extra="ignore")

    api_base_url: str = "http://localhost:8001"
    token_url: str = "/oauth/token"
    client_id: str = "crewgate-agent"
    client_secret: SecretStr = SecretStr("dev-only-secret")
    scopes: str = "roster.read swap.write"
    timeout_seconds: float = 10.0
    # Interactive tools: wait out a partner's Retry-After only if it is short.
    max_retry_after_seconds: float = 5.0
    # Shared secret for HMAC request signing (X-Crewgate-Signature). None turns signing off.
    hmac_secret: SecretStr | None = SecretStr("dev-only-hmac-secret")
