"""Tests for semaphore.cli (the command-line interface).

Be careful when writing tests in this framework because the click command
handling code spawns its own async worker pools when needed.  You therefore
cannot use the ``setup`` fixture here because the two thread pools will
conflict with each other.
"""

from click.testing import CliRunner

from semaphore.cli import main


def test_help() -> None:
    runner = CliRunner()

    result = runner.invoke(main, ["-h"])
    assert result.exit_code == 0
    assert "Commands:" in result.output

    result = runner.invoke(main, ["help"])
    assert result.exit_code == 0
    assert "Commands:" in result.output

    result = runner.invoke(main, ["help", "unknown-command"])
    assert result.exit_code != 0
    assert "Unknown help topic unknown-command" in result.output
