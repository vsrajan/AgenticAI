"""Tests for the MCP server's static token auth (auth.py).

Run:
    cd mcp-server
    uv run --with pytest pytest tests/ -q
"""

import asyncio

import pytest

from mcp_docs_server.auth import (
    StaticTokenVerifier,
    build_token_verifier,
    parse_agent_tokens,
)


# -- parse_agent_tokens --

def test_parse_pairs():
    assert parse_agent_tokens("agnes:tok1, hr-bot:tok2") == {
        "agnes": "tok1",
        "hr-bot": "tok2",
    }


@pytest.mark.parametrize("raw", ["justatoken", "name:", ":tok", "", " , "])
def test_parse_rejects_malformed(raw):
    with pytest.raises(ValueError):
        parse_agent_tokens(raw)


def test_parse_rejects_duplicate_names():
    with pytest.raises(ValueError):
        parse_agent_tokens("agnes:tok1,agnes:tok2")


# -- StaticTokenVerifier --

def test_verify_token_identifies_the_agent():
    verifier = StaticTokenVerifier({"agnes": "tok1", "hr-bot": "tok2"})
    access_token = asyncio.run(verifier.verify_token("tok2"))
    assert access_token is not None
    assert access_token.client_id == "hr-bot"


@pytest.mark.parametrize("bad", ["wrong", "", "tok1x"])
def test_verify_token_rejects_unknown_tokens(bad):
    verifier = StaticTokenVerifier({"agnes": "tok1"})
    assert asyncio.run(verifier.verify_token(bad)) is None


# -- build_token_verifier --

def test_build_fails_closed_without_tokens(monkeypatch):
    monkeypatch.setenv("MCP_AUTH", "static")
    monkeypatch.delenv("MCP_AUTH_TOKENS", raising=False)
    with pytest.raises(RuntimeError):
        build_token_verifier()


def test_build_modes(monkeypatch):
    monkeypatch.setenv("MCP_AUTH", "static")
    monkeypatch.setenv("MCP_AUTH_TOKENS", "agnes:tok1")
    assert isinstance(build_token_verifier(), StaticTokenVerifier)

    monkeypatch.setenv("MCP_AUTH", "none")
    assert build_token_verifier() is None

    monkeypatch.setenv("MCP_AUTH", "oauth")
    with pytest.raises(RuntimeError):
        build_token_verifier()
