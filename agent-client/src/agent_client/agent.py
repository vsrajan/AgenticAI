"""LangGraph ReAct agent wired to the MCP docs server.

The agent connects to the MCP server over stdio, loads the three
documentation tools (list_topics, search_docs, read_page), and wraps
them in a ReAct loop powered by AzureOpenAI.
"""

import logging
import os
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

from agent_client.llm import get_llm

logger = logging.getLogger("agent_client.agent")

# ---------------------------------------------------------------------------
# MCP server command — resolve path relative to this repo
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_MCP_SERVER_DIR = _REPO_ROOT / "mcp-server"

SYSTEM_PROMPT = (
    "You are an Access Governance assistant. You help users understand and "
    "navigate the Access Governance application by consulting its documentation.\n\n"
    "You have access to three documentation tools:\n"
    "- list_topics: shows available documentation topics\n"
    "- search_docs: searches documentation with a query\n"
    "- read_page: reads the full content of a documentation page\n\n"
    "Guidelines:\n"
    "1. Always search the documentation before answering a question.\n"
    "2. Cite your sources — include the document name and page number "
    "(e.g. 'Source: ordering_faq.pdf, Page 2').\n"
    "3. If the documentation does not cover the user's question, say so clearly.\n"
    "4. Be concise but thorough.\n"
)


def _get_mcp_server_config() -> dict:
    """Build the MCP server connection config."""
    # Allow overriding the server command via env vars
    command = os.environ.get("MCP_SERVER_COMMAND", "uv")
    args_str = os.environ.get(
        "MCP_SERVER_ARGS",
        f"run --directory {_MCP_SERVER_DIR} mcp-docs-server",
    )
    args = args_str.split()

    return {
        "access-governance-docs": {
            "command": command,
            "args": args,
            "transport": "stdio",
        }
    }


async def run_agent_loop(on_response=None):
    """Run the interactive agent loop.

    Args:
        on_response: Optional callback ``(str) -> None`` called with each
            assistant response.  Defaults to ``print``.
    """
    if on_response is None:
        on_response = print

    llm = get_llm()
    mcp_config = _get_mcp_server_config()

    logger.info("Connecting to MCP server: %s", mcp_config)

    async with MultiServerMCPClient(mcp_config) as client:
        tools = client.get_tools()
        logger.info("Loaded %d MCP tools", len(tools))

        agent = create_react_agent(llm, tools, prompt=SYSTEM_PROMPT)

        print("\nAccess Governance Assistant")
        print("=" * 40)
        print('Type your question below. Type "exit" to quit.\n')

        while True:
            try:
                user_input = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye!")
                break

            if not user_input:
                continue
            if user_input.lower() == "exit":
                print("Goodbye!")
                break

            logger.info("User query: %s", user_input)

            try:
                response = await agent.ainvoke(
                    {"messages": [HumanMessage(content=user_input)]}
                )
                # The last message is the assistant's final answer
                answer = response["messages"][-1].content
                logger.debug("Agent response: %s", answer)
                on_response(f"\nAssistant: {answer}\n")
            except Exception:
                logger.exception("Error processing query")
                on_response("\nAssistant: Sorry, an error occurred while processing your question. Please try again.\n")
