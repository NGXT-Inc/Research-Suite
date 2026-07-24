# Replit Agent

Replit Agent configures remote MCP servers in account settings. The connection
applies across the user's repls; a template or `.replit` file cannot pre-wire
it.

## OAuth setup

OAuth is the primary setup path:

1. Open the **MCP Servers** settings pane.
2. Select **+ Add MCP server**.
3. Enter a display name such as `Merv` and the server URL
   `https://experiments.rapidreview.io/mcp`.
4. Select **Test & save**.
5. Follow the browser sign-in and consent flow.

Merv advertises OAuth 2.1 dynamic client registration with PKCE through RFC
8414 discovery and an RFC 9728 protected-resource challenge. Replit registers
the client automatically before guiding sign-in.

## Bearer-header fallback

For a static project key, first mint one in the UI at
[RapidReview](https://rapidreview.io/map); see the
[hosted client quickstart](../../docs/HOSTED_CLIENT_QUICKSTART.md). Then open
the server's advanced settings and add a custom header:

```text
Header name:  Authorization
Header value: Bearer paste the key
```

**UNCONFIRMED:** Replit documents custom header name/value pairs with an
`X-API-Key` example, but does not specifically confirm a literal
`Authorization: Bearer ...` header. Use OAuth if this fallback does not pass
**Test & save**.

## Constraints

All MCP traffic passes Replit's security scanner. Replit does not document
per-tool grants or a tool-count ceiling. Merv skills and reviewer agents are not
auto-installed by this account-scoped connection; for a review handoff, start a
second session or agent with the matching review skill and the fresh prompt
returned by `review.request`, or perform the handoff inline when separate
execution is unavailable.
