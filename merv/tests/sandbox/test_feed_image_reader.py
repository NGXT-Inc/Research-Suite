from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend.dataplane.feed_images import LocalFeedImageReader
from backend.utils import ValidationError


_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff03000006000557bff8a40000000049454e44ae426082"
)


class LocalFeedImageReaderTest(unittest.TestCase):
    def test_reads_repo_relative_image_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "figures").mkdir()
            (repo / "figures" / "plot.png").write_bytes(_PNG)

            observed = LocalFeedImageReader(repo_root=repo).read_image(
                path="figures/plot.png"
            )

        self.assertEqual(observed["path"], "plot.png")
        self.assertEqual(observed["data"], _PNG)

    def test_rejects_non_repo_image_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            reader = LocalFeedImageReader(repo_root=repo)
            for state_dir in (".research_plugin", ".merv"):
                (repo / state_dir).mkdir()
                (repo / state_dir / "plot.png").write_bytes(_PNG)

            for path in (
                "/tmp/plot.png",
                "../plot.png",
                ".research_plugin/plot.png",
                ".merv/plot.png",
                "missing.png",
            ):
                with self.subTest(path=path):
                    with self.assertRaises(ValidationError):
                        reader.read_image(path=path)


if __name__ == "__main__":
    unittest.main()
