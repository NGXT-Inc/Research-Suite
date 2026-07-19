# Migrating from Research Suite (`research-plugin`) to Merv

Research Suite is now **Merv**. Everything user-facing was renamed in
v0.0012, and the hosted brain now requires sign-in. Your data carries over
untouched — the switch is a plugin swap plus a one-time login.

| | Before | After |
|---|---|---|
| Plugin | `research-plugin` | `merv` |
| Marketplace | `research-suite` | `rapidreview` (`https://rapidreview.io/marketplace.json`) |
| GitHub repo | `NGXT-Inc/Research-Suite` | `NGXT-Inc/Merv` (old URLs redirect) |
| Plugin directory in the repo | `research_plugin/` | `merv/` |
| Binaries | `research-plugin-*` | `merv-mcp`, `merv-client`, `merv-http` |
| Web UI | `experiments.rapidreview.io` | [`rapidreview.io/merv`](https://rapidreview.io/merv) — update your bookmarks |
| Hosted brain | open | requires a RapidReview account |

## Your data is safe — touch nothing

- The `.research_plugin/` folder inside each project checkout (the
  folder-to-project link, activity log, sandbox keys) keeps its name and
  format. The new plugin reads the exact same files. Do not move, rename,
  or delete it.
- From v0.0013, checkouts that don't already have a `.research_plugin/`
  folder keep their state in `.merv/` instead (fresh clones included). Any
  existing `.research_plugin/` folder keeps working forever and always wins
  when both directories are present — no migration, ever.
- The same rule now covers machine config and environment variables: fresh
  machines keep client state in `~/.merv/`, while an existing
  `~/.research_plugin/` keeps working forever and wins when present. Every
  environment variable has a `MERV_*` primary spelling (for example
  `MERV_CONTROL_URL`); the old `RESEARCH_PLUGIN_*` names remain supported
  forever as a fallback — set values are honored, with a one-line
  deprecation notice pointing at the new name.
- Projects, claims, experiments, reviews, and reflections live in the
  hosted brain, keyed by project id. Linked folders reconnect automatically
  once you sign in.
- Running sandboxes and detached runs are unaffected — the swap is entirely
  client-side, and the sandbox keys in `.research_plugin/` keep working.

## Claude Code

The old plugin cannot be updated in place; swap it:

```bash
claude plugin uninstall research-plugin@research-suite
claude plugin marketplace remove research-suite
claude plugin marketplace add https://rapidreview.io/marketplace.json
claude plugin install merv@rapidreview
```

Restart Claude Code, then [sign in](#sign-in). If you already run
`merv@rapidreview`, this is just `claude plugin marketplace update
rapidreview && claude plugin update merv@rapidreview` (the full
`name@marketplace` form is required).

## Cursor

Replace the old install with a real copy of the renamed plugin directory
(Cursor rejects symlinks that point outside `~/.cursor/plugins/local`):

```bash
git clone https://github.com/NGXT-Inc/Merv.git ~/Merv
mkdir -p ~/.cursor/plugins/local
rm -rf ~/.cursor/plugins/local/research-plugin ~/.cursor/plugins/local/merv
rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.egg-info' \
  ~/Merv/merv/ ~/.cursor/plugins/local/merv/
# Optional only for merv-client/merv-http when `python3` is older than 3.11;
# merv-mcp itself runs on Python 3.9+:
python3.11 -m venv ~/.cursor/plugins/local/merv/.venv
```

If you already have a clone (any folder name — old remote URLs redirect),
`git pull` it instead of cloning and substitute its path for `~/Merv` in
every command here and in [Sign in](#sign-in).

On Cursor's Customize → Plugins page, remove any leftover `research-plugin`
entry or "Research Suite" marketplace (marketplace registrations are synced
to your Cursor account, so they survive local deletes), enable **merv**, and
restart Cursor. Then [sign in](#sign-in).

To update later: `git -C ~/Merv pull`, re-run the `rsync`, reload Cursor.

## Codex

First update your clone — this is what creates the renamed `merv/`
directory and its binaries (the old `research-plugin-mcp` no longer
exists):

```bash
git -C /path/to/your-clone pull   # old remote URLs redirect
```

Then in `~/.codex/config.toml`, replace the old `research-plugin` server
entry (use the real absolute path to your clone):

```toml
[mcp_servers.merv]
command = "/path/to/your-clone/merv/bin/merv-mcp"
```

If your old entry had an `env` block (`RESEARCH_PLUGIN_*` variables), copy
it over unchanged — the old names keep working. New configs should prefer
the `MERV_*` spellings (e.g. `MERV_CONTROL_URL`). Restart Codex, then
[sign in](#sign-in).

For Gemini CLI and OpenCode, update the extension paths the same way; see
[merv/docs/CLIENTS.md](merv/docs/CLIENTS.md).

## Sign in

The old brain was open; the new one requires an account. You don't have one
yet — the sign-in page lets you sign up (Google or email), and access to
your existing projects was granted **by email** at cutover. Sign up with the
email your project owner registered for you; a different email gives you an
empty project list.

Once per machine. `merv-client` ships inside the plugin (it is not on your
PATH) — run the copy your install created:

```bash
# Claude Code (marketplace install; if several versions are cached, use the newest):
~/.claude/plugins/cache/rapidreview/merv/*/merv/bin/merv-client login

# Cursor / Codex (from your clone):
~/Merv/merv/bin/merv-client login
```

This opens the browser and **waits until sign-in completes** there (agents:
run it in the background rather than a foreground shell that will time out).
The session is stored locally and shared by every client on the machine. On a headless box, add
`--no-browser` (prints the URL to open elsewhere) or use
`--api-key rr_sk_...` (keys are issued by your project owner).

## Verify

Open a previously linked project folder and ask the agent to call
`project(action="current")` — it should return your existing project. Then
`workflow.status_and_next()` picks up wherever you left off.

## If something fails

- **Authorization errors on the old plugin** are expected — the hosted
  brain now requires sign-in. Migrating fixes them.
- **Empty project list after login**: you signed in with an email that
  isn't a member of your projects. Re-run `merv-client login` with the
  right account (it replaces the stored session), or ask your project
  owner to add your email. Still stuck? Open an issue on
  [github.com/NGXT-Inc/Merv](https://github.com/NGXT-Inc/Merv/issues).
- **Cursor still lists `research-plugin`**: that's the stale account-synced
  marketplace — remove it from the Customize → Plugins page (deleting local
  folders is not enough).

## Remaining pre-rename compatibility

Two legacy symbols still ship for older checkouts and will be removed once no
supported deployment or recorded run references them:

- `rp_run` — superseded by `merv_run`; the alias remains until recorded runs
  referencing it age out.
- `RP_EXPERIMENT_DIR` — superseded by the `MERV_*` spelling; the dual-read
  resolver keeps the old name working.

Remove them together, with a changelog note, once telemetry shows no reads of
the legacy names.
