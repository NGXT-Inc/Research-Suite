"""Shared transcript-tail wire format for sandbox transcript readers.

Both transcript transports (management SSH for VM backends, control-plane exec
for Modal) stream text, decoded with ``errors="replace"`` — which would corrupt
raw bytes and skew byte-offset cursor math. So the remote command emits an
ASCII-safe frame instead:

    <total transcript size in bytes, from wc -c>\n
    <tail window, base64-encoded>

``parse_transcript_tail`` recovers the exact tail bytes plus the true total,
letting ``sandbox.terminal`` keep its cursor in absolute byte offsets even
after the log outgrows the tail window.
"""

from __future__ import annotations

import base64
import binascii
import shlex
from typing import Sequence

from ..sandbox_backend import TranscriptTail


TRANSCRIPT_TAIL_DEFAULT = 50_000


def transcript_tail_command(*, paths: Sequence[str], limit: int) -> str:
    """Shell command printing the frame above for the first existing path.

    ``wc -c`` runs before ``tail`` in the same shell, so under concurrent
    appends the reported total can only lag the window — the next poll at
    ``since=total`` re-reads the boundary instead of skipping past it.
    """
    parts: list[str] = []
    for index, path in enumerate(paths):
        quoted = shlex.quote(path)
        parts.append(
            f"{'if' if index == 0 else 'elif'} [ -f {quoted} ]; then "
            f"wc -c < {quoted}; tail -c {int(limit)} {quoted} | base64; "
        )
    parts.append("fi")
    return "".join(parts)


def parse_transcript_tail(output: str) -> TranscriptTail:
    """Parse a transcript_tail_command frame back into bytes + true total.

    Empty output (no transcript file yet) is an empty transcript. Output that
    doesn't match the frame degrades to window-only semantics — the text is
    treated as the whole transcript — rather than failing the read.
    """
    if not output:
        return TranscriptTail(data=b"", total_bytes=0)
    head, _, body = output.partition("\n")
    try:
        total = int(head.strip())
        data = base64.b64decode(body)
    except (ValueError, binascii.Error):
        data = output.encode("utf-8")
        return TranscriptTail(data=data, total_bytes=len(data))
    # The file can grow between `wc -c` and `tail`; never report a total the
    # window extends past, or window-start math would go negative.
    return TranscriptTail(data=data, total_bytes=max(total, len(data)))


__all__ = [
    "TRANSCRIPT_TAIL_DEFAULT",
    "parse_transcript_tail",
    "transcript_tail_command",
]
