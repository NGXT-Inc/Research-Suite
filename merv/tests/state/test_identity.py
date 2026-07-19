from __future__ import annotations

import unittest

from merv.brain.config import ALLOWED_ORIGINS_ENV_VAR, resolve_allowed_origins
from merv.brain.services.identity import LOCAL_PRINCIPAL, Principal
from merv.brain.kernel.utils import ValidationError


class IdentityConfigTest(unittest.TestCase):
    def test_local_principal_shape(self) -> None:
        self.assertEqual(LOCAL_PRINCIPAL, Principal(tenant_id="local", client_id="local"))

    def test_resolve_allowed_origins(self) -> None:
        self.assertEqual(resolve_allowed_origins(env={}), [])
        self.assertEqual(
            resolve_allowed_origins(
                env={
                    ALLOWED_ORIGINS_ENV_VAR: (
                        " https://ui.example.com/, "
                        "http://localhost:5173, ,"
                    )
                }
            ),
            ["https://ui.example.com", "http://localhost:5173"],
        )
        with self.assertRaises(ValidationError):
            resolve_allowed_origins(env={ALLOWED_ORIGINS_ENV_VAR: "localhost:5173"})
        with self.assertRaises(ValidationError):
            resolve_allowed_origins(
                env={ALLOWED_ORIGINS_ENV_VAR: "https://ui.example.com/app"}
            )
        with self.assertRaises(ValidationError):
            resolve_allowed_origins(
                env={ALLOWED_ORIGINS_ENV_VAR: "https://ui.example.com?x=1"}
            )
        with self.assertRaises(ValidationError):
            resolve_allowed_origins(
                env={ALLOWED_ORIGINS_ENV_VAR: "https://user@ui.example.com"}
            )


if __name__ == "__main__":
    unittest.main()
