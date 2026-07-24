# Authentication & project membership

The hosted research suite authenticates against the **same Supabase project as
RapidReview** — same accounts, same `rr_sk_` API keys. Localhost is auth-free:
`build_local_server` passes no verifier, so the local brain never reads
`SUPABASE_*` env, never imports PyJWT, and serves every request as the
implicit local principal exactly as before.

## How it works

One `Authorization: Bearer <credential>` header, three credential shapes,
dispatched by prefix (RapidReview's contract, reimplemented in
`src/merv/brain/surface/auth.py`):

- **Supabase session JWT** — browser sign-in via supabase-js in the UI.
  Verified locally (HS256, `SUPABASE_JWT_SECRET`, audience `authenticated`);
  anonymous sessions are rejected. No Supabase round-trip per request.
- **`rr_sk_` API key** — RapidReview-minted, owner-scoped; everything headless
  (direct `/mcp` clients, agents, MLflow, curl). sha256-hashed and looked up in
  the shared `api_keys` table over PostgREST (`SUPABASE_SERVICE_KEY`), cached 60s.
  These keys are minted/revoked in RapidReview.
- **`mk_` project key** — minted/revoked **in this repo** via the key-mint UI and
  stored in the `project_api_keys` table. Each key binds one immutable project,
  and the gateway rejects any request whose `project_id` argument does not equal
  the key's bound project. OAuth (DCR + PKCE) mints audience-confined `mk_`
  access tokens (+ `mrt_` refresh) for cloud platforms (Codex, Replit).

Enforcement lives in the `attach_principal` middleware
(`src/merv/brain/surface/transport/api/app.py`): OPTIONS, `/health`, `/api/meta`, and
`/internal/auth/mlflow` stay open; the 426 version floor runs before auth so
stale clients get "upgrade", not "login". A verified credential becomes
`Principal(user_id=<supabase sub>)`.

**Project membership** is the authorization layer: `project_members`
(project_id, user_id) in the research store. Authenticated requests see only
member projects — enforced at two funnels: the HTTP path gate
(`/api/projects/{id}/...` → 404 for non-members; `/api/activity` +
`/api/debug/*` additionally require `?project_id=`) and the MCP funnel
(`route_call_tool`, including review tools via their resolved project).
Creating a project records the creator as its first member. Share/assign via:

```
POST   /api/projects/{id}/members   {"user_id": "<supabase auth.users uuid>"}
DELETE /api/projects/{id}/members/{user_id}
GET    /api/projects/{id}/members
```

Any member can manage members (two-trusted-users model; no roles).

## Client setup

- **Web UI**: `/api/meta` advertises `auth: {required, supabase_url,
  supabase_anon_key}`; the AuthGate then shows sign-in (email/password or
  Google). Nothing is baked into the bundle; local backends advertise
  `required: false` and the UI never loads supabase-js.
- **MCP clients** (local Claude Code, cloud Codex, Replit, browser-driven):
  every agent connects directly to the brain's `POST /mcp` endpoint. Sign in
  at [RapidReview Maps](https://rapidreview.io/map), open **Account →
  Settings → API Keys**, mint a project-scoped key, and export it as
  `MERV_MCP_KEY`. The committed `.mcp.json` uses `type: "http"`,
  `url: "https://experiments.rapidreview.io/mcp"`, and
  `headers.Authorization: "Bearer ${MERV_MCP_KEY}"` — the key is read from the
  env var and is **never** inlined into a committed file (it is
  bearer-equivalent to full access to its one bound project, so export it in
  your shell and keep any local key file `.gitignore`d). `merv-client
  configure` writes the machine config and `merv-client env` prints that
  `.mcp.json` snippet; see the
  [hosted client quickstart](HOSTED_CLIENT_QUICKSTART.md) for the full
  walkthrough. A key binds one immutable project; the agent passes `project_id`
  explicitly and the gateway enforces that it equals the key's bound project,
  while project membership still controls authorization.
- **Agents / MLflow**: `mlflow.context` env blocks carry
  `MLFLOW_TRACKING_USERNAME/PASSWORD` (the key in the password slot) when
  `MERV_MLFLOW_AGENT_KEY` is set; sandbox provisioning also
  delivers the pair ambiently (VM secrets file / modal.Secret), so training
  code logs with zero ceremony from anywhere.

## Rollout runbook (hosted VM)

1. **Rotate the Supabase JWT secret and service-role key first** (both leaked
   into RapidReview git history). Coordinated change: rotation signs out live
   RapidReview sessions.
2. Set env on the VM: `SUPABASE_URL`, `SUPABASE_JWT_SECRET`,
   `SUPABASE_SERVICE_KEY`, `SUPABASE_ANON_KEY`,
   `MERV_REQUIRE_AUTH=1`,
   `MERV_UI_BASE_URL=https://rapidreview.io/merv`. Restart the brain.
3. Backfill membership for existing projects (one insert per project):
   ```sql
   INSERT INTO project_members (project_id, user_id, added_at)
   SELECT id, '<founder auth.users uuid>', NOW() FROM projects
   ON CONFLICT DO NOTHING;
   ```
4. Each user: sign in on the UI; mint a project-scoped key in RapidReview,
   export it as `MERV_MCP_KEY`, and drop the `.mcp.json` http snippet
   (`merv-client env`) on each machine.
5. MLflow gate: mint a dedicated `rr_sk_` key, set
   `MERV_MLFLOW_AGENT_KEY`, then wrap the Caddy `/mlflow*` handles
   (except `/mlflow/health` and the MinIO presigned bucket paths) in:
   ```
   forward_auth 127.0.0.1:8787 {
       uri /internal/auth/mlflow
       copy_headers Authorization
   }
   ```
   The endpoint answers 204/401 (+`WWW-Authenticate: Basic` so browsers
   prompt; any username, the key as password).
6. Bump `MIN_PROXY_VERSION` once clients have upgraded, so pre-auth clients
   get the actionable 426 instead of a bare 401.
7. Assign projects to the second user via the members endpoint above.

## Notes

- SSE under auth: EventSource cannot send the header; the hosted stream 401s
  and the UI's ETag-polling fallback carries updates (~3s latency). Stream
  tickets are a known follow-up if realtime matters.
- `/api/admin/*` requires authentication but not membership — acceptable for
  a small trusted user set; revisit with roles if that changes.
- Same accounts ≠ SSO: users sign in once per product origin.
