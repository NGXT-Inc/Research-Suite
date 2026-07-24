"""Version + client-compatibility floors.

The control plane publishes its own version and the MINIMUM MCP proxy version
it will serve at ``GET /api/meta``; clients stamp their version on every request
in ``X-RP-Client-Version``; the control plane rejects below-floor clients with
an actionable upgrade error (control mode only — local mode never enforces
this).

Floors are plain constants bumped by hand when a wire change makes an older
client unsafe to serve. The floor moves rarely; it exists so a breaking change has
a refusal mechanism instead of a confusing partial failure. The header is the
client-version channel for stdio proxy /mcp + /api forwards.

Kept tiny and dependency-free so the stdlib-only proxy could read it if it ever
needs to (it currently only SENDS its version, sourced from its own package).
"""

from __future__ import annotations

from .. import __version__

# The header clients stamp their version on. Missing header is TOLERATED
# (documented choice):
# a client may predate the handshake, and refusing it would strand in-flight
# upgrades; only an explicitly-too-old version is rejected. Once every
# shipped client sends the header, the floor moving is the enforcement lever.
CLIENT_VERSION_HEADER = "X-RP-Client-Version"

# The current server version (single source: merv.brain.__version__).
SERVER_VERSION = __version__

# Minimum MCP proxy version the control plane will serve.
# 0.0013 fences pre-key-era proxies (Checkpoint 1): older stdio clients predate
# the artifact-submit + mk_-key surface and get the clean 426 "upgrade" error.
MIN_PROXY_VERSION = "0.0013"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted numeric version to a comparable tuple.

    Lenient: non-numeric segments contribute 0 so a malformed version sorts low
    (and is therefore rejected against any real floor) rather than raising.
    """
    parts: list[int] = []
    for segment in str(version).strip().split("."):
        try:
            parts.append(int(segment))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def is_below_floor(*, client_version: str, floor: str) -> bool:
    """True when ``client_version`` is strictly older than ``floor``."""
    return _version_tuple(client_version) < _version_tuple(floor)


def meta() -> dict[str, str]:
    """The /api/meta payload: server version + the client floors."""
    return {
        "server_version": SERVER_VERSION,
        "min_proxy_version": MIN_PROXY_VERSION,
    }
