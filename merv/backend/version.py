"""Transitional shim — moved to backend.kernel.version; deleted at de-shim."""
import sys

from .kernel import version as _moved

sys.modules[__name__] = _moved
