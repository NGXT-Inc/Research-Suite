"""Deploy-artifact structural lints (cloud plan Phase 9).

Pure file parsing — no docker — that the reference deploy stack references the
right entrypoints, extras, and config, and that no secrets are committed. Keeps
the Dockerfile/compose from silently drifting away from the console scripts and
the §3.4 config matrix the rest of the phase wired.
"""

from __future__ import annotations

import tomllib
import unittest

from tests.paths import PLUGIN_ROOT


DEPLOY = PLUGIN_ROOT / "deploy"


class DeployArtifactsTest(unittest.TestCase):
    def test_deploy_dir_has_the_expected_files(self) -> None:
        for name in (
            "Dockerfile",
            "Dockerfile.mlflow",
            "docker-compose.yml",
            "doctor.py",
            "README.md",
            ".dockerignore",
            ".env.example",
        ):
            with self.subTest(file=name):
                self.assertTrue((DEPLOY / name).is_file(), f"missing deploy/{name}")

    def test_dockerfile_installs_control_extra_and_runs_control_entrypoint(self) -> None:
        text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
        # Installs the `control` extra (Postgres + object store + provider SDK).
        self.assertIn('.[control]', text)
        # Shared stdlib-only config helpers are imported by backend.config and
        # must be present before the wheel/install step runs in the image.
        self.assertIn("COPY research_plugin_shared ./research_plugin_shared", text)
        # Runs the control console-script entrypoint, not a raw module.
        self.assertIn("research-plugin-control", text)
        # Non-root user.
        self.assertIn("USER ", text)
        self.assertIn("useradd", text)
        # HEALTHCHECK hits the version handshake (or /health).
        self.assertIn("HEALTHCHECK", text)
        self.assertTrue("/api/meta" in text or "/health" in text)
        # Hosted control needs ssh for Lambda management operations, and the
        # reference compose key-init job needs ssh-keygen.
        self.assertIn("openssh-client", text)
        # The hosted control entrypoint now runs without a checkout/staging dir.
        self.assertNotIn("RESEARCH_PLUGIN_REPO_ROOT", text)

    def test_control_entrypoint_exists_in_pyproject(self) -> None:
        with (PLUGIN_ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        scripts = pyproject["project"]["scripts"]
        self.assertEqual(
            scripts.get("research-plugin-control"),
            "backend.transport.http_server:control_main",
        )
        # The control extra exists and carries the Postgres + object-store deps.
        control_extra = " ".join(
            pyproject["project"]["optional-dependencies"]["control"]
        )
        self.assertIn("psycopg", control_extra)
        self.assertIn("boto3", control_extra)
        self.assertNotIn("mlflow", control_extra)

    def test_compose_wires_control_postgres_object_store_and_management_key(self) -> None:
        text = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
        # The three legs of the reference stack.
        for service in ("control:", "postgres:", "minio:", "mgmtkey:"):
            self.assertIn(service, text)
        # Control points at the Postgres dialect and the blob bucket (§3.4).
        self.assertIn("RESEARCH_PLUGIN_DB_URL", text)
        self.assertIn("postgresql://", text)
        self.assertIn("RESEARCH_PLUGIN_BLOB_BUCKET", text)
        self.assertIn("RESEARCH_PLUGIN_MGMT_KEY_PATH", text)
        self.assertIn("RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW", text)
        self.assertIn("RESEARCH_PLUGIN_REQUIRE_SANDBOX_BACKEND", text)
        self.assertIn("RESEARCH_PLUGIN_EXECUTION_BACKEND", text)
        self.assertIn("RESEARCH_PLUGIN_LAMBDA_API_KEY", text)
        self.assertIn("LAMBDA_LABS_API_KEY", text)
        self.assertIn("HF_TOKEN", text)
        self.assertIn("${RESEARCH_PLUGIN_STORAGE_ENDPOINT_URL:-http://minio:9000}", text)
        self.assertIn("${AWS_ENDPOINT_URL_S3:-http://minio:9000}", text)
        self.assertIn("ssh-keygen", text)
        self.assertIn("mgmtkey:/run/secrets/research_plugin_mgmt_key:ro", text)
        # Builds from the deploy Dockerfile.
        self.assertIn("dockerfile: deploy/Dockerfile", text)
        self.assertIn("dockerfile: deploy/Dockerfile.mlflow", text)

    def test_env_example_documents_control_matrix(self) -> None:
        text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
        for var in (
            "RESEARCH_PLUGIN_MODE",
            "RESEARCH_PLUGIN_DB_URL",
            "RESEARCH_PLUGIN_BLOB_BUCKET",
            "RESEARCH_PLUGIN_MGMT_KEY_PATH",
            "RESEARCH_PLUGIN_MGMT_PUBLIC_KEY",
            "RESEARCH_PLUGIN_ALLOWED_ORIGINS",
            "RESEARCH_PLUGIN_MLFLOW_TRACKING_URI",
            "RESEARCH_PLUGIN_REQUIRE_AGENT_MLFLOW",
            "RESEARCH_PLUGIN_EXECUTION_BACKEND",
            "RESEARCH_PLUGIN_REQUIRE_SANDBOX_BACKEND",
            "RESEARCH_PLUGIN_LAMBDA_API_KEY",
            "AWS_ENDPOINT_URL_S3",
        ):
            self.assertIn(var, text)

    def test_doctor_script_covers_startup_readiness_sweep(self) -> None:
        text = (DEPLOY / "doctor.py").read_text(encoding="utf-8")
        for token in (
            "/api/meta",
            "/api/sandboxes/health",
            "sandbox.options",
            "storage.put_object",
            "storage.complete_upload",
            "deploy_doctor_ready",
            "ajax-api/2.0/mlflow",
            "RP_DOCTOR_URL_REWRITE",
        ):
            self.assertIn(token, text)

    def test_no_real_secrets_committed(self) -> None:
        # .env.example must only carry placeholders, never a filled-in token.
        text = (DEPLOY / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CHANGE_ME", text)
        # No real HF token prefix (hf_<chars>) in the example file.
        import re

        self.assertIsNone(
            re.search(r"\bhf_[A-Za-z0-9]{8,}", text),
            "deploy/.env.example appears to contain a real HF token",
        )
        # .dockerignore excludes secret files + the UI from the build context.
        ignore = (DEPLOY / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn(".env", ignore)
        self.assertIn("credentials.json", ignore)
        self.assertIn("research_state_ui/", ignore)


if __name__ == "__main__":
    unittest.main()
