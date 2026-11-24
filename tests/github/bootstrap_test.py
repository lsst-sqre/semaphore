"""Tests for loading broadcast messages from GitHub at startup, and for the
reload worker that reloads what GitHub failed to return.
"""

import asyncio
import itertools
from collections.abc import Iterator
from typing import Any, Literal

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Request
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.dependencies.reloadqueue import reload_queue_dependency
from semaphore.github import broadcastservices
from semaphore.github.reloadqueue import (
    QueuedInstallation,
    QueuedInstallationListing,
    QueuedRepo,
    ReloadQueue,
)
from semaphore.github.repolocks import RepoLocks

from ..support.broadcasts import create_active_message
from ..support.constants import TEST_BASE_URL
from ..support.github import (
    GITHUB_INSTALLATION_ID,
    GITHUB_REPO,
    GitHubMock,
    HeldResponse,
    configure_github_app,
    make_installation,
    make_message_id,
    make_repository,
)

OTHER_REPO = "lsst-sqre/other"
"""A repository the app can see that is not in the allowlist."""

FORK_OWNER = "rubin-fork"
"""An account that owns `FORK_REPO`."""

FORK_REPO = f"{FORK_OWNER}/rsp_broadcast"
"""A fork of the broadcast repository whose name is in the allowlist."""

FORK_INSTALLATION_ID = 9012
"""ID of the GitHub App installation, on `FORK_OWNER`, that can see
`FORK_REPO`.
"""

OUTSIDE_OWNER = "someone-else"
"""An account that owns no repository in the allowlist."""

OUTSIDE_INSTALLATION_ID = 3456
"""ID of a GitHub App installation on `OUTSIDE_OWNER`."""

PARTNER_OWNER = "rubin-partner"
"""A second account that owns a repository in the allowlist,
`PARTNER_REPO`.
"""

PARTNER_REPO = f"{PARTNER_OWNER}/rsp_broadcast"
"""A repository in the allowlist, owned by `PARTNER_OWNER`."""

PARTNER_INSTALLATION_ID = 7890
"""ID of the GitHub App installation, on `PARTNER_OWNER`, that can see
`PARTNER_REPO`.
"""

SECOND_REPO = "lsst-sqre/rsp_broadcast_extra"
"""A second repository in the allowlist."""

MISSPELLED_REPO = "lsst-sqre/rsp_broadcst"
"""A repository in the allowlist that no installation can see, owned by an
account the app is installed on.
"""

UNINSTALLED_REPO = "rubin-elsewhere/rsp_broadcast"
"""A repository in the allowlist, owned by an account the app is not
installed on.
"""

ALLOWLIST = f"{GITHUB_REPO},{FORK_REPO}"
"""Value of ``SEMAPHORE_GITHUB_REPOS`` in these tests, unless set."""

HELLO_MD = "---\nsummary: Hello from GitHub.\n---\n"
"""A broadcast message file."""

GOODBYE_MD = "---\nsummary: Goodbye from GitHub.\n---\n"
"""Updated contents of the `HELLO_MD` broadcast message file."""

MALFORMED_MD = "---\nsummary: [Hello from GitHub.\n---\n"
"""A broadcast message file whose front matter is not valid YAML."""

SHA_A = "aaaa" + "0" * 36
"""Commit SHA that a reloaded repository's default branch points to."""

QUEUED_REPO = QueuedRepo(
    installation_id=str(GITHUB_INSTALLATION_ID), full_name=GITHUB_REPO
)
"""The reload queue's entry for `GITHUB_REPO`."""

type InstallationFailure = Literal["token", "listing"]
"""Which request for an installation GitHub fails: creating its token, or
listing its repositories.
"""

INSTALLATION_FAILURES: list[InstallationFailure] = ["token", "listing"]
"""Each `InstallationFailure`, to parametrize tests with."""

type ListingFailure = Literal["server_error", "unauthorized", "unreachable"]
"""How GitHub fails the listing of the app's installations: with a 502
(``server_error``) or a 401 (``unauthorized``) response, or by not being
reachable (``unreachable``).
"""

LISTING_FAILURES: list[ListingFailure] = [
    "server_error",
    "unauthorized",
    "unreachable",
]
"""Each `ListingFailure`, to parametrize tests with."""

pytestmark = pytest.mark.usefixtures(
    "empty_database", "broadcast_repo", "repo_locks", "reload_queue"
)
"""Each test starts the app with an empty database and broadcast
repository, and its own per-repository locks and reload queue.
"""


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the reload worker reload what is queued without waiting between
    attempts.
    """
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 0)


def get_reload_task() -> asyncio.Task[None] | None:
    """Return the reload worker's task that the running app started, or
    `None` if it started none.
    """
    return main.app.state.github_reload_task


async def wait_for_reloads() -> None:
    """Wait, for up to five seconds, until the running app's reload worker
    has reloaded everything in the reload queue.
    """
    reload_queue = await reload_queue_dependency()
    async with asyncio.timeout(5):
        await reload_queue.wait_until_empty()


async def get_served_ids() -> list[str]:
    """Return the identifiers of the broadcasts that the app serves."""
    async with AsyncClient(
        transport=ASGITransport(main.app), base_url=TEST_BASE_URL
    ) as client:
        response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    return [b["id"] for b in response.json()]


def get_listing_requests(github_mock: GitHubMock) -> list[Request]:
    """Return the requests for the listing of `GITHUB_REPO`'s broadcasts
    directory.
    """
    path = f"/repos/{GITHUB_REPO}/contents/broadcasts"
    return [r for r in github_mock.get_requests() if r.url.path == path]


def get_logs(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return parse_log_tuples("semaphore", caplog.record_tuples)


def get_summaries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Return the logs that summarize loading broadcasts at startup."""
    return [
        log
        for log in get_logs(caplog)
        if log["event"] == "Loaded broadcasts from GitHub"
    ]


def get_unseen_repo_warnings(
    caplog: pytest.LogCaptureFixture,
) -> list[dict[str, Any]]:
    """Return the warnings about allowed repositories that no installation
    can see.
    """
    return [
        log
        for log in get_logs(caplog)
        if log["event"]
        == "No GitHub App installation can see these allowed repositories"
    ]


def mock_github(github_mock: GitHubMock) -> None:
    """Mock two installations of the GitHub App.

    The first installation, on the owner of `GITHUB_REPO`, can see
    `GITHUB_REPO` and `OTHER_REPO`; the second, on `FORK_OWNER`, can see
    `FORK_REPO`. Each repository has one broadcast,
    ``broadcasts/hello.md``. Requests for the contents of `OTHER_REPO` and
    `FORK_REPO` are optional, so a test fails on loading their broadcasts
    rather than on requesting them.
    """
    github_mock.mock_installations(
        [
            make_installation(GITHUB_INSTALLATION_ID),
            make_installation(FORK_INSTALLATION_ID, account=FORK_OWNER),
        ]
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
    github_mock.mock_installations([make_installation()])
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(repo) for repo in repos],
        installation_id=GITHUB_INSTALLATION_ID,
        per_page=per_page,
    )


def mock_skipped_installation(
    github_mock: GitHubMock, installation: dict[str, Any]
) -> None:
    """Mock an installation that Semaphore must skip, listed before an
    installation that can see only `GITHUB_REPO`, whose broadcast is
    ``broadcasts/hello.md``.

    Nothing of the skipped installation is mocked but its listing, so a
    request to create its token fails the test.
    """
    github_mock.mock_installations(
        [installation, make_installation(GITHUB_INSTALLATION_ID)]
    )
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(GITHUB_REPO)], installation_id=GITHUB_INSTALLATION_ID
    )
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)


def mock_suspended_installation(github_mock: GitHubMock) -> None:
    """Mock a suspended installation on `FORK_OWNER` with
    `mock_skipped_installation`.
    """
    mock_skipped_installation(
        github_mock,
        make_installation(
            FORK_INSTALLATION_ID, account=FORK_OWNER, suspended=True
        ),
    )


def mock_outside_installation(github_mock: GitHubMock) -> None:
    """Mock an installation on `OUTSIDE_OWNER` with
    `mock_skipped_installation`.
    """
    mock_skipped_installation(
        github_mock,
        make_installation(OUTSIDE_INSTALLATION_ID, account=OUTSIDE_OWNER),
    )


def mock_installation_failure(
    github_mock: GitHubMock,
    failure: InstallationFailure,
    *,
    installation_id: int = GITHUB_INSTALLATION_ID,
    repos: list[str] | None = None,
) -> None:
    """Mock GitHub failing, with a 502, the first request to create an
    installation's token (``token``) or, with a token, to list its
    repositories (``listing``).

    If ``repos`` is set, GitHub then recovers: the installation is
    authorized again and lists ``repos``. Their contents are left for the
    test to mock.
    """
    if failure == "token":
        github_mock.mock_installation_token_error(
            installation_id, status_code=502
        )
        if repos is not None:
            github_mock.mock_installation_token(installation_id)
    else:
        github_mock.mock_installation_token(installation_id)
        github_mock.mock_installation_repositories_error(
            installation_id, status_code=502
        )
    if repos is not None:
        github_mock.mock_installation_repositories(
            [make_repository(repo) for repo in repos],
            installation_id=installation_id,
        )


def mock_listing_failure(
    github_mock: GitHubMock, failure: ListingFailure
) -> None:
    """Mock GitHub failing the first request for the list of the app's
    installations, as `ListingFailure` describes.
    """
    match failure:
        case "server_error":
            github_mock.mock_installations_error(status_code=502)
        case "unauthorized":
            github_mock.mock_installations_error(status_code=401)
        case "unreachable":
            github_mock.mock_installations_unreachable()


def mock_partner_installations(
    github_mock: GitHubMock, *, failed_page: int | None = None
) -> None:
    """Mock one listing of two installations, one on each page.

    The first page lists the installation on the owner of `GITHUB_REPO` and
    the second the installation on `PARTNER_OWNER`. If ``failed_page`` is
    set, GitHub fails that page as `GitHubMock.mock_installations`
    describes.
    """
    github_mock.mock_installations(
        [
            make_installation(GITHUB_INSTALLATION_ID),
            make_installation(PARTNER_INSTALLATION_ID, account=PARTNER_OWNER),
        ],
        per_page=1,
        failed_page=failed_page,
    )


def mock_startup_load(github_mock: GitHubMock) -> None:
    """Mock loading, at startup, the installation on the owner of
    `GITHUB_REPO`, which can see only `GITHUB_REPO`, whose broadcast is
    ``broadcasts/hello.md``.
    """
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories([make_repository(GITHUB_REPO)])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)


def mock_reload_listing(
    github_mock: GitHubMock,
    names: list[str],
    *,
    branch: str = "main",
    sha: str = SHA_A,
) -> None:
    """Mock the requests with which the reload worker lists the broadcasts
    of `GITHUB_REPO` to reload it: the head of the default branch,
    ``branch``, at ``sha``, and the broadcasts ``names`` at ``sha``.

    The files are left for the test to mock, at ``sha``. So is the
    repository's resource, which the worker requests for a queued
    repository but not for a repository of a queued installation.
    """
    github_mock.mock_branch_head(sha, branch=branch)
    github_mock.mock_directory("broadcasts", names, ref=sha)


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

    assert get_summaries(caplog) == [
        {
            "event": "Loaded broadcasts from GitHub",
            "severity": "info",
            "github_repos": [GITHUB_REPO, FORK_REPO],
            "repo_count": 1,
            "failed_installation_count": 0,
            "failed_repo_count": 0,
            "message_count": 1,
            "skipped_file_count": 0,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 500, 502])
async def test_summary_counts_failed_repos(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status_code: int,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    mock_installation(github_mock, [GITHUB_REPO, SECOND_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=status_code)
    github_mock.mock_directory("broadcasts", ["hello.md"], repo=SECOND_REPO)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, repo=SECOND_REPO)

    async with LifespanManager(main.app):
        pass

    assert [
        (s["repo_count"], s["failed_repo_count"])
        for s in get_summaries(caplog)
    ] == [(1, 1)]


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
async def test_loads_other_files_when_github_fails_one(
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
async def test_repo_without_broadcasts_dir_is_not_a_warning(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=404)

    async with LifespanManager(main.app):
        pass

    assert [
        log
        for log in get_logs(caplog)
        if log["severity"] not in ("debug", "info") or "exception" in log
    ] == []


@pytest.mark.asyncio
async def test_counts_repo_without_broadcasts_dir(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    mock_installation(github_mock, [GITHUB_REPO, SECOND_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=404)
    github_mock.mock_directory("broadcasts", ["hello.md"], repo=SECOND_REPO)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, repo=SECOND_REPO)

    async with LifespanManager(main.app):
        pass

    assert [
        (s["repo_count"], s["message_count"]) for s in get_summaries(caplog)
    ] == [(2, 1)]


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

    assert [
        (s["message_count"], s["skipped_file_count"])
        for s in get_summaries(caplog)
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


@pytest.mark.asyncio
async def test_does_not_authorize_suspended_installation(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_suspended_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    assert github_mock.get_token_requests(FORK_INSTALLATION_ID) == []


@pytest.mark.asyncio
async def test_loads_installation_listed_after_suspended_one(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_suspended_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]


@pytest.mark.asyncio
async def test_warns_about_suspended_installation(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_suspended_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [
        log
        for log in get_logs(caplog)
        if log["event"] == "Skipping a suspended GitHub App installation"
    ] == [
        {
            "event": "Skipping a suspended GitHub App installation",
            "severity": "warning",
            "installation_id": str(FORK_INSTALLATION_ID),
            "github_account": FORK_OWNER,
            "suspended_at": "2026-01-01T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_does_not_authorize_installation_on_outside_account(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_outside_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    assert github_mock.get_token_requests(OUTSIDE_INSTALLATION_ID) == []


@pytest.mark.asyncio
async def test_warns_about_installation_on_outside_account(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_outside_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert {
        "event": (
            "Skipping a GitHub App installation on an account that owns no "
            "allowed repository"
        ),
        "severity": "warning",
        "installation_id": str(OUTSIDE_INSTALLATION_ID),
        "github_account": OUTSIDE_OWNER,
    } in warnings


@pytest.mark.asyncio
async def test_matches_installation_account_case_insensitively(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=GITHUB_REPO.upper())
    github_mock.mock_installations(
        [make_installation(account=GITHUB_REPO.split("/")[0].title())]
    )
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", INSTALLATION_FAILURES)
async def test_loads_other_installations_when_github_fails_one(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: InstallationFailure,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    github_mock.mock_installations(
        [
            make_installation(FORK_INSTALLATION_ID, account=FORK_OWNER),
            make_installation(GITHUB_INSTALLATION_ID),
        ]
    )
    mock_installation_failure(
        github_mock, failure, installation_id=FORK_INSTALLATION_ID
    )
    github_mock.mock_installation_token(GITHUB_INSTALLATION_ID)
    github_mock.mock_installation_repositories([make_repository(GITHUB_REPO)])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pass

    assert [m.identifier for m in broadcast_repo.iter()] == [
        make_message_id("broadcasts/hello.md")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", INSTALLATION_FAILURES)
async def test_warns_about_installation_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: InstallationFailure,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, failure)

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert [
        (w["event"], w["installation_id"], w["github_account"])
        for w in warnings
    ] == [
        (
            (
                "Could not authorize a GitHub App installation or list its "
                "repositories, will retry"
            ),
            str(GITHUB_INSTALLATION_ID),
            GITHUB_REPO.split("/")[0],
        )
    ]
    assert "gidgethub.GitHubBroken" in warnings[0]["exception"]


@pytest.mark.asyncio
async def test_summary_counts_failed_installations(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "listing")

    async with LifespanManager(main.app):
        pass

    assert [
        (s["repo_count"], s["failed_installation_count"])
        for s in get_summaries(caplog)
    ] == [(0, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", INSTALLATION_FAILURES)
async def test_queues_installation_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
    failure: InstallationFailure,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, failure)

    async with LifespanManager(main.app):
        pending = reload_queue.pending()

    assert pending == [
        QueuedInstallation(
            installation_id=str(GITHUB_INSTALLATION_ID),
            account=GITHUB_REPO.split("/")[0],
        )
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
@pytest.mark.parametrize("failure", INSTALLATION_FAILURES)
async def test_retries_installation_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: InstallationFailure,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, failure, repos=[GITHUB_REPO])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_retry_does_not_read_rejected_repos(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(
        github_mock, "listing", repos=[OTHER_REPO, GITHUB_REPO]
    )
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert [
        r.url.path
        for r in github_mock.get_requests()
        if "/contents/" in r.url.path
    ] == [
        f"/repos/{GITHUB_REPO}/contents/broadcasts",
        f"/repos/{GITHUB_REPO}/contents/broadcasts/hello.md",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_retry_retries_repo_github_fails_to_list(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository of a retried installation that GitHub fails to list is
    queued as a repository, and reloaded in the next attempt.
    """
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "listing", repos=[GITHUB_REPO])
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_contents_error("broadcasts", status_code=502, ref=SHA_A)
    github_mock.mock_repository(make_repository())
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]
    assert len(get_listing_requests(github_mock)) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_retry_waits_for_repo_lock(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    repo_locks: RepoLocks,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "listing", repos=[GITHUB_REPO])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    # Held as a push for the repository would hold it.
    lock = repo_locks.get(GITHUB_REPO)
    await lock.acquire()
    async with LifespanManager(main.app):
        # Long enough for the retry to finish if it did not wait for the
        # lock.
        await asyncio.sleep(0.1)
        locked_requests = get_listing_requests(github_mock)
        lock.release()
        await wait_for_reloads()

    assert locked_requests == []
    assert len(get_listing_requests(github_mock)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_retry_removes_messages_pushed_since_startup(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    broadcast_repo: BroadcastMessageRepository,
    repo_locks: RepoLocks,
) -> None:
    """The repositories of a retried installation are reloaded in full, so
    a message stored since startup whose file is no longer in the default
    branch is removed.
    """
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "listing", repos=[GITHUB_REPO])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    # Held until a push for the repository, since startup, has stored a
    # message whose file was removed before the retry.
    lock = repo_locks.get(GITHUB_REPO)
    await lock.acquire()
    async with LifespanManager(main.app):
        broadcast_repo.add(
            create_active_message(make_message_id("broadcasts/gone.md"))
        )
        lock.release()
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_logs_installation_retries(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    github_mock.mock_installations([make_installation()])
    github_mock.mock_installation_token_error(status_code=502, times=2)
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository(GITHUB_REPO)])
    mock_reload_listing(github_mock, ["bad.md", "hello.md"])
    github_mock.mock_file("broadcasts/bad.md", MALFORMED_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    account = GITHUB_REPO.split("/")[0]
    retry_logs = [
        log
        for log in get_logs(caplog)
        if log["event"].startswith(
            ("Retrying loading", "Loaded broadcasts from a GitHub App")
        )
    ]
    retrying = "Retrying loading broadcasts from a GitHub App installation"
    loaded = "Loaded broadcasts from a GitHub App installation after retrying"
    assert retry_logs == [
        {
            "event": retrying,
            "severity": "info",
            "installation_id": str(GITHUB_INSTALLATION_ID),
            "github_account": account,
            "attempt": 1,
        },
        {
            "event": retrying,
            "severity": "info",
            "installation_id": str(GITHUB_INSTALLATION_ID),
            "github_account": account,
            "attempt": 2,
        },
        {
            "event": loaded,
            "severity": "info",
            "installation_id": str(GITHUB_INSTALLATION_ID),
            "github_account": account,
            "attempt": 2,
            "repo_count": 1,
            "failed_repo_count": 0,
            "message_count": 1,
            "skipped_file_count": 1,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", LISTING_FAILURES)
async def test_starts_when_github_fails_to_list_installations(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: ListingFailure,
) -> None:
    configure_github_app(monkeypatch)
    mock_listing_failure(github_mock, failure)

    async with LifespanManager(main.app):
        served = await get_served_ids()

    assert served == []


@pytest.mark.asyncio
async def test_warns_about_installation_listing_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_listing_failure(github_mock, "server_error")

    async with LifespanManager(main.app):
        pass

    warnings = [
        log for log in get_logs(caplog) if log["severity"] == "warning"
    ]
    assert [w["event"] for w in warnings] == [
        "Could not list the GitHub App's installations, will retry"
    ]
    assert "gidgethub.GitHubBroken" in warnings[0]["exception"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", LISTING_FAILURES)
async def test_queues_installation_listing_github_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
    failure: ListingFailure,
) -> None:
    configure_github_app(monkeypatch)
    mock_listing_failure(github_mock, failure)

    async with LifespanManager(main.app):
        pending = reload_queue.pending()

    assert pending == [QueuedInstallationListing()]


@pytest.mark.asyncio
async def test_loads_installations_listed_before_listing_fails(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installations on the pages that GitHub returned before failing
    are loaded at startup.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{PARTNER_REPO}")
    mock_partner_installations(github_mock, failed_page=2)
    mock_startup_load(github_mock)

    async with LifespanManager(main.app):
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
async def test_logs_summary_when_listing_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{PARTNER_REPO}")
    mock_partner_installations(github_mock, failed_page=2)
    mock_startup_load(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [
        (s["repo_count"], s["failed_installation_count"], s["message_count"])
        for s in get_summaries(caplog)
    ] == [(1, 0, 1)]


@pytest.mark.asyncio
async def test_does_not_warn_about_unseen_repos_while_listing_fails(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(
        monkeypatch, repos=f"{GITHUB_REPO},{PARTNER_REPO},{MISSPELLED_REPO}"
    )
    mock_partner_installations(github_mock, failed_page=2)
    mock_startup_load(github_mock)

    async with LifespanManager(main.app):
        pass

    assert get_unseen_repo_warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
@pytest.mark.parametrize("failure", LISTING_FAILURES)
async def test_retries_installation_listing(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    failure: ListingFailure,
) -> None:
    configure_github_app(monkeypatch)
    mock_listing_failure(github_mock, failure)
    github_mock.mock_installations([make_installation()])
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_installation_listing_github_fails_again(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listing that GitHub fails again stays queued and is tried in the
    next attempt.
    """
    configure_github_app(monkeypatch)
    github_mock.mock_installations_error(status_code=502, times=2)
    github_mock.mock_installations([make_installation()])
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]
    assert len(github_mock.get_installations_requests()) == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_listing_retry_loads_installations_of_every_page(
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a listing that failed on its second page, the installations of
    every page are loaded, and each broadcast of a repository that loaded at
    startup is stored once.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{PARTNER_REPO}")
    mock_partner_installations(github_mock, failed_page=2)
    mock_startup_load(github_mock)
    mock_partner_installations(github_mock)
    github_mock.mock_installation_repositories([make_repository(GITHUB_REPO)])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_installation_token(PARTNER_INSTALLATION_ID)
    github_mock.mock_installation_repositories(
        [make_repository(PARTNER_REPO)],
        installation_id=PARTNER_INSTALLATION_ID,
    )
    github_mock.mock_branch_head(SHA_A, repo=PARTNER_REPO)
    github_mock.mock_directory(
        "broadcasts", ["hello.md"], repo=PARTNER_REPO, ref=SHA_A
    )
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, repo=PARTNER_REPO, ref=SHA_A
    )

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    expected = [
        make_message_id("broadcasts/hello.md"),
        make_message_id("broadcasts/hello.md", repo=PARTNER_REPO),
    ]
    assert sorted(served) == sorted(expected)
    assert sorted(m.identifier for m in broadcast_repo.iter()) == sorted(
        expected
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_listing_retry_creates_jwt_each_attempt(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    jwts = (f"test-jwt-{number}" for number in itertools.count(1))
    monkeypatch.setattr(broadcastservices, "get_app_jwt", lambda: next(jwts))
    configure_github_app(monkeypatch)
    github_mock.mock_installations_error(status_code=502, times=2)
    github_mock.mock_installations([])

    async with LifespanManager(main.app):
        await wait_for_reloads()

    # One JWT at startup, then one for each of the worker's two attempts.
    assert [
        r.headers["authorization"]
        for r in github_mock.get_installations_requests()
    ] == ["bearer test-jwt-1", "bearer test-jwt-2", "bearer test-jwt-3"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
@pytest.mark.parametrize(
    "skipped",
    [
        make_installation(
            FORK_INSTALLATION_ID, account=FORK_OWNER, suspended=True
        ),
        make_installation(OUTSIDE_INSTALLATION_ID, account=OUTSIDE_OWNER),
    ],
    ids=["suspended", "outside_account"],
)
async def test_installation_listing_retry_does_not_authorize_skipped(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    skipped: dict[str, Any],
) -> None:
    """The worker skips the installations that startup skips, without
    requesting a token for them.
    """
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_listing_failure(github_mock, "server_error")
    github_mock.mock_installations([skipped, make_installation()])
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert github_mock.get_token_requests(skipped["id"]) == []
    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_logs_installation_listing_retries(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    github_mock.mock_installations_error(status_code=502, times=2)
    github_mock.mock_installations(
        [
            make_installation(
                FORK_INSTALLATION_ID, account=FORK_OWNER, suspended=True
            ),
            make_installation(),
        ]
    )
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    listing_logs = [
        log
        for log in get_logs(caplog)
        if log["event"].startswith(("Retrying listing", "Listed the"))
    ]
    retrying = "Retrying listing the GitHub App's installations"
    assert listing_logs == [
        {"event": retrying, "severity": "info", "attempt": 1},
        {"event": retrying, "severity": "info", "attempt": 2},
        {
            "event": "Listed the GitHub App's installations after retrying",
            "severity": "info",
            "attempt": 2,
            "installation_count": 1,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_warns_about_unseen_repos_after_listing_installations(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{MISSPELLED_REPO}")
    mock_listing_failure(github_mock, "server_error")
    github_mock.mock_installations([make_installation()])
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert get_unseen_repo_warnings(caplog) == [
        {
            "event": (
                "No GitHub App installation can see these allowed repositories"
            ),
            "severity": "warning",
            "github_repos": [MISSPELLED_REPO],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_listing_retry_does_not_warn_about_repos_of_failed_installation(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The warning after the worker lists the installations leaves out the
    repositories of an account whose installation is still pending, and is
    not logged again once that installation loads.
    """
    configure_github_app(
        monkeypatch, repos=f"{GITHUB_REPO},{PARTNER_REPO},{MISSPELLED_REPO}"
    )
    mock_listing_failure(github_mock, "server_error")
    mock_partner_installations(github_mock)
    github_mock.mock_installation_token()
    github_mock.mock_installation_repositories([make_repository()])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    mock_installation_failure(
        github_mock,
        "token",
        installation_id=PARTNER_INSTALLATION_ID,
        repos=[PARTNER_REPO],
    )
    github_mock.mock_branch_head(SHA_A, repo=PARTNER_REPO)
    github_mock.mock_directory("broadcasts", [], repo=PARTNER_REPO, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert [w["github_repos"] for w in get_unseen_repo_warnings(caplog)] == [
        [MISSPELLED_REPO]
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_installation_retry_does_not_warn_about_unseen_repos(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only a retried listing of the installations warns about unseen
    repositories, since the worker knows what every installation can see
    only after listing them.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{MISSPELLED_REPO}")
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "token", repos=[GITHUB_REPO])
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert get_unseen_repo_warnings(caplog) == []


@pytest.mark.asyncio
async def test_warns_about_allowed_repos_no_installation_can_see(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(
        monkeypatch,
        repos=f"{GITHUB_REPO},{MISSPELLED_REPO},{UNINSTALLED_REPO}",
    )
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pass

    assert get_unseen_repo_warnings(caplog) == [
        {
            "event": (
                "No GitHub App installation can see these allowed repositories"
            ),
            "severity": "warning",
            "github_repos": [MISSPELLED_REPO, UNINSTALLED_REPO],
        }
    ]


@pytest.mark.asyncio
async def test_does_not_warn_when_every_allowed_repo_is_seen(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        pass

    assert get_unseen_repo_warnings(caplog) == []


@pytest.mark.asyncio
async def test_sees_allowed_repos_case_insensitively(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=GITHUB_REPO.upper())
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pass

    assert get_unseen_repo_warnings(caplog) == []


@pytest.mark.asyncio
async def test_warns_about_allowed_repos_of_suspended_installation(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_suspended_installation(github_mock)

    async with LifespanManager(main.app):
        pass

    assert [w["github_repos"] for w in get_unseen_repo_warnings(caplog)] == [
        [FORK_REPO]
    ]


@pytest.mark.asyncio
async def test_does_not_warn_about_repos_of_failed_installation(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(
        monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO},{UNINSTALLED_REPO}"
    )
    github_mock.mock_installations([make_installation()])
    mock_installation_failure(github_mock, "listing")

    async with LifespanManager(main.app):
        pass

    assert [w["github_repos"] for w in get_unseen_repo_warnings(caplog)] == [
        [UNINSTALLED_REPO]
    ]


@pytest.mark.asyncio
async def test_queues_repo_github_fails_to_list(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)

    async with LifespanManager(main.app):
        pending = reload_queue.pending()

    assert pending == [QUEUED_REPO]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_github_fails_to_list(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    github_mock.mock_repository(make_repository())
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_github_fails_to_return_file(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["broken.md", "hello.md"])
    github_mock.mock_contents_error("broadcasts/broken.md", status_code=500)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    github_mock.mock_repository(make_repository())
    mock_reload_listing(github_mock, ["broken.md", "hello.md"])
    github_mock.mock_file("broadcasts/broken.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert sorted(served) == [
        make_message_id("broadcasts/broken.md"),
        make_message_id("broadcasts/hello.md"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_whose_file_times_out(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_contents_timeout("broadcasts/hello.md")
    github_mock.mock_repository(make_repository())
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_removes_file_deleted_before_retry(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository that failed at startup is reloaded in full, so a file
    that was deleted before the retry stops being served.
    """
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["broken.md", "gone.md"])
    github_mock.mock_contents_error("broadcasts/broken.md", status_code=502)
    github_mock.mock_file("broadcasts/gone.md", HELLO_MD)
    repo_response = HeldResponse()
    github_mock.mock_repository(make_repository(), held=repo_response)
    mock_reload_listing(github_mock, ["broken.md"])
    github_mock.mock_file("broadcasts/broken.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await repo_response.wait_for_request()
        served_at_startup = await get_served_ids()
        repo_response.release()
        await wait_for_reloads()
        served = await get_served_ids()

    assert make_message_id("broadcasts/gone.md") in served_at_startup
    assert served == [make_message_id("broadcasts/broken.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_follows_default_branch_change(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository whose default branch changed before the retry serves one
    copy of each broadcast, from the new default branch.
    """
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"])
    github_mock.mock_contents_error("broadcasts/a.md", status_code=502)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD)
    github_mock.mock_repository(make_repository(default_branch="trunk"))
    mock_reload_listing(github_mock, ["a.md", "b.md"], branch="trunk")
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert sorted(served) == [
        make_message_id("broadcasts/a.md", ref="refs/heads/trunk"),
        make_message_id("broadcasts/b.md", ref="refs/heads/trunk"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_github_fails_again(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository that GitHub fails again while it is reloaded stays
    queued and is reloaded in the next attempt. The file that failed keeps
    being served meanwhile.
    """
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"])
    github_mock.mock_file("broadcasts/a.md", HELLO_MD)
    github_mock.mock_contents_error("broadcasts/b.md", status_code=502)
    for _ in range(2):
        github_mock.mock_repository(make_repository())
        mock_reload_listing(github_mock, ["a.md", "b.md"])
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_A
    )
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/a.md", GOODBYE_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        served = await get_served_ids()

    assert sorted(served) == [
        make_message_id("broadcasts/a.md"),
        make_message_id("broadcasts/b.md"),
    ]
    assert len(github_mock.get_repository_requests()) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_drops_repo_github_cannot_find(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    github_mock.mock_repository_error(status_code=404)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert [
        (log["event"], log["github_repo"])
        for log in get_logs(caplog)
        if log["event"].startswith("Dropping")
    ] == [
        (
            (
                "Dropping a repository that GitHub cannot find from the "
                "reload queue"
            ),
            GITHUB_REPO,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_drops_repo_source_policy_rejects(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A queued repository that is now a fork is dropped from the queue
    without reading its broadcasts.
    """
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    github_mock.mock_repository(make_repository(fork=True))

    async with LifespanManager(main.app):
        await wait_for_reloads()

    assert len(get_listing_requests(github_mock)) == 1
    assert [
        (log["event"], log["github_repo"], log["reason"])
        for log in get_logs(caplog)
        if log["event"].startswith("Dropping")
    ] == [
        (
            (
                "Dropping a repository that may not publish broadcasts from "
                "the reload queue"
            ),
            GITHUB_REPO,
            "fork",
        )
    ]


@pytest.mark.asyncio
async def test_does_not_queue_malformed_file(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
) -> None:
    configure_github_app(monkeypatch)
    mock_malformed_file(github_mock)

    async with LifespanManager(main.app):
        pending = reload_queue.pending()

    assert pending == []


@pytest.mark.asyncio
async def test_does_not_queue_file_that_does_not_exist(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["gone.md", "hello.md"])
    github_mock.mock_contents_error("broadcasts/gone.md", status_code=404)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        pending = reload_queue.pending()

    assert pending == []


@pytest.mark.asyncio
async def test_reload_worker_runs_when_every_repo_loads(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        reload_task = get_reload_task()
        assert reload_task is not None
        await asyncio.sleep(0)
        running = not reload_task.done()
        pending = reload_queue.pending()

    assert running
    assert pending == []


@pytest.mark.asyncio
async def test_no_reload_worker_when_github_app_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_github_app(monkeypatch, repos=None)

    async with LifespanManager(main.app):
        reload_task = get_reload_task()

    assert reload_task is None


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_retry(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker is cancelled while it waits for its first attempt."""
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)

    async with LifespanManager(main.app):
        reload_task = get_reload_task()
        assert reload_task is not None
        await asyncio.sleep(0)
        assert not reload_task.done()

    assert reload_task.cancelled()
    assert [
        log
        for log in get_logs(caplog)
        if log["severity"] in ("error", "critical")
    ] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_shutdown_cancels_retry_in_progress(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    repo_locks: RepoLocks,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker is cancelled while it waits for GitHub during a reload,
    and the repository's lock is released.
    """
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    repo_response = HeldResponse()
    github_mock.mock_repository(make_repository(), held=repo_response)

    async with LifespanManager(main.app):
        reload_task = get_reload_task()
        assert reload_task is not None
        await repo_response.wait_for_request()

    assert reload_task.cancelled()
    assert not repo_locks.get(GITHUB_REPO).locked()
    assert [
        log
        for log in get_logs(caplog)
        if log["severity"] in ("error", "critical")
    ] == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_logs_retries(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    for _ in range(2):
        github_mock.mock_repository(make_repository())
        github_mock.mock_branch_head(SHA_A)
    github_mock.mock_contents_error("broadcasts", status_code=502, ref=SHA_A)
    github_mock.mock_directory("broadcasts", ["bad.md", "hello.md"], ref=SHA_A)
    github_mock.mock_file("broadcasts/bad.md", MALFORMED_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    retry_logs = [
        log
        for log in get_logs(caplog)
        if log["event"].startswith(
            ("Retrying loading", "Loaded broadcasts from a repository")
        )
    ]
    assert retry_logs == [
        {
            "event": "Retrying loading broadcasts from a repository",
            "severity": "info",
            "github_repo": GITHUB_REPO,
            "attempt": 1,
        },
        {
            "event": "Retrying loading broadcasts from a repository",
            "severity": "info",
            "github_repo": GITHUB_REPO,
            "attempt": 2,
        },
        {
            "event": "Loaded broadcasts from a repository after retrying",
            "severity": "info",
            "github_repo": GITHUB_REPO,
            "attempt": 2,
            "message_count": 1,
            "skipped_file_count": 1,
        },
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_waits_for_repo_lock(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    repo_locks: RepoLocks,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    github_mock.mock_repository(make_repository())
    mock_reload_listing(github_mock, ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    # Held as a push for the repository would hold it.
    lock = repo_locks.get(GITHUB_REPO)
    await lock.acquire()
    async with LifespanManager(main.app):
        # Long enough for the retry to finish if it did not wait for the
        # lock.
        await asyncio.sleep(0.1)
        locked_requests = github_mock.get_repository_requests()
        lock.release()
        await wait_for_reloads()

    assert locked_requests == []
    assert len(github_mock.get_repository_requests()) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_authorizes_each_attempt(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    for _ in range(2):
        github_mock.mock_repository(make_repository())
        github_mock.mock_branch_head(SHA_A)
    github_mock.mock_contents_error("broadcasts", status_code=502, ref=SHA_A)
    github_mock.mock_directory("broadcasts", ["hello.md"], ref=SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()

    # One token at startup, then one for each of the two attempts.
    assert len(github_mock.get_token_requests()) == 3


@pytest.mark.asyncio
async def test_retry_backoff_restarts_once_queue_empties(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    reload_queue: ReloadQueue,
) -> None:
    """The waits between attempts grow while something stays queued, and
    start again from the initial delay for what is queued after the queue
    has emptied.
    """
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 10)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 300)
    iter_delays = broadcastservices._iter_retry_delays
    rounds: list[list[float]] = []

    def iter_recorded_delays() -> Iterator[float]:
        """Record the delays of each round of attempts, without waiting."""
        delays: list[float] = []
        rounds.append(delays)
        for delay in iter_delays():
            delays.append(delay)
            yield 0

    monkeypatch.setattr(
        broadcastservices, "_iter_retry_delays", iter_recorded_delays
    )
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    # The first round's two attempts, then the second round's one.
    github_mock.mock_contents_error("broadcasts", status_code=502, ref=SHA_A)
    for _ in range(2):
        mock_reload_listing(github_mock, [])
    for _ in range(3):
        github_mock.mock_repository(make_repository())
    github_mock.mock_branch_head(SHA_A)

    async with LifespanManager(main.app):
        await wait_for_reloads()
        reload_queue.add_repo(
            installation_id=str(GITHUB_INSTALLATION_ID),
            repo_full_name=GITHUB_REPO,
        )
        await wait_for_reloads()

    assert rounds == [[10, 20], [10]]


def test_retry_delays_back_off_exponentially_to_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 10)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 50)

    delays = broadcastservices._iter_retry_delays()

    assert list(itertools.islice(delays, 5)) == [10, 20, 40, 50, 50]
