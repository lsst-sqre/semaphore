"""Configuration for Semaphore."""

from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from safir.logging import LogLevel, Profile

__all__ = ["Config", "config"]


class Config(BaseSettings):
    """Configuration for Semaphore."""

    model_config = SettingsConfigDict(
        env_prefix="SEMAPHORE_", case_sensitive=False
    )

    enable_github_app: bool = Field(
        True, validation_alias="SEMAPHORE_ENABLE_GITHUB_APP"
    )
    """Toggle to enable GitHub App functionality.

    If configurations required to function as a GitHub App are not set,
    this configuration is automatically toggled to False. It also also be
    manually toggled to False if necessary.
    """

    github_app_id: str | None = Field(
        None, validation_alias="SEMAPHORE_GITHUB_APP_ID"
    )
    """The GitHub App ID, as determined by GitHub when setting up a GitHub
    App.
    """

    github_app_private_key: SecretStr | None = Field(
        None, validation_alias="SEMAPHORE_GITHUB_APP_PRIVATE_KEY"
    )
    """The GitHub app private key. See
    https://docs.github.com/en/developers/apps/building-github-apps/authenticating-with-github-apps#generating-a-private-key
    """

    github_webhook_secret: SecretStr | None = Field(
        None, validation_alias="SEMAPHORE_GITHUB_WEBHOOK_SECRET"
    )
    """The GitHub app's webhook secret, as set when the App was created. See
    https://docs.github.com/en/developers/webhooks-and-events/webhooks/securing-your-webhooks
    """

    log_level: LogLevel = Field(LogLevel.INFO, title="Log level")

    log_profile: Profile = Field(Profile.production, title="Logging profile")

    name: str = Field("semaphore", title="Name of application")

    path_prefix: str = Field("/semaphore", title="URL prefix for application")

    phalanx_env: str = Field(..., validation_alias="SEMAPHORE_PHALANX_ENV")
    """Name of the Phalanx environment this Semaphore installation is running
    in (e.g. ``idfprod``).

    This configuration aids in determining which broadcast messages from a
    shared GitHub repository to index, based on the ``env`` YAML/markdown
    front-matter keyword.

    For a list of environments, see https://github.com/lsst-sqre/phalanx.
    """

    slack_webhook: SecretStr | None = Field(
        None,
        title="Slack webhook for alerts",
        description="If set, alerts will be posted to this Slack webhook",
    )

    @field_validator(
        "github_webhook_secret", "github_app_private_key", mode="before"
    )
    @classmethod
    def validate_none_secret(cls, v: str | None) -> str | None:
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

    @model_validator(mode="after")
    def validate_github_app(self) -> Self:
        """Validate ``enable_github_app`` by ensuring that other GitHub
        configurations are also set.
        """
        # Allow the GitHub app to be disabled regardless of other
        # configurations.
        if not self.enable_github_app:
            return self

        # If any setting is missing, change enable to false.
        if (
            self.github_app_private_key is None
            or self.github_webhook_secret is None
            or self.github_app_id is None
        ):
            self.enable_github_app = False

        return self


config = Config()
