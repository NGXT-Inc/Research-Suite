#!/bin/sh
# Install Research Plugin skills and reviewer agents for OpenCode.
#
# OpenCode has no declarative plugin bundle, so this script symlinks the
# plugin's canonical skills and the OpenCode-specific reviewer agents into
# the global OpenCode config directory. Symlinks keep installs in sync with
# the plugin source; re-run after pulling plugin updates only if files were
# added or renamed.
set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/opencode"

mkdir -p "$CONFIG_DIR/skills" "$CONFIG_DIR/agents"

for skill_dir in "$PLUGIN_DIR"/skills/*/; do
  name=$(basename "$skill_dir")
  ln -sfn "$PLUGIN_DIR/skills/$name" "$CONFIG_DIR/skills/$name"
  echo "skill   $name -> $CONFIG_DIR/skills/$name"
done

for agent_file in "$PLUGIN_DIR"/clients/opencode/agents/*.md; do
  name=$(basename "$agent_file")
  ln -sfn "$agent_file" "$CONFIG_DIR/agents/$name"
  echo "agent   $name -> $CONFIG_DIR/agents/$name"
done

cat <<EOF

Done. Register the MCP server in your research repo's opencode.json
(or globally in $CONFIG_DIR/opencode.json):

{
  "mcp": {
    "research-plugin": {
      "type": "local",
      "command": ["$PLUGIN_DIR/bin/research-plugin-mcp"],
      "enabled": true,
      "environment": {
        "RESEARCH_PLUGIN_DAEMON_URL": "http://127.0.0.1:8787"
      }
    }
  }
}

OpenCode spawns local MCP servers with cwd = project root, which is exactly
what the launcher needs. Start the HTTP daemon first:

  $PLUGIN_DIR/bin/research-plugin-http
EOF
