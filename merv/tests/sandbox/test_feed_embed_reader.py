from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.dataplane.feed_embeds import LocalFeedEmbedReader
from backend.domain.feed_embeds import MAX_FEED_EMBED_BYTES
from backend.utils import ValidationError


_HTML = b"<!doctype html><html><body><div>chart</div></body></html>"


class LocalFeedEmbedReaderTest(unittest.TestCase):
    def test_reads_repo_relative_embed_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "figures").mkdir()
            (repo / "figures" / "chart.html").write_bytes(_HTML)

            observed = LocalFeedEmbedReader(repo_root=repo).read_embed(
                path="figures/chart.html"
            )

        self.assertEqual(observed["path"], "chart.html")
        self.assertEqual(observed["data"], _HTML)

    def test_rejects_non_repo_embed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reader = LocalFeedEmbedReader(repo_root=repo)
            for state_dir in (".research_plugin", ".merv"):
                (repo / state_dir).mkdir()
                (repo / state_dir / "chart.html").write_bytes(_HTML)

            for path in (
                "/tmp/chart.html",
                "../chart.html",
                ".research_plugin/chart.html",
                ".merv/chart.html",
                "missing.html",
            ):
                with self.subTest(path=path):
                    with self.assertRaises(ValidationError):
                        reader.read_embed(path=path)

    def test_rejects_oversized_embed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reader = LocalFeedEmbedReader(repo_root=repo)
            oversized = b"<html>" + b"x" * MAX_FEED_EMBED_BYTES
            (repo / "big.html").write_bytes(oversized)
            with self.assertRaises(ValidationError):
                reader.read_embed(path="big.html")

    def test_rejects_binary_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reader = LocalFeedEmbedReader(repo_root=repo)
            (repo / "not-html.bin").write_bytes(b"\x89PNG\r\n\x1a\n")
            with self.assertRaises(ValidationError):
                reader.read_embed(path="not-html.bin")


if __name__ == "__main__":
    unittest.main()
