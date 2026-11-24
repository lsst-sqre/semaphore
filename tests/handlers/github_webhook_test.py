"""Tests for the GitHub App webhook, ``/semaphore/github/webhook``."""

import asyncio
import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Request
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.github import broadcastservices, webhooks
from semaphore.github.reloadqueue import QueuedRepo, ReloadQueue
from semaphore.github.repolocks import RepoLocks

from ..support.constants import TEST_BASE_URL
from ..support.github import (
    DEFAULT_AFTER_SHA,
    GITHUB_INSTALLATION_ID,
    GITHUB_REPO,
    NULL_SHA,
    GitHubMock,
    HeldResponse,
    configure_github_app,
    make_message_id,
    make_push_payload,
    make_repository,
    post_github_webhook,
    post_github_webhook_body,
    sign_webhook_body,
)

HELLO_MD = "---\nsummary: Hello from GitHub.\n---\n"
"""A broadcast message file."""

GOODBYE_MD = "---\nsummary: Goodbye from GitHub.\n---\n"
"""Updated contents of the `HELLO_MD` broadcast message file."""

HELLO_THIS_ENV_MD = "---\nsummary: Hello from GitHub.\nenv: [idfdev]\n---\n"
"""A broadcast message file targeted at the test Phalanx environment."""

HELLO_OTHER_ENV_MD = "---\nsummary: Hello from GitHub.\nenv: [usdfprod]\n---\n"
"""A broadcast message file targeted only at another Phalanx environment."""

SHA_A = "aaaa" + "0" * 36
"""Commit SHA that a first test push moves its branch to."""

SHA_B = "bbbb" + "0" * 36
"""Commit SHA that a second test push moves its branch to."""

SHA_C = "cccc" + "0" * 36
"""Commit SHA that a third test push moves its branch to."""

SECOND_REPO = "lsst-sqre/rsp_broadcast_extra"
"""A second repository that tests may add to the allowlist."""

HELLO_PUSH = make_push_payload(added=["broadcasts/hello.md"])
"""Payload of a push that adds a broadcast message file."""

QUEUED_REPO = QueuedRepo(
    installation_id=str(GITHUB_INSTALLATION_ID), full_name=GITHUB_REPO
)
"""The reload queue's entry for `GITHUB_REPO`, as the test pushes' installation
sees it.
"""


@pytest_asyncio.fixture
async def client_without_repos(
    empty_database: None,
    broadcast_repo: BroadcastMessageRepository,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """Client for the app started with GitHub App credentials but no
    allowlist.
    """
    configure_github_app(monkeypatch, repos=None)
    async with LifespanManager(main.app):
        async with AsyncClient(
            transport=ASGITransport(main.app), base_url=TEST_BASE_URL
        ) as client:
            yield client


@pytest_asyncio.fixture
async def reloading_client(
    *,
    empty_database: None,
    broadcast_repo: BroadcastMessageRepository,
    repo_locks: RepoLocks,
    reload_queue: ReloadQueue,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[AsyncClient]:
    """Client for the app started with the GitHub App enabled, so that the
    reload worker runs.

    The allowlist is `GITHUB_REPO` and `SECOND_REPO`. No installation of the
    app is listed at startup, so no broadcast is loaded then. The worker
    reloads without waiting between attempts, and webhooks are processed
    without the delay that lets GitHub reach consistency.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    monkeypatch.setattr(webhooks, "GITHUB_CONSISTENCY_DELAY", 0)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_INITIAL_DELAY", 0)
    monkeypatch.setattr(broadcastservices, "BOOTSTRAP_RETRY_MAX_DELAY", 0)
    github_mock.mock_installations([])
    async with LifespanManager(main.app):
        async with AsyncClient(
            transport=ASGITransport(main.app), base_url=TEST_BASE_URL
        ) as client:
            yield client


async def wait_for_reloads(reload_queue: ReloadQueue) -> None:
    """Wait, for up to five seconds, until the reload worker has reloaded
    everything in the reload queue.
    """
    async with asyncio.timeout(5):
        await reload_queue.wait_until_empty()


async def get_broadcasts(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    return response.json()


async def get_summaries(client: AsyncClient) -> dict[str, str]:
    """Return the summary of each served broadcast, by identifier."""
    return {b["id"]: b["summary"]["gfm"] for b in await get_broadcasts(client)}


def get_warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    logs = parse_log_tuples("semaphore", caplog.record_tuples)
    return [log for log in logs if log["severity"] == "warning"]


def get_head_requests(github_mock: GitHubMock) -> list[Request]:
    """Return the requests for the commit that a branch points to."""
    return [r for r in github_mock.get_requests() if "/git/ref/" in r.url.path]


def get_contents_requests(github_mock: GitHubMock) -> list[Request]:
    """Return the requests for the contents of files."""
    return [
        r for r in github_mock.get_requests() if "/contents/" in r.url.path
    ]


def mock_main_branch_broadcasts(github_mock: GitHubMock) -> dict[str, Any]:
    """Mock reading the files of a push to ``main``, the default branch,
    that adds ``a.md`` and ``b.md`` at `SHA_A`, and return its payload.
    """
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_A)
    return make_push_payload(
        after=SHA_A, added=["broadcasts/a.md", "broadcasts/b.md"]
    )


@pytest.mark.asyncio
async def test_without_repos_index(client_without_repos: AsyncClient) -> None:
    response = await client_without_repos.get("/semaphore/")

    assert response.status_code == 200
    assert response.json()["github_app_enabled"] is False


@pytest.mark.asyncio
async def test_without_repos_webhook(
    client_without_repos: AsyncClient, github_mock: GitHubMock
) -> None:
    payload = make_push_payload(added=["broadcasts/hello.md"])

    response = await post_github_webhook(client_without_repos, "push", payload)

    assert response.status_code == 501
    assert await get_broadcasts(client_without_repos) == []
    assert github_mock.get_requests() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("empty_database", "broadcast_repo", "github_mock")
async def test_without_repos_startup_warning(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_github_app(monkeypatch, repos=None)

    async with LifespanManager(main.app):
        pass

    warnings = get_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0]["event"] == "GitHub App is disabled"
    assert "SEMAPHORE_GITHUB_REPOS" in warnings[0]["reason"]


@pytest.mark.asyncio
async def test_push_adds_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(DEFAULT_AFTER_SHA)
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, ref=DEFAULT_AFTER_SHA
    )
    payload = make_push_payload(added=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/hello.md")
    ]
    assert broadcasts[0]["summary"]["gfm"] == "Hello from GitHub."


@pytest.mark.asyncio
async def test_push_reads_files_at_branch_head(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """For a push whose ``after`` is the branch head, every file is read at
    the head, which is looked up once.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/goodbye.md", GOODBYE_MD, ref=SHA_A)
    payload = make_push_payload(
        after=SHA_A,
        added=["broadcasts/hello.md"],
        modified=["broadcasts/goodbye.md"],
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 2
    assert len(get_head_requests(github_mock)) == 1
    contents_refs = [
        r.url.params.get("ref") for r in get_contents_requests(github_mock)
    ]
    assert contents_refs == [SHA_A, SHA_A]


@pytest.mark.asyncio
async def test_push_to_default_branch_other_than_main(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(DEFAULT_AFTER_SHA, branch="develop")
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, ref=DEFAULT_AFTER_SHA
    )
    payload = make_push_payload(
        ref="refs/heads/develop",
        default_branch="develop",
        added=["broadcasts/hello.md"],
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/hello.md", ref="refs/heads/develop")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ref", "default_branch"),
    [
        ("refs/heads/tickets/DM-1", "main"),
        ("refs/heads/main", "develop"),
        ("refs/tags/main", "main"),
    ],
)
async def test_push_to_other_branch(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    ref: str,
    default_branch: str,
) -> None:
    payload = make_push_payload(
        ref=ref, default_branch=default_branch, added=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []


@pytest.mark.asyncio
async def test_push_updates_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD, ref=SHA_B)
    add = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    modify = make_push_payload(
        before=SHA_A, after=SHA_B, modified=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", modify)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/hello.md")
    ]
    assert broadcasts[0]["summary"]["gfm"] == "Goodbye from GitHub."


@pytest.mark.asyncio
async def test_push_adds_broadcast_for_other_env(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(DEFAULT_AFTER_SHA)
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_OTHER_ENV_MD, ref=DEFAULT_AFTER_SHA
    )
    payload = make_push_payload(added=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []
    logs = parse_log_tuples("semaphore", caplog.record_tuples)
    assert [log for log in logs if log["severity"] == "error"] == []


@pytest.mark.asyncio
async def test_push_retargeting_broadcast_to_other_env(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A broadcast whose ``env`` no longer includes this environment is
    removed.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_THIS_ENV_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", HELLO_OTHER_ENV_MD, ref=SHA_B)
    add = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    assert len(await get_broadcasts(github_app_client)) == 1
    retarget = make_push_payload(
        before=SHA_A, after=SHA_B, modified=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", retarget)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


@pytest.mark.asyncio
async def test_push_removes_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/hello.md", status_code=404, ref=SHA_B
    )
    add = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    assert len(await get_broadcasts(github_app_client)) == 1
    remove = make_push_payload(
        before=SHA_A, after=SHA_B, removed=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", remove)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


@pytest.mark.asyncio
async def test_push_with_github_error_on_file(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """A GitHub error on one file keeps its stored message, even for a
    removed file, and the push's other files are still processed. The
    repository is queued to be reloaded.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_file("broadcasts/b.md", GOODBYE_MD, ref=SHA_B)
    add = make_push_payload(
        after=SHA_A, added=["broadcasts/a.md", "broadcasts/b.md"]
    )
    await post_github_webhook(github_app_client, "push", add)
    change = make_push_payload(
        before=SHA_A,
        after=SHA_B,
        removed=["broadcasts/a.md"],
        modified=["broadcasts/b.md"],
    )

    response = await post_github_webhook(github_app_client, "push", change)

    assert response.status_code == 202
    broadcasts = {
        b["id"]: b["summary"]["gfm"]
        for b in await get_broadcasts(github_app_client)
    }
    assert broadcasts == {
        make_message_id("broadcasts/a.md"): "Hello from GitHub.",
        make_message_id("broadcasts/b.md"): "Goodbye from GitHub.",
    }
    assert reload_queue.pending() == [QUEUED_REPO]


@pytest.mark.asyncio
async def test_push_with_missing_file_does_not_queue_repo(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """A file that does not exist, or whose content is malformed, is not a
    GitHub failure, so the repository is not queued to be reloaded.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_contents_error(
        "broadcasts/gone.md", status_code=404, ref=SHA_A
    )
    github_mock.mock_file(
        "broadcasts/bad.md", "---\nsummary: [Bad\n---\n", ref=SHA_A
    )
    payload = make_push_payload(
        after=SHA_A,
        added=["broadcasts/bad.md"],
        removed=["broadcasts/gone.md"],
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert len(reload_queue) == 0


@pytest.mark.asyncio
async def test_push_without_broadcast_changes(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A push that changes no broadcast file reads nothing from GitHub."""
    github_mock.mock_installation_token()
    payload = make_push_payload(
        added=["broadcasts/README.md"], modified=["README.md"]
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert get_head_requests(github_mock) == []
    assert get_contents_requests(github_mock) == []


@pytest.mark.asyncio
async def test_push_with_github_error_on_branch_head(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the branch head cannot be looked up, no file is read, the stored
    messages are kept and the repository is queued to be reloaded.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head_error(status_code=503)
    add = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    remove = make_push_payload(
        before=SHA_A, after=SHA_B, removed=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", remove)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 1
    assert len(get_contents_requests(github_mock)) == 1
    assert reload_queue.pending() == [QUEUED_REPO]
    warnings = get_warnings(caplog)
    assert [(w["event"], w["github_repo"]) for w in warnings] == [
        (
            (
                "Could not look up the default branch's head, will reload "
                "the repository"
            ),
            GITHUB_REPO,
        )
    ]
    assert "gidgethub.GitHubBroken" in warnings[0]["exception"]


@pytest.mark.asyncio
async def test_redelivered_add_after_removal(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """Push A adds a file and push B removes it. Delivering A again leaves
    the file removed.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/hello.md", status_code=404, ref=SHA_B
    )
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/hello.md", status_code=404, ref=SHA_B
    )
    push_a = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    push_b = make_push_payload(
        before=SHA_A, after=SHA_B, removed=["broadcasts/hello.md"]
    )
    await post_github_webhook(github_app_client, "push", push_a)
    await post_github_webhook(github_app_client, "push", push_b)

    response = await post_github_webhook(github_app_client, "push", push_a)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


@pytest.mark.asyncio
async def test_redelivered_modification(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """Delivering an old push that modified a file again keeps the file's
    newest content.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD, ref=SHA_B)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD, ref=SHA_B)
    push_a = make_push_payload(after=SHA_A, modified=["broadcasts/hello.md"])
    push_b = make_push_payload(
        before=SHA_A, after=SHA_B, modified=["broadcasts/hello.md"]
    )
    await post_github_webhook(github_app_client, "push", push_a)
    await post_github_webhook(github_app_client, "push", push_b)

    response = await post_github_webhook(github_app_client, "push", push_a)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert len(broadcasts) == 1
    assert broadcasts[0]["summary"]["gfm"] == "Goodbye from GitHub."


@pytest.mark.asyncio
async def test_redelivered_removal_after_readd(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """Push A removes a file and push B adds it back. Delivering A again
    keeps the file with B's content.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_contents_error(
        "broadcasts/hello.md", status_code=404, ref=SHA_A
    )
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD, ref=SHA_B)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD, ref=SHA_B)
    push_a = make_push_payload(after=SHA_A, removed=["broadcasts/hello.md"])
    push_b = make_push_payload(
        before=SHA_A, after=SHA_B, added=["broadcasts/hello.md"]
    )
    await post_github_webhook(github_app_client, "push", push_a)
    await post_github_webhook(github_app_client, "push", push_b)

    response = await post_github_webhook(github_app_client, "push", push_a)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/hello.md")
    ]
    assert broadcasts[0]["summary"]["gfm"] == "Goodbye from GitHub."


@pytest.mark.asyncio
async def test_push_reads_only_touched_files(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A push that is not forced, to the ref that the stored broadcasts came
    from, reads only the head and the files it touches, without listing the
    broadcasts.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/a.md", GOODBYE_MD, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    request_count = len(github_mock.get_requests())
    modify = make_push_payload(
        before=SHA_A, after=SHA_B, modified=["broadcasts/a.md"]
    )

    response = await post_github_webhook(github_app_client, "push", modify)

    assert response.status_code == 202
    github_paths = [
        r.url.path
        for r in github_mock.get_requests()[request_count:]
        if r.method == "GET"
    ]
    repo_path = f"/repos/{GITHUB_REPO}"
    assert github_paths == [
        f"{repo_path}/git/ref/heads/main",
        f"{repo_path}/contents/broadcasts/a.md",
    ]


@pytest.mark.asyncio
async def test_forced_push_reloads_repo(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A force push replaces the repository's broadcasts with those in the
    new history, including removing a file that only the discarded history
    had.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/x.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_file("broadcasts/y.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["y.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/y.md", GOODBYE_MD, ref=SHA_B)
    add = make_push_payload(
        after=SHA_A, added=["broadcasts/x.md", "broadcasts/y.md"]
    )
    await post_github_webhook(github_app_client, "push", add)
    # The new history's commits list neither file.
    force = make_push_payload(
        before=SHA_A, after=SHA_B, forced=True, modified=["README.md"]
    )

    response = await post_github_webhook(github_app_client, "push", force)

    assert response.status_code == 202
    assert await get_summaries(github_app_client) == {
        make_message_id("broadcasts/y.md"): "Goodbye from GitHub."
    }


@pytest.mark.asyncio
async def test_forced_push_with_github_error_on_file(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A GitHub error on a listed file keeps its stored message, while the
    other listed files are still reloaded.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_file("broadcasts/b.md", GOODBYE_MD, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    force = make_push_payload(before=SHA_A, after=SHA_B, forced=True)

    response = await post_github_webhook(github_app_client, "push", force)

    assert response.status_code == 202
    assert await get_summaries(github_app_client) == {
        make_message_id("broadcasts/a.md"): "Hello from GitHub.",
        make_message_id("broadcasts/b.md"): "Goodbye from GitHub.",
    }


@pytest.mark.asyncio
async def test_forced_push_without_broadcasts_dir(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A force push to a history without a broadcasts directory removes all
    of the repository's broadcasts.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error("broadcasts", status_code=404, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    force = make_push_payload(before=SHA_A, after=SHA_B, forced=True)

    response = await post_github_webhook(github_app_client, "push", force)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


@pytest.mark.asyncio
async def test_forced_push_with_github_error_on_listing(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If GitHub fails to list the broadcasts, the stored broadcasts are
    kept and the repository is queued to be reloaded.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error("broadcasts", status_code=503, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    force = make_push_payload(before=SHA_A, after=SHA_B, forced=True)

    response = await post_github_webhook(github_app_client, "push", force)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 2
    assert reload_queue.pending() == [QUEUED_REPO]
    warnings = get_warnings(caplog)
    assert [(w["event"], w["github_repo"]) for w in warnings] == [
        ("Could not list the repository's broadcasts, will retry", GITHUB_REPO)
    ]


@pytest.mark.asyncio
async def test_forced_push_keeps_other_repos(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading a repository leaves the broadcasts of other repositories as
    they are.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A, repo=SECOND_REPO)
    github_mock.mock_file(
        "broadcasts/a.md", HELLO_MD, repo=SECOND_REPO, ref=SHA_A
    )
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["README.md"], ref=SHA_B)
    for repo in (SECOND_REPO, GITHUB_REPO):
        add = make_push_payload(
            repo=repo, after=SHA_A, added=["broadcasts/a.md"]
        )
        await post_github_webhook(github_app_client, "push", add)
    force = make_push_payload(
        before=SHA_A, after=SHA_B, forced=True, removed=["broadcasts/a.md"]
    )

    response = await post_github_webhook(github_app_client, "push", force)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/a.md", repo=SECOND_REPO)
    ]


@pytest.mark.asyncio
async def test_push_ignores_refs_of_other_repos(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broadcasts of another repository from a different branch do not make
    a push reload its repository.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A, repo=SECOND_REPO)
    github_mock.mock_file(
        "broadcasts/a.md", HELLO_MD, repo=SECOND_REPO, ref=SHA_A
    )
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    other = make_push_payload(
        repo=SECOND_REPO, after=SHA_A, added=["broadcasts/a.md"]
    )
    await post_github_webhook(github_app_client, "push", other)
    push = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        after=SHA_B,
        added=["broadcasts/b.md"],
    )

    response = await post_github_webhook(github_app_client, "push", push)

    assert response.status_code == 202
    assert {b["id"] for b in await get_broadcasts(github_app_client)} == {
        make_message_id("broadcasts/a.md", repo=SECOND_REPO),
        make_message_id("broadcasts/b.md", ref="refs/heads/trunk"),
    }


@pytest.mark.asyncio
async def test_push_after_default_branch_change(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """After the default branch changes from ``main`` to ``trunk``, a push
    to ``trunk`` replaces the broadcasts loaded from ``main``.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/a.md", GOODBYE_MD, ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    push = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        before=SHA_A,
        after=SHA_B,
        modified=["broadcasts/a.md"],
    )

    response = await post_github_webhook(github_app_client, "push", push)

    assert response.status_code == 202
    assert await get_summaries(github_app_client) == {
        make_message_id(
            "broadcasts/a.md", ref="refs/heads/trunk"
        ): "Goodbye from GitHub.",
        make_message_id(
            "broadcasts/b.md", ref="refs/heads/trunk"
        ): "Hello from GitHub.",
    }


@pytest.mark.asyncio
async def test_default_branch_change_with_github_error_on_file(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """A broadcast from the old default branch keeps being served if GitHub
    fails to return its file from the new one, and the repository is queued
    to be reloaded. The other broadcasts move to the new branch.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    await post_github_webhook(github_app_client, "push", add)
    push = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        before=SHA_A,
        after=SHA_B,
        modified=["README.md"],
    )

    response = await post_github_webhook(github_app_client, "push", push)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert sorted(b["id"] for b in broadcasts) == [
        make_message_id("broadcasts/a.md"),
        make_message_id("broadcasts/b.md", ref="refs/heads/trunk"),
    ]
    assert reload_queue.pending() == [QUEUED_REPO]


@pytest.mark.asyncio
async def test_removal_after_default_branch_change(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """After a push to the new default branch replaces the broadcasts loaded
    from the old one, removing a file from the new branch removes its
    broadcast.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    github_mock.mock_branch_head(SHA_C, branch="trunk")
    github_mock.mock_contents_error(
        "broadcasts/b.md", status_code=404, ref=SHA_C
    )
    await post_github_webhook(github_app_client, "push", add)
    modify = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        before=SHA_A,
        after=SHA_B,
        modified=["broadcasts/a.md"],
    )
    await post_github_webhook(github_app_client, "push", modify)
    remove = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        before=SHA_B,
        after=SHA_C,
        removed=["broadcasts/b.md"],
    )

    response = await post_github_webhook(github_app_client, "push", remove)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/a.md", ref="refs/heads/trunk")
    ]


@pytest.mark.asyncio
async def test_reloads_repo_after_github_error_on_branch_head(
    reloading_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """A push that removes a file while GitHub fails to return the branch
    head is not dropped: the reload worker removes the file's broadcast
    once GitHub recovers.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A)
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head_error(status_code=502)
    # The reload's first attempt fails as well.
    github_mock.mock_branch_head_error(status_code=502)
    github_mock.mock_branch_head(SHA_B)
    for _ in range(2):
        github_mock.mock_repository(make_repository())
    github_mock.mock_contents_error("broadcasts", status_code=404, ref=SHA_B)
    add = make_push_payload(after=SHA_A, added=["broadcasts/hello.md"])
    await post_github_webhook(reloading_client, "push", add)
    remove = make_push_payload(
        before=SHA_A, after=SHA_B, removed=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(reloading_client, "push", remove)
    await wait_for_reloads(reload_queue)

    assert response.status_code == 202
    assert await get_broadcasts(reloading_client) == []
    assert len(github_mock.get_repository_requests()) == 2


@pytest.mark.asyncio
async def test_reloads_repo_after_github_error_on_file(
    reloading_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """After a push during which GitHub fails to return a file, the reload
    worker makes the broadcasts match the branch head.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_file("broadcasts/b.md", GOODBYE_MD, ref=SHA_B)
    github_mock.mock_repository(make_repository())
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", GOODBYE_MD, ref=SHA_B)
    await post_github_webhook(reloading_client, "push", add)
    change = make_push_payload(
        before=SHA_A,
        after=SHA_B,
        removed=["broadcasts/a.md"],
        modified=["broadcasts/b.md"],
    )

    response = await post_github_webhook(reloading_client, "push", change)
    await wait_for_reloads(reload_queue)

    assert response.status_code == 202
    assert await get_summaries(reloading_client) == {
        make_message_id("broadcasts/b.md"): "Goodbye from GitHub."
    }


@pytest.mark.asyncio
async def test_reloads_repo_after_github_error_on_forced_push_listing(
    reloading_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """After a force push for which GitHub fails to list the broadcasts, the
    reload worker makes the broadcasts match the branch head.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error("broadcasts", status_code=503, ref=SHA_B)
    github_mock.mock_repository(make_repository())
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", GOODBYE_MD, ref=SHA_B)
    await post_github_webhook(reloading_client, "push", add)
    force = make_push_payload(before=SHA_A, after=SHA_B, forced=True)

    response = await post_github_webhook(reloading_client, "push", force)
    await wait_for_reloads(reload_queue)

    assert response.status_code == 202
    assert await get_summaries(reloading_client) == {
        make_message_id("broadcasts/b.md"): "Goodbye from GitHub."
    }


@pytest.mark.asyncio
async def test_reloads_repo_after_default_branch_change_with_github_error(
    reloading_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """On the first push to a new default branch, a broadcast whose file
    GitHub fails to return keeps being served from the old branch until the
    reload worker stores it once, from the new branch.
    """
    github_mock.mock_installation_token()
    add = mock_main_branch_broadcasts(github_mock)
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/a.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    repo_response = HeldResponse()
    github_mock.mock_repository(
        make_repository(default_branch="trunk"), held=repo_response
    )
    github_mock.mock_branch_head(SHA_B, branch="trunk")
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/a.md", GOODBYE_MD, ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    await post_github_webhook(reloading_client, "push", add)
    push = make_push_payload(
        ref="refs/heads/trunk",
        default_branch="trunk",
        before=SHA_A,
        after=SHA_B,
        modified=["README.md"],
    )

    response = await post_github_webhook(reloading_client, "push", push)
    await repo_response.wait_for_request()
    served_before_reload = await get_summaries(reloading_client)
    repo_response.release()
    await wait_for_reloads(reload_queue)

    assert response.status_code == 202
    assert served_before_reload == {
        make_message_id("broadcasts/a.md"): "Hello from GitHub.",
        make_message_id(
            "broadcasts/b.md", ref="refs/heads/trunk"
        ): "Hello from GitHub.",
    }
    assert await get_summaries(reloading_client) == {
        make_message_id(
            "broadcasts/a.md", ref="refs/heads/trunk"
        ): "Goodbye from GitHub.",
        make_message_id(
            "broadcasts/b.md", ref="refs/heads/trunk"
        ): "Hello from GitHub.",
    }


@pytest.mark.asyncio
async def test_failures_while_repo_is_queued_reload_it_once(
    reloading_client: AsyncClient,
    github_mock: GitHubMock,
    reload_queue: ReloadQueue,
) -> None:
    """Pushes that fail for a repository that is already queued leave one
    entry for it, which is reloaded once in the worker's next attempt.

    The worker is held reloading `SECOND_REPO` while the pushes fail.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head_error(status_code=502, repo=SECOND_REPO)
    second_response = HeldResponse()
    github_mock.mock_repository(
        make_repository(SECOND_REPO), held=second_response
    )
    github_mock.mock_branch_head(SHA_A, repo=SECOND_REPO)
    github_mock.mock_contents_error(
        "broadcasts", status_code=404, repo=SECOND_REPO, ref=SHA_A
    )
    github_mock.mock_branch_head_error(status_code=502)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_contents_error(
        "broadcasts/b.md", status_code=502, ref=SHA_B
    )
    github_mock.mock_repository(make_repository())
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_directory("broadcasts", ["a.md", "b.md"], ref=SHA_B)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_B)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    second = make_push_payload(
        repo=SECOND_REPO, after=SHA_A, added=["broadcasts/x.md"]
    )
    await post_github_webhook(reloading_client, "push", second)
    await second_response.wait_for_request()
    push_a = make_push_payload(after=SHA_A, added=["broadcasts/a.md"])
    push_b = make_push_payload(
        before=SHA_A, after=SHA_B, added=["broadcasts/b.md"]
    )

    responses = [
        await post_github_webhook(reloading_client, "push", push)
        for push in (push_a, push_b)
    ]
    pending = reload_queue.pending()
    second_response.release()
    await wait_for_reloads(reload_queue)

    assert [r.status_code for r in responses] == [202, 202]
    assert pending == [
        QueuedRepo(
            installation_id=str(GITHUB_INSTALLATION_ID), full_name=SECOND_REPO
        ),
        QUEUED_REPO,
    ]
    assert len(github_mock.get_repository_requests()) == 1
    assert sorted(b["id"] for b in await get_broadcasts(reloading_client)) == [
        make_message_id("broadcasts/a.md"),
        make_message_id("broadcasts/b.md"),
    ]


@pytest.mark.asyncio
async def test_pushes_to_same_repo_run_one_at_a_time(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A push waits until the previous push for its repository is done."""
    head_a = HeldResponse()
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A, held=head_a)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B)
    github_mock.mock_file("broadcasts/b.md", HELLO_MD, ref=SHA_B)
    push_a = make_push_payload(after=SHA_A, added=["broadcasts/a.md"])
    push_b = make_push_payload(
        before=SHA_A, after=SHA_B, added=["broadcasts/b.md"]
    )

    task_a = asyncio.create_task(
        post_github_webhook(github_app_client, "push", push_a)
    )
    try:
        await head_a.wait_for_request()
        task_b = asyncio.create_task(
            post_github_webhook(github_app_client, "push", push_b)
        )
        # Without the lock, push B would finish well within this time, since
        # none of its GitHub responses are held.
        await asyncio.sleep(0.1)
        b_waited = not task_b.done()
    finally:
        head_a.release()
    responses = await asyncio.gather(task_a, task_b)

    assert b_waited
    assert [r.status_code for r in responses] == [202, 202]
    github_paths = [
        r.url.path
        for r in github_mock.get_requests()
        if r.method == "GET" and r.url.host == "api.github.com"
    ]
    repo_path = f"/repos/{GITHUB_REPO}"
    assert github_paths == [
        f"{repo_path}/git/ref/heads/main",
        f"{repo_path}/contents/broadcasts/a.md",
        f"{repo_path}/git/ref/heads/main",
        f"{repo_path}/contents/broadcasts/b.md",
    ]


@pytest.mark.asyncio
async def test_pushes_to_other_repos_run_concurrently(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A push is processed while a push for another repository is still
    being processed.
    """
    configure_github_app(monkeypatch, repos=f"{GITHUB_REPO},{SECOND_REPO}")
    head_a = HeldResponse()
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(SHA_A, held=head_a)
    github_mock.mock_file("broadcasts/a.md", HELLO_MD, ref=SHA_A)
    github_mock.mock_branch_head(SHA_B, repo=SECOND_REPO)
    github_mock.mock_file(
        "broadcasts/b.md", HELLO_MD, repo=SECOND_REPO, ref=SHA_B
    )
    push_a = make_push_payload(after=SHA_A, added=["broadcasts/a.md"])
    push_b = make_push_payload(
        repo=SECOND_REPO, after=SHA_B, added=["broadcasts/b.md"]
    )

    task_a = asyncio.create_task(
        post_github_webhook(github_app_client, "push", push_a)
    )
    try:
        await head_a.wait_for_request()
        async with asyncio.timeout(5):
            response_b = await post_github_webhook(
                github_app_client, "push", push_b
            )
        a_was_running = not task_a.done()
        broadcasts_before_a = await get_broadcasts(github_app_client)
    finally:
        head_a.release()
    response_a = await task_a

    assert response_b.status_code == 202
    assert a_was_running
    assert [b["id"] for b in broadcasts_before_a] == [
        make_message_id("broadcasts/b.md", repo=SECOND_REPO)
    ]
    assert response_a.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deleted", "after"),
    [(True, NULL_SHA), (True, DEFAULT_AFTER_SHA), (False, NULL_SHA)],
    ids=["deleted-and-null-after", "deleted", "null-after"],
)
async def test_push_deleting_branch(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    deleted: bool,  # noqa: FBT001
    after: str,
) -> None:
    """A push is a branch deletion if either ``deleted`` is set or ``after``
    is the null SHA.
    """
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(DEFAULT_AFTER_SHA)
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, ref=DEFAULT_AFTER_SHA
    )
    add = make_push_payload(added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    request_count = len(github_mock.get_requests())
    # GitHub sends no commits with a branch deletion. A removed file is added
    # here to show that the push is ignored as a whole.
    delete = make_push_payload(
        deleted=deleted, after=after, removed=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", delete)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 1
    assert len(github_mock.get_requests()) == request_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        json.dumps(
            {k: v for k, v in HELLO_PUSH.items() if k != "installation"}
        ),
        json.dumps({**HELLO_PUSH, "installation": None}),
        json.dumps([HELLO_PUSH]),
        "null",
    ],
    ids=["no-installation", "null-installation", "array-body", "null-body"],
)
async def test_push_without_installation(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
    body: str,
) -> None:
    """A signed push whose body has no ``installation.id`` is rejected."""
    response = await post_github_webhook_body(
        github_app_client,
        "push",
        body.encode(),
        signature=sign_webhook_body(body.encode()),
    )

    assert response.status_code == 400
    assert response.text == (
        "Did not find installation.id in the webhook event payload"
    )
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert [w["event"] for w in warnings] == [
        "Rejected a GitHub webhook without an installation ID"
    ]


@pytest.mark.asyncio
async def test_ping(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    payload = {"zen": "Keep it logically awesome.", "hook_id": 1}

    response = await post_github_webhook(github_app_client, "ping", payload)

    assert response.status_code == 200
    assert github_mock.get_requests() == []


@pytest.mark.asyncio
async def test_webhook_without_signature(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps(make_push_payload(added=["broadcasts/hello.md"]))

    response = await post_github_webhook_body(
        github_app_client, "push", body.encode(), signature=None
    )

    assert response.status_code == 401
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert [w["event"] for w in warnings] == [
        "Rejected a GitHub webhook without a valid signature"
    ]


@pytest.mark.asyncio
async def test_webhook_with_wrong_signature(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps(make_push_payload(added=["broadcasts/hello.md"]))
    signature = sign_webhook_body(b"some other body")

    response = await post_github_webhook_body(
        github_app_client, "push", body.encode(), signature=signature
    )

    assert response.status_code == 401
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert [w["event"] for w in warnings] == [
        "Rejected a GitHub webhook without a valid signature"
    ]


@pytest.mark.asyncio
async def test_webhook_with_unsupported_content_type(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = json.dumps(make_push_payload(added=["broadcasts/hello.md"]))

    response = await post_github_webhook_body(
        github_app_client,
        "push",
        body.encode(),
        signature=sign_webhook_body(body.encode()),
        content_type="text/plain",
    )

    assert response.status_code == 400
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert [w["event"] for w in warnings] == [
        "Rejected a GitHub webhook whose body could not be decoded"
    ]


@pytest.mark.asyncio
async def test_push_from_repo_not_in_allowlist(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = make_push_payload(
        repo="lsst-sqre/other", added=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0]["github_repo"] == "lsst-sqre/other"
    assert warnings[0]["reason"] == "not_in_allowlist"


@pytest.mark.asyncio
async def test_push_from_fork(
    github_app_client: AsyncClient,
    github_mock: GitHubMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fork is rejected even though its name is in the allowlist."""
    payload = make_push_payload(fork=True, added=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []
    warnings = get_warnings(caplog)
    assert len(warnings) == 1
    assert warnings[0]["github_repo"] == GITHUB_REPO
    assert warnings[0]["reason"] == "fork"


@pytest.mark.asyncio
async def test_push_from_allowlisted_repo_other_case(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_branch_head(
        DEFAULT_AFTER_SHA, repo="LSST-SQRE/RSP_Broadcast"
    )
    github_mock.mock_file(
        "broadcasts/hello.md",
        HELLO_MD,
        repo="LSST-SQRE/RSP_Broadcast",
        ref=DEFAULT_AFTER_SHA,
    )
    payload = make_push_payload(
        repo="LSST-SQRE/RSP_Broadcast", added=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 1
