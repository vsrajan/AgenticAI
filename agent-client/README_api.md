# Agent API

HTTP API wrapping the Access Governance agent so external clients (web
UI, Teams bot, Slack, another agent) can use it. Purely additive layer:
no existing file was modified -- all API code lives in new `_api` files.

```
agent_api.py   -- AgentService core (event-stream wrapper around the
                  LangGraph graph) + FastAPI app and endpoints
auth_api.py    -- pluggable authentication (static bearer token now,
                  Azure Entra OAuth2 later)
cli_api.py     -- server entry point (PEP 723 script, runs uvicorn)
.env_api       -- configuration template (committed, placeholders only)
pyproject_api.toml -- merge-ready manifest; see the header comment
```

The existing CLI (`uv run agent-client`), scanner, and MCP server are
untouched and work exactly as before.

## Run

The MCP server must be running first (see the repository README). Then:

```bash
cd agent-client

# set a token (or put AGENT_API_TOKEN in the gitignored .env)
export AGENT_API_TOKEN=your-secret-token

uv run src/agent_client/cli_api.py
```

The PEP 723 header in cli_api.py makes uv provision fastapi and uvicorn
in an isolated environment -- pyproject.toml is not modified.

Configuration (all optional except the token):

| Variable | Default | Meaning |
|----------|---------|---------|
| `AGENT_API_AUTH` | `static` | `static` = shared bearer token, `none` = no auth (local dev only) |
| `AGENT_API_TOKEN` | unset | required in static mode; server refuses to start without it |
| `AGENT_API_HOST` | `127.0.0.1` | bind address |
| `AGENT_API_PORT` | `8080` | port |

The usual agent variables (`AZURE_OPENAI_*`, `MCP_SERVER_URL` /
`MCP_TRANSPORT`) are read from `.env` exactly like the CLI.

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
