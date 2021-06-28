"""Configuration for Semaphore.

There are two, mostly-parallel models defined here.  The ones ending in
``Settings`` are the pydantic models used to read the settings file from disk,
the root of which is `SettingsFile`.  This is then processed and broken up into
configuration dataclasses for various components and then exposed to the rest
of Semaphore as the `Config` object.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseSettings, Field

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


config = Config()
