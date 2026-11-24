# Development container image for Semaphore.
#
# This is the base for local development (VS Code, Codespaces). It is not the
# production image; see the Dockerfile at the repository root for that.

FROM mcr.microsoft.com/devcontainers/python:3-3.14-trixie

# Install uv. The version should match UV_VERSION in
# .github/workflows/ci.yaml and the uv image in the top-level Dockerfile.
COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /uvx /usr/local/bin/

# Install system packages needed by the documentation build (matching the
# docs job in .github/workflows/ci.yaml).
RUN apt-get update \
    && export DEBIAN_FRONTEND=noninteractive \
    && apt-get -y install --no-install-recommends graphviz imagemagick \
    && rm -rf /var/lib/apt/lists/*

# The workspace and the uv cache may be on different file systems, so hard
# links from the cache are not always possible.
ENV UV_LINK_MODE=copy
