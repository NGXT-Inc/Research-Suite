from __future__ import annotations

import asyncio
import time
import unittest

import httpx
from fastapi import FastAPI

from backend.transport.mcp_http import register_mcp_routes


class McpCallThreadpoolTest(unittest.TestCase):
    """A slow synchronous tool call must not stall the event loop for
    every other agent and UI request (it runs in the threadpool)."""

    SLOW_SECONDS = 0.5

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        def call_tool(name, arguments, context, request):
            if name == "slow.tool":
                time.sleep(self.SLOW_SECONDS)
            return {"ok": True, "name": name}

        register_mcp_routes(
            app,
            list_tools=lambda: [{"name": "slow.tool"}],
            call_tool=call_tool,
        )
        return app

    def test_slow_sync_tool_call_does_not_block_the_event_loop(self) -> None:
        app = self._build_app()

        async def scenario() -> None:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://mcp"
            ) as client:
                started = time.monotonic()
                slow = asyncio.create_task(
                    client.post(
                        "/mcp/call", json={"name": "slow.tool", "arguments": {}}
                    )
                )
                await asyncio.sleep(0.05)
                fast = await client.get("/mcp/tools")
                elapsed = time.monotonic() - started
                self.assertEqual(fast.status_code, 200)
                # With the loop blocked, the fast request cannot complete
                # before the slow tool call's sleep finishes.
                self.assertLess(elapsed, self.SLOW_SECONDS * 0.9)
                response = await slow
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["result"]["name"], "slow.tool")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
