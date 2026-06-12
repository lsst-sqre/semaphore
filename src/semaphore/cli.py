"""Administrative command-line interface."""

import asyncio
import subprocess
from pathlib import Path

import click
import structlog
from safir.asyncio import run_with_asyncio
from safir.click import display_help
from safir.database import (
    create_database_engine,
    initialize_database,
    is_database_current,
    stamp_database,
)

from .config import config
from .schema import SchemaBase

__all__ = [
    "help",
    "init",
    "main",
    "update_schema",
    "validate_schema",
]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(message="%(version)s")
def main() -> None:
    """Administrative command-line interface for wobbly."""


@main.command()
@click.argument("topic", default=None, required=False, nargs=1)
@click.pass_context
def help(ctx: click.Context, topic: str | None) -> None:
    """Show help for any command."""
    display_help(main, ctx, topic)


@main.command()
@click.option(
    "--alembic-config-path",
    envvar="SEMAPHORE_ALEMBIC_CONFIG_PATH",
    type=click.Path(path_type=Path),
    default=Path("/app/alembic.ini"),
    help="Alembic configuration file.",
)
@click.option(
    "--reset", is_flag=True, help="Delete all existing database data."
)
def init(*, alembic_config_path: Path, reset: bool) -> None:
    """Initialize the database."""
    logger = structlog.get_logger("semaphore")
    engine = create_database_engine(
        config.database_url, config.database_password
    )

    async def _init_db() -> None:
        base = SchemaBase.metadata
        await initialize_database(engine, logger, schema=base, reset=reset)
        await engine.dispose()

    asyncio.run(_init_db())
    stamp_database(alembic_config_path)


@main.command()
@click.option(
    "--alembic-config-path",
    envvar="SEMAPHORE_ALEMBIC_CONFIG_PATH",
    type=click.Path(path_type=Path),
    default=Path("/app/alembic.ini"),
    help="Alembic configuration file.",
)
def update_schema(*, alembic_config_path: Path) -> None:
    """Update the schema."""
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        cwd=str(alembic_config_path.parent),
    )


@main.command()
@click.option(
    "--alembic-config-path",
    envvar="SEMAPHORE_ALEMBIC_CONFIG_PATH",
    type=click.Path(path_type=Path),
    default=Path("/app/alembic.ini"),
    help="Alembic configuration file.",
)
@run_with_asyncio
async def validate_schema(*, alembic_config_path: Path) -> None:
    """Validate that the database schema is current."""
    engine = create_database_engine(
        config.database_url, config.database_password
    )
    logger = structlog.get_logger("example")
    if not await is_database_current(engine, logger, alembic_config_path):
        raise click.ClickException("Database schema is not current")
