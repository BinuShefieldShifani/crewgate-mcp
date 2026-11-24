"""Settings for the console (env prefix WEB_)."""

from urllib.parse import urlsplit

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEB_", env_file=".env", extra="ignore")

    # What the browser uses to reach the console. Redirects and cookies are built from it.
    public_url: str = "http://localhost:8090"
    # The realm as browsers see it (the "iss" in tokens) and as this server reaches it.
    issuer: str = "http://localhost:8080/realms/crewgate"
    internal_issuer: str = "http://localhost:8080/realms/crewgate"
    client_id: str = "crewgate-web"
    client_secret: SecretStr = SecretStr("dev-only-web-secret")
    audience: str = "crewgate-gateway"  # tokens must be for the gateway, nothing else
    scopes: list[str] = ["openid", "profile", "roster.read", "swap.write", "booking.read", "booking.write"]
    gateway_mcp_url: str = "http://localhost:8000/mcp"
    grafana_url: str = ""  # shown as a link when set
    demo_users_note: str = "Demo users: anna / dev-only-anna (planner), erik / dev-only-erik (viewer)"

    # Protect a public demo's model quota and the partner sandbox.
    max_turns_per_hour: int = 20
    max_model_calls_per_day: int = 300
    max_message_chars: int = 1000
    session_ttl_seconds: int = 3600
    host: str = "127.0.0.1"  # the container image sets WEB_HOST=0.0.0.0; the edge decides what is public
    port: int = 8090

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return self.public_url.rstrip("/") + "/callback"

    @property
    def authorize_url(self) -> str:
        return self.issuer.rstrip("/") + "/protocol/openid-connect/auth"

    @property
    def logout_url(self) -> str:
        return self.issuer.rstrip("/") + "/protocol/openid-connect/logout"

    @property
    def token_url(self) -> str:
        return self.internal_issuer.rstrip("/") + "/protocol/openid-connect/token"

    @property
    def jwks_url(self) -> str:
        return self.internal_issuer.rstrip("/") + "/protocol/openid-connect/certs"
