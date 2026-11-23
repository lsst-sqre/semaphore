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
    GITHUB_REPO,
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
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    payload = make_push_payload(added=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    broadcasts = await get_broadcasts(github_app_client)
    assert [b["id"] for b in broadcasts] == [
        make_message_id("broadcasts/hello.md")
    ]
    assert broadcasts[0]["summary"]["gfm"] == "Hello from GitHub."


@pytest.mark.asyncio
async def test_push_updates_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    github_mock.mock_file("broadcasts/hello.md", GOODBYE_MD)
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
async def test_push_removes_broadcast(
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    github_mock.mock_installation_token()
    github_mock.mock_file("broadcasts/hello.md", HELLO_MD)
    add = make_push_payload(added=["broadcasts/hello.md"])
    await post_github_webhook(github_app_client, "push", add)
    assert len(await get_broadcasts(github_app_client)) == 1
    remove = make_push_payload(removed=["broadcasts/hello.md"])

    response = await post_github_webhook(github_app_client, "push", remove)

    assert response.status_code == 202
    assert await get_broadcasts(github_app_client) == []


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
        "broadcasts/hello.md", HELLO_MD, repo="LSST-SQRE/RSP_Broadcast"
    )
    payload = make_push_payload(
        repo="LSST-SQRE/RSP_Broadcast", added=["broadcasts/hello.md"]
    )

    response = await post_github_webhook(github_app_client, "push", payload)

    assert response.status_code == 202
    assert len(await get_broadcasts(github_app_client)) == 1
