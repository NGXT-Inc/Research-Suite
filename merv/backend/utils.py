"""Transitional shim — moved to backend.kernel.utils; deleted at de-shim."""
import sys

from .kernel import utils as _moved

sys.modules[__name__] = _moved
