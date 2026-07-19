"""Transitional shim — moved to backend.kernel.ports; deleted at de-shim."""
import sys

from ..kernel import ports as _moved
from ..kernel.ports import (
    mgmt_keys,
    object_store,
    quota_admission,
    reflection_writers,
    resource_records,
    review_policy,
    sandbox_lifecycle,
    sandbox_worker,
    task_channel,
    workflow_readers,
)

# Alias every submodule so backend.ports.mgmt_keys et al. resolve to the SAME
# module objects as backend.kernel.ports.* (identity-preserving).
for _sub in (
    mgmt_keys,
    object_store,
    quota_admission,
    reflection_writers,
    resource_records,
    review_policy,
    sandbox_lifecycle,
    sandbox_worker,
    task_channel,
    workflow_readers,
):
    sys.modules[f"{__name__}.{_sub.__name__.rsplit('.', 1)[1]}"] = _sub
sys.modules[__name__] = _moved
