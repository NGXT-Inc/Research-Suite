"""Experiment creation application command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Unpack

from ...research_core.facade import ExperimentCreateArgs, ExperimentState, ResearchCore, experiment_folder_rel
from .presentation import rich_experiment_state


@dataclass(slots=True)
class CreateExperiment:
    """Create through Research, then add the necessarily empty object view."""

    research: ResearchCore

    def __call__(
        self, **kwargs: Unpack[ExperimentCreateArgs]
    ) -> ExperimentState:
        state = self.research.create_experiment(**kwargs)
        state["folder"] = experiment_folder_rel(
            experiment_id=str(state.get("id") or ""), name=str(state.get("name") or "")
        )
        state["folder_guidance"] = (
            f"Use {state['folder']} as the experiment's one local folder. "
            "Data-plane actions create it on demand; work in it from the start: "
            "plan.md, scripts, configs, retained results, report, and graph all "
            "live there. This local folder is not uploaded to a sandbox "
            "automatically: create, fetch, or explicitly transfer sandbox inputs "
            "after provisioning. Pull selected light outputs back with "
            "sandbox.pull_outputs, or upload heavy outputs to configured object "
            "storage, before the sandbox is released."
        )
        return rich_experiment_state(state, storage_objects=())


__all__ = ["CreateExperiment"]
