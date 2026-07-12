from __future__ import annotations

import base64
import sys
import unittest

from backend.execution.transcript_wire import (
    parse_transcript_tail,
    transcript_tail_command,
)
from backend.execution.vm_ssh import run_ssh, run_ssh_input
from backend.sandbox.sandbox_backend import TranscriptTail


class VmSshSubprocessDecodeTest(unittest.TestCase):
    def test_run_ssh_replaces_invalid_utf8_output(self) -> None:
        result = run_ssh(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'\\x95ok')",
            ]
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "\ufffdok")

    def test_run_ssh_input_replaces_invalid_utf8_output(self) -> None:
        result = run_ssh_input(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "sys.stdout.buffer.write(b'\\x95' + sys.stdin.read().encode())"
                ),
            ],
            "ok",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "\ufffdok")


class TranscriptWireTest(unittest.TestCase):
    """The size+base64 frame the transcript readers speak (both backends)."""

    def test_command_reports_size_then_base64_tail_per_path(self) -> None:
        command = transcript_tail_command(paths=["/a/log", "/b/log"], limit=512)

        self.assertEqual(
            command,
            "if [ -f /a/log ]; then wc -c < /a/log; tail -c 512 /a/log | base64; "
            "elif [ -f /b/log ]; then wc -c < /b/log; tail -c 512 /b/log | base64; fi",
        )

    def test_command_quotes_paths(self) -> None:
        command = transcript_tail_command(paths=["/a dir/log"], limit=10)
        self.assertIn("'/a dir/log'", command)

    def test_parse_round_trips_raw_bytes(self) -> None:
        # Offsets are raw bytes: invalid UTF-8 (e.g. a multibyte char split by
        # `tail -c`) must survive the text-mode pipe byte-for-byte, which is
        # what the base64 leg of the frame guarantees.
        data = b"\x95ok \xf0\x9f\x8e" + b"\n" * 3
        frame = f"60000\n{base64.encodebytes(data).decode('ascii')}"

        tail = parse_transcript_tail(frame)

        self.assertEqual(tail, TranscriptTail(data=data, total_bytes=60_000))

    def test_parse_empty_output_is_empty_transcript(self) -> None:
        self.assertEqual(parse_transcript_tail(""), TranscriptTail(data=b"", total_bytes=0))

    def test_parse_unframed_output_degrades_to_window_only(self) -> None:
        # Output that doesn't match the frame (no size line) is treated as the
        # whole transcript instead of failing the read.
        tail = parse_transcript_tail("plain tail text\n")
        self.assertEqual(tail.data, b"plain tail text\n")
        self.assertEqual(tail.total_bytes, len(b"plain tail text\n"))

    def test_parse_never_reports_total_below_window(self) -> None:
        # The file can grow between `wc -c` and `tail`; the parsed total must
        # never lag the window length or window-start math would go negative.
        data = b"grew after wc ran"
        frame = f"3\n{base64.encodebytes(data).decode('ascii')}"
        self.assertEqual(parse_transcript_tail(frame).total_bytes, len(data))

    def test_frame_survives_a_real_text_mode_pipe(self) -> None:
        # End-to-end through the same decoding run_ssh applies (text=True,
        # errors="replace"): the frame is pure ASCII, so nothing is lost.
        data = bytes(range(256))
        script = (
            "import base64, sys;"
            "data = bytes(range(256));"
            "sys.stdout.write(f'{len(data)}\\n');"
            "sys.stdout.write(base64.encodebytes(data).decode('ascii'))"
        )
        result = run_ssh([sys.executable, "-c", script])

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            parse_transcript_tail(result.stdout),
            TranscriptTail(data=data, total_bytes=len(data)),
        )


if __name__ == "__main__":
    unittest.main()
