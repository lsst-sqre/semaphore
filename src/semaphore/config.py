"""Configuration for Semaphore.

There are two, mostly-parallel models defined here.  The ones ending in
``Settings`` are the pydantic models used to read the settings file from disk,
the root of which is `SettingsFile`.  This is then processed and broken up into
configuration dataclasses for various components and then exposed to the rest
of Semaphore as the `Config` object.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping, Optional

from pydantic import BaseSettings, Field, SecretStr, validator

# from safir.logging import configure_logging

__all__ = ["Config", "Profile", "LogLevel"]


class Profile(str, Enum):

    production = "production"

    development = "development"


class LogLevel(str, Enum):

    DEBUG = "DEBUG"

    INFO = "INFO"

    WARNING = "WARNING"

    ERROR = "ERROR"

    CRITICAL = "CRITICAL"


class Config(BaseSettings):

    name: str = Field("semaphore", env="SAFIR_NAME")

    profile: Profile = Field(Profile.production, env="SAFIR_PROFILE")

    log_level: LogLevel = Field(LogLevel.INFO, env="SAFIR_LOG_LEVEL")

    logger_name: str = Field("semaphore", env="SAFIR_LOGGER")

    github_app_id: Optional[str] = Field(None, env="SEMAPHORE_GITHUB_APP_ID")
    """The GitHub App ID, as determined by GitHub when setting up a GitHub
    App.
    """

    github_webhook_secret: Optional[SecretStr] = Field(
        None, env="SEMAPHORE_GITHUB_WEBHOOK_SECRET"
    )
    """The GitHub app's webhook secret, as set when the App was created. See
    https://docs.github.com/en/developers/webhooks-and-events/webhooks/securing-your-webhooks
    """

    github_app_private_key: Optional[SecretStr] = Field(
        None, env="SEMAPHORE_GITHUB_APP_PRIVATE_KEY"
    )
    """The GitHub app private key. See
    https://docs.github.com/en/developers/apps/building-github-apps/authenticating-with-github-apps#generating-a-private-key
    """

    enable_github_app: bool = Field(True, env="SEMAPHORE_ENABLE_GITHUB_APP")
    """Toggle to enable GitHub App functionality.

    If configurations required to function as a GitHub App are not set,
    this configuration is automatically toggled to False. It also also be
    manually toggled to False if necessary.
    """

    phalanx_env: str = Field(..., env="SEMAPHORE_PHALANX_ENV")
    """Name of the Phalanx environment this Semaphore installation is running
    in (e.g. ``idfprod``).

    This configuration aids in determining which broadcast messages from a
    shared GitHub repository to index, based on the ``env`` YAML/markdown
    front-matter keyword.

    For a list of environments, see https://github.com/lsst-sqre/phalanx.
    """

    @validator("github_webhook_secret", "github_app_private_key", pre=True)
    def validate_none_secret(
        cls, v: Optional[SecretStr]
    ) -> Optional[SecretStr]:
        """Validate a SecretStr setting which may be "None" that is intended
        to be `None`.

        This is useful for secrets generated from 1Password or environment
        variables where the value cannot be null.
        """
        if v is None:
            return v
        elif isinstance(v, str):
            if v.strip().lower() == "none":
                return None
            else:
                return v
        else:
            raise ValueError(f"Value must be None or a string: {v!r}")

    @validator("enable_github_app")
    def validate_github_app(cls, v: bool, values: Mapping[str, Any]) -> bool:
        """Validate ``enable_github_app`` by ensuring that other GitHub
        configurations are also set.
        """
        if v is False:
            # Allow the GitHub app to be disabled regardless of other
            # configurations.
            return False

        if (
            (values.get("github_app_private_key") is None)
            or (values.get("github_webhook_secret") is None)
            or (values.get("github_app_id") is None)
        ):
            return False

        return True


config = Config()
