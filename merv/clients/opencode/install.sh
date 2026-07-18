#!/bin/sh
# Install Merv skills and reviewer agents for OpenCode.
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
    "merv": {
      "type": "local",
      "command": ["$PLUGIN_DIR/bin/merv-mcp"],
      "enabled": true,
      "environment": {
        "MERV_CONTROL_URL": ""
      }
    }
  }
}

OpenCode spawns local MCP servers with cwd = project root, which is exactly
what the proxy needs for checkout-local operations. An empty control URL uses
the configured/default hosted brain. For a local deployment, set it to
http://127.0.0.1:8787 and start $PLUGIN_DIR/bin/merv-http first.
EOF
