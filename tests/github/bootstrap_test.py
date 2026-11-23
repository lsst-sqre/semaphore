"""Tests for loading broadcast messages from GitHub at startup."""

import asyncio
import itertools
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Request
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.github import broadcastservices
from semaphore.github.repolocks import RepoLocks

from ..support.constants import TEST_BASE_URL
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

pytestmark = pytest.mark.usefixtures(
    "empty_database", "broadcast_repo", "repo_locks"
)
"""Each test starts the app with an empty database and broadcast
repository, and its own per-repository locks.
"""


@pytest.fixture
def no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry loading the repositories that failed at startup without
    waiting between attempts.
    """
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 0)


def get_retry_task() -> asyncio.Task[None] | None:
    """Return the task that the running app started to retry the
    repositories that failed at startup, or `None` if it started none.
    """
    return main.app.state.github_retry_task


async def wait_for_retry() -> None:
    """Wait, for up to five seconds, until the running app's retry task has
    loaded every repository that failed at startup.
    """
    task = get_retry_task()
    assert task is not None
    async with asyncio.timeout(5):
        await task


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

    assert get_summaries(caplog) == [
        {
            "event": "Loaded broadcasts from GitHub",
            "severity": "info",
            "github_repos": [GITHUB_REPO, FORK_REPO],
            "repo_count": 1,
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
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_github_fails_to_list(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        await wait_for_retry()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retries_repo_github_fails_to_return_file(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    for _ in range(2):
        github_mock.mock_directory("broadcasts", ["broken.md", "hello.md"])
        github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    github_mock.mock_contents_error("broadcasts/broken.md", status_code=500)
    github_mock.mock_file("broadcasts/broken.md", HELLO_MD)

    async with LifespanManager(main.app):
        await wait_for_retry()
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
    for _ in range(2):
        github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_contents_timeout("broadcasts/hello.md")
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        await wait_for_retry()
        served = await get_served_ids()

    assert served == [make_message_id("broadcasts/hello.md")]


@pytest.mark.asyncio
async def test_does_not_retry_malformed_file(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_malformed_file(github_mock)

    async with LifespanManager(main.app):
        retry_task = get_retry_task()

    assert retry_task is None


@pytest.mark.asyncio
async def test_does_not_retry_file_that_does_not_exist(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_directory("broadcasts", ["gone.md", "hello.md"])
    github_mock.mock_contents_error("broadcasts/gone.md", status_code=404)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        retry_task = get_retry_task()

    assert retry_task is None


@pytest.mark.asyncio
async def test_does_not_retry_when_every_repo_loads(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch, repos=ALLOWLIST)
    mock_github(github_mock)

    async with LifespanManager(main.app):
        retry_task = get_retry_task()

    assert retry_task is None


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_retry(
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502)

    async with LifespanManager(main.app):
        retry_task = get_retry_task()
        assert retry_task is not None
        assert not retry_task.done()

    assert retry_task.cancelled()
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
    github_mock.mock_contents_error("broadcasts", status_code=502, times=2)
    github_mock.mock_directory("broadcasts", ["bad.md", "hello.md"])
    github_mock.mock_file("broadcasts/bad.md", MALFORMED_MD)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        await wait_for_retry()

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
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    # Held as a push for the repository would hold it.
    lock = repo_locks.get(GITHUB_REPO)
    await lock.acquire()
    async with LifespanManager(main.app):
        # Long enough for the retry to finish if it did not wait for the
        # lock.
        await asyncio.sleep(0.1)
        locked_requests = get_listing_requests(github_mock)
        lock.release()
        await wait_for_retry()

    assert len(locked_requests) == 1
    assert len(get_listing_requests(github_mock)) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_retry_delay")
async def test_retry_authorizes_each_attempt(
    github_mock: GitHubMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch)
    mock_installation(github_mock, [GITHUB_REPO])
    github_mock.mock_contents_error("broadcasts", status_code=502, times=2)
    github_mock.mock_directory("broadcasts", ["hello.md"])
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)

    async with LifespanManager(main.app):
        await wait_for_retry()

    # One token at startup, then one for each of the two attempts.
    assert len(github_mock.get_token_requests()) == 3


def test_retry_delays_back_off_exponentially_to_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 10)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 50)

    delays = broadcastservices._iter_retry_delays()

    assert list(itertools.islice(delays, 5)) == [10, 20, 40, 50, 50]
