from __future__ import annotations

import unittest

from merv.brain.sandbox.execution.backends._values import find_option
from merv.brain.sandbox.execution.backends.digitalocean.catalog import (
    find_option as digitalocean_find_option,
)
from merv.brain.sandbox.execution.backends.hyperstack.catalog import (
    find_option as hyperstack_find_option,
)
from merv.brain.sandbox.execution.backends.verda.catalog import (
    find_option as verda_find_option,
)
from merv.brain.sandbox.execution.backends.voltage_park.catalog import (
    find_option as voltage_park_find_option,
)


class ProviderCatalogValueTest(unittest.TestCase):
    def test_catalogs_reexport_one_normalized_option_lookup(self) -> None:
        implementations = (
            digitalocean_find_option,
            hyperstack_find_option,
            verda_find_option,
            voltage_park_find_option,
        )
        self.assertTrue(
            all(implementation is find_option for implementation in implementations)
        )
        options = [{"instance_type": "  H100-SXM  "}]
        self.assertIs(find_option(options, instance_type="h100-sxm"), options[0])
        self.assertIsNone(find_option(options, instance_type="missing"))


if __name__ == "__main__":
    unittest.main()
