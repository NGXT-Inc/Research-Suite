from __future__ import annotations

import io
import unittest
from typing import Any
from unittest.mock import call, patch
from urllib.error import HTTPError, URLError

from merv.brain.sandbox.execution.backends._http import request_json
from merv.brain.sandbox.execution.backends.digitalocean.client import (
    DigitalOceanClient,
)
from merv.brain.sandbox.execution.backends.digitalocean.config import (
    DigitalOceanCloudConfig,
)
from merv.brain.sandbox.execution.backends.hyperstack.client import HyperstackClient
from merv.brain.sandbox.execution.backends.hyperstack.config import (
    HyperstackCloudConfig,
)
from merv.brain.sandbox.execution.backends.lambda_labs.client import (
    LambdaCloudClient,
)
from merv.brain.sandbox.execution.backends.lambda_labs.config import LambdaCloudConfig
from merv.brain.sandbox.execution.backends.tensordock.client import TensorDockClient
from merv.brain.sandbox.execution.backends.tensordock.config import (
    TensorDockCloudConfig,
)
from merv.brain.sandbox.execution.backends.thunder_compute.client import (
    ThunderComputeClient,
)
from merv.brain.sandbox.execution.backends.thunder_compute.config import (
    ThunderCloudConfig,
)
from merv.brain.sandbox.execution.backends.verda.client import VerdaClient
from merv.brain.sandbox.execution.backends.verda.config import VerdaCloudConfig
from merv.brain.sandbox.execution.backends.voltage_park.client import (
    VoltageParkClient,
)
from merv.brain.sandbox.execution.backends.voltage_park.config import (
    VoltageParkCloudConfig,
)
from merv.brain.sandbox.sandbox_backend import BackendUnavailableError


URL_OPEN = "merv.brain.sandbox.execution.backends._http.urlopen"
BASE_URL = "https://provider.test/root"
TIMEOUT = 12.5


class _Response:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class ProviderHttpClientTest(unittest.TestCase):
    def _cases(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "digitalocean",
                "provider": "DigitalOcean",
                "client": DigitalOceanClient(
                    config=DigitalOceanCloudConfig(token="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Authorization", "Bearer token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0013"),
                ],
                "object_only": True,
                "http_status": 418,
            },
            {
                "name": "hyperstack",
                "provider": "Hyperstack",
                "client": HyperstackClient(
                    config=HyperstackCloudConfig(api_key="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Api_key", "token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0013"),
                ],
                "object_only": True,
                "http_status": 418,
            },
            {
                "name": "lambda",
                "provider": "Lambda Cloud",
                "client": LambdaCloudClient(
                    config=LambdaCloudConfig(api_key="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Authorization", "Bearer token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0005"),
                ],
                "object_only": True,
                "http_status": 418,
            },
            {
                "name": "tensordock",
                "provider": "TensorDock",
                "client": TensorDockClient(
                    config=TensorDockCloudConfig(token="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Authorization", "Bearer token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0013"),
                ],
                "object_only": False,
                "http_status": 418,
            },
            {
                "name": "thunder",
                "provider": "Thunder Compute",
                "client": ThunderComputeClient(
                    config=ThunderCloudConfig(api_key="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Authorization", "Bearer token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0005"),
                ],
                "object_only": True,
                "http_status": None,
            },
            {
                "name": "verda",
                "provider": "Verda",
                "client": VerdaClient(
                    config=VerdaCloudConfig(
                        client_id="id", client_secret="secret", base_url=BASE_URL
                    ),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._raw_request(
                    "POST", "/probe", body={"probe": 1}, token="token"
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0013"),
                    ("Authorization", "Bearer token"),
                ],
                "object_only": False,
                "http_status": 418,
            },
            {
                "name": "voltage_park",
                "provider": "Voltage Park",
                "client": VoltageParkClient(
                    config=VoltageParkCloudConfig(token="token", base_url=BASE_URL),
                    timeout=TIMEOUT,
                ),
                "invoke": lambda client: client._request(
                    "POST", "/probe", body={"probe": 1}
                ),
                "headers": [
                    ("Accept", "application/json"),
                    ("Authorization", "Bearer token"),
                    ("Content-type", "application/json"),
                    ("User-agent", "merv/0.0013"),
                ],
                "object_only": False,
                "http_status": 418,
            },
        ]

    def test_request_wire_contract(self) -> None:
        for case in self._cases():
            with self.subTest(provider=case["name"]):
                with patch(URL_OPEN, return_value=_Response(b'{"ok": true}')) as opened:
                    self.assertEqual(case["invoke"](case["client"]), {"ok": True})
                request = opened.call_args.args[0]
                self.assertEqual(request.full_url, f"{BASE_URL}/probe")
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.data, b'{"probe": 1}')
                self.assertEqual(opened.call_args.kwargs, {"timeout": TIMEOUT})
                self.assertEqual(request.header_items(), case["headers"])

    def test_response_shape_contract(self) -> None:
        for case in self._cases():
            with self.subTest(provider=case["name"]):
                with patch(URL_OPEN, return_value=_Response(b"[]")):
                    if case["object_only"]:
                        with self.assertRaisesRegex(
                            BackendUnavailableError,
                            f"{case['provider']} API returned a non-object response",
                        ):
                            case["invoke"](case["client"])
                    else:
                        self.assertEqual(case["invoke"](case["client"]), [])

        hyperstack = self._cases()[1]["client"]
        with patch(URL_OPEN, return_value=_Response(b"[]")):
            self.assertEqual(
                hyperstack._request(
                    "POST", "/probe", body={"probe": 1}, bare=True
                ),
                [],
            )

    def test_http_error_contract(self) -> None:
        for case in self._cases():
            with self.subTest(provider=case["name"]):
                error = HTTPError(
                    f"{BASE_URL}/probe",
                    418,
                    "teapot",
                    hdrs=None,
                    fp=io.BytesIO(b"detail"),
                )
                with patch(URL_OPEN, side_effect=error):
                    with self.assertRaises(BackendUnavailableError) as raised:
                        case["invoke"](case["client"])
                self.assertEqual(
                    str(raised.exception),
                    f"{case['provider']} API POST /probe failed with HTTP 418: detail",
                )
                self.assertEqual(raised.exception.status, case["http_status"])
                self.assertIs(raised.exception.__cause__, error)

    def test_common_transport_failures_and_empty_response(self) -> None:
        def invoke() -> Any:
            return request_json(
                provider="Probe",
                method="GET",
                base_url=BASE_URL,
                path="/probe",
                body=None,
                headers={},
                timeout=TIMEOUT,
            )

        with patch(URL_OPEN, return_value=_Response(b"")):
            self.assertEqual(invoke(), {})
        with patch(URL_OPEN, return_value=_Response(b"{")):
            with self.assertRaisesRegex(
                BackendUnavailableError, "Probe API returned invalid JSON"
            ):
                invoke()
        with patch(URL_OPEN, side_effect=URLError("offline")):
            with self.assertRaisesRegex(
                BackendUnavailableError,
                "Probe API is unreachable: <urlopen error offline>",
            ):
                invoke()
        with patch(URL_OPEN, side_effect=TimeoutError):
            with self.assertRaisesRegex(
                BackendUnavailableError, "Probe API request timed out"
            ):
                invoke()

    def test_verda_oauth_omits_auth_and_401_replays_once(self) -> None:
        client = VerdaClient(
            config=VerdaCloudConfig(
                client_id="id", client_secret="secret", base_url=BASE_URL
            ),
            timeout=TIMEOUT,
        )
        with patch(
            URL_OPEN,
            return_value=_Response(b'{"access_token": "fresh", "expires_in": 120}'),
        ) as opened:
            self.assertEqual(client._bearer_token(), "fresh")
        token_request = opened.call_args.args[0]
        self.assertEqual(token_request.full_url, f"{BASE_URL}/v1/oauth2/token")
        self.assertIsNone(token_request.get_header("Authorization"))

        unauthorized = BackendUnavailableError("unauthorized", status=401)
        with (
            patch.object(client, "_bearer_token", side_effect=["old", "new"]) as bearer,
            patch.object(
                client, "_raw_request", side_effect=[unauthorized, {"ok": True}]
            ) as raw_request,
        ):
            self.assertEqual(client._request("GET", "/v1/instances"), {"ok": True})
        self.assertEqual(bearer.call_args_list, [call(), call(force=True)])
        self.assertEqual(
            raw_request.call_args_list,
            [
                call("GET", "/v1/instances", body=None, token="old"),
                call("GET", "/v1/instances", body=None, token="new"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
