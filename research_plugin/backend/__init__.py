"""Research Plugin HTTP daemon backend.

Owns SQLite state, the shadow git store, the activity log, the job execution
backend, and the volume sync poller. Fronted to Codex by the stdio MCP proxy
in `mcp_server`.
"""

__version__ = "0.0004"
