# This Dockerfile has four stages:
#
# base-image
#   Updates the base Python image with security patches and common system
#   packages. This image becomes the base of all other images.
# install-image
#   Installs third-party dependencies into a virtual environment and
#   installs the application into /app. This directory will be copied
#   across build stages.
# runtime-image
#   - Copies the virtual environment into place.
#   - Runs as a non-root user.
#   - Sets up the entrypoint and port.

FROM python:3.14.4-slim-trixie AS base-image

# Update system packages
COPY scripts/install-base-packages.sh .
RUN ./install-base-packages.sh && rm ./install-base-packages.sh

FROM base-image AS install-image

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /bin/uv

# Install some additional packages required for building dependencies.
COPY scripts/install-dependency-packages.sh .
RUN ./install-dependency-packages.sh

# Disable hard links during uv package installation since we're using a
# cache on a separate file system.
ENV UV_LINK_MODE=copy

# Force use of system Python so that the Python version is controlled by
# the Docker base image version, not by whatever uv decides to install.
ENV UV_PYTHON_PREFERENCE=only-system

# Install the dependencies.
WORKDIR /app
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-default-groups --compile-bytecode --no-install-project

# Install the Python application.
ADD . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --compile-bytecode --no-editable

FROM base-image AS runtime-image

# Create a non-root user
RUN useradd --create-home appuser

# Copy the virtualenv
COPY --from=install-image /app/.venv /app/.venv

# Set the working directory.
WORKDIR /app

# Switch to the non-root user.
USER appuser

# Expose the port.
EXPOSE 8080

# Make sure we use the virtualenv
ENV PATH="/app/.venv/bin:$PATH"

# Run the application.
CMD ["uvicorn", "semaphore.main:app", "--host", "0.0.0.0", "--port", "8080"]
