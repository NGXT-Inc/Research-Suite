"""Social feed service tests (Feed_PRD.md)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pathlib import Path as _P

from backend.app import ResearchPluginApp
from backend.domain import feed_policy
from backend.domain.feed_images import SERVEABLE_IMAGE_TYPES, sniff_image_type
from backend.services.feed import POST_TEXT_MAX
from backend.services.feed_unfurl import UnfurlError, extract_card, unfurl
from backend.transport.feed_http import _image_headers
from backend.utils import ValidationError

# Minimal valid 1x1 PNG.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100ffff03000006000557bff8a40000000049454e44ae426082"
)

# A small SVG whose root is preceded by an XML declaration (matplotlib-shaped),
# carrying a <script> we expect to survive storage but be served inert.
_SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    b'<rect width="10" height="10"/><script>1</script></svg>'
)


class FeedServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = ResearchPluginApp(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.pid = self.call("project.create", name="Feed Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    # -- identity -----------------------------------------------------------

    def test_register_is_idempotent_per_session(self) -> None:
        first = self.call("feed.register", project_id=self.pid, handle="Nova-7", session_id="s1")
        self.assertTrue(first["created"])
        again = self.call("feed.register", project_id=self.pid, handle="Nova-7", session_id="s1")
        self.assertFalse(again["created"])

    def test_handle_collision_across_sessions_rejected(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7", session_id="s1")
        with self.assertRaises(ValidationError):
            self.call("feed.register", project_id=self.pid, handle="Nova-7", session_id="s2")

    def test_post_requires_registered_handle(self) -> None:
        with self.assertRaises(ValidationError):
            self.call("feed.post", project_id=self.pid, handle="Ghost", text="hi")

    # -- writing ------------------------------------------------------------

    def test_post_and_list_reverse_chronological(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        self.call("feed.post", project_id=self.pid, handle="Nova-7", text="first")
        self.call("feed.post", project_id=self.pid, handle="Nova-7", text="second")
        posts = self.call("feed.list", project_id=self.pid)["posts"]
        self.assertEqual([p["text"] for p in posts], ["second", "first"])

    def test_char_cap_enforced(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaises(ValidationError):
            self.call("feed.post", project_id=self.pid, handle="Nova-7", text="x" * (POST_TEXT_MAX + 1))

    def test_empty_text_rejected(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaises(ValidationError):
            self.call("feed.post", project_id=self.pid, handle="Nova-7", text="   ")

    def test_image_captured_and_served(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        (self.repo / "plot.png").write_bytes(_PNG)
        result = self.call("feed.post", project_id=self.pid, handle="Nova-7", text="plot", image_path="plot.png")
        self.assertTrue(result["post"]["has_image"])
        data, ctype = self.app.feed.get_image(project_id=self.pid, post_id=result["post"]["id"])
        self.assertEqual(data, _PNG)
        self.assertEqual(ctype, "image/png")

    def test_sniff_detects_svg_but_not_arbitrary_text(self) -> None:
        self.assertEqual(sniff_image_type(_P("c.svg"), _SVG), "image/svg+xml")
        # An xml-declared svg with leading whitespace still sniffs.
        self.assertEqual(sniff_image_type(_P("c.svg"), b"  \n" + _SVG), "image/svg+xml")
        # Plain text / html that merely mentions svg later is not an image.
        self.assertIsNone(sniff_image_type(_P("note.txt"), b"the svg chart is nice"))

    def test_svg_served_inert_via_csp_sandbox(self) -> None:
        # External (unfurl) re-host set stays raster-only — svg never enters it.
        self.assertNotIn("image/svg+xml", SERVEABLE_IMAGE_TYPES)
        png_headers = _image_headers("image/png")
        self.assertNotIn("Content-Security-Policy", png_headers)
        svg_headers = _image_headers("image/svg+xml")
        self.assertIn("sandbox", svg_headers["Content-Security-Policy"])
        self.assertIn("script-src 'none'", svg_headers["Content-Security-Policy"])
        self.assertEqual(svg_headers["X-Content-Type-Options"], "nosniff")

    def test_svg_image_captured_and_served(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        (self.repo / "chart.svg").write_bytes(_SVG)
        result = self.call("feed.post", project_id=self.pid, handle="Nova-7", text="vec", image_path="chart.svg")
        self.assertTrue(result["post"]["has_image"])
        data, ctype = self.app.feed.get_image(project_id=self.pid, post_id=result["post"]["id"])
        self.assertEqual(data, _SVG)
        self.assertEqual(ctype, "image/svg+xml")

    def test_feed_service_rejects_unobserved_local_image_path(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        (self.repo / "plot.png").write_bytes(_PNG)
        with self.assertRaises(ValidationError):
            self.app.feed.post(
                project_id=self.pid,
                handle="Nova-7",
                text="plot",
                image_path="plot.png",
            )

    def test_observed_image_bytes_are_captured_and_served(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.app.feed.post_observed(
            project_id=self.pid,
            handle="Nova-7",
            text="plot",
            image_path="/private/path/plot.png",
            image_bytes=_PNG,
        )
        data, ctype = self.app.feed.get_image(
            project_id=self.pid, post_id=result["post"]["id"]
        )
        self.assertEqual(data, _PNG)
        self.assertEqual(ctype, "image/png")

    def test_missing_image_rejected(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaises(ValidationError):
            self.call("feed.post", project_id=self.pid, handle="Nova-7", text="x", image_path="nope.png")

    def test_feed_post_preflights_before_reading_image(self) -> None:
        with self.assertRaisesRegex(ValidationError, "not registered"):
            self.call(
                "feed.post",
                project_id=self.pid,
                handle="Ghost",
                text="plot",
                image_path="missing.png",
            )

    def test_bad_link_degrades_to_plain_chip(self) -> None:
        # An unreachable/disallowed URL must NOT fail the post (PRD edge case).
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="see", url="http://127.0.0.1/secret"
        )
        preview = result["post"]["link_preview"]
        self.assertTrue(preview and preview.get("error"))

    def test_post_view_does_not_leak_blob_hash(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        (self.repo / "p.png").write_bytes(_PNG)
        result = self.call("feed.post", project_id=self.pid, handle="Nova-7", text="x", image_path="p.png")
        self.assertNotIn("image_sha256", result["post"])

    # -- nudge --------------------------------------------------------------

    def test_nudge_excludes_feed_events(self) -> None:
        # Registering + posting produce feed.* events; they must not count as
        # "activity since last post" that would nudge the agent to post again.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        self.call("feed.post", project_id=self.pid, handle="Nova-7", text="hello")
        with self.app.store.transaction() as conn:
            signal = self.app.feed.feed_signal(project_id=self.pid, conn=conn)
        self.assertEqual(signal["events_since_last_post"], 0)

    def test_feed_list_surfaces_nudge_on_first_page_only(self) -> None:
        # The nudge reaches the agent through the feed's OWN surface (feed.list),
        # not the research workflow — keeping the feed standalone.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        orig = feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS
        feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = 1, 0.0
        try:
            self.call("claim.create", project_id=self.pid, statement="a claim")
            first = self.call("feed.list", project_id=self.pid)
            self.assertIn("nudge", first)
            self.assertTrue(first["nudge"]["should_post"])
            # A paginated read (cursor set) omits the nudge — no nagging mid-scroll.
            paged = self.call("feed.list", project_id=self.pid, before_seq=10_000_000)
            self.assertNotIn("nudge", paged)
        finally:
            feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = orig

    def test_nudge_fires_on_real_activity(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        orig_events, orig_hours = feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS
        feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = 1, 0.0
        try:
            self.call("claim.create", project_id=self.pid, statement="a claim")
            with self.app.store.transaction() as conn:
                nudge = self.app.feed.feed_nudge(project_id=self.pid, conn=conn)
            self.assertIsNotNone(nudge)
            self.assertTrue(nudge["should_post"])
        finally:
            feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = orig_events, orig_hours


_ARXIV_HTML = b"""
<html><head>
<title>[1608.03983] SGDR</title>
<meta name="citation_title" content="SGDR: Stochastic Gradient Descent with Warm Restarts"/>
<meta name="citation_author" content="Loshchilov, Ilya"/>
<meta name="citation_author" content="Hutter, Frank"/>
<meta name="citation_date" content="2016/08/13"/>
<meta property="og:title" content="SGDR: Stochastic Gradient Descent with Warm Restarts"/>
<meta property="og:description" content="Restart techniques are common in gradient-free optimization."/>
<meta property="og:image" content="/static/arxiv-logo.png"/>
</head><body></body></html>
"""

_REPO_HTML = b"""
<html><head>
<meta property="og:title" content="GitHub - huggingface/peft"/>
<meta property="og:description" content="Parameter-Efficient Fine-Tuning."/>
</head><body></body></html>
"""

_BLOG_HTML = b"""
<html><head><title>Some post</title>
<meta property="og:description" content="Thoughts."/>
</head><body></body></html>
"""


class FeedUnfurlCardTest(unittest.TestCase):
    def test_paper_card_from_citation_meta(self) -> None:
        card = extract_card("https://arxiv.org/abs/1608.03983", "text/html", _ARXIV_HTML)
        self.assertEqual(card["kind"], "paper")
        self.assertEqual(card["title"], "SGDR: Stochastic Gradient Descent with Warm Restarts")
        self.assertEqual(card["authors"], ["Loshchilov, Ilya", "Hutter, Frank"])
        self.assertEqual(card["year"], "2016")
        self.assertTrue(card["trusted"])
        self.assertEqual(card["image_url"], "https://arxiv.org/static/arxiv-logo.png")

    def test_citation_meta_marks_paper_on_any_host(self) -> None:
        card = extract_card("https://journal.example.org/x", "text/html", _ARXIV_HTML)
        self.assertEqual(card["kind"], "paper")
        self.assertFalse(card["trusted"])

    def test_repo_card_by_host(self) -> None:
        card = extract_card("https://github.com/huggingface/peft", "text/html", _REPO_HTML)
        self.assertEqual(card["kind"], "repo")
        self.assertEqual(card["authors"], [])
        self.assertEqual(card["year"], "")

    def test_plain_page_card(self) -> None:
        card = extract_card("https://blog.example.com/post", "text/html", _BLOG_HTML)
        self.assertEqual(card["kind"], "page")
        self.assertEqual(card["title"], "Some post")

    def test_non_html_is_minimal_page_card(self) -> None:
        card = extract_card("https://example.com/paper.pdf", "application/pdf", b"%PDF-1.5")
        self.assertEqual(card["kind"], "page")
        self.assertEqual(card["title"], "")
        self.assertEqual(card["authors"], [])

    def test_author_list_is_capped(self) -> None:
        many = b"<html><head><meta name='citation_title' content='T'/>" + b"".join(
            f"<meta name='citation_author' content='Author {i}'/>".encode() for i in range(30)
        ) + b"</head></html>"
        card = extract_card("https://arxiv.org/abs/x", "text/html", many)
        self.assertEqual(len(card["authors"]), 10)


class FeedUnfurlSsrfTest(unittest.TestCase):
    def test_rejects_private_and_non_http(self) -> None:
        for bad in (
            "http://127.0.0.1/x",
            "http://localhost/x",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/",
            "http://[::1]/",
            "file:///etc/passwd",
            "ftp://example.com/x",
            "http://example.com:22/",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(UnfurlError):
                    unfurl(bad)


if __name__ == "__main__":
    unittest.main()
