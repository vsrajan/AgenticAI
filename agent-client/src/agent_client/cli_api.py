# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "fastapi>=0.115",
#     "uvicorn>=0.30",
#     "agent-client",
# ]
#
# [tool.uv.sources]
# agent-client = { path = "../..", editable = true }
# ///
"""Command-line entry point for the agent API server.

Mirrors cli.py's shape (load env -> configure logging -> run) but starts
the HTTP API instead of the interactive loop. The PEP 723 block above
lets uv provision fastapi/uvicorn plus this package in an isolated
environment without touching pyproject.toml:

    cd agent-client && uv run src/agent_client/cli_api.py

Configuration: .env_api is the complete template for the whole framework
(Azure OpenAI, agent behavior, MCP connection) plus the API settings:
    AGENT_API_AUTH   -- static (default) or none
    AGENT_API_TOKEN  -- shared bearer token, required in static mode
    AGENT_API_HOST   -- bind address (default 127.0.0.1)
    AGENT_API_PORT   -- port (default 8080)
Real secrets belong in the gitignored .env, which takes precedence.
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# load the real (gitignored) .env first, then the committed .env_api
# template -- load_dotenv never overrides variables that are already
# set, so real values always win over template placeholders
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env")
load_dotenv(_PROJECT_ROOT / ".env_api")

# -- Logging --
LOG_LEVEL = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logging.getLogger("agent_client").setLevel(LOG_LEVEL)
logger = logging.getLogger("agent_client.cli_api")


def main():
    """Entry point for the agent API server."""
    import uvicorn

    from agent_client.agent_api import create_app

    host = os.environ.get("AGENT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_API_PORT", "8080"))

    logger.info("Starting Access Governance Agent API on %s:%d", host, port)
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
