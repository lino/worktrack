"""Worktrack anti-surveillance personal time ledger and planning cockpit."""

from __future__ import annotations

from worktrack.config import ConfigurationError, WorktrackSettings, load_settings

__version__ = "0.1.0"

__all__ = [
    "ConfigurationError",
    "WorktrackSettings",
    "__version__",
    "load_settings",
]
