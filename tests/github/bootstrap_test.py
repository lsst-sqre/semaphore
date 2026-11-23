"""Tests for loading broadcast messages from GitHub at startup."""

from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository

from ..support.github import (
    GITHUB_INSTALLATION_ID,
    GITHUB_REPO,
    GitHubMock,
    configure_github_app,
    make_message_id,
    make_repository,
)

OTHER_REPO = "lsst-sqre/other"
"""A repository the app can see that is not in the allowlist."""

FORK_REPO = "rubin-fork/rsp_broadcast"
"""A fork of the broadcast repository whose name is in the allowlist."""

FORK_INSTALLATION_ID = 9012
"""ID of the GitHub App installation that can see `FORK_REPO`."""

ALLOWLIST = f"{GITHUB_REPO},{FORK_REPO}"
"""Value of ``SEMAPHORE_GITHUB_REPOS`` in these tests."""

HELLO_MD = "---\nsummary: Hello from GitHub.\n---\n"
"""A broadcast message file."""

pytestmark = pytest.mark.usefixtures("empty_database", "broadcast_repo")
"""Each test starts the app with an empty database and broadcast
repository.
"""


def get_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return parse_log_tuples("semaphore", caplog.record_tuples)


def mock_github(github_mock: GitHubMock) -> None:
    """Mock two installations of the GitHub App.

    The first installation can see `GITHUB_REPO` and `OTHER_REPO`; the second
    can see `FORK_REPO`. Each repository has one broadcast,
    ``broadcasts/hello.md``. Requests for the contents of `OTHER_REPO` and
    `FORK_REPO` are optional, so a test fails on loading their broadcasts
    rather than on requesting them.
    """
    github_mock.mock_installations(
        [GITHUB_INSTALLATION_ID, FORK_INSTALLATION_ID]
    )
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(GITHUB_REPO), make_repository(OTHER_REPO)],
        installation_id=GITHUB_INSTALLATION_ID,
    )
    github_mock.mock_installation_token(FORK_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(FORK_REPO, fork=True)],
        installation_id=FORK_INSTALLATION_ID,
    )
    for repo in (GITHUB_REPO, OTHER_REPO, FORK_REPO):
        optional = repo != GITHUB_REPO
        github_mock.mock_directory(
            "broadcasts", ["hello.md"], repo=repo, optional=optional
        )
        github_mock.mock_file(
            "broadcasts/hello.md", HELLO_MD, repo=repo, optional=optional
        )


@pytest.mark.asyncio
async def test_loads_only_allowed_repos(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]


@pytest.mark.asyncio
async def test_does_not_read_rejected_repos(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        pass

    contents_requests = [
        r.url.path
        for r in github_mock.get_requests()
        if "/contents/" in r.url.path
    ]
    assert contents_requests == [
        f"/repos/{GITHUB_REPO}/contents/broadcasts",
        f"/repos/{GITHUB_REPO}/contents/broadcasts/hello.md",
    ]


@pytest.mark.asyncio
async def test_warns_about_rejected_repos(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert [(w["github_repo"], w["reason"]) for w in warnings] == [
        (OTHER_REPO, "not_in_allowlist"),
        (FORK_REPO, "fork"),
    ]


@pytest.mark.asyncio
async def test_logs_summary(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        pass

    summaries = [
        log
        for log in get_logs(caplog)
        if log["event"] == "Loaded broadcasts from GitHub"
    ]
    assert summaries == [
        {
            "event": "Loaded broadcasts from GitHub",
            "severity": "info",
            "github_repos": [GITHUB_REPO, FORK_REPO],
            "repo_count": 1,
            "message_count": 1,
        }
    ]
