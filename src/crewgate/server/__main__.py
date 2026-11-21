"""Run the gateway:  python -m crewgate.server   (or: make gateway)"""

import uvicorn

from crewgate.core.safe_logging import configure_logging

from .app import create_app
from .settings import GatewaySettings


def main() -> None:
    settings = GatewaySettings()
    configure_logging(settings.pseudonym_key, level=settings.log_level, service="crewgate-gateway")
    app, _ = create_app(settings)
    # log_config=None keeps our redacting JSON formatter for uvicorn's own logs too
    uvicorn.run(app, host=settings.host, port=settings.port, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
