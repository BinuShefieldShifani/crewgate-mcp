"""Agent settings (env prefix AGENT_). Defaults point at Groq's free plan.

The key is read from AGENT_LLM_API_KEY only. It is never taken from GITHUB_TOKEN or any other
variable, so a CI token can't be sent to a model provider by accident.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm import OpenAICompatibleChat


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "openai/gpt-oss-120b"
    llm_api_key: SecretStr | None = None
    # gpt-oss models reason before answering; "low" keeps answers fast and inside the free
    # tokens-per-minute limit. Empty for models that don't take this parameter.
    llm_reasoning_effort: str | None = "low"
    # Groq free plan: 30 requests a minute. 2.2 s apart stays under it.
    llm_min_interval_seconds: float = 2.2
    llm_max_wait_seconds: float = 65.0
    max_steps: int = 8  # model calls per user turn; tool calls in between
    max_tool_result_chars: int = 3000  # keeps each request well under 8,000 tokens a minute
    default_market: str = "SE"
    gateway_url: str = "http://localhost:8000/mcp"

    def api_key(self) -> str:
        return self.llm_api_key.get_secret_value() if self.llm_api_key is not None else ""

    def chat_model(self) -> OpenAICompatibleChat:
        return OpenAICompatibleChat(
            base_url=self.llm_base_url,
            model=self.llm_model,
            api_key=self.api_key(),
            min_interval=self.llm_min_interval_seconds,
            max_wait=self.llm_max_wait_seconds,
            reasoning_effort=self.llm_reasoning_effort or None,
        )
