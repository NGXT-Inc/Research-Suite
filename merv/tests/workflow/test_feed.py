"""Social feed service tests (Feed_PRD.md)."""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from pathlib import Path as _P

from fastapi.testclient import TestClient

from tests.support.brain import TestBrain
from merv.brain.feed import feed_policy
from merv.brain.feed.facade import Feed
from merv.shared.feed_embeds import MAX_FEED_EMBED_BYTES, wrap_embed_html
from merv.shared.feed_images import SERVEABLE_IMAGE_TYPES, sniff_image_type
from merv.brain.feed.feed import POST_TEXT_MAX, REACTION_KINDS
from merv.brain.feed.feed_unfurl import UnfurlError, extract_card, unfurl
from merv.brain.surface.transport.http_api import create_fastapi_app
from merv.brain.surface.transport.feed_http import _image_headers
from merv.brain.kernel.utils import NotFoundError, ValidationError

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

# Minimal artifact bodies that satisfy the workflow gate lints (plan spine,
# report spine, graph envelope) — used only to drive one experiment all the
# way to `complete` for the feed_note transition-integration test below.
_VALID_PLAN = (
    "## Summary\n"
    "A toy experiment used to exercise the feed_note attach point.\n\n"
    "## Objective & hypothesis\n"
    "Test that the threshold rule beats the majority baseline.\n\n"
    "## Evaluation\n"
    "Metric: accuracy vs the majority-class baseline; success if accuracy > 0.6.\n"
)

_VALID_REPORT = (
    "## Summary\n"
    "Ran the toy experiment per the approved plan.\n\n"
    "## Results\n\n"
    "| Metric | Target | Achieved |\n"
    "|--------|--------|----------|\n"
    "| accuracy | 0.60 | 0.72 |\n\n"
    "## Deviations from plan\n"
    "None.\n\n"
    "## Conclusion\n"
    "Decision rule met: accuracy 0.72 > 0.6 threshold.\n"
)

_VALID_GRAPH = (
    '{"version": 1, "nodes": ['
    '{"id": "obj", "kind": "objective", "label": "Beat the majority baseline"},'
    '{"id": "out", "kind": "outcome", "label": "Threshold met at 0.72"}],'
    ' "edges": [{"from": "obj", "to": "out", "label": "confirmed by"}]}\n'
)


class FeedServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.pid = self.call("project", action="create", name="Feed Test")["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    # -- identity -----------------------------------------------------------

    def test_register_is_idempotent_per_session(self) -> None:
        first = self.call(
            "feed.register", project_id=self.pid, handle="Nova-7", session_id="s1"
        )
        self.assertTrue(first["created"])
        again = self.call(
            "feed.register", project_id=self.pid, handle="Nova-7", session_id="s1"
        )
        self.assertFalse(again["created"])

    def test_handle_collision_across_sessions_rejected(self) -> None:
        self.call(
            "feed.register", project_id=self.pid, handle="Nova-7", session_id="s1"
        )
        with self.assertRaises(ValidationError):
            self.call(
                "feed.register", project_id=self.pid, handle="Nova-7", session_id="s2"
            )

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
            self.call(
                "feed.post",
                project_id=self.pid,
                handle="Nova-7",
                text="x" * (POST_TEXT_MAX + 1),
            )

    def test_empty_text_rejected(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaises(ValidationError):
            self.call("feed.post", project_id=self.pid, handle="Nova-7", text="   ")

    def test_image_captured_and_served(self) -> None:
        # feed.post mints a token; the PUT (agent's curl) pushes the bytes and
        # finalizes the post — the identical blob sink a live post uses.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.app.post_feed_media(
            project_id=self.pid,
            handle="Nova-7",
            text="plot",
            image_path="plot.png",
            data=_PNG,
        )
        self.assertTrue(result["post"]["has_image"])
        data, ctype = self.app.feed.get_image(
            project_id=self.pid, post_id=result["post"]["id"]
        )
        self.assertEqual(data, _PNG)
        self.assertEqual(ctype, "image/png")

    def test_media_post_mints_single_use_token(self) -> None:
        # The mint shape: feed.post with a visual returns {post_id, run} (a
        # /api/feed/u/ curl), NOT {post} — and the token is single-use.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        pending = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="plot",
            image_path="figures/plot.png",
        )
        self.assertIn("post_id", pending)
        self.assertNotIn("post", pending)
        self.assertIn("/api/feed/u/", pending["run"])
        self.assertIn("curl -sf -T", pending["run"])
        # The path label rides into the curl verbatim (the agent runs it as-is).
        self.assertIn("figures/plot.png", pending["run"])
        token = pending["run"].rsplit("/", 1)[-1].rstrip("'")
        first = self.app.upload_feed_bytes(token=token, data=_PNG)
        self.assertEqual(first["post"]["id"], pending["post_id"])
        self.assertTrue(first["post"]["has_image"])
        # Re-running the same curl 404s: the token was consumed at completion.
        with self.assertRaises(NotFoundError):
            self.app.upload_feed_bytes(token=token, data=_PNG)

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
        result = self.app.post_feed_media(
            project_id=self.pid,
            handle="Nova-7",
            text="vec",
            image_path="chart.svg",
            data=_SVG,
        )
        self.assertTrue(result["post"]["has_image"])
        data, ctype = self.app.feed.get_image(
            project_id=self.pid, post_id=result["post"]["id"]
        )
        self.assertEqual(data, _SVG)
        self.assertEqual(ctype, "image/svg+xml")

    def test_media_upload_enforces_the_image_byte_cap(self) -> None:
        # The transport streams against MAX_FEED_IMAGE_BYTES and refuses an
        # oversized body with 413 before the post is written.
        from merv.shared.feed_images import MAX_FEED_IMAGE_BYTES

        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        pending = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="huge",
            image_path="big.png",
        )
        token = pending["run"].rsplit("/", 1)[-1].rstrip("'")
        response = self.app._client.put(
            f"/api/feed/u/{token}", content=b"x" * (MAX_FEED_IMAGE_BYTES + 1)
        )
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["error_code"], "payload_too_large")

    def test_chunked_media_rejects_before_buffering_over_cap(self) -> None:
        import asyncio

        from merv.brain.surface.transport.feed_http import _read_capped

        class _UniterableChunk:
            def __len__(self) -> int:
                return 17

            def __iter__(self):
                raise AssertionError("over-cap chunk was buffered")

        async def _stream():
            yield _UniterableChunk()

        class _ChunkedRequest:
            headers: dict[str, str] = {}

            def stream(self):
                return _stream()

        self.assertIsNone(asyncio.run(_read_capped(_ChunkedRequest(), cap=16)))

    def test_media_post_is_absent_until_the_upload_lands(self) -> None:
        # Minting does not create the post: feed.list stays empty until the PUT.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="pending",
            image_path="plot.png",
        )
        self.assertEqual(self.call("feed.list", project_id=self.pid)["posts"], [])

    def test_feed_post_mints_regardless_of_local_file(self) -> None:
        # The server never reads the path at mint time (the agent's curl does),
        # so a path that does not exist locally still mints a valid token.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        pending = self.app.feed.post(
            project_id=self.pid,
            handle="Nova-7",
            text="plot",
            image_path="does/not/exist.png",
        )
        self.assertIn("post_id", pending)
        self.assertIn("/api/feed/u/", pending["run"])

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

    def test_feed_post_preflights_handle_before_minting(self) -> None:
        # The mint validates the handle up front, so an unregistered author is
        # rejected before any token is minted (the image path is never read).
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
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="see",
            url="http://127.0.0.1/secret",
        )
        preview = result["post"]["link_preview"]
        self.assertTrue(preview and preview.get("error"))

    def test_non_web_scheme_never_stores_a_clickable_link(self) -> None:
        # javascript:/data: are attacker-shaped, not degradable — the post
        # survives but nothing that could become an href is persisted.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        for url in (
            "javascript:alert(1)",
            "data:text/html,<script>1</script>",
            "file:///etc/passwd",
        ):
            with self.subTest(url=url):
                result = self.call(
                    "feed.post",
                    project_id=self.pid,
                    handle="Nova-7",
                    text="see",
                    url=url,
                )
                post = result["post"]
                self.assertIsNone(post["link_url"])
                self.assertFalse(post["link_preview"]["url"])
                self.assertTrue(post["link_preview"]["error"])

    def test_post_kind_persists_and_lists(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="ruled out",
            kind="kill",
        )
        self.assertEqual(result["post"]["kind"], "kill")
        posts = self.call("feed.list", project_id=self.pid)["posts"]
        self.assertEqual(posts[0]["kind"], "kill")

    def test_post_kind_optional_and_validated(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        plain = self.call("feed.post", project_id=self.pid, handle="Nova-7", text="hi")
        self.assertIsNone(plain["post"]["kind"])
        # The MCP contract enum rejects bad kinds up front; the service check
        # covers the daemon/HTTP paths that bypass pydantic.
        with self.assertRaisesRegex(ValidationError, "unknown post kind"):
            self.app.feed.validate_post_intent(
                project_id=self.pid, handle="Nova-7", text="x", kind="rant"
            )

    def test_status_kind_is_accepted(self) -> None:
        # `status` marks a mid-run checkpoint in a live experiment thread —
        # the sixth kind, added alongside finding/hunch/bottleneck/kill/direction.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="40% through training, no surprises yet",
            kind="status",
        )
        self.assertEqual(result["post"]["kind"], "status")
        posts = self.call("feed.list", project_id=self.pid)["posts"]
        self.assertEqual(posts[0]["kind"], "status")

    def test_kind_column_migration_is_idempotent(self) -> None:
        # Rebuilding the service on an existing DB must survive the ALTER.
        from merv.brain.feed.feed import FeedService

        FeedService(
            store=self.app.store,
            blobs=self.app.feed.blobs,
            link_unfurl=self.app.feed.link_unfurl,
        )
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="still fine",
            kind="finding",
        )
        self.assertEqual(result["post"]["kind"], "finding")

    def test_post_view_does_not_leak_blob_hash(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.app.post_feed_media(
            project_id=self.pid,
            handle="Nova-7",
            text="x",
            image_path="p.png",
            data=_PNG,
        )
        self.assertNotIn("image_sha256", result["post"])

    def test_every_post_view_carries_a_reactions_map(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.call("feed.post", project_id=self.pid, handle="Nova-7", text="hi")
        self.assertEqual(
            result["post"]["reactions"], {k: False for k in REACTION_KINDS}
        )

    # -- reactions ------------------------------------------------------------

    def test_reaction_toggle_is_idempotent(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        post_id = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="hi"
        )["post"]["id"]
        first = self.app.feed.set_reaction(
            project_id=self.pid, post_id=post_id, kind="fire", on=True
        )
        self.assertTrue(first["post"]["reactions"]["fire"])
        # Setting it on again is a no-op, not an error (idempotent insert).
        again = self.app.feed.set_reaction(
            project_id=self.pid, post_id=post_id, kind="fire", on=True
        )
        self.assertTrue(again["post"]["reactions"]["fire"])
        cleared = self.app.feed.set_reaction(
            project_id=self.pid, post_id=post_id, kind="fire", on=False
        )
        self.assertFalse(cleared["post"]["reactions"]["fire"])
        # Clearing an already-clear reaction is also a no-op.
        cleared_again = self.app.feed.set_reaction(
            project_id=self.pid, post_id=post_id, kind="fire", on=False
        )
        self.assertFalse(cleared_again["post"]["reactions"]["fire"])

    def test_reaction_rejects_unknown_kind(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        post_id = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="hi"
        )["post"]["id"]
        with self.assertRaises(ValidationError):
            self.app.feed.set_reaction(
                project_id=self.pid, post_id=post_id, kind="love", on=True
            )

    def test_reaction_rejects_missing_post(self) -> None:
        with self.assertRaises(NotFoundError):
            self.app.feed.set_reaction(
                project_id=self.pid, post_id="post_missing", kind="fire", on=True
            )

    def test_researcher_attention_surfaces_reacted_posts_on_first_page(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        post_a = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="a" * 100
        )["post"]["id"]
        self.call("feed.post", project_id=self.pid, handle="Nova-7", text="untouched")
        self.app.feed.set_reaction(
            project_id=self.pid, post_id=post_a, kind="eyes", on=True
        )
        first = self.call("feed.list", project_id=self.pid)
        self.assertIn("researcher_attention", first)
        attention = first["researcher_attention"]
        self.assertEqual(len(attention), 1)
        self.assertEqual(attention[0]["post_id"], post_a)
        self.assertEqual(attention[0]["reactions"], ["eyes"])
        self.assertEqual(attention[0]["text_snippet"], ("a" * 100)[:80])
        # A paginated read must not recompute/carry it (nudge-like: first page only).
        paged = self.call("feed.list", project_id=self.pid, before_seq=10_000_000)
        self.assertNotIn("researcher_attention", paged)

    def test_researcher_attention_absent_when_nothing_reacted(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        self.call("feed.post", project_id=self.pid, handle="Nova-7", text="hi")
        first = self.call("feed.list", project_id=self.pid)
        self.assertNotIn("researcher_attention", first)

    def test_researcher_attention_caps_at_five(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        for i in range(7):
            post_id = self.call(
                "feed.post", project_id=self.pid, handle="Nova-7", text=f"post {i}"
            )["post"]["id"]
            self.app.feed.set_reaction(
                project_id=self.pid, post_id=post_id, kind="fire", on=True
            )
        first = self.call("feed.list", project_id=self.pid)
        self.assertEqual(len(first["researcher_attention"]), 5)

    # -- researcher replies -----------------------------------------------------

    def test_researcher_reply_creates_threaded_post_and_registers_handle(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        original = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="finding"
        )["post"]["id"]
        result = self.app.feed.researcher_reply(
            project_id=self.pid, post_id=original, text="nice catch"
        )
        reply = result["post"]
        self.assertEqual(reply["author_handle"], "Researcher")
        self.assertEqual(reply["author_role"], "researcher")
        self.assertEqual(reply["in_reply_to"], original)
        posts = self.call("feed.list", project_id=self.pid)["posts"]
        self.assertEqual([p["text"] for p in posts], ["nice catch", "finding"])

    def test_researcher_reply_enforces_char_cap(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        original = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="finding"
        )["post"]["id"]
        with self.assertRaises(ValidationError):
            self.app.feed.researcher_reply(
                project_id=self.pid, post_id=original, text="x" * (POST_TEXT_MAX + 1)
            )

    def test_agent_post_can_thread_via_in_reply_to(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        original = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="finding"
        )["post"]["id"]
        result = self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="follow-up",
            in_reply_to=original,
        )
        self.assertEqual(result["post"]["in_reply_to"], original)

    def test_in_reply_to_validates_target_exists(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaisesRegex(ValidationError, "in_reply_to"):
            self.call(
                "feed.post",
                project_id=self.pid,
                handle="Nova-7",
                text="follow-up",
                in_reply_to="post_missing",
            )

    def test_researcher_reply_does_not_reset_cold_feed_clock(self) -> None:
        # A researcher reply is not agent activity: the nudge clock must still
        # measure from the last AGENT post, not the reply.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        original = self.call(
            "feed.post", project_id=self.pid, handle="Nova-7", text="finding"
        )["post"]["id"]
        with self.app.store.transaction() as conn:
            before = self.app.feed.feed_signal(project_id=self.pid, conn=conn)
        self.app.feed.researcher_reply(
            project_id=self.pid, post_id=original, text="nice catch"
        )
        with self.app.store.transaction() as conn:
            after = self.app.feed.feed_signal(project_id=self.pid, conn=conn)
        self.assertEqual(before["last_post_at"], after["last_post_at"])

    # -- embeds -----------------------------------------------------------------

    def test_embed_size_cap_enforced(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        oversized = b"<html><body>" + b"x" * MAX_FEED_EMBED_BYTES + b"</body></html>"
        with self.assertRaises(ValidationError):
            self.app.feed.post_observed(
                project_id=self.pid,
                handle="Nova-7",
                text="chart",
                html_path="chart.html",
                html_bytes=oversized,
            )

    def test_embed_rejects_non_html_bytes(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaises(ValidationError):
            self.app.feed.post_observed(
                project_id=self.pid,
                handle="Nova-7",
                text="chart",
                html_path="chart.html",
                html_bytes=_PNG,
            )

    def test_embed_captured_and_served_wrapped(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        fragment = b"<div>hello</div>"
        result = self.app.feed.post_observed(
            project_id=self.pid,
            handle="Nova-7",
            text="chart",
            html_path="chart.html",
            html_bytes=fragment,
        )
        self.assertTrue(result["post"]["has_embed"])
        wrapped = self.app.feed.get_embed(
            project_id=self.pid, post_id=result["post"]["id"]
        )
        self.assertIn("<div>hello</div>", wrapped)
        self.assertIn("Content-Security-Policy", wrapped)

    def test_image_and_embed_are_mutually_exclusive(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaisesRegex(ValidationError, "image or an embed"):
            self.app.feed.post_observed(
                project_id=self.pid,
                handle="Nova-7",
                text="both",
                image_path="p.png",
                image_bytes=_PNG,
                html_path="c.html",
                html_bytes=b"<html><body>x</body></html>",
            )

    def test_html_post_mints_and_upload_serves_wrapped_embed(self) -> None:
        # An html_path post mints a token; the PUT pushes the embed bytes and
        # the served document is CSP-wrapped, same as post_observed did.
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        result = self.app.post_feed_media(
            project_id=self.pid,
            handle="Nova-7",
            text="chart",
            html_path="chart.html",
            data=b"<div>hello</div>",
        )
        self.assertTrue(result["post"]["has_embed"])
        wrapped = self.app.feed.get_embed(
            project_id=self.pid, post_id=result["post"]["id"]
        )
        self.assertIn("<div>hello</div>", wrapped)

    def test_image_and_html_path_together_rejected_at_mint(self) -> None:
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        with self.assertRaisesRegex(ValidationError, "image or an embed"):
            self.call(
                "feed.post",
                project_id=self.pid,
                handle="Nova-7",
                text="both",
                image_path="p.png",
                html_path="c.html",
            )

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
        orig_events, orig_hours = (
            feed_policy.NUDGE_AFTER_EVENTS,
            feed_policy.NUDGE_AFTER_HOURS,
        )
        feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = 1, 0.0
        try:
            self.call("claim.create", project_id=self.pid, statement="a claim")
            with self.app.store.transaction() as conn:
                nudge = self.app.feed.feed_nudge(project_id=self.pid, conn=conn)
            self.assertIsNotNone(nudge)
            self.assertTrue(nudge["should_post"])
        finally:
            feed_policy.NUDGE_AFTER_EVENTS, feed_policy.NUDGE_AFTER_HOURS = (
                orig_events,
                orig_hours,
            )


class FeedEmbedWrapTest(unittest.TestCase):
    def test_fragment_gets_wrapped_in_minimal_document(self) -> None:
        wrapped = wrap_embed_html(b"<div>hi</div>")
        self.assertTrue(wrapped.lower().startswith("<!doctype html>"))
        self.assertIn("<div>hi</div>", wrapped)
        self.assertIn("Content-Security-Policy", wrapped)

    def test_full_document_gets_csp_as_first_head_child(self) -> None:
        doc = b"<!doctype html><html><head><title>t</title></head><body>x</body></html>"
        wrapped = wrap_embed_html(doc)
        head_start = wrapped.lower().index("<head")
        head_close = wrapped.index(">", head_start) + 1
        self.assertTrue(
            wrapped[head_close:].startswith(
                '<meta http-equiv="Content-Security-Policy"'
            )
        )
        self.assertIn("<title>t</title>", wrapped)

    def test_wrapped_csp_forbids_scripts_by_default_src(self) -> None:
        wrapped = wrap_embed_html(b"<div>hi</div>")
        self.assertIn("default-src 'none'", wrapped)

    def test_header_fragment_is_not_mistaken_for_head(self) -> None:
        wrapped = wrap_embed_html(b"<header>title bar</header><div>body</div>")
        self.assertTrue(wrapped.lower().startswith("<!doctype html>"))
        head_start = wrapped.lower().index("<head>")
        head_close = head_start + len("<head>")
        self.assertTrue(
            wrapped[head_close:].startswith(
                '<meta http-equiv="Content-Security-Policy"'
            )
        )
        self.assertIn("<header>title bar</header>", wrapped)


class FeedHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.client = TestClient(create_fastapi_app(self.app.http))
        self.pid = self.app.call_tool(
            "project", {"action": "create", "name": "Feed HTTP Test"}
        )["id"]
        self.app.call_tool(
            "feed.register", {"project_id": self.pid, "handle": "Nova-7"}
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reaction_endpoint_returns_updated_post_view(self) -> None:
        post_id = self.app.call_tool(
            "feed.post", {"project_id": self.pid, "handle": "Nova-7", "text": "hi"}
        )["post"]["id"]
        response = self.client.post(
            f"/api/projects/{self.pid}/feed/{post_id}/reactions",
            json={"kind": "fire", "on": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["post"]["reactions"]["fire"])

    def test_reply_endpoint_creates_researcher_post(self) -> None:
        post_id = self.app.call_tool(
            "feed.post", {"project_id": self.pid, "handle": "Nova-7", "text": "hi"}
        )["post"]["id"]
        response = self.client.post(
            f"/api/projects/{self.pid}/feed/{post_id}/reply",
            json={"text": "nice"},
        )
        self.assertEqual(response.status_code, 200)
        reply = response.json()["post"]
        self.assertEqual(reply["author_role"], "researcher")
        self.assertEqual(reply["in_reply_to"], post_id)

    def test_feed_listing_enriches_embed_url(self) -> None:
        post_id = self.app.feed.post_observed(
            project_id=self.pid,
            handle="Nova-7",
            text="chart",
            html_path="chart.html",
            html_bytes=b"<html><body>x</body></html>",
        )["post"]["id"]
        response = self.client.get(f"/api/projects/{self.pid}/feed")
        self.assertEqual(response.status_code, 200)
        posts = {p["id"]: p for p in response.json()["posts"]}
        self.assertEqual(
            posts[post_id]["embed_url"],
            f"/api/projects/{self.pid}/feed/{post_id}/embed",
        )

    def test_embed_endpoint_serves_wrapped_html_with_sandbox_headers(self) -> None:
        post_id = self.app.feed.post_observed(
            project_id=self.pid,
            handle="Nova-7",
            text="chart",
            html_path="chart.html",
            html_bytes=b"<html><body><script>1</script></body></html>",
        )["post"]["id"]
        response = self.client.get(f"/api/projects/{self.pid}/feed/{post_id}/embed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        csp = response.headers["content-security-policy"]
        self.assertIn("sandbox allow-scripts", csp)
        self.assertIn("default-src 'none'", csp)
        self.assertIn("<script>1</script>", response.text)

    def test_media_upload_token_is_redacted_in_activity_log(self) -> None:
        # INV-12: the one-time token is a bearer credential in the URL path; the
        # activity log must persist only the redacted form, never the token.
        import io

        pending = self.app.call_tool(
            "feed.post",
            {
                "project_id": self.pid,
                "handle": "Nova-7",
                "text": "plot",
                "image_path": "plot.png",
            },
        )
        token = pending["run"].rsplit("/", 1)[-1].rstrip("'")
        buffer = io.StringIO()
        logger = self.app.http.structured_log
        logger.enabled = True
        logger._stream = buffer
        response = self.client.put(f"/api/feed/u/{token}", content=_PNG)
        self.assertEqual(response.status_code, 200, response.text)
        logged = buffer.getvalue()
        self.assertIn("/api/feed/u/<redacted>", logged)
        self.assertNotIn(token, logged)


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
        card = extract_card(
            "https://arxiv.org/abs/1608.03983", "text/html", _ARXIV_HTML
        )
        self.assertEqual(card["kind"], "paper")
        self.assertEqual(
            card["title"], "SGDR: Stochastic Gradient Descent with Warm Restarts"
        )
        self.assertEqual(card["authors"], ["Loshchilov, Ilya", "Hutter, Frank"])
        self.assertEqual(card["year"], "2016")
        self.assertTrue(card["trusted"])
        self.assertEqual(card["image_url"], "https://arxiv.org/static/arxiv-logo.png")

    def test_citation_meta_marks_paper_on_any_host(self) -> None:
        card = extract_card("https://journal.example.org/x", "text/html", _ARXIV_HTML)
        self.assertEqual(card["kind"], "paper")
        self.assertFalse(card["trusted"])

    def test_repo_card_by_host(self) -> None:
        card = extract_card(
            "https://github.com/huggingface/peft", "text/html", _REPO_HTML
        )
        self.assertEqual(card["kind"], "repo")
        self.assertEqual(card["authors"], [])
        self.assertEqual(card["year"], "")

    def test_plain_page_card(self) -> None:
        card = extract_card("https://blog.example.com/post", "text/html", _BLOG_HTML)
        self.assertEqual(card["kind"], "page")
        self.assertEqual(card["title"], "Some post")

    def test_non_html_is_minimal_page_card(self) -> None:
        card = extract_card(
            "https://example.com/paper.pdf", "application/pdf", b"%PDF-1.5"
        )
        self.assertEqual(card["kind"], "page")
        self.assertEqual(card["title"], "")
        self.assertEqual(card["authors"], [])

    def test_author_list_is_capped(self) -> None:
        many = (
            b"<html><head><meta name='citation_title' content='T'/>"
            + b"".join(
                f"<meta name='citation_author' content='Author {i}'/>".encode()
                for i in range(30)
            )
            + b"</head></html>"
        )
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


class FeedUnfurlArxivPdfTest(unittest.TestCase):
    """Direct arxiv PDF links unfurl via their /abs/ page for citation meta."""

    _ABS_HTML = (
        b"<html><head>"
        b'<meta name="citation_title" content="LoRA: Low-Rank Adaptation">'
        b'<meta name="citation_author" content="Hu, Edward J.">'
        b'<meta name="citation_date" content="2021/06/17">'
        b"</head><body></body></html>"
    )

    def test_pdf_link_fetches_abs_page_and_keeps_pdf_url(self) -> None:
        cases = {
            "https://arxiv.org/pdf/2106.09685#page=7": "2106.09685",
            "https://arxiv.org/pdf/2106.09685v2": "2106.09685v2",
            "http://export.arxiv.org/pdf/cond-mat/0703470.pdf": "cond-mat/0703470",
        }
        for pdf_url, arxiv_id in cases.items():
            fetched: list[str] = []

            def fake_fetch(url, *a, _fetched=fetched, **kw):
                _fetched.append(url)
                return (url, "text/html", self._ABS_HTML)

            with self.subTest(url=pdf_url):
                with unittest.mock.patch(
                    "merv.brain.feed.feed_unfurl.safe_fetch", fake_fetch
                ):
                    card = unfurl(pdf_url)
                self.assertEqual(fetched, [f"https://arxiv.org/abs/{arxiv_id}"])
                self.assertEqual(card["url"], pdf_url)
                self.assertEqual(card["kind"], "paper")
                self.assertEqual(card["title"], "LoRA: Low-Rank Adaptation")
                self.assertEqual(card["authors"], ["Hu, Edward J."])
                self.assertEqual(card["year"], "2021")

    def test_non_pdf_arxiv_link_is_untouched(self) -> None:
        fetched: list[str] = []

        def fake_fetch(url, *a, **kw):
            fetched.append(url)
            return (url, "text/html", self._ABS_HTML)

        with unittest.mock.patch("merv.brain.feed.feed_unfurl.safe_fetch", fake_fetch):
            card = unfurl("https://arxiv.org/abs/2106.09685")
        self.assertEqual(fetched, ["https://arxiv.org/abs/2106.09685"])
        self.assertEqual(card["url"], "https://arxiv.org/abs/2106.09685")


class FeedNoteForTest(unittest.TestCase):
    """Unit coverage for FeedService.feed_note_for (Part 2's dedupe helper):
    other services never touch the posts table directly (test_module_boundaries
    enforces that), they call this. Wiring it into tool responses is covered
    separately (tests/surface/test_feed_note_attach.py) plus one end-to-end
    transition test below."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.pid = self.app.call_tool(
            "project", {"action": "create", "name": "Feed Note Test"}
        )["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_returns_a_note_for_an_unmentioned_entity(self) -> None:
        note = self.app.feed.feed_note_for(
            project_id=self.pid,
            entity_id="exp_unmentioned",
            event="experiment_complete",
        )
        self.assertIsNotNone(note)
        self.assertIn("exp_unmentioned", note)
        self.assertIn("feed-posting skill", note)

    def test_service_implements_the_public_transition_advisory(self) -> None:
        self.assertIsInstance(self.app.feed, Feed)
        note = self.app.feed.transition_advisory(
            project_id=self.pid,
            experiment_id="exp_public",
            event="experiment_complete",
        )
        self.assertIsNotNone(note)
        self.assertIn("exp_public", note)

    def test_none_when_a_post_ref_names_the_entity(self) -> None:
        self.app.feed.register(handle="Nova-7", project_id=self.pid)
        self.app.feed.post(
            handle="Nova-7", project_id=self.pid, text="wrapped up", ref="exp_ref_1"
        )
        note = self.app.feed.feed_note_for(
            project_id=self.pid, entity_id="exp_ref_1", event="experiment_complete"
        )
        self.assertIsNone(note)

    def test_none_when_a_posts_text_mentions_the_entity_inline(self) -> None:
        self.app.feed.register(handle="Nova-7", project_id=self.pid)
        self.app.feed.post(
            handle="Nova-7",
            project_id=self.pid,
            text="wrapping up exp_inline_1 today, results look solid",
        )
        note = self.app.feed.feed_note_for(
            project_id=self.pid, entity_id="exp_inline_1", event="experiment_complete"
        )
        self.assertIsNone(note)

    def test_an_unrelated_post_does_not_suppress_the_note(self) -> None:
        self.app.feed.register(handle="Nova-7", project_id=self.pid)
        self.app.feed.post(
            handle="Nova-7", project_id=self.pid, text="something else entirely"
        )
        note = self.app.feed.feed_note_for(
            project_id=self.pid, entity_id="exp_untouched", event="experiment_complete"
        )
        self.assertIsNotNone(note)

    def test_entity_id_underscore_is_escaped_not_treated_as_a_wildcard(self) -> None:
        # LIKE's "_" matches any single char; left unescaped, a post about an
        # unrelated id that merely has the same shape would falsely look like
        # a mention of exp_12 (the "_" wildcarding one arbitrary character).
        self.app.feed.register(handle="Nova-7", project_id=self.pid)
        self.app.feed.post(
            handle="Nova-7",
            project_id=self.pid,
            text="expX12 is a different experiment",
        )
        note = self.app.feed.feed_note_for(
            project_id=self.pid, entity_id="exp_12", event="experiment_complete"
        )
        self.assertIsNotNone(note)

    def test_missing_project_id_or_entity_id_returns_none(self) -> None:
        self.assertIsNone(
            self.app.feed.feed_note_for(
                project_id="", entity_id="exp_1", event="experiment_complete"
            )
        )
        self.assertIsNone(
            self.app.feed.feed_note_for(
                project_id=self.pid, entity_id="", event="experiment_complete"
            )
        )

    def test_unknown_event_still_produces_a_generic_note(self) -> None:
        note = self.app.feed.feed_note_for(
            project_id=self.pid, entity_id="exp_x", event="some_future_event"
        )
        self.assertIsNotNone(note)
        self.assertIn("exp_x", note)


class FeedNoteTransitionIntegrationTest(unittest.TestCase):
    """End-to-end coverage of Part 2's main attach point: a real experiment,
    driven through the actual gate/review stack to `complete`, carries
    `feed_note` in experiment.transition's response when the feed has never
    mentioned it, and omits the field once a post references it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
        )
        self.pid = self.call(
            "project", action="create", name="Feed Note Transition Test"
        )["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def call(self, tool_name: str, **kwargs):
        return self.app.call_tool(tool_name, kwargs)

    def _submit(self, *, exp_id: str, path: str, role: str, body: str) -> None:
        self.app.submit_artifact(
            project_id=self.pid,
            target_type="experiment",
            target_id=exp_id,
            role=role,
            path=path,
            body=body,
        )

    def _pass_review(self, *, exp_id: str, role: str) -> None:
        req = self.call(
            "review.request",
            project_id=self.pid,
            target_type="experiment",
            target_id=exp_id,
            role=role,
        )
        session = self.call(
            "review.start",
            review_request_id=req["review_request_id"],
            reviewer_capability=req["reviewer_capability"],
            caller_session_id=f"{role}-reviewer",
        )
        self.call(
            "review.submit",
            review_session_id=session["review_session_id"],
            verdict="pass",
            synopsis="The plan and results check out, so the attempt stands as reported.",
        )

    def _drive_to_ready_for_complete(self, *, name: str) -> str:
        exp_id = self.call(
            "experiment.create",
            name=name,
            project_id=self.pid,
            intent="Feed note attach-point coverage.",
        )["id"]
        self._submit(
            exp_id=exp_id, path="plan.md", role="plan", body=_VALID_PLAN
        )
        self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="submit_design",
        )
        self._pass_review(exp_id=exp_id, role="design_reviewer")
        self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="mark_ready_to_run",
        )
        self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="start_running",
        )
        self._submit(
            exp_id=exp_id, path="results.json", role="result", body='{"metric": 1}\n'
        )
        self._submit(
            exp_id=exp_id, path="report.md", role="report", body=_VALID_REPORT
        )
        self._submit(
            exp_id=exp_id, path="graph.json", role="graph", body=_VALID_GRAPH
        )
        self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="submit_results",
        )
        self._pass_review(exp_id=exp_id, role="experiment_reviewer")
        return exp_id

    def test_complete_transition_carries_feed_note_when_feed_is_silent(self) -> None:
        exp_id = self._drive_to_ready_for_complete(name="exp-silent")
        result = self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="complete",
        )
        self.assertEqual(result["status"], "complete")
        self.assertIn("feed_note", result)
        self.assertIn(exp_id, result["feed_note"])

    def test_complete_transition_omits_feed_note_once_a_post_mentions_it(self) -> None:
        exp_id = self._drive_to_ready_for_complete(name="exp-mentioned")
        self.call("feed.register", project_id=self.pid, handle="Nova-7")
        self.call(
            "feed.post",
            project_id=self.pid,
            handle="Nova-7",
            text="wrapping this one up now",
            ref=exp_id,
        )
        result = self.call(
            "experiment.transition",
            project_id=self.pid,
            experiment_id=exp_id,
            transition="complete",
        )
        self.assertEqual(result["status"], "complete")
        self.assertNotIn("feed_note", result)


if __name__ == "__main__":
    unittest.main()
