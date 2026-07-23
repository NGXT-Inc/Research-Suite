"""MultiplexingSandboxBackend: routing, id prefixes, merged catalogs."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from merv.brain.sandbox.execution import build_sandbox_backend
from merv.brain.sandbox.execution.backends.fake import FakeSandboxBackend
from merv.brain.sandbox.execution.multiplexer import MultiplexingSandboxBackend
from merv.brain.sandbox.sandbox_backend import (
    BackendCapabilities,
    BackendUnavailableError,
    BackendValidationError,
    SandboxRequest,
)


def _request(**overrides) -> SandboxRequest:
    fields = {
        "experiment_id": "exp_1",
        "project_id": "proj_1",
        "public_key": "ssh-ed25519 AAAA test",
    }
    fields.update(overrides)
    return SandboxRequest(**fields)


class MultiplexerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.alpha = FakeSandboxBackend()
        self.alpha.capabilities = BackendCapabilities(
            name="alpha", enforce_expiry=False, lifetime_extension_supported=True
        )
        self.beta = FakeSandboxBackend(requires_hardware_selection=True)
        self.beta.capabilities = BackendCapabilities(
            name="beta",
            enforce_expiry=True,
            lifetime_extension_supported=False,
            requires_hardware_selection=True,
            configurable_resources=False,
        )
        self.mux = MultiplexingSandboxBackend(
            backends={"alpha": self.alpha, "beta": self.beta},
            default="alpha",
            aliases={"beta_alias": "beta"},
        )

    # ---- routing + prefixes ----

    def test_acquire_routes_by_request_provider_and_prefixes_id(self) -> None:
        provisioned = self.mux.acquire(request=_request(provider="beta"))

        self.assertTrue(provisioned.sandbox_id.startswith("beta:"))
        self.assertEqual(len(self.beta.acquired), 1)
        self.assertEqual(len(self.alpha.acquired), 0)

    def test_acquire_defaults_and_resolves_aliases(self) -> None:
        default = self.mux.acquire(request=_request())
        aliased = self.mux.acquire(request=_request(provider="beta_alias"))

        self.assertTrue(default.sandbox_id.startswith("alpha:"))
        self.assertTrue(aliased.sandbox_id.startswith("beta:"))

    def test_acquire_unknown_provider_lists_configured(self) -> None:
        with self.assertRaisesRegex(BackendValidationError, "alpha, beta"):
            self.mux.acquire(request=_request(provider="nope"))

    def test_on_created_receives_prefixed_id(self) -> None:
        created: list[str] = []
        self.mux.acquire(
            request=_request(provider="beta"),
            on_created=lambda sid, _name: created.append(sid),
        )

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].startswith("beta:"))

    def test_id_addressed_calls_round_trip_through_owner(self) -> None:
        provisioned = self.mux.acquire(request=_request(provider="beta"))
        native = provisioned.sandbox_id.split(":", 1)[1]

        self.assertTrue(self.mux.is_alive(sandbox_id=provisioned.sandbox_id))
        self.assertTrue(self.mux.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertIn(native, self.beta.terminated)
        self.assertNotIn(native, self.alpha.terminated)
        self.assertFalse(self.mux.is_alive(sandbox_id=provisioned.sandbox_id))

    def test_unprefixed_id_routes_to_default_backend(self) -> None:
        provisioned = self.alpha.acquire(request=_request())  # legacy: no prefix

        self.assertTrue(self.mux.is_alive(sandbox_id=provisioned.sandbox_id))
        self.assertTrue(self.mux.terminate(sandbox_id=provisioned.sandbox_id))
        self.assertIn(provisioned.sandbox_id, self.alpha.terminated)

    def test_unknown_prefix_raises_instead_of_answering(self) -> None:
        # A wrong-provider 404 would read as "gone" and get a live VM's row
        # marked terminated; an unconfigured prefix must never be answered.
        with self.assertRaises(BackendUnavailableError):
            self.mux.is_alive(sandbox_id="gamma:sb-1")
        with self.assertRaises(BackendUnavailableError):
            self.mux.terminate(sandbox_id="gamma:sb-1")

    def test_find_sandbox_id_returns_prefixed_hit(self) -> None:
        self.beta.acquire(request=_request(sandbox_uid="uid_beta"))

        found = self.mux.find_sandbox_id(experiment_id="exp_1", sandbox_uid="uid_beta")

        self.assertIsNotNone(found)
        self.assertTrue(found.startswith("beta:"))

    def test_write_secrets_routes_by_prefix(self) -> None:
        calls: list[str] = []
        self.beta.write_secrets = (  # type: ignore[method-assign]
            lambda *, sandbox_id, secrets, **_kw: calls.append(sandbox_id) or True
        )

        self.assertTrue(
            self.mux.write_secrets(sandbox_id="beta:sb-9", secrets={"T": "v"})
        )
        self.assertEqual(calls, ["sb-9"])

    # ---- capabilities ----

    def test_capabilities_for_resolves_per_provider(self) -> None:
        self.assertEqual(self.mux.capabilities_for(provider=None).name, "alpha")
        self.assertEqual(self.mux.capabilities_for(provider="beta").name, "beta")
        self.assertEqual(self.mux.capabilities_for(provider="beta_alias").name, "beta")
        with self.assertRaises(BackendValidationError):
            self.mux.capabilities_for(provider="nope")

    def test_aggregate_capabilities_mirror_default_with_expiry_union(self) -> None:
        caps = self.mux.capabilities

        self.assertEqual(caps.name, "alpha")
        self.assertTrue(caps.configurable_resources)
        # Billing protection wins: any enforcing backend makes the fleet enforce.
        self.assertTrue(caps.enforce_expiry)

    # ---- merged catalog ----

    def test_hardware_catalog_merges_and_tags_options_with_provider(self) -> None:
        catalog = self.mux.hardware_catalog()

        self.assertIsNotNone(catalog)
        self.assertEqual(catalog["provider"], "alpha")
        self.assertEqual(catalog["providers"], ["beta"])  # alpha has no catalog
        self.assertTrue(catalog["options"])
        self.assertTrue(all(o["provider"] == "beta" for o in catalog["options"]))
        prices = [o["price_usd_per_hour"] for o in catalog["options"]]
        self.assertEqual(prices, sorted(prices))

    def test_health_aggregates_and_names_failures(self) -> None:
        self.beta.healthy = False

        health = self.mux.health()

        self.assertFalse(health["ok"])
        self.assertIn("beta", health["error"])
        self.assertTrue(health["backends"]["alpha"]["ok"])

    def test_environment_and_secrets_merge_across_backends(self) -> None:
        # Post-Phase-C the facade threads the provisioning user's HF token; the
        # multiplexer forwards it to each backend.
        self.alpha.sandbox_secrets = lambda *, hf_token="": {"A": hf_token or "1"}  # type: ignore[method-assign]
        self.beta.sandbox_secrets = lambda *, hf_token="": {"B": "2"}  # type: ignore[method-assign]

        self.assertEqual(
            self.mux.sandbox_secrets(hf_token="hf_x"), {"A": "hf_x", "B": "2"}
        )
        self.assertEqual(self.mux.sandbox_secrets(), {"A": "1", "B": "2"})
        self.assertEqual(
            self.mux.sandbox_environment(), {"available_tokens": [], "notes": []}
        )


class MultiplexedServiceTest(unittest.TestCase):
    """SandboxService over a two-provider multiplexer (whole-stack routing)."""

    def setUp(self) -> None:
        from merv.brain.mlflow import CentralMlflowService
        from tests.support.brain import TestBrain

        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.alpha = FakeSandboxBackend()
        self.alpha.capabilities = BackendCapabilities(
            name="alpha", enforce_expiry=False, lifetime_extension_supported=True
        )
        self.beta = FakeSandboxBackend(requires_hardware_selection=True)
        self.beta.capabilities = BackendCapabilities(
            name="beta",
            enforce_expiry=False,
            lifetime_extension_supported=True,
            requires_hardware_selection=True,
            configurable_resources=False,
        )
        self.mux = MultiplexingSandboxBackend(
            backends={"alpha": self.alpha, "beta": self.beta}, default="alpha"
        )
        self.app = TestBrain(
            repo_root=self.repo,
            db_path=self.repo / ".research_plugin" / "state.sqlite",
            execution_backend=self.mux,
            mlflow_tracking=CentralMlflowService(
                mode="external",
                tracking_uri="https://mlflow.test",
                health_check=lambda: True,
            ),
        )
        self.project_id = self.app.call_tool(
            "project", {"action": "create", "name": "Mux Project"}
        )["id"]

    def tearDown(self) -> None:
        self.app.shutdown()
        self.tmp.cleanup()

    def call(self, tool: str, **kwargs):
        return self.app.call_tool(tool, kwargs)

    def test_request_routes_to_named_provider_and_records_it(self) -> None:
        result = self.call(
            "sandbox.request",
            project_id=self.project_id,
            provider="beta",
            instance_type="gpu_1x_a10",
        )

        self.assertEqual(result["status"], "running")
        self.assertTrue(result["sandbox_id"].startswith("beta:"))
        self.assertEqual(result["provider"], "beta")
        self.assertEqual(len(self.beta.acquired), 1)
        self.assertEqual(len(self.alpha.acquired), 0)
        with self.app.store.transaction() as conn:
            generation = conn.execute(
                "SELECT provider, price_usd_per_hour FROM sandbox_generations"
            ).fetchone()
        self.assertEqual(generation["provider"], "beta")
        self.assertEqual(generation["price_usd_per_hour"], 0.75)

    def test_default_provider_serves_when_omitted(self) -> None:
        result = self.call("sandbox.request", project_id=self.project_id)

        self.assertEqual(result["status"], "running")
        self.assertTrue(result["sandbox_id"].startswith("alpha:"))
        self.assertEqual(result["provider"], "alpha")

    def test_unknown_provider_is_a_clean_validation_error(self) -> None:
        from merv.brain.kernel.utils import ValidationError

        with self.assertRaisesRegex(ValidationError, "alpha, beta"):
            self.call("sandbox.request", project_id=self.project_id, provider="gamma")

    def test_selection_gate_keys_on_the_requested_provider(self) -> None:
        # beta bundles hardware: no instance_type => the merged selection menu.
        result = self.call(
            "sandbox.request", project_id=self.project_id, provider="beta"
        )

        self.assertEqual(result["status"], "needs_selection")
        self.assertTrue(
            all(o["provider"] == "beta" for o in result["options"])
        )

    def test_release_of_prefixed_sandbox_terminates_owner_vm(self) -> None:
        created = self.call(
            "sandbox.request",
            project_id=self.project_id,
            provider="beta",
            instance_type="gpu_1x_a10",
        )
        native = created["sandbox_id"].split(":", 1)[1]

        released = self.call(
            "sandbox.release",
            project_id=self.project_id,
            sandbox_uid=created["sandbox_uid"],
            confirm_retained=True,
        )

        self.assertEqual(released["status"], "terminated")
        self.assertIn(native, self.beta.terminated)
        self.assertNotIn(native, self.alpha.terminated)

    def test_options_returns_merged_provider_tagged_menu(self) -> None:
        result = self.call("sandbox.options", project_id=self.project_id)

        self.assertEqual(result["backend"], "alpha")
        self.assertEqual(result["providers"], ["beta"])
        self.assertTrue(all(o["provider"] == "beta" for o in result["options"]))


class BuildFactoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_single_name_env_builds_direct_backend(self) -> None:
        with mock.patch.dict(
            os.environ, {"MERV_EXECUTION_BACKENDS": "fake"}, clear=False
        ):
            backend = build_sandbox_backend(repo_root=self.repo)

        self.assertIsInstance(backend, FakeSandboxBackend)

    def test_multiple_names_build_multiplexer_with_legacy_default(self) -> None:
        env = {
            "MERV_EXECUTION_BACKENDS": "fake, lambda_labs",
            "MERV_EXECUTION_BACKEND": "lambda_labs",
        }
        with mock.patch.dict(os.environ, env, clear=False):
            backend = build_sandbox_backend(repo_root=self.repo)

        self.assertIsInstance(backend, MultiplexingSandboxBackend)
        self.assertEqual(backend.default, "lambda_labs")
        self.assertEqual(sorted(backend.backends), ["fake", "lambda_labs"])

    def test_multiple_names_default_to_first_configured(self) -> None:
        env = {"MERV_EXECUTION_BACKENDS": "fake,lambda_labs"}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MERV_EXECUTION_BACKEND", None)
            backend = build_sandbox_backend(repo_root=self.repo)

        self.assertIsInstance(backend, MultiplexingSandboxBackend)
        self.assertEqual(backend.default, "fake")

    def test_explicit_name_arg_bypasses_multi_config(self) -> None:
        env = {"MERV_EXECUTION_BACKENDS": "fake,lambda_labs"}
        with mock.patch.dict(os.environ, env, clear=False):
            backend = build_sandbox_backend(repo_root=self.repo, name="fake")

        self.assertIsInstance(backend, FakeSandboxBackend)


if __name__ == "__main__":
    unittest.main()
