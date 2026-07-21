"""merv-client login device flow + proxy session refresh (stdlib wire mocks)."""

from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from merv.client.cli import configure_client, main
from merv.proxy import __version__ as PROXY_VERSION
from merv.proxy.proxy import HttpProxyMcpServer, ProxyConfig


def _response(payload: dict) -> mock.MagicMock:
    body = json.dumps(payload).encode("utf-8")
    ctx = mock.MagicMock()
    ctx.read.return_value = body
    ctx.__enter__ = mock.Mock(return_value=ctx)
    ctx.__exit__ = mock.Mock(return_value=False)
    return ctx


def _proxy_headers(proxy: HttpProxyMcpServer, *, is_cloud: bool) -> dict[str, str]:
    return proxy._credentials.headers(
        is_cloud=is_cloud,
        client_version=PROXY_VERSION,
    )


class DeviceFlowLoginTest(unittest.TestCase):
    def test_login_polls_and_stores_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            responses = [
                _response({"session_id": "sess1", "auth_url": "https://ui/auth/sdk?session=sess1"}),
                _response({"status": "pending"}),
                _response(
                    {
                        "status": "complete",
                        "access_token": "jwt-abc",
                        "refresh_token": "refresh-1",
                        "expires_in": 3600,
                        "email": "user@example.com",
                    }
                ),
            ]
            with (
                mock.patch("merv.client.cli.urllib.request.urlopen", side_effect=responses) as opened,
                mock.patch("merv.client.cli.webbrowser.open", return_value=True) as browser,
                mock.patch("merv.client.cli.time.sleep"),
                mock.patch("sys.stdout", new=io.StringIO()) as out,
            ):
                code = main(["--config", str(config_path), "login", "--control-url", "https://brain.example"])
            self.assertEqual(code, 0)
            browser.assert_called_once_with("https://ui/auth/sdk?session=sess1")
            self.assertEqual(opened.call_count, 3)
            stored = json.loads(config_path.read_text())
            self.assertEqual(stored["access_token"], "jwt-abc")
            self.assertEqual(stored["refresh_token"], "refresh-1")
            self.assertEqual(stored["email"], "user@example.com")
            self.assertGreater(int(stored["expires_at"]), time.time())
            self.assertIn("Logged in as user@example.com", out.getvalue())
            # Secrets never echo: only set/signed-in markers.
            self.assertNotIn("jwt-abc", out.getvalue())

    def test_login_with_api_key_skips_the_browser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            with (
                mock.patch("merv.client.cli.webbrowser.open") as browser,
                mock.patch("sys.stdout", new=io.StringIO()),
            ):
                code = main(["--config", str(config_path), "login", "--api-key", "rr_sk_x"])
            self.assertEqual(code, 0)
            browser.assert_not_called()
            self.assertEqual(json.loads(config_path.read_text())["api_key"], "rr_sk_x")

    def test_reconfigure_preserves_the_stored_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            configure_client(
                config_path=config_path,
                control_url="https://brain.example",
                session={"access_token": "jwt", "refresh_token": "r", "expires_at": 9, "email": "e@x"},
            )
            configure_client(config_path=config_path, control_url="https://brain2.example")
            stored = json.loads(config_path.read_text())
            self.assertEqual(stored["control_url"], "https://brain2.example")
            self.assertEqual(stored["access_token"], "jwt")


class ProxySessionRefreshTest(unittest.TestCase):
    def test_headers_refresh_an_expiring_session_and_persist_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "control_url": "https://brain.example",
                        "access_token": "jwt-old",
                        "refresh_token": "refresh-1",
                        "expires_at": int(time.time()) + 10,  # inside the skew
                    }
                )
            )
            proxy = HttpProxyMcpServer(
                config=ProxyConfig(
                    repo_root=Path(tmp),
                    control_url="https://brain.example",
                    api_key="",
                    client_config_path=config_path,
                )
            )
            with mock.patch(
                "merv.proxy.proxy.urlopen",
                return_value=_response(
                    {"access_token": "jwt-new", "refresh_token": "refresh-2", "expires_in": 3600}
                ),
            ) as opened:
                headers = _proxy_headers(proxy, is_cloud=True)
            self.assertEqual(headers["Authorization"], "Bearer jwt-new")
            refresh_request = opened.call_args[0][0]
            self.assertEqual(
                refresh_request.full_url, "https://brain.example/api/sdk/auth/refresh"
            )
            stored = json.loads(config_path.read_text())
            self.assertEqual(stored["access_token"], "jwt-new")
            self.assertEqual(stored["refresh_token"], "refresh-2")

    def test_fresh_session_and_api_key_never_hit_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "client.json"
            config_path.write_text(
                json.dumps(
                    {
                        "access_token": "jwt-live",
                        "refresh_token": "refresh-1",
                        "expires_at": int(time.time()) + 3600,
                    }
                )
            )
            proxy = HttpProxyMcpServer(
                config=ProxyConfig(
                    repo_root=Path(tmp),
                    control_url="https://brain.example",
                    client_config_path=config_path,
                )
            )
            with mock.patch("merv.proxy.proxy.urlopen") as opened:
                self.assertEqual(
                    _proxy_headers(proxy, is_cloud=True)["Authorization"], "Bearer jwt-live"
                )
                keyed = HttpProxyMcpServer(
                    config=ProxyConfig(
                        repo_root=Path(tmp),
                        control_url="https://brain.example",
                        api_key="rr_sk_k",
                        client_config_path=config_path,
                    )
                )
                self.assertEqual(
                    _proxy_headers(keyed, is_cloud=True)["Authorization"], "Bearer rr_sk_k"
                )
            opened.assert_not_called()

    def test_loopback_sends_no_credential(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proxy = HttpProxyMcpServer(
                config=ProxyConfig(repo_root=Path(tmp), control_url="http://127.0.0.1:8787")
            )
            self.assertNotIn("Authorization", _proxy_headers(proxy, is_cloud=False))


if __name__ == "__main__":
    unittest.main()
