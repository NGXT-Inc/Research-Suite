"""Version + client-compatibility floors (cloud plan Phase 9).

The control plane publishes its own version and the MINIMUM daemon/proxy version
it will serve at ``GET /api/meta``; clients stamp their version on every request
in ``X-RP-Client-Version``; the control plane rejects below-floor clients with an
actionable upgrade error (control mode only — local mode never enforces this).

Floors are plain constants bumped by hand when a wire change makes an older
client unsafe to serve. Within a major version the contract is additive-only
(plan §4 Phase 9), so the floor moves rarely; it exists so a breaking change has
a refusal mechanism instead of a confusing partial failure. The header is the
shared client-version channel for BOTH the daemon (its long-poll/sync-target
calls) and the stdio proxy (its /mcp + /api forwards); they send the same field.

Kept tiny and dependency-free so the stdlib-only proxy could read it if it ever
needs to (it currently only SENDS its version, sourced from its own package).
"""

from __future__ import annotations

from . import __version__

# The header clients stamp their version on (daemon long-poll + sync-target
# calls, stdio proxy forwards). Missing header is TOLERATED (documented choice):
# a pre-Phase-9 client predates the handshake, and refusing it would strand
# in-flight upgrades; only an explicitly-too-old version is rejected. Once every
# shipped client sends the header, the floor moving is the enforcement lever.
CLIENT_VERSION_HEADER = "X-RP-Client-Version"

# The current server version (single source: backend.__version__).
SERVER_VERSION = __version__

# Minimum client versions the control plane will serve. Both track the single
# plugin version today (one wheel ships proxy + daemon), so both equal the
# floor below; they are separate constants so the two clients can diverge later.
MIN_DAEMON_VERSION = "0.0007"
MIN_PROXY_VERSION = "0.0007"


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
        "min_daemon_version": MIN_DAEMON_VERSION,
        "min_proxy_version": MIN_PROXY_VERSION,
    }
