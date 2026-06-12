"""Test that Alembic migrations are up-to-date with the database schema."""

import subprocess

import pytest
from safir.database import create_database_engine, drop_database

from semaphore.config import config
from semaphore.schema import SchemaBase


@pytest.mark.asyncio
async def test_schema() -> None:
    engine = create_database_engine(
        config.database_url, config.database_password
    )
    await drop_database(engine, SchemaBase.metadata)
    await engine.dispose()
    subprocess.run(["alembic", "upgrade", "head"], check=True)
    subprocess.run(["alembic", "check"], check=True)
