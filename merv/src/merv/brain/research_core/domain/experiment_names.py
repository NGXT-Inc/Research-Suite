"""Experiment naming rules shared by record services and reflection proposals."""

from __future__ import annotations

import re

from ...kernel.utils import ValidationError


# The experiment name doubles as its folder name (experiments/<name>/), so it
# must be short and filesystem-safe as written — no sanitization, what the
# agent names is what appears on disk.
MAX_EXPERIMENT_NAME_LEN = 48
MIN_EXPERIMENT_NAME_LEN = 3
_EXPERIMENT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def validate_experiment_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValidationError(
            "name is required: a short, folder-safe experiment name — it "
            "becomes the experiment folder experiments/<name>/"
        )
    if (
        len(name) < MIN_EXPERIMENT_NAME_LEN
        or len(name) > MAX_EXPERIMENT_NAME_LEN
        or not _EXPERIMENT_NAME_RE.fullmatch(name)
    ):
        raise ValidationError(
            "experiment name must work as a folder name: start with a letter "
            "or digit and use only letters, digits, '.', '_' and '-', between "
            f"{MIN_EXPERIMENT_NAME_LEN} and {MAX_EXPERIMENT_NAME_LEN} characters"
        )
    return name
