"""Tests for the GitHub App webhook, ``/semaphore/github/webhook``."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient, Request
from safir.testing.logging import parse_log_tuples

from semaphore import main
from semaphore.broadcast.repository import BroadcastMessageRepository

from ..support.constants import TEST_BASE_URL
from ..support.github import (
    DEFAULT_AFTER_SHA,
    GITHUB_REPO,
    NULL_SHA,
    GitHubMock,
    HeldResponse,
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

SHA_A = "aaaa" + "0" * 36
"""Commit SHA that a first test push moves its branch to."""

SHA_B = "bbbb" + "0" * 36
"""Commit SHA that a second test push moves its branch to."""

SECOND_REPO = "lsst-sqre/rsp_broadcast_extra"
"""A second repository that tests may add to the allowlist."""


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


def get_head_requests(github_mock: GitHubMock) -> list[Request]:
    """Return the requests for the commit that a branch points to."""
    return [r for r in github_mock.get_requests() if "/git/ref/" in r.url.path]


def get_contents_requests(github_mock: GitHubMock) -> list[Request]:
    """Return the requests for the contents of files."""
    return [
        r for r in github_mock.get_requests() if "/contents/" in r.url.path
    ]


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
    github_app_client: AsyncClient, github_mock: GitHubMock
) -> None:
    """A GitHub error on one file keeps its stored message, even for a
    removed file, and the push's other files are still processed.
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
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the branch head cannot be looked up, no file is read and the
    stored messages are kept.
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
    logs = parse_log_tuples("semaphore", caplog.record_tuples)
    errors = [log for log in logs if log["severity"] == "error"]
    assert [e["event"] for e in errors] == [
        "Could not look up the default branch's head, ignoring the push"
    ]


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
