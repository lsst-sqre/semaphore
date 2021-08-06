"""Models for the external API handlers."""

from typing import Optional

from pydantic import BaseModel, Field
from safir.metadata import Metadata as SafirMetadata

__all__ = ["Index"]


class Index(BaseModel):
    """The application's root response, including metadata and information
    about the APIs.
    """

    metadata: SafirMetadata = Field(..., title="Package metadata")

    github_app_id: Optional[str]
    """The GitHub APP ID, if Semaphore is configured as a GitHub App."""

    github_app_enabled: bool
    """Flag indicating if the GitHub app functionality is enabled."""

    api_docs_path: str
    """Path to the web API documentation."""

    openapi_path: str
    """Path to the web API's OpenAPI specification."""
