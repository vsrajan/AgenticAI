# Agent API

HTTP API wrapping the Access Governance agent so external clients (web
UI, Teams bot, Slack, another agent) can use it. Purely additive layer:
no existing file was modified -- all API code lives in new `_api` files.

```
agent_api.py   -- AgentService core (event-stream wrapper around the
                  LangGraph graph) + FastAPI app and endpoints
auth_api.py    -- pluggable authentication (static bearer token now,
                  Azure Entra OAuth2 later)
cli_api.py     -- server entry point (runs uvicorn)
webclient_api.html -- POC single-page web client (streaming); open it
                  directly in a browser
.env_api       -- configuration TEMPLATE (never read by code); copy to
                  the single .env file before starting
pyproject_api.toml -- complete manifest for the API (superset of
                  pyproject.toml); activate by copying over pyproject.toml
```

The existing CLI (`uv run agent-client`), scanner, and MCP server are
untouched and work exactly as before.

## Run

The MCP server must be running first (see the repository README). Then
activate the API manifest and configuration, and start the server:

```bash
cd agent-client

# activate the API manifest (a superset of pyproject.toml, so the CLI
# and scanner keep working from the same environment)
cp pyproject_api.toml pyproject.toml
uv sync

# create the single .env config from the template, then edit it with
# real values (Azure key, AGENT_API_TOKEN, ...)
cp .env_api .env

uv run agent-api
```

To go back to the original manifest: `git checkout pyproject.toml`
(and `uv sync` again).

## Configuration

ALL configuration is read from the single gitignored `.env` file -- the
same file the CLI uses. The code reads nothing else (no shell exports
required, no second env file).

`.env_api` is the template for that file: it lists every variable the
whole framework needs (a superset of `.env.example`) plus the
API-specific ones, with placeholder values. Copy it to `.env` (or copy
the values you need into your existing `.env`) and fill in real values.
The template is committed to git, so it never contains real secrets.

Framework variables (same meaning as in `.env.example`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `AZURE_OPENAI_API_KEY` | -- | Azure OpenAI API key (secret) |
| `AZURE_OPENAI_ENDPOINT` | -- | e.g. `https://<resource>.openai.azure.com/` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-4o` | deployment name |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | API version |
| `AGENT_LOG_LEVEL` | `INFO` | agent logging level |
| `KEEP_LAST_N_MSGS` | `20` | context window in messages (0 = unlimited) |
| `MAX_TOOL_CONTENT_LEN` | `80000` | max characters per tool response (0 = unlimited) |
| `MCP_SERVER_NAME` | `access-governance-docs` | must match the MCP server |
| `MCP_TRANSPORT` | `sse` | `sse` or `stdio` |
| `MCP_SERVER_URL` | `http://127.0.0.1:8000/sse` | SSE transport URL |
| `MCP_SERVER_COMMAND` / `MCP_SERVER_ARGS` | unset | stdio transport command |

API variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `AGENT_API_AUTH` | `static` | `static` = shared bearer token, `none` = no auth (local dev only) |
| `AGENT_API_TOKEN` | unset | required in static mode; server refuses to start without it |
| `AGENT_API_HOST` | `127.0.0.1` | bind address |
| `AGENT_API_PORT` | `8080` | port |
| `AGENT_API_CORS_ORIGINS` | `*` | comma-separated origins browsers may call from; tighten for deployments |

## POC web client

`webclient_api.html` is a self-contained single-page client using the
streaming endpoint. With the API server running, open the file directly
in a browser and pass the token in the url (or edit the constant at the
top of its script):

```
file:///path/to/agent-client/webclient_api.html?token=your-secret-token
```

It creates a session on the first message, then streams answers
token-by-token with live phase updates (Routing, Calling tools, ...).

The MCP server process has its own settings (`MCP_DOCS_DIR`, `MCP_HOST`,
`MCP_PORT`, `MCP_LOG_LEVEL`) read from `mcp-server/.env` -- see
`mcp-server/.env.example`. They are listed as a commented reference
section at the bottom of `.env_api`.

## Authentication

Every endpoint except `/health` requires:

```
Authorization: Bearer <token>
```

Static mode compares the token against `AGENT_API_TOKEN` using a
constant-time comparison. Failures return `401` with
`WWW-Authenticate: Bearer`.

The Bearer scheme is deliberate: Azure Entra OAuth2 access tokens use
the same header, so the planned Entra migration (an `entra` mode in
`build_authenticator` validating JWTs against the tenant's JWKS) changes
how tokens are obtained, not how clients send them. The `Principal`
returned by the authenticator will then carry the user's identity so
sessions can be bound to their owner.

## Endpoints

### GET /health (no auth)

```bash
curl http://127.0.0.1:8080/health
# {"status": "ok", "tools": 12}
```

### POST /sessions

Creates a conversation session. The id maps onto a LangGraph thread, so
follow-up messages in the same session keep conversation history.
Sessions live in process memory (MemorySaver) and are lost on restart.

```bash
curl -X POST http://127.0.0.1:8080/sessions \
  -H "Authorization: Bearer $AGENT_API_TOKEN"
# {"session_id": "3f2a..."}
```

### POST /sessions/{id}/messages

Buffered request/response -- for clients that cannot stream (bots).

```bash
curl -X POST http://127.0.0.1:8080/sessions/3f2a.../messages \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "How do I set up delegations?"}'
# {"answer": "..."}
```

### POST /sessions/{id}/messages/stream

Server-sent events for live UIs. Events arrive as:

```
event: phase
data: {"data": "Routing"}

event: token
data: {"data": "To set"}

event: answer
data: {"data": "<the complete answer text>"}
```

- `phase` -- the graph moved to a new node (routing, calling tools, ...)
- `token` -- one streamed answer token
- `answer` -- always the last event, carries the full answer text
- `error` -- emitted in-band if something fails mid-stream

```bash
curl -N -X POST http://127.0.0.1:8080/sessions/3f2a.../messages/stream \
  -H "Authorization: Bearer $AGENT_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "What access do finance analysts have?"}'
```

## Tests

```bash
cd agent-client
uv run --with pytest --with fastapi --with uvicorn --with httpx \
  pytest tests_api/ -q
```

The tests use a fake agent (no Azure OpenAI or MCP server needed) and
cover the AgentService event stream, all endpoints, and the auth matrix.
