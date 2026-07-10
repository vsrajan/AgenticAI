"""Authentication for the MCP server.

Agents connecting over HTTP (sse / streamable-http) must present a
bearer token. Tokens are static and named per agent deployment:

    MCP_AUTH_TOKENS=agnes:tok_abc123,hr-bot:tok_xyz789

The client only ever sends the token (Authorization: Bearer <token>);
the name is a server-side label derived by lookup, so identity comes
from possession of the secret and cannot be asserted by the caller.
The name shows up in tool-call audit logs and makes tokens revocable
per agent.

Plugs into the MCP SDK's TokenVerifier hook, which is the same slot a
future OAuth2/Entra JWT validator uses -- swapping the verifier is a
config change, not an endpoint change (mirrors auth_api.py on the
agent side).

Modes (selected via MCP_AUTH):
  static -- default; requires MCP_AUTH_TOKENS, fails closed without it
  none   -- accepts every caller; explicit local-dev opt-out only

stdio transport has no HTTP layer, so auth does not apply there (the
server is a child process of whoever already has local access).

Never log token values.
"""

import logging
import os
import secrets

from mcp.server.auth.provider import AccessToken, TokenVerifier

logger = logging.getLogger("mcp_docs_server.auth")


class StaticTokenVerifier(TokenVerifier):
    """Verifies bearer tokens against a static name:token mapping."""

    def __init__(self, tokens_by_agent: dict[str, str]):
        if not tokens_by_agent:
            raise ValueError("tokens_by_agent must not be empty")
        self._tokens_by_agent = tokens_by_agent

    async def verify_token(self, token: str) -> AccessToken | None:
        # compare against every configured token, constant-time each
        # (secrets.compare_digest), and do not stop at the first match
        # -- response timing then reveals nothing about which tokens
        # exist or where in the list they sit
        matched_agent = None
        for agent_name, expected in self._tokens_by_agent.items():
            if secrets.compare_digest(token, expected):
                matched_agent = agent_name
        if matched_agent is None:
            return None
        # client_id carries the agent's name into tool-call audit logs
        return AccessToken(token=token, client_id=matched_agent, scopes=[])


def parse_agent_tokens(raw: str) -> dict[str, str]:
    """Parse MCP_AUTH_TOKENS: comma-separated name:token pairs.

    Input:  "agnes:tok1, hr-bot:tok2"
    Output: {"agnes": "tok1", "hr-bot": "tok2"}
    Raises ValueError on malformed or duplicate entries.
    """
    tokens: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, token = pair.partition(":")
        name, token = name.strip(), token.strip()
        if not sep or not name or not token:
            raise ValueError(
                f"Malformed MCP_AUTH_TOKENS entry {pair!r}: expected name:token"
            )
        if name in tokens:
            raise ValueError(f"Duplicate agent name in MCP_AUTH_TOKENS: {name!r}")
        tokens[name] = token
    if not tokens:
        raise ValueError("MCP_AUTH_TOKENS contains no name:token pairs")
    return tokens


def build_token_verifier() -> StaticTokenVerifier | None:
    """Build the verifier selected by the MCP_AUTH env var.

    Returns None when auth is explicitly disabled (MCP_AUTH=none).
    Fails closed: static mode without MCP_AUTH_TOKENS refuses to start
    rather than silently running an open server.
    """
    mode = os.environ.get("MCP_AUTH", "static").lower()

    if mode == "none":
        logger.warning("MCP auth is DISABLED (MCP_AUTH=none) -- local development only")
        return None

    if mode == "static":
        raw = os.environ.get("MCP_AUTH_TOKENS", "")
        if not raw:
            raise RuntimeError(
                "MCP_AUTH=static requires MCP_AUTH_TOKENS (comma-separated "
                "name:token pairs, one per agent deployment). Set it, or "
                "explicitly opt out with MCP_AUTH=none."
            )
        tokens = parse_agent_tokens(raw)
        logger.info(
            "MCP auth: static bearer tokens for %d agent(s): %s",
            len(tokens), ", ".join(sorted(tokens)),
        )
        return StaticTokenVerifier(tokens)

    raise RuntimeError(f"Unsupported MCP_AUTH={mode!r}. Use 'static' or 'none'.")
