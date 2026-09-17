# Entra ID authentication -- implementation guide

This guide describes how to replace the static bearer tokens with
Microsoft Entra ID (formerly Azure AD) on both authentication legs of
the system, written for a reader who knows some Python but has not
worked with OAuth2 or Entra before. It explains the concepts, walks
through every design choice (with recommendations and the reasoning),
and points at the exact hooks in the codebase where each change lands.

The two legs are different problems and are treated separately:

```
[Browser / Teams bot]  --leg 1-->  [Agent API]  --leg 2-->  [MCP server]
   a HUMAN signs in                a daemon calls
   (delegated user token)          (app-only token)
```

- **Leg 1 (Client -> Agent API):** a real person sits behind the
  browser or the Teams bot. The token should say WHO THE PERSON IS,
  not just which app is calling -- that identity is what makes
  per-user sessions, audit, and later per-user entitlement answers
  possible.
- **Leg 2 (Agent -> MCP server):** no human. The agent service is a
  long-running daemon; it authenticates as ITSELF (an application
  identity), and its token should say which agent deployment is
  calling and what it is allowed to do.

## Contents

1. [Concepts, from scratch](#1-concepts-from-scratch)
2. [Leg 1: Client to Agent API](#2-leg-1-client-to-agent-api)
3. [Bot authentication and validation, in depth](#3-bot-authentication-and-validation-in-depth)
4. [Leg 2: Agent to MCP server](#4-leg-2-agent-to-mcp-server)
5. [Rollout plan and checklist](#5-rollout-plan-and-checklist)

---

## 1. Concepts, from scratch

### What is Entra ID?

Microsoft's cloud identity provider. Your organisation has a
**tenant** (identified by a tenant id, a GUID). Users live in the
tenant; applications are registered in it. When something needs to
prove who it is, it asks Entra for a **token** and presents that token
to whoever it wants to talk to.

### App registrations

Every application involved gets an **app registration** in the tenant
-- an identity record with a **client id** (GUID). Our system needs
several:

| Registration | Represents | Kind |
|---|---|---|
| "Agnes Agent API" | the FastAPI service (leg 1's protector) | exposes an API |
| "Agnes Web Client" | the browser page | public client (SPA) |
| "Agnes Teams Bot" | the bot service | confidential client |
| "Agnes Agent" | the agent daemon (leg 2's caller) | confidential client |
| "Agnes MCP Server" | the MCP server (leg 2's protector) | exposes an API |

"Public" vs "confidential": a browser page cannot keep a secret
(anyone can View Source), so it proves nothing about itself and relies
on the USER signing in. A server-side service can hold a **client
secret** (or better, a certificate / managed identity) and can
therefore prove its own identity.

### Tokens, JWTs, and claims

An Entra access token is a **JWT** (JSON Web Token): three
base64-encoded parts, `header.payload.signature`. The payload carries
**claims** -- facts asserted by Entra:

| Claim | Meaning |
|---|---|
| `iss` | issuer -- which Entra tenant minted this token |
| `aud` | audience -- which application the token is FOR |
| `exp` | expiry (tokens live roughly 60-90 minutes) |
| `oid` | the user's or app's immutable object id in the tenant |
| `name`, `preferred_username` | human-readable user info |
| `scp` | delegated scopes -- what the USER consented the client may do |
| `roles` | app roles -- what an APPLICATION is allowed to do |
| `azp` / `appid` | which client application requested the token |

The signature is made with Entra's private key. Anyone can fetch the
matching **public keys** from the tenant's **JWKS endpoint**
(`https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys`) and
verify the signature offline. That is the whole trick: **validating a
token requires no call to Entra per request and no shared secret** --
just check the signature against the published keys, then check
`iss`, `aud`, and `exp`.

`scp` vs `roles` is the claim-level fingerprint of the two legs:
tokens for leg 1 (a user delegated to a client) carry `scp`; tokens
for leg 2 (an application acting as itself) carry `roles`.

### The flows we will use

A common misconception to clear up front: the flow is NOT chosen by
whether a human is present. It is chosen by **where the OAuth client
code runs** and what that code is capable of:

| Where does the OAuth client run? | Can it drive a browser redirect? | Flow |
|---|---|---|
| The user's own browser | yes | Authorization Code + PKCE |
| A server, acting for a user who authenticated one hop upstream | no | On-Behalf-Of (token exchange), fed by Teams SSO |
| A server, acting as itself (no user) | no | Client Credentials |

Applied to our clients:

| Flow | Used by | Human present? |
|---|---|---|
| Authorization Code + PKCE | browser web client | yes -- Microsoft sign-in page |
| Teams SSO / On-Behalf-Of | Teams bot | yes -- but the bot is a SERVER; see section 3 |
| Client Credentials | agent daemon -> MCP | no -- secret proves app identity |

The browser and the Teams bot pursue the same GOAL (a delegated user
token reaching the Agent API) through different flows because their
topology differs: the browser talks to the API directly, while the
bot sits one hop away from the user. Section 3 unpacks this fully.

You do not need to memorise the protocol internals: Microsoft's MSAL
libraries (`msal-browser` for JavaScript, `msal` for Python) implement
the flows. Our own code only ever does two things: **send a token**
(clients) or **validate a token** (servers).

One naming trap to avoid early: **MSAL acquires tokens; it does not
validate them.** For validation we use `PyJWT` -- a common beginner
mistake is reaching for MSAL on the server side.

---

## 2. Leg 1: Client to Agent API

### 2.1 The target picture

```
Browser                    Entra ID                  Agent API
  |  1. sign-in popup ------->  |                        |
  |  <-- 2. access token ------ |                        |
  |  3. POST /sessions, Authorization: Bearer <jwt> ---> |
  |                             |    4. validate JWT     |
  |                             |       (JWKS, offline)  |
  |  <-- 5. session for THIS user ---------------------- |
```

The crucial architectural fact: **the Agent API becomes a pure
"resource server"**. It never shows a login page, never redirects,
never stores passwords, never holds an OAuth secret. It receives JWTs
and validates them -- that is all. All sign-in complexity lives in the
client (where MSAL does it for us) and in Entra.

This is why the migration is small: the API already speaks
`Authorization: Bearer` on every endpoint, so the ONLY server-side
change is what the validator does with the string after "Bearer".

### 2.2 What exists today (the hooks)

The static-token design reserved every seam this needs:

| Hook | Where | What it gives us |
|---|---|---|
| `Authenticator` protocol: `authenticate(token) -> Principal` | `auth_api.py` | the interface the new validator implements |
| `Principal(subject, claims)` | `auth_api.py` | `claims` has been an empty dict waiting for exactly this |
| `build_authenticator()` with `entra` mode reserved (currently raises) | `auth_api.py` | the config switch |
| `current_principal` FastAPI dependency | `agent_api.py` | already runs before every endpoint and yields the Principal |
| Bearer header transport | web client, curl examples | unchanged |

### 2.3 Design choices, walked through

**Choice 1: app-only or user tokens?** Since a human sits behind the
browser and the Teams bot, use **delegated user tokens**. An app-only
token would tell you "the web client called" -- true and useless. A
user token tells you "priya@yourco.com called", which is what audit
needs and what unlocks per-user behavior later ("what access do *I*
have?" answered from the caller's own identity instead of asking them
who they are). Recommendation: delegated tokens for leg 1, no
exceptions -- a client that has no human (a cron script) should talk
to the API with its own app registration and client credentials, and
you will see `roles` instead of `scp` in its token and can decide
whether to allow that separately.

**Choice 2: who validates, and with what library?** The API validates
locally with **`PyJWT[crypto]`** (`pyjwt` plus the RSA crypto extras)
and its `PyJWKClient`, which fetches and caches the tenant's public
keys and picks the right key by the token's `kid` header
automatically (this also handles Microsoft's routine key rotation).
Alternatives considered: `python-jose` (fine, less maintained), a
hand-rolled validator (never), MSAL (wrong tool -- acquisition only).

**Choice 3: which claim becomes `Principal.subject`?** Use **`oid`**
(the user's immutable object id in the tenant), falling back to `sub`.
`oid` is stable across all apps in the tenant; `sub` is stable only
per-app. Email-like claims (`preferred_username`) can change when
people marry or change names -- display them, never key on them.

**Choice 4: enforce a scope?** Yes. Define one scope on the API's app
registration -- `access_as_user` -- and require it in the validator
(`scp` must contain it). Without this check, ANY token minted for
your API's audience passes, even one a different client obtained for
some other purpose. With it, only clients you granted the permission
to can produce acceptable tokens.

**Choice 5: what happens to sessions?** Today any valid caller can use
any session id. With real identity, bind sessions to their owner:
`create_session()` records `principal.subject`; every later access
checks that the caller's subject matches. Design detail worth copying:
return **404, not 403**, for someone else's session -- a 403 confirms
the session exists, a 404 reveals nothing. This lands entirely in the
existing registry (`_Session` gains an `owner` field;
`_require_session` gains an `owner` argument).

**Choice 6: keep static mode?** Yes. `AGENT_API_AUTH` stays a switch
(`static` | `entra` | `none`), so tests keep running offline with the
fake authenticator, local dev keeps working without a tenant, and the
migration is a config change per environment -- the same story as
every auth decision so far.

### 2.4 Entra setup (portal work, once)

1. **Register "Agnes Agent API".** Entra admin center -> App
   registrations -> New. No redirect URI (it never signs anyone in).
   Under **Expose an API**: set the Application ID URI (accept the
   default `api://<client-id>`), then **Add a scope** named
   `access_as_user` ("Access the agent as the signed-in user", admins
   and users can consent).
2. **Register "Agnes Web Client".** Platform: Single-page application,
   redirect URI = wherever the page is served (e.g.
   `https://your-web-host/webclient_api.html`). Under **API
   permissions**: add a delegated permission to Agnes Agent API ->
   `access_as_user`, and grant admin consent.
3. Note three values: **tenant id**, the **API's client id** (this
   becomes the expected audience), the **SPA's client id** (goes into
   the web client's MSAL config).

### 2.5 Server-side changes, file by file

**`auth_api.py` -- the new authenticator** (the `entra` branch of
`build_authenticator` stops raising and returns this):

```python
import jwt                      # pyjwt[crypto] -- new dependency
from jwt import PyJWKClient

class EntraAuthenticator:
    """Validates Entra-issued JWTs. Same Authenticator protocol as
    StaticTokenAuthenticator -- endpoints do not change."""

    def __init__(self, tenant_id: str, audience: str,
                 required_scope: str = "access_as_user"):
        self._issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        self._audience = audience
        self._required_scope = required_scope
        # PyJWKClient fetches Entra's public keys once and caches them;
        # it re-fetches automatically when a token arrives signed with
        # a key id it has not seen (Microsoft rotates keys routinely)
        jwks_url = (f"https://login.microsoftonline.com/{tenant_id}"
                    "/discovery/v2.0/keys")
        self._jwks = PyJWKClient(jwks_url, cache_keys=True)

    def authenticate(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token, key,
                algorithms=["RS256"],          # never accept "none"
                audience=self._audience,       # token minted FOR US
                issuer=self._issuer,           # ... BY OUR tenant
                leeway=60,                     # 60s clock-skew tolerance
            )
        except jwt.PyJWTError as exc:
            raise AuthError(f"Token validation failed: {exc}") from exc

        scopes = claims.get("scp", "").split()
        if self._required_scope not in scopes:
            raise AuthError("Token lacks the required scope.")

        # oid: the user's immutable id in the tenant (stable across
        # apps); sub is the per-app fallback. Display names live in
        # claims for logging/UI -- never key on them.
        return Principal(subject=claims.get("oid") or claims["sub"],
                         claims=claims)
```

Config (all fail-closed, mirroring static mode):

```
AGENT_API_AUTH=entra
AGENT_API_ENTRA_TENANT_ID=<tenant guid>
AGENT_API_ENTRA_AUDIENCE=<api client id>     # or api://<client id>
```

Notes for the implementer:

- `authenticate` stays synchronous like the protocol demands; the only
  network call is PyJWKClient's occasional key fetch. If that blocking
  call bothers you under load, wrap the dependency's call in
  `asyncio.to_thread` -- do not redesign the protocol for it.
- **v1 vs v2 tokens gotcha:** Entra can mint two token formats. The
  v2.0 issuer is `login.microsoftonline.com/<tenant>/v2.0`; v1.0
  tokens have an `sts.windows.net` issuer and put `api://<id>` in
  `aud` where v2.0 puts the bare client id. Set
  `requestedAccessTokenVersion: 2` in the API app registration's
  manifest and expect v2.0 everywhere; if a v1 token appears, the
  issuer check will reject it and this is the first thing to look at.
- **The Graph-token mistake:** if a client asks MSAL for
  `User.Read` (a Microsoft Graph scope) and sends THAT token, `aud`
  will be Graph, not you, and validation fails. Clients must request
  YOUR scope: `api://<api-client-id>/access_as_user`.

**`agent_api.py` -- session ownership** (uses the existing registry):

- `_Session` gains `owner: str`
- `create_session(owner: str)` stores it; the endpoint passes
  `principal.subject`
- `_require_session(session_id, owner)` raises `UnknownSessionError`
  when the owner does not match (-> the existing 404 path; no new
  error handling needed)
- `stream/ask/get_history` grow an `owner` parameter that the
  endpoints fill from the Principal

Static mode keeps working: `StaticTokenAuthenticator` returns
`subject="static-client"`, so all static-mode sessions share one
owner -- exactly today's behavior.

### 2.6 Client-side changes

**Browser (`webclient_api.html`):** replace token-in-URL with MSAL.
The flow, using `msal-browser` (a `<script>` include):

```javascript
const msalApp = new msal.PublicClientApplication({
  auth: {
    clientId: "<spa client id>",
    authority: "https://login.microsoftonline.com/<tenant id>",
    redirectUri: window.location.origin + window.location.pathname,
  },
});
const SCOPES = ["api://<api client id>/access_as_user"];

async function getToken() {
  await msalApp.initialize();
  let account = msalApp.getAllAccounts()[0];
  if (!account) {                       // first visit: interactive sign-in
    await msalApp.loginPopup({ scopes: SCOPES });
    account = msalApp.getAllAccounts()[0];
  }
  // silent renewal from cache; MSAL handles expiry/refresh internally
  const result = await msalApp.acquireTokenSilent({ scopes: SCOPES, account });
  return result.accessToken;
}
```

Then every fetch uses `Authorization: "Bearer " + await getToken()`.
Because tokens expire in ~an hour, call `getToken()` per request (it
is cheap -- MSAL returns the cached token until renewal is needed)
instead of stashing the string once. Requirements that come with this:
the page must be served over **HTTPS** from a registered redirect URI
(the `file://` trick dies here -- this is the moment the POC client
becomes a real hosted page), and `AGENT_API_CORS_ORIGINS` should be
tightened to exactly that origin.

**Teams bot:** the bot obtains a delegated user token through Teams
SSO plus the On-Behalf-Of flow, and what reaches the Agent API is a
normal delegated user token -- same `scp`, same `oid`, so the
validator above needs NOTHING extra for Teams. Because the bot's
topology (a server one hop away from the user) makes this the least
obvious part of the design, it has its own full section: see
section 3, "Bot authentication and validation, in depth".

### 2.7 Testing leg 1 without a tenant

- Unit tests keep using `StaticTokenAuthenticator` / fakes -- the
  endpoints only see the `Principal`, which is the point of the
  protocol.
- To test `EntraAuthenticator` itself offline: generate an RSA keypair
  in the test, mint JWTs locally with `jwt.encode` (right and wrong
  audience/issuer/expiry/scope), and monkeypatch the JWKS client to
  return the test public key. That covers every rejection path with
  zero network.
- The session-ownership change is testable today with static auth by
  constructing two Principals directly.

---

## 3. Bot authentication and validation, in depth

The browser client and the Teams bot pursue the same goal -- a
delegated user token, minted by Entra, reaching the Agent API -- but
they cannot use the same flow. This section explains why, walks the
two-step mechanism the bot uses, and lists the changes (mostly
optional hardening) on the agent side.

### 3.1 Why the bot cannot use Auth Code + PKCE

The hidden assumption to discard: that the flow is chosen by whether
a human is present. It is chosen by **where the OAuth client code
runs**.

In the browser case, the code that acquires and spends the token runs
on the user's machine, inside a browser. A browser can be redirected
to `login.microsoftonline.com` and back -- which is the mechanical
requirement of Authorization Code + PKCE. The SPA therefore gets a
token for the Agent API directly. One hop.

A Teams bot is different in a way that is easy to miss: **the bot
does not run inside the user's Teams client.** It is a server-side
service in your infrastructure. When the user types a message,
Microsoft's Teams service relays it over HTTPS to the bot's endpoint.
The entity that must call the Agent API is therefore a headless
server that (a) has no browser it can redirect for a sign-in dance,
and (b) receives the user's words second-hand, through Microsoft:

```
Browser case:  user's browser -----------------------------> Agent API
               (the browser IS the OAuth client; one hop)

Teams case:    user's Teams client --> Teams service --> YOUR bot --> Agent API
               (the OAuth client is the bot server; the user's
                identity must cross the middle hop somehow)
```

Getting the user's identity across that middle hop -- verifiably, not
as a string the bot merely claims -- is what the two-step mechanism
below solves.

### 3.2 Step 1: Teams SSO -- getting a user token TO the bot

Teams SSO is **not an OAuth flow**; it is Teams-platform plumbing.
The user is already signed into Teams itself -- they completed a full
interactive Entra sign-in when they logged into Teams, and Microsoft
ran that flow, not you. Because the Teams client holds a live Entra
session for the user, it can silently ask Entra for **token A**: a
delegated user token whose audience is the BOT's own app registration
(enabled by the `webApplicationInfo` section of the Teams app
manifest). Token A carries the user's real claims -- `oid`, `name` --
signed by Entra, and is delivered to the bot server through the Bot
Framework's token machinery. No sign-in page appears because no new
authentication happened; an existing session was reused. That is all
"SSO" means here.

Token A alone is useless for calling the Agent API: its `aud` is the
bot, not the API. Presenting it to the API would (correctly) fail the
audience check in `EntraAuthenticator`.

### 3.3 Step 2: On-Behalf-Of -- exchanging token A for token B

On-Behalf-Of (OBO) IS a real, standardised OAuth grant -- Microsoft's
implementation of **OAuth 2.0 Token Exchange (RFC 8693)**, using
`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`. It exists
precisely for the multi-hop case: a middle-tier service needs to call
a downstream API as the user who called it.

The bot makes one call to Entra's token endpoint presenting TWO
credentials at once:

1. **token A** (the "assertion") -- Entra-signed proof that user X
   delegated to this bot
2. **its client secret** -- proof that the caller really is the bot
   (this is where being a confidential client matters)

Entra validates both, checks that the bot's registration holds the
delegated `access_as_user` permission on the Agent API, and mints
**token B**:

| Claim in token B | Value |
|---|---|
| `aud` | the Agent API's client id |
| `oid` | user X (the human) |
| `scp` | `access_as_user` |
| `azp` | the bot's client id (which app acted for the user) |

In MSAL Python, the whole exchange is one call on the bot side:

```python
result = msal.ConfidentialClientApplication(
    bot_client_id,
    authority=f"https://login.microsoftonline.com/{tenant_id}",
    client_credential=bot_secret,
).acquire_token_on_behalf_of(
    user_assertion=token_a,
    scopes=["api://<api client id>/access_as_user"],
)
token_b = result["access_token"]
```

### 3.4 How a confidential client conveys user identity without forging it

The question that makes or breaks understanding OBO: if the bot is a
confidential client authenticating with a secret, how do USER details
end up in the JWT it sends?

**The bot never writes a single claim.** It trades one Entra-signed
token for another Entra-signed token; ENTRA asserts the user claims
in token B, having verified them from token A. The division of labour
inside the exchange:

- the client secret authenticates THE BOT
- the assertion (token A) carries THE USER
- token B truthfully records BOTH identities: `oid` = the human,
  `azp` = which app acted for them

A malicious or buggy bot cannot claim to be a different user, because
it cannot forge token A -- it does not have Entra's signing key.

Contrast with the anti-pattern this design exists to prevent: the bot
calling the API with an app-only client-credentials token plus a
`"user": "priya"` field in the request body. That is the bot
SELF-ASSERTING user identity -- unverifiable and forgeable. If you
ever find yourself passing a username in a request body alongside an
app token, you wanted OBO.

### 3.5 What the Agent API validates

The payoff of the whole arrangement: token B is the same KIND of
token the browser SPA sends -- delegated user token, correct
audience, `scp` contains `access_as_user`, `oid` identifies the
human. Side by side, for the same user (Priya) talking to the same
API:

| | Browser SPA token | Bot token B | Same or different? |
|---|---|---|---|
| `iss` (who minted it) | `login.microsoftonline.com/<tenant>/v2.0` | `login.microsoftonline.com/<tenant>/v2.0` | same -- both minted by your tenant |
| `aud` (minted for whom) | Agent API client id | Agent API client id | same -- both FOR the API |
| `scp` (delegated scope) | `access_as_user` | `access_as_user` | same -- user-delegated in both |
| `oid` (which human) | Priya's object id | Priya's object id | same -- the SAME user identity |
| `name`, `preferred_username` | Priya's | Priya's | same |
| `roles` | absent | absent | same -- absent, because these are user tokens, not app tokens |
| `exp` / lifetime | ~60-90 min | ~60-90 min | same |
| signature | Entra's key | Entra's key | same -- validated against the same JWKS |
| **`azp` (which app acted)** | **SPA client id** | **bot client id** | **DIFFERENT -- the only claim-level difference** |
| how it was acquired | Auth Code + PKCE, user's browser talked to Entra directly | Teams SSO -> OBO exchange, the bot's server talked to Entra | different -- but acquisition history is NOT in the token |

Read the table bottom-up and the design lesson falls out: the entire
difference between "Priya via the web page" and "Priya via the Teams
bot" collapses into one claim, `azp`. Everything the validator checks
-- signature, `iss`, `aud`, `exp`, `scp` -- is identical, which is
WHY `EntraAuthenticator` (section 2.5) needs zero Teams-specific
code. And `azp` is exactly the claim the optional hardening in
section 3.6 uses: the allowed-clients allowlist keys on it, and the
audit log surfaces it.

The full chain:

```
user --> Teams client --(SSO: token A, aud=bot)--> bot
bot  --> Entra: OBO(assertion=token A + bot secret)
Entra--> bot: token B (aud=API, oid=user, scp=access_as_user, azp=bot)
bot  --> Agent API: Authorization: Bearer <token B>
API  --> validate signature/iss/aud/exp/scp -> Principal(subject=user oid)
```

### 3.6 Agent-side code changes

**Required for correctness: none.** That is the headline -- the leg-1
work in section 2 already handles bot traffic, because token B is a
standard delegated token. The items below are hardening and
observability, all landing in existing hooks.

**1. Optional: an allowed-clients allowlist (`azp` check).** Today any
client application in the tenant that a user has consented to can
mint tokens for the API's audience. If you want only YOUR clients
(the SPA and the bot) to be accepted, check `azp` in
`EntraAuthenticator.authenticate`, right after the scope check:

```python
# in __init__, from AGENT_API_ENTRA_ALLOWED_CLIENTS (comma-separated
# client ids; empty = any client app in the tenant is acceptable):
self._allowed_clients = set(allowed) if allowed else None

# in authenticate(), after the scope check:
if self._allowed_clients is not None:
    if claims.get("azp") not in self._allowed_clients:
        raise AuthError("Token presented by an unapproved client application.")
```

Config: `AGENT_API_ENTRA_ALLOWED_CLIENTS=<spa client id>,<bot client id>`.
Recommendation: enable it -- it is two lines, and it turns "any app a
user trusts" into "only apps YOU trust".

**2. Optional: audit which app brought the user.** `azp` is already in
`Principal.claims` -- surface it where sessions are created, in the
existing log line in the `create_session` endpoint (`agent_api.py`):

```python
logger.info("Created session %s for %s via client %s",
            session_id, principal.subject,
            principal.claims.get("azp", "n/a"))
```

Log lines then distinguish "priya via the web client" from "priya via
the Teams bot" -- useful when diagnosing which frontend misbehaves.

**3. Usage pattern the bot should follow (no API change needed):**

- bots cannot render a token stream, so the bot uses the buffered
  endpoint `POST /sessions/{id}/messages` -- which exists for exactly
  this reason
- one API session per Teams conversation: the bot keeps a
  conversation-id -> session-id map, and recreates the session on a
  404 (TTL eviction), same as the web client does
- session ownership (section 2.5) already isolates users correctly,
  because each user's messages arrive under their own `oid` -- two
  users in Teams never share a session even if the bot mixes up its
  map, they just get a 404

**4. Bot-side responsibilities (for completeness -- not agent code):**
the Teams app manifest needs the `webApplicationInfo` section for
SSO; the bot registration needs the delegated `access_as_user`
permission on the API (admin-consented) plus its client secret; and
the bot service calls `acquire_token_on_behalf_of` (section 3.3) and
caches the result per user (MSAL's token cache handles renewal).

## 4. Leg 2: Agent to MCP server

### 4.1 Design choices, walked through

**Choice 1: whose identity does the agent present?** Two options:

- **Client credentials (recommended first):** the agent daemon has its
  own app registration and secret; it acquires an app-only token
  ("I am Agnes-production"). Simple, robust, no human involved --
  matches the daemon's nature. The MCP audit line stays
  `caller=agnes`.
- **On-Behalf-Of chaining (later, optional):** the agent exchanges the
  USER's leg-1 token for an MCP token, so the MCP server sees
  `caller=priya@yourco.com via agnes`. Strictly better audit, but it
  couples the legs (every MCP call needs a live user token, which
  breaks the scanner and any background work) and multiplies the
  moving parts. Do client credentials now; OBO is an additive upgrade
  for the interactive path only.

**Choice 2: how does authorization work?** This is where Entra beats
the static list. Define **app roles** on the MCP server's registration
(e.g. `Tools.Knowledgebase`, `Tools.Resource`, `Tools.Quality`,
`Tools.Request`), assign roles to each agent's registration, and they
arrive in the token's `roles` claim. The verifier copies them into
`AccessToken.scopes` -- a field that exists today and is empty. Then a
small check in each tool group enforces it. Result: the scanner's
registration gets only `Tools.Knowledgebase` and physically cannot
call `raise_entitlement_request`.

**Choice 3: token refresh on the client.** Static tokens never expire;
Entra tokens die in ~an hour, and the agent holds long-lived
connections. A static `headers` dict is therefore NOT enough. The
right hook exists: `SSEConnection` accepts an **`auth: httpx.Auth`**
object, which httpx consults on every request -- so a tiny
`httpx.Auth` subclass that asks MSAL for a token each time (MSAL
caches it and silently renews near expiry) makes refresh automatic.

### 4.2 What exists today (the hooks)

| Hook | Where | What it gives us |
|---|---|---|
| `TokenVerifier.verify_token(token) -> AccessToken \| None` | `mcp-server/auth.py` | the slot `StaticTokenVerifier` fills; the Entra verifier is a drop-in sibling |
| `build_token_verifier()` mode switch | `mcp-server/auth.py` | grows an `entra` branch |
| `AccessToken.scopes` | already returned (empty) | carries the app roles |
| `_caller()` + caller-tagged tool logs | `server.py` | audit keeps working unchanged (`client_id` now from the token claim) |
| `auth: httpx.Auth` field | `SSEConnection` (client) | per-request token injection with refresh |
| `_get_mcp_server_config()` | `agnes_agent_graph.py` | the single place all three entry points build the connection |

### 4.3 Entra setup

1. **Register "Agnes MCP Server".** Expose an API (Application ID URI
   `api://<mcp client id>`). Under **App roles**: create roles with
   "Applications" as the allowed member type -- `Tools.Knowledgebase`,
   `Tools.Resource`, `Tools.Quality`, `Tools.Request` (or a coarse
   `Tools.All` to start).
2. **Register "Agnes Agent"** (one per agent deployment -- this
   replaces the name in `MCP_AUTH_TOKENS`). Create a **client secret**
   (or use a managed identity if the agent runs on Azure -- no secret
   to store at all). Under **API permissions**: add APPLICATION
   permissions to Agnes MCP Server (the roles above), grant admin
   consent.

### 4.4 Server-side changes (mcp-server)

**`auth.py` -- `EntraTokenVerifier`**, same JWT validation as leg 1
but async (the protocol is async) and reading app-token claims:

```python
class EntraTokenVerifier(TokenVerifier):
    """Validates app-only Entra JWTs. Drop-in for StaticTokenVerifier."""

    def __init__(self, tenant_id: str, audience: str):
        ...same issuer/audience/PyJWKClient setup as leg 1...

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"],
                                audience=self._audience,
                                issuer=self._issuer, leeway=60)
        except jwt.PyJWTError:
            return None                      # protocol: None = reject
        return AccessToken(
            token=token,
            # azp (v2) / appid (v1): which application called. Map to a
            # display name if you want prettier logs; the GUID is the
            # truthful identity either way.
            client_id=claims.get("azp") or claims.get("appid", "unknown"),
            scopes=claims.get("roles", []),  # app roles -> scopes
        )
```

Config: `MCP_AUTH=entra`, `MCP_ENTRA_TENANT_ID`, `MCP_ENTRA_AUDIENCE`.
Fail-closed like everything else. Note the leg-1 validator is ~the
same 40 lines; the two packages deploy separately, so duplicate it
rather than inventing a shared library for one function.

**Per-tool-group authorization** -- a tiny helper next to `_caller()`
in `server.py`:

```python
def _require_role(role: str) -> None:
    """Reject the call unless the agent's token carries the app role.
    No-op when auth is off (static mode grants everything, as today)."""
    access_token = get_access_token()
    if access_token is None or not access_token.scopes:
        return                        # auth off, or static mode: allow
    if role not in access_token.scopes and "Tools.All" not in access_token.scopes:
        raise PermissionError(f"caller lacks role {role}")
```

then one line at the top of each tool: `_require_role("Tools.Knowledgebase")`
in the three doc tools, `Tools.Resource` in the dataset tools, and so
on. (Design note: the MCP SDK's `AuthSettings.required_scopes` could
enforce ONE scope globally, but per-group enforcement needs these
in-tool checks -- that is why the helper exists.)

### 4.5 Client-side changes (agent-client)

A token provider using MSAL Python (`msal` -- new dependency), wired
through the `httpx.Auth` hook:

```python
import httpx
import msal

class EntraClientCredentialsAuth(httpx.Auth):
    """Injects a fresh app-only token into every MCP request.

    MSAL caches the token internally and renews it shortly before
    expiry, so acquire_token_for_client is cheap on the happy path --
    this is what makes hour-long agent processes survive token expiry
    without any reconnect logic of our own.
    """

    def __init__(self, tenant_id, client_id, client_secret, mcp_app_id):
        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )
        # .default = "all app permissions I have been granted"
        self._scopes = [f"api://{mcp_app_id}/.default"]

    def auth_flow(self, request):
        result = self._app.acquire_token_for_client(scopes=self._scopes)
        if "access_token" not in result:
            raise RuntimeError(f"MCP token acquisition failed: "
                               f"{result.get('error_description')}")
        request.headers["Authorization"] = f"Bearer {result['access_token']}"
        yield request
```

`_get_mcp_server_config()` grows an `entra` branch (config:
`MCP_AUTH_MODE=entra`, `MCP_ENTRA_TENANT_ID`, `MCP_ENTRA_CLIENT_ID`,
`MCP_ENTRA_CLIENT_SECRET`, `MCP_ENTRA_APP_ID`) that sets
`connection["auth"] = EntraClientCredentialsAuth(...)` instead of the
static `headers` dict. Because all three entry points (CLI, scanner,
API) build their connection here, they all upgrade at once -- same
one-place property as the static token.

Secret handling: the client secret goes in the gitignored `.env` only;
on Azure, prefer a managed identity (`msal` supports it via
`ManagedIdentityClient`) and delete the secret entirely.

### 4.6 Testing leg 2

- `EntraTokenVerifier`: same offline pattern as leg 1 -- local RSA
  keypair, self-minted JWTs with good/bad `aud`/`iss`/`exp`/`roles`,
  monkeypatched JWKS.
- `_require_role`: unit-test with a fake access token in the SDK's
  auth context (or factor the check to take scopes as an argument).
- `EntraClientCredentialsAuth`: fake the MSAL app object; assert the
  header lands on the request and that acquisition failure raises.
- Static mode remains the integration-test path (fast, no tenant).

### 4.7 Multi-agent consumers: the trigger for building leg 2

A stated goal of the MCP server is that agents beyond Agnes consume
its tools. Most of what that needs already exists: per-agent tokens
with independent revocation, caller identity in every audit line, a
stateless transport any MCP client can speak, and dynamic tool
discovery (a new agent needs a URL, a credential, and an MCP client
library -- none of Agnes's code). The NETWORK side of enablement --
NetworkPolicy allowlisting inside the cluster, an internal
LoadBalancer or ingress route plus mandatory TLS for consumers
outside it -- is covered in [aks.md](aks.md) section 12.

What does NOT exist yet is per-agent AUTHORIZATION, and that is the
gap that makes leg 2 worth building the moment a second consumer
appears:

**Today authentication is all-or-nothing.** Any agent presenting a
valid token gets all 12 tools -- including `raise_entitlement_request`,
a WRITE. For Agnes alone that is fine. For an HR reporting bot it is
not: it should search docs and read data, and be PHYSICALLY unable to
raise entitlement requests.

**The proper fix is Choice 2 above** (app roles -> tool groups):
each consuming agent's registration is assigned only the roles it
needs (`Tools.Knowledgebase`, `Tools.Resource`, `Tools.Quality`,
`Tools.Request`), the verifier copies the token's `roles` claim into
`AccessToken.scopes`, and a small per-tool-group check enforces it.
Authorization then lives in Entra where security teams manage it --
granting a new agent access is an app-role assignment, not a config
deploy.

**Interim static variant, if a second consumer arrives before leg 2
is built:** extend the static token config with tool groups, e.g.

```
MCP_AUTH_TOKENS=agnes:tok1:all,hr-bot:tok2:knowledgebase+resource
```

parse the third field in `parse_agent_tokens`, stash it in the same
`AccessToken.scopes` field the Entra verifier will use, and add the
same per-tool-group check. Deliberately shaped so the enforcement
code is written ONCE and only the token SOURCE changes when Entra
lands. A modest, contained change (auth.py + a decorator-level check
in server.py) -- but if leg 2 is weeks away, skip the interim and
build it properly.

**Also on the checklist for a second consumer** (from aks.md s12):
capacity/noisy-neighbor review (the per-call `caller=X took=ms` logs
give per-agent usage), tool schemas becoming an API contract
(additive changes only), and a data-access review -- a new consumer
of entitlement data is a governance conversation, not just a token.

---

## 5. Rollout plan and checklist

Order matters less than you would think, because every mode is a
config switch and static/entra can differ per environment. Sensible
sequence:

1. **Leg 1 server** (EntraAuthenticator + session ownership) -- ship
   dark behind `AGENT_API_AUTH=static`; enable `entra` in a test
   environment.
2. **Web client MSAL** -- requires the page to be hosted over HTTPS
   first (registered redirect URI). This retires the token-in-URL POC
   pattern.
3. **Leg 2** (MCP verifier + client credentials) -- independent of leg
   1; can go before or after.
4. **App roles / per-tool authorization** -- once more than one agent
   deployment exists.
5. **Teams bot with SSO/OBO** -- when the bot is built; the API is
   already ready for it after step 1.
6. Later, optionally: OBO chaining so MCP audit sees end users, and
   managed identities to eliminate the client secret.

Portal prerequisites checklist: tenant id; API registration with
`access_as_user` scope and `requestedAccessTokenVersion: 2`; SPA
registration with redirect URI + delegated permission + admin consent;
MCP registration with app roles; agent registration with secret + role
assignments + admin consent.

New Python dependencies when the time comes: `pyjwt[crypto]` (both
packages, validation) and `msal` (agent-client only, leg-2 token
acquisition). The web client adds the `msal-browser` script.

What does NOT change anywhere: the `Authorization: Bearer` transport,
the `Authenticator`/`TokenVerifier` interfaces, the endpoints, the
`Principal`/`AccessToken` shapes, the fail-closed configuration
philosophy, and the tests that use static/fake auth.
