"""Research Plugin HTTP daemon backend.

Owns SQLite state, the activity log, the job execution backend, and sandbox SSH.
Fronted to Codex by the stdio MCP proxy in `mcp_server`.
"""

__version__ = "0.0011"
