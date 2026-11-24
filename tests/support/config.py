"""Test utilities for changing Semaphore's configuration."""

import pytest

from semaphore.config import Config, config

__all__ = ["reload_config"]


def reload_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebuild the global configuration from the current environment.

    Modules hold references to the global ``config`` object, so its settings
    are replaced in place rather than replacing the object. The monkeypatch
    fixture restores the original settings after the test.

    Parameters
    ----------
    monkeypatch
        The pytest monkeypatch fixture of the test.
    """
    new_config = Config()
    for name in [*Config.model_fields, *Config.__private_attributes__]:
        monkeypatch.setattr(config, name, getattr(new_config, name))
