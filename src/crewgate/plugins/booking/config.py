"""Duffel settings (env prefix DUFFEL_). Test mode only: a live token is refused at startup."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DuffelSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DUFFEL_", env_file=".env", extra="ignore")

    access_token: SecretStr | None = None  # duffel_test_...; booking tools report "not configured" without it
    base_url: str = "https://api.duffel.com"
    api_version: str = "v2"  # sent as the Duffel-Version header on every request
    # Duffel waits up to 20 s per airline by default. An agent's tool call can't, so we cap it.
    supplier_timeout_ms: int = 8000
    timeout_seconds: float = 15.0
    max_offers: int = 50  # one page of offers; we return the cheapest few
    max_retry_after_seconds: float = 5.0
    webhook_secrets: list[SecretStr] = []  # several during a rotation
    webhook_tolerance_seconds: float = 300.0

    @field_validator("access_token")
    @classmethod
    def _test_mode_only(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not v.get_secret_value().strip():
            return None  # an empty env var (compose passes "" when unset) means "not configured"
        if v is not None and v.get_secret_value().startswith("duffel_live_"):
            raise ValueError(
                "crewgate is a prototype: only Duffel test-mode tokens (duffel_test_...) are allowed"
            )
        return v

    @field_validator("supplier_timeout_ms")
    @classmethod
    def _duffel_range(cls, v: int) -> int:
        if not 2000 <= v <= 60000:  # Duffel ignores values outside 2 to 60 seconds
            raise ValueError("supplier_timeout_ms must be between 2000 and 60000")
        return v
