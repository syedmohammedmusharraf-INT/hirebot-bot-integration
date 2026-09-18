"""Local assistant record store. Implements plan section 4.3.

Loads the YAML file backing an ``assistant_ref`` and validates it through the
shared ``model_support`` package so the control image and the agent image
apply the exact same rules to the exact same record.
"""

from __future__ import annotations

import logging
import os

import yaml

from model_support.assistant_config import AssistantConfig, InvalidAssistantConfig, load_assistant_config

logger = logging.getLogger(__name__)


class AssistantNotFoundError(Exception):
    """Raised when ``{assistants_dir}/{ref}.yaml`` does not exist."""


__all__ = ["AssistantNotFoundError", "InvalidAssistantConfig", "load_assistant"]


def load_assistant(ref: str, assistants_dir: str) -> AssistantConfig:
    """Load and validate the local assistant record for ``ref``.

    Raises :class:`AssistantNotFoundError` if the YAML file is missing, or
    lets :class:`model_support.assistant_config.InvalidAssistantConfig`
    propagate if the file exists but fails validation.
    """
    path = os.path.join(assistants_dir, f"{ref}.yaml")
    if not os.path.isfile(path):
        raise AssistantNotFoundError(f"No local assistant record at {path} for assistant_ref={ref!r}")

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    try:
        config = load_assistant_config(raw)
    except InvalidAssistantConfig:
        logger.exception("Assistant record %s failed validation", path)
        raise

    logger.debug("Loaded assistant record %s (mode=%s)", path, config.assistant_mode)
    return config
