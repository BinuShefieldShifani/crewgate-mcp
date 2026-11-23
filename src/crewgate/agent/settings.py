"""Agent settings (env prefix AGENT_). The model key falls back to GITHUB_TOKEN, which GitHub
Actions provides when the workflow has `permissions: models: read`."""

import os

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm import OpenAICompatibleChat


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    llm_base_url: str = "https://models.github.ai/inference"
    llm_model: str = "openai/gpt-4o-mini"
    llm_api_key: SecretStr | None = None
    # GitHub Models free tier: 15 requests/min for low-tier models. 4.2 s apart stays under it.
    llm_min_interval_seconds: float = 4.2
    llm_max_wait_seconds: float = 65.0
    max_steps: int = 8  # model calls per user turn; tool calls in between
    max_tool_result_chars: int = 3000  # keep requests under the provider's 8000 input-token limit
    default_market: str = "SE"
    gateway_url: str = "http://localhost:8000/mcp"

    def api_key(self) -> str:
        if self.llm_api_key is not None:
            return self.llm_api_key.get_secret_value()
        return os.environ.get("GITHUB_TOKEN", "")

    def chat_model(self) -> OpenAICompatibleChat:
        return OpenAICompatibleChat(
            base_url=self.llm_base_url,
            model=self.llm_model,
            api_key=self.api_key(),
            min_interval=self.llm_min_interval_seconds,
            max_wait=self.llm_max_wait_seconds,
        )
