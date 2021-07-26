"""Models for the external API handlers."""

from pydantic import BaseModel, Field
from safir.metadata import Metadata as SafirMetadata

__all__ = ["Index"]


class Index(BaseModel):
    """The application's root response, including metadata and information
    about the APIs.
    """

    metadata: SafirMetadata = Field(..., title="Package metadata")
