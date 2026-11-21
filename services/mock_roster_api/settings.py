"""Mock API settings. Values come from env vars or .env; the defaults are for local and CI only."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ApiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCKAPI_", env_file=".env", extra="ignore")

    audience: str = "crew-roster-api"
    jwt_signing_key: SecretStr = SecretStr("dev-only-signing-key-change-me-0123456789")
    token_ttl_seconds: int = 300
    rate_limit_per_minute: int = 60
    page_size_max: int = 50
    # client_id -> (secret, scopes that client may request)
    clients: dict[str, tuple[str, list[str]]] = {
        "crewgate-agent": ("dev-only-secret", ["roster.read", "swap.write"]),
        "readonly-agent": ("dev-only-readonly", ["roster.read"]),
    }
