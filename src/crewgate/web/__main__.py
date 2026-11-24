"""Run the console:  python -m crewgate.web   (or: make web)"""

import os

import uvicorn
from pydantic import SecretStr

from crewgate.agent import AgentSettings
from crewgate.core.safe_logging import configure_logging

from .app import create_app
from .settings import WebSettings


def main() -> None:
    settings = WebSettings()
    key = SecretStr(os.environ.get("WEB_PSEUDONYM_KEY", "dev-only-pseudonym-key"))
    configure_logging(key, service="crewgate-web")
    agent = AgentSettings()
    # Without AGENT_LLM_API_KEY the console still runs (sign-in, the page), with chat switched off.
    model = agent.chat_model_with_fallbacks() if agent.api_key() else None
    app = create_app(settings, model)
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
