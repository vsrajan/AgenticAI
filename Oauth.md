# OAuth 2.0 Authorization Code Grant Flow — Full Conversation
> A working session on OAuth 2.0 with Microsoft Entra ID as the identity provider.
> Covers grant flow theory, runtime stages, SPA integration, APIM architecture,
> and a real two-SP enterprise POC setup.

---

## Q: Explain the authorization code grant flow and how it differs from client credentials flow.

### The core split: delegated vs. application identity

The cleanest mental model is to stop thinking "two flows" and start thinking "two questions":

**Client credentials** answers *"this service needs to act as itself."* There is no human in the loop.
The client **is** the principal. It's a two-legged exchange: the client authenticates directly to
Entra's token endpoint with its own credential and gets back an access token scoped to itself. Think
nightly batch jobs, a backend syncing data, a daemon calling Microsoft Graph at 3am when nobody's
logged in.

**Authorization code** answers *"this app needs to act on behalf of a signed-in user."* There is a
human who authenticates and **delegates** a subset of their authority to the app. It's three-legged:
user (resource owner), client, and Entra (authorization server). Think a web app or