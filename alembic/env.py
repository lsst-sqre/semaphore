"""Alembic migration environment."""

from safir.database import run_migrations_offline, run_migrations_online
from safir.logging import configure_alembic_logging, configure_logging

from alembic import context
from semaphore.config import config
from semaphore.schema import SchemaBase

# Configure structlog.
configure_logging(name="example", log_level=config.log_level)
configure_alembic_logging()

# Run the migrations.
if context.is_offline_mode():
    run_migrations_offline(SchemaBase.metadata, config.database_url)
else:
    run_migrations_online(
        SchemaBase.metadata,
        config.database_url,
        config.database_password,
    )
