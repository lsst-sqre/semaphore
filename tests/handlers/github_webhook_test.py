"""Tests for the GitHub App webhook, ``/semaphore/github/webhook``."""

from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository

from ..support.constants import TEST_BASE_URL
from ..support.github import (
    DEFAULT_AFTER_SHA,
    GITHUB_REPO,
    NULL_SHA,
    GitHubMock,
    configure_github_app,
    make_message_id,
    make_push_payload,
    post_github_webhook,
)

HELLO_MD = "---\nsummary: Hello from GitHub.\n---\n"
"""A broadcast message file."""

GOODBYE_MD = "---\nsummary: Goodbye from GitHub.\n---\n"
"""Updated contents of the `HELLO_MD` broadcast message file."""

HELLO_THIS_ENV_MD = "---\nsummary: Hello from GitHub.\nenv: [idfdev]\n---\n"
"""A broadcast message file targeted at the test Phalanx environment."""

HELLO_OTHER_ENV_MD = "---\nsummary: Hello from GitHub.\nenv: [usdfprod]\n---\n"
"""A broadcast message file targeted only at another Phalanx environment."""


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


async def get_broadcasts(client: AsyncClient) -> list[dict[str, Any]]:
    response = await client.get("/semaphore/v1/broadcasts")
    assert response.status_code == 200
    return response.json()


def get_warnings(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    logs = parse_log_tuples("semaphore", caplog.record_tuples)
    return [log for log in logs if log["severity"] == "warning"]


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
async def test_push_reads_files_at_pushed_commit(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    after = "feed" + "0" * 36
    github_mock.mock_installation_token()
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD, ref=after)
    github_mock.mock_file("broadcasts/goodbye.md", GOODBYE_MD, ref=after)
    payload = make_push_payload(
        after=after,
        added=["broadcasts/hello.md"],
        modified=["broadcasts/goodbye.md"],
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 2
    contents_refs = [
        r.url.params.get("ref")
        for r in github_mock.get_requests()
        if "/contents/" in r.url.path
    ]
    assert contents_refs == [after, after]


@pytest.mark.asyncio
async def test_push_to_default_branch_other_than_main(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
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
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, ref=DEFAULT_AFTER_SHA
    )
    github_mock.mock_file(
        "broadcasts/hello.md", GOODBYE_MD, ref=DEFAULT_AFTER_SHA
    )
    add = make_push_payload(added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    modify = make_push_payload(modified=["broadcasts/hello.md"])

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
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_THIS_ENV_MD, ref=DEFAULT_AFTER_SHA
    )
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_OTHER_ENV_MD, ref=DEFAULT_AFTER_SHA
    )
    add = make_push_payload(added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    assert len(await get_broadcasts(github_app_client)) == 1
    retarget = make_push_payload(modified=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", retarget)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


@pytest.mark.asyncio
async def test_push_removes_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_file(
        "broadcasts/hello.md", HELLO_MD, ref=DEFAULT_AFTER_SHA
    )
    add = make_push_payload(added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    assert len(await get_broadcasts(github_app_client)) == 1
    remove = make_push_payload(removed=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", remove)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


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
async def test_push_without_installation(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    payload = make_push_payload(added=["broadcasts/hello.md"])
    del payload["installation"]

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 500
    assert response.text == (
        "Did not find installation.id in the webhook event payload"
    )
    assert await get_broadcasts(github_app_client) == []
    assert github_mock.get_requests() == []


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
