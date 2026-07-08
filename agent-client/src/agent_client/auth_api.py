"""Authentication for the agent API.

Pluggable authenticator model: the API layer depends only on the
Authenticator protocol, so swapping the static shared token for Azure
Entra OAuth2 (JWT validation) later is a configuration change, not an
endpoint change. Clients always send Authorization: Bearer <token>.

Modes (selected via AGENT_API_AUTH):
  static -- default; compares against the AGENT_API_TOKEN env var
  none   -- accepts every caller; explicit local-dev opt-out only
  entra  -- reserved for the future Azure Entra JWT/JWKS validator

Never log token values.
"""

import os
import secrets
from dataclasses import dataclass, field
from typing import Protocol


class AuthError(Exception):
    """Raised when a presented credential is missing or invalid."""


@dataclass
class Principal:
    """The authenticated caller.

    subject is a stable identifier for the caller. claims is empty in
    static mode; once Entra is plugged in it carries the token claims
    so sessions can later be bound to a real user identity.
    """
    subject: str
    claims: dict = field(default_factory=dict)


class Authenticator(Protocol):
    """Validates a bearer token and returns the caller's Principal."""

    def authenticate(self, token: str) -> Principal:
        """Return a Principal for a valid token, raise AuthError otherwise."""
        ...


class StaticTokenAuthenticator:
    """Compares the presented bearer token against a single shared secret."""

    def __init__(self, expected_token: str):
        if not expected_token:
            raise ValueError("expected_token must be a non-empty string")
        self._expected = expected_token

    def authenticate(self, token: str) -> Principal:
        # constant-time compare so the token can't be probed byte by byte
        if not token or not secrets.compare_digest(token, self._expected):
            raise AuthError("Invalid or missing bearer token.")
        return Principal(subject="static-client")


class NoAuthAuthenticator:
    """Accepts any caller. Local development only (AGENT_API_AUTH=none)."""

    def authenticate(self, token: str) -> Principal:
        return Principal(subject="anonymous")


def build_authenticator() -> Authenticator:
    """Build the authenticator selected by the AGENT_API_AUTH env var.

    Fails closed: static mode without AGENT_API_TOKEN set refuses to
    start rather than silently running unauthenticated. Running open
    requires an explicit AGENT_API_AUTH=none.
    """
    mode = os.environ.get("AGENT_API_AUTH", "static").lower()

    if mode == "static":
        token = os.environ.get("AGENT_API_TOKEN", "")
        if not token:
            raise RuntimeError(
                "AGENT_API_AUTH=static requires AGENT_API_TOKEN to be set. "
                "Set a token, or explicitly opt out with AGENT_API_AUTH=none."
            )
        return StaticTokenAuthenticator(token)

    if mode == "none":
        return NoAuthAuthenticator()

    raise RuntimeError(
        f"Unsupported AGENT_API_AUTH={mode!r}. Use 'static' or 'none'. "
        "('entra' is reserved for the future Azure Entra validator.)"
    )
