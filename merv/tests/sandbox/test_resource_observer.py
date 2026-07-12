from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from backend.dataplane.resource_observer import LocalResourceObserver
from backend.utils import NotFoundError, ValidationError


class LocalResourceObserverTest(unittest.TestCase):
    def test_observes_repo_relative_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "notes").mkdir()
            path = repo / "notes" / "plan.txt"
            content = b"# Plan\n"
            path.write_bytes(content)

            observed = LocalResourceObserver(repo_root=repo).observe_file(
                path="notes/plan.txt",
                kind="plan",
                title="Plan",
                created_by="tester",
            )

        self.assertEqual(observed["path"], "notes/plan.txt")
        self.assertEqual(observed["kind"], "plan")
        self.assertEqual(observed["title"], "Plan")
        self.assertEqual(observed["created_by"], "tester")
        self.assertEqual(observed["size_bytes"], len(content))
        self.assertEqual(
            observed["content_sha256"],
            hashlib.sha256(content).hexdigest(),
        )
        self.assertEqual(observed["content_type"], "text/plain")
        self.assertIsInstance(observed["mtime_ns"], int)
        self.assertIsInstance(observed["ctime_ns"], int)

    def test_rejects_non_repo_file_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            observer = LocalResourceObserver(repo_root=repo)
            (repo / ".research_plugin").mkdir()
            (repo / ".research_plugin" / "state.sqlite").write_text("internal\n")
            (repo / "folder").mkdir()

            cases = [
                ("/tmp/file.txt", ValidationError),
                ("../outside.txt", ValidationError),
                (".research_plugin/state.sqlite", ValidationError),
                ("missing.txt", NotFoundError),
                ("folder", ValidationError),
            ]
            for path, error in cases:
                with self.subTest(path=path):
                    with self.assertRaises(error):
                        observer.observe_file(path=path)


if __name__ == "__main__":
    unittest.main()
