"""Run with: python -m mock_roster_api   (serves on 0.0.0.0:8001 inside the container)."""

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "mock_roster_api.app:app",
        host=os.getenv("MOCKAPI_HOST", "127.0.0.1"),
        port=int(os.getenv("MOCKAPI_PORT", "8001")),
    )
