# Semaphore Alembic configuration

This directory contains the Alembic configuration for managing the Semaphore database.
It is installed into the Semaphore Docker image and is used to check whether the schema is up-to-date at startup of any Semaphore component.
It is also used by the Helm hook that updates the Semaphore schema if `config.updateSchema` is enabled.
