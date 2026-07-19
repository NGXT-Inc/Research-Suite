"""Transitional shim — moved to backend.kernel.env; deleted at de-shim."""
import sys

from .kernel import env as _moved

sys.modules[__name__] = _moved
