"""Command-line entry point for the Access Governance agent."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load environment variables from .env file (project root = agent-client/)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("AGENT_LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logging.getLogger("ease_clients").setLevel(LOG_LEVEL)
logger = logging.getLogger("ease_clients")


def main():
    """Entry point invoked by ``agent-client`` console script."""
    from ease_clients.utils.agnes_agent_graph import run_agent_loop

    logger.info("Starting Access Governance Agent")

    try:
        asyncio.run(run_agent_loop())
    except KeyboardInterrupt:
        print("\nGoodbye!")


if __name__ == "__main__":
    main()
