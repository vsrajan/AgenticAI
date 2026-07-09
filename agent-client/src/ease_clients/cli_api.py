"""Command-line entry point for the agent API server.

Mirrors cli.py's shape (load env -> configure logging -> run) but starts
the HTTP API instead of the interactive loop.

Run with:

    cd agent-client
    uv run agent-api

For a full beginner-oriented guide to the API, see docs/agent_api.md.

Configuration: everything is read from the single gitignored .env file
(same file the CLI uses). .env.example is the complete template
covering Azure OpenAI, agent behavior, MCP connection, and the API
settings:
    AGENT_API_AUTH         -- static (default) or none
    AGENT_API_TOKEN        -- shared bearer token, required in static mode
    AGENT_API_HOST         -- bind address (default 127.0.0.1)
    AGENT_API_PORT         -- port (default 8080)
    AGENT_API_CORS_ORIGINS -- CORS allowlist for browser clients (default *)
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# all configuration comes from the single gitignored .env file
# (project root = agent-client/), same as cli.py
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# -- Logging --
LOG_LEVEL = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logging.getLogger("ease_clients").setLevel(LOG_LEVEL)
logger = logging.getLogger("ease_clients.cli_api")


def main():
    """Entry point for the agent API server."""
    # imports live inside main (same pattern as cli.py) so that the
    # .env loading and logging setup at module level above have already
    # run before any agent code executes
    import uvicorn

    from ease_clients.agent_api import create_app

    host = os.environ.get("AGENT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_API_PORT", "8080"))

    logger.info("Starting Access Governance Agent API on %s:%d", host, port)
    # FastAPI only describes the app -- uvicorn is the actual web
    # server. This call listens on host:port and hands every incoming
    # HTTP request to the app; it blocks until the process is stopped
    # (ctrl-c). Startup work (MCP connection, auth config) runs inside
    # create_app's lifespan handler on the first lines of serving.
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
