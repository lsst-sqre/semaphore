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

SECOND_REPO = "lsst-sqre/rsp_broadcast_extra"
"""A second repository in the allowlist."""

ALLOWLIST = f"{GITHUB_REPO},{FORK_REPO}"
"""Value of ``SEMAPHORE_GITHUB_REPOS`` in these tests, unless set."""

HELLO_MD = "---\nsummary: Hello from GitHub.\n---\n"
"""A broadcast message file."""

MALFORMED_MD = "---\nsummary: [Hello from GitHub.\n---\n"
"""A broadcast message file whose front matter is not valid YAML."""

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


def mock_installation(
    github_mock: GitHubMock, repos: list[str], *, per_page: int | None = None
) -> None:
    """Mock one installation of the GitHub App that can see ``repos``.

    The installation lists ``per_page`` repositories per page, or all of them
    on one page if `None`. The contents of the repositories are left for the
    test to mock.
    """
    github_mock.mock_installations([GITHUB_INSTALLATION_ID])
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(repo) for repo in repos],
        installation_id=GITHUB_INSTALLATION_ID,
        per_page=per_page,
    )


def mock_malformed_file(github_mock: GitHubMock) -> None:
    """Mock one installation that can see only `GITHUB_REPO`, whose
    broadcasts are a malformed ``bad.md`` followed by a valid ``hello.md``.
    """
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["bad.md", "hello.md"])
    github_mock.mock_file("broadcasts/bad.md", MALFORMED_MD)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)


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
            "skipped_file_count": 0,
        }
    ]


@pytest.mark.asyncio
async def test_skips_malformed_file(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch)
    mock_malformed_file(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]


@pytest.mark.asyncio
async def test_warns_about_malformed_file(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_malformed_file(github_mock)

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert [(w["github_repo"], w["path"]) for w in warnings] == [
        (GITHUB_REPO, "broadcasts/bad.md")
    ]
    assert "yaml.parser.ParserError" in warnings[0]["exception"]


@pytest.mark.asyncio
async def test_skips_file_github_fails(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    mock_installation(github_mock, [GITHUB_REPO, SECOND_REPO])
    github_mock.mock_directory("broadcasts", ["broken.md", "hello.md"])
    github_mock.mock_contents_error("broadcasts/broken.md", status_code=500)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    github_mock.mock_directory("broadcasts", ["hello.md"], repo=SECOND_REPO)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, repo=SECOND_REPO)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md"),
        make_message_id("broadcasts/hello.md", repo=SECOND_REPO),
    ]


@pytest.mark.asyncio
async def test_skips_repo_github_fails(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    mock_installation(github_mock, [GITHUB_REPO, SECOND_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=404)
    github_mock.mock_directory("broadcasts", ["hello.md"], repo=SECOND_REPO)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, repo=SECOND_REPO)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md", repo=SECOND_REPO),
    ]


@pytest.mark.asyncio
async def test_warns_about_repo_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=404)

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert [w["github_repo"] for w in warnings] == [GITHUB_REPO]
    assert "gidgethub.BadRequest" in warnings[0]["exception"]


@pytest.mark.asyncio
async def test_summary_counts_skipped_files(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_malformed_file(github_mock)

    async with LifespanManager(main.app):
        pass

    summaries = [
        log
        for log in get_logs(caplog)
        if log["event"] == "Loaded broadcasts from GitHub"
    ]
    assert [
        (s["message_count"], s["skipped_file_count"]) for s in summaries
    ] == [(1, 1)]


@pytest.mark.asyncio
async def test_loads_repos_on_later_pages(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [OTHER_REPO, GITHUB_REPO], per_page=1)
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]
