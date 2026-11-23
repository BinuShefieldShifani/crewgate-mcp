"""Gateway settings (env prefix CREWGATE_). Token and identity settings live in AuthSettings."""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CREWGATE_", env_file=".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8000
    public_url: str = "http://localhost:8000"  # what browsers use; approval links are built from it
    mcp_path: str = "/mcp"
    plugins: list[str] = ["crew", "booking"]
    entitlements_path: str = "config/entitlements.toml"
    audit_path: str = "var/audit.jsonl"
    audit_key: SecretStr = SecretStr("dev-only-audit-key")
    pseudonym_key: SecretStr = SecretStr("dev-only-pseudonym-key")
    log_level: str = "INFO"
    # Hosts the MCP endpoint answers to (DNS-rebinding protection). Add your real hostname in production.
    allowed_hosts: list[str] = ["localhost:*", "127.0.0.1:*", "gateway:*"]
    allowed_origins: list[str] = ["http://localhost:*", "http://127.0.0.1:*"]

    # Human approval of write tools: a separate OAuth client the agent never holds credentials for.
    approvals_client_id: str = "crewgate-approvals"
    approvals_client_secret: SecretStr = SecretStr("dev-only-approvals-secret")
    approvals_authorize_url: str = "http://localhost:8080/realms/crewgate/protocol/openid-connect/auth"
    approval_ttl_seconds: float = 600.0
    max_sign_in_age_seconds: float = 300.0
