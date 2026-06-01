# Claude Frontend Handoff

Use this as the prompt/context for rebuilding the UI in a new folder.

## Goal

Build a new frontend for `research_plugin`, the lean replacement for
`research_state_mockup/backend`.

The frontend should live in a new folder outside `research_plugin`.

Do not rebuild agent chat. Chat stays in Codex for now. The UI is for inspecting
and lightly controlling research state.

## Existing Context To Read

Read these files first:

- `research_plugin/docs/UI_API.md`
- `research_plugin/docs/RESOURCE_MODEL.md`
- `research_plugin/docs/WORKFLOW_AND_REVIEW.md`
- `research_plugin/docs/REVIEW_IDENTITY.md`
- `research_plugin/skills/research-workflow/SKILL.md`

For visual inspiration only, inspect the old UI under:

- `research_state_mockup/app/src`

Do not assume the old backend API still exists. The new API is smaller and is
documented in `research_plugin/docs/UI_API.md`.

## Product Model

The app has three core objects:

- Claim: what we think.
- Experiment: what we try.
- Resource: a regular file in the local repo, saved into the resource system by
  registering/syncing with the backend.

Review and workflow are governance around experiments:

- design review happens before execution
- experiment review happens after result sync
- failed reviews return the experiment to `planned` with revision context
- MCP/Codex handles reviewer agents, but the UI should display review status

## Backend

Start the backend:

```bash
/Users/guraltoo/Documents/dev/proj/experiments/Papyrus/research_plugin/bin/research-plugin-http \
  --repo /path/to/research-repo \
  --host 127.0.0.1 \
  --port 8787
```

Health check:

```http
GET http://127.0.0.1:8787/health
```

Set the frontend backend URL to:

```text
http://127.0.0.1:8787
```

## Required Screens

Build a quiet, dense research dashboard, not a marketing page.

Minimum useful screens:

1. Project home
   - project summary
   - active claims
   - active experiments
   - resource count
   - current workflow status / next action
   - recent events

2. Claims
   - list claims
   - create claim
   - claim detail with experiments testing it

3. Experiments
   - list experiments
   - create experiment linked to claims
   - detail view showing status, attempt index, current gate, next action,
     tested claims, current-attempt resources, all historical resources, reviews
   - transition buttons based on `workflow.allowed_actions` and `next_action`

4. Resources
   - list/group by kind
   - register resource by repo-relative path
   - associate resource to experiment with role
   - view text content for text-like files

5. Reviews
   - show review requests and submitted reviews
   - show verdicts and findings
   - do not implement reviewer-agent chat

6. Sandboxes
   - show the per-experiment sandbox status + SSH details (read-only)
   - show the live terminal transcript for the experiment's sandbox
   - expose release for a running sandbox

## Important UX Rules

- The UI should always use `/home` or `/status` to orient itself.
- The UI must keep an explicit active `project_id`; the backend does not default
  to the first project.
- The UI should show `workflow.next_action` prominently on experiment detail.
- Do not guess workflow state in frontend code.
- Do not hide failed reviews; they are crucial context.
- Distinguish current-attempt resources from historical resources.
- Resource paths are important; show them clearly.
- No agent chat composer in this version.

## Key API Calls

Bootstrap:

```http
GET /api/projects
POST /api/projects
GET /api/projects/{project_id}/home
```

Experiment orientation:

```http
GET /api/projects/{project_id}/experiments/{experiment_id}/status
```

Resource registration:

```http
POST /api/projects/{project_id}/resources
POST /api/projects/{project_id}/resources/{resource_id}/associate
```

Review display:

```http
GET /api/projects/{project_id}/reviews?target_type=experiment&target_id={experiment_id}
```

Sandbox display:

```http
GET /api/projects/{project_id}/experiments/{experiment_id}/sandbox
GET /api/projects/{project_id}/experiments/{experiment_id}/sandbox/terminal?tail=50000
```

## Differences From Old UI

Remove or ignore:

- agent chat
- speech
- artifact refs
- manifests
- tracker refs
- application task panels unless you make them placeholders
- old review change-set flow
- old complex run history

Keep or reinterpret:

- home dashboard
- claims/experiments/resources navigation
- belief graph if it can be fed from lean data
- experiment status strip
- review/gate banners
- resource content preview
- event timeline

## Suggested Tech

Use the same frontend stack as the old app if convenient: Vite + React.

Keep the first version small. Make the data model readable and reliable before
polishing visuals.
