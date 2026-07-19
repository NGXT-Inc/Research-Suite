"""Transitional shim — moved to backend.kernel.secret_tokens; deleted at de-shim."""
import sys

from .kernel import secret_tokens as _moved

sys.modules[__name__] = _moved
