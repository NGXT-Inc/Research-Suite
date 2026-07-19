"""Transitional shim — moved to backend.kernel.state; deleted at de-shim."""
import sys

from ..kernel import state as _moved
from ..kernel.state import activity, dialects, store, tool_call_stats, tool_calls

# Alias every submodule so backend.state.store et al. resolve to the SAME
# module objects as backend.kernel.state.* (identity-preserving).
for _sub in (activity, dialects, store, tool_call_stats, tool_calls):
    sys.modules[f"{__name__}.{_sub.__name__.rsplit('.', 1)[1]}"] = _sub
sys.modules[__name__] = _moved
