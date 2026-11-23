"""Test utilities for Semaphore's GitHub App integration.

These helpers configure the GitHub App, sign and post webhook events to
Semaphore, and mock the GitHub REST API with pytest-httpx, including the app
installations, repositories and contents that Semaphore reads at startup and
the branch heads that it resolves for pushes.
"""

import asyncio
import hashlib
import hmac
import json
import zlib
from collections.abc import Iterable
from functools import cache
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from pytest_httpx import HTTPXMock

from .config import reload_config

__all__ = [
    "DEFAULT_AFTER_SHA",
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "NULL_SHA",
    "GitHubMock",
    "HeldResponse",
    "configure_github_app",
    "get_installation_token",
    "make_message_id",
    "make_push_payload",
    "make_repository",
    "post_github_webhook",
]

GITHUB_API_URL = "https://api.github.com"
"""Base URL of the GitHub REST API."""

GITHUB_APP_ID = "1234"
"""GitHub App ID used in tests."""

GITHUB_INSTALLATION_ID = 5678
"""ID of the GitHub App installation that sends test webhooks."""

GITHUB_REPO = "lsst-sqre/rsp_broadcast"
"""Repository in the allowlist that `configure_github_app` sets."""

GITHUB_WEBHOOK_SECRET = "test-webhook-secret"
"""Webhook secret that `configure_github_app` sets and webhooks are signed
with.
"""

DEFAULT_AFTER_SHA = "c0ffee" + "0" * 34
"""Commit SHA a test push moves its branch to, unless set."""

DEFAULT_BEFORE_SHA = "decade" + "0" * 34
"""Commit SHA a test push moves its branch from, unless set."""

NULL_SHA = "0" * 40
"""Commit SHA GitHub reports for the far side of a push that creates or
deletes a ref.
"""


@cache
def get_github_app_private_key() -> str:
    """Return a PEM-encoded RSA key to sign GitHub App JWTs with.

    The key is generated once per test session rather than stored in the
    repository.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption()
    )
    return pem.decode()


def configure_github_app(
    monkeypatch: pytest.MonkeyPatch, *, repos: str | None = GITHUB_REPO
) -> None:
    """Configure the GitHub App through the environment for one test.

    Parameters
    ----------
    monkeypatch
        The pytest monkeypatch fixture of the test.
    repos
        Value of ``SEMAPHORE_GITHUB_REPOS``, or `None` to leave it unset.
    """
    monkeypatch.delenv("SEMAPHORE_ENABLE_GITHUB_APP", raising=False)
    monkeypatch.setenv("SEMAPHORE_GITHUB_APP_ID", GITHUB_APP_ID)
    monkeypatch.setenv(
        "SEMAPHORE_GITHUB_APP_PRIVATE_KEY", get_github_app_private_key()
    )
    monkeypatch.setenv(
        "SEMAPHORE_GITHUB_WEBHOOK_SECRET", GITHUB_WEBHOOK_SECRET
    )
    if repos is None:
        monkeypatch.delenv("SEMAPHORE_GITHUB_REPOS", raising=False)
    else:
        monkeypatch.setenv("SEMAPHORE_GITHUB_REPOS", repos)
    reload_config(monkeypatch)


def get_installation_token(installation_id: int) -> str:
    """Return the access token `GitHubMock` issues to an installation."""
    return f"ghs_test-installation-{installation_id}"


def make_message_id(
    path: str, *, repo: str = GITHUB_REPO, ref: str = "refs/heads/main"
) -> str:
    """Return the identifier of a broadcast message sourced from GitHub."""
    return f"github.com/{repo}/{path}/?ref={ref}"


def make_repository(
    repo: str = GITHUB_REPO,
    *,
    fork: bool = False,
    default_branch: str = "main",
) -> dict[str, Any]:
    """Make a GitHub repository resource, as listed by the REST API.

    Only the fields Semaphore reads, plus a few for realism, are included.

    Parameters
    ----------
    repo
        Full name (``owner/repo``) of the repository.
    fork
        Whether the repository is a fork.
    default_branch
        The repository's default branch.

    Returns
    -------
    dict
        The repository resource.
    """
    owner, name = repo.split("/")
    return {
        "id": zlib.crc32(repo.encode()),
        "name": name,
        "full_name": repo,
        "owner": {"login": owner},
        "private": False,
        "fork": fork,
        "default_branch": default_branch,
        "url": f"{GITHUB_API_URL}/repos/{repo}",
        "contents_url": f"{GITHUB_API_URL}/repos/{repo}/contents/{{+path}}",
    }


def make_push_payload(
    *,
    repo: str = GITHUB_REPO,
    fork: bool = False,
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    before: str = DEFAULT_BEFORE_SHA,
    after: str = DEFAULT_AFTER_SHA,
    deleted: bool = False,
    forced: bool = False,
    added: list[str] | None = None,
    modified: list[str] | None = None,
    removed: list[str] | None = None,
    installation_id: int = GITHUB_INSTALLATION_ID,
) -> dict[str, Any]:
    """Make the payload of a GitHub ``push`` webhook event.

    The payload has one commit with the given file changes. Only the fields
    Semaphore reads, plus a few for realism, are included.

    Parameters
    ----------
    repo
        Full name (``owner/repo``) of the pushed repository.
    fork
        Whether the pushed repository is a fork.
    ref
        Git ref that was pushed.
    default_branch
        The repository's default branch.
    before
        Commit SHA of the ref before the push.
    after
        Commit SHA of the ref after the push.
    deleted
        Whether the push deletes the ref. GitHub also sets ``after`` to
        `NULL_SHA` for such a push.
    forced
        Whether the push is a force push, which may discard commits that
        were on the ref.
    added
        Paths of files the commit adds.
    modified
        Paths of files the commit modifies.
    removed
        Paths of files the commit removes.
    installation_id
        ID of the GitHub App installation that sends the event.

    Returns
    -------
    dict
        The webhook payload.
    """
    owner = repo.split("/", maxsplit=1)[0]
    commit = {
        "id": after,
        "message": "Update broadcasts",
        "added": added or [],
        "modified": modified or [],
        "removed": removed or [],
    }
    return {
        "ref": ref,
        "before": before,
        "after": after,
        "created": False,
        "deleted": deleted,
        "forced": forced,
        "repository": {
            **make_repository(repo, fork=fork, default_branch=default_branch),
            # Push payloads describe the repository slightly differently
            # from the REST API.
            "owner": {"name": owner, "login": owner},
            "master_branch": default_branch,
            "url": f"https://github.com/{repo}",
        },
        "installation": {"id": installation_id},
        "commits": [commit],
        "head_commit": commit,
    }


async def post_github_webhook(
    client: httpx.AsyncClient, event: str, payload: dict[str, Any]
) -> httpx.Response:
    """Post a webhook event to Semaphore, signed as GitHub would sign it.

    Parameters
    ----------
    client
        Client for the Semaphore app.
    event
        Name of the GitHub event, such as ``push``.
    payload
        Payload of the event.

    Returns
    -------
    httpx.Response
        Semaphore's response.
    """
    body = json.dumps(payload).encode()
    digest = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": str(uuid4()),
        "X-Hub-Signature-256": f"sha256={digest}",
    }
    return await client.post(
        "/semaphore/github/webhook", content=body, headers=headers
    )


class HeldResponse:
    """Hold a mocked GitHub response until the test releases it.

    Pass an instance to a `GitHubMock` method that accepts one, so that the
    test can act while Semaphore waits for GitHub's response.
    """

    def __init__(self) -> None:
        self._requested = asyncio.Event()
        self._released = asyncio.Event()

    async def wait_for_request(self) -> None:
        """Wait, for up to five seconds, until the request is made."""
        async with asyncio.timeout(5):
            await self._requested.wait()

    def release(self) -> None:
        """Let the mock send the response."""
        self._released.set()

    async def hold(self) -> None:
        """Note the request and wait until the test releases the response.

        The mock calls this when the request is made.
        """
        self._requested.set()
        await self._released.wait()


class GitHubMock:
    """Mock the GitHub REST API as seen by Semaphore's GitHub App.

    Requests that no mocked response matches fail the test, as do mocked
    responses that are never requested.

    Parameters
    ----------
    httpx_mock
        The pytest-httpx fixture.
    """

    def __init__(self, httpx_mock: HTTPXMock) -> None:
        self._httpx_mock = httpx_mock

    def mock_installations(self, installation_ids: Iterable[int]) -> None:
        """Mock one request for the list of installations of the app.

        Parameters
        ----------
        installation_ids
            IDs of the installations, in the order GitHub lists them.
        """
        self._httpx_mock.add_response(
            method="GET",
            url=f"{GITHUB_API_URL}/app/installations",
            json=[
                {"id": installation_id} for installation_id in installation_ids
            ],
        )

    def mock_installation_token(
        self, installation_id: int = GITHUB_INSTALLATION_ID
    ) -> None:
        """Mock creating access tokens for an installation of the app.

        Any number of tokens may be created, but at least one must be. The
        token is `get_installation_token` of the installation.
        """
        self._httpx_mock.add_response(
            method="POST",
            url=(
                f"{GITHUB_API_URL}/app/installations/{installation_id}"
                "/access_tokens"
            ),
            status_code=201,
            json={
                "token": get_installation_token(installation_id),
                "expires_at": "2099-01-01T00:00:00Z",
            },
            is_reusable=True,
        )

    def mock_installation_repositories(
        self,
        repositories: list[dict[str, Any]],
        *,
        installation_id: int = GITHUB_INSTALLATION_ID,
        per_page: int | None = None,
    ) -> None:
        """Mock listing the repositories an installation can access.

        The responses are only served to a client authenticated with the
        installation's token, so each installation can list different
        repositories. Mock the token with `mock_installation_token`.

        The listing is split into pages, each requested once. As GitHub does,
        the first page is at the bare URL, a later page is at the ``page``
        query parameter, and each page but the last links to the next one
        with a ``Link`` header.

        Parameters
        ----------
        repositories
            Repository resources, such as from `make_repository`.
        installation_id
            ID of the installation.
        per_page
            Number of repositories on each page, or `None` to list them all
            on one page.
        """
        token = get_installation_token(installation_id)
        url = f"{GITHUB_API_URL}/installation/repositories"
        size = per_page or max(len(repositories), 1)
        pages = [
            repositories[i : i + size]
            for i in range(0, max(len(repositories), 1), size)
        ]
        for number, page in enumerate(pages, start=1):
            headers = {}
            if number < len(pages):
                headers["Link"] = f'<{url}?page={number + 1}>; rel="next"'
            self._httpx_mock.add_response(
                method="GET",
                url=url,
                match_params={"page": str(number)} if number > 1 else None,
                match_headers={"authorization": f"token {token}"},
                headers=headers,
                json={
                    "total_count": len(repositories),
                    "repositories": page,
                },
            )

    def mock_branch_head(
        self,
        sha: str,
        *,
        branch: str = "main",
        repo: str = GITHUB_REPO,
        held: HeldResponse | None = None,
    ) -> None:
        """Mock one request for the commit that a branch points to.

        Mock the branch again to serve a different commit to a later request.

        Parameters
        ----------
        sha
            Commit SHA that the branch points to.
        branch
            Name of the branch.
        repo
            Full name (``owner/repo``) of the repository.
        held
            If set, the response is held until the test releases it, so that
            the test can act while Semaphore waits for GitHub.
        """
        url = f"{GITHUB_API_URL}/repos/{repo}/git/ref/heads/{branch}"
        body = {
            "ref": f"refs/heads/{branch}",
            "url": url,
            "object": {
                "type": "commit",
                "sha": sha,
                "url": f"{GITHUB_API_URL}/repos/{repo}/git/commits/{sha}",
            },
        }
        if held is None:
            self._httpx_mock.add_response(method="GET", url=url, json=body)
            return

        async def respond(request: httpx.Request) -> httpx.Response:
            await held.hold()
            return httpx.Response(200, json=body)

        self._httpx_mock.add_callback(respond, method="GET", url=url)

    def mock_branch_head_error(
        self,
        *,
        status_code: int,
        branch: str = "main",
        repo: str = GITHUB_REPO,
    ) -> None:
        """Mock one request for the commit that a branch points to that
        GitHub fails.

        Parameters
        ----------
        status_code
            HTTP status code of GitHub's error response.
        branch
            Name of the branch.
        repo
            Full name (``owner/repo``) of the repository.
        """
        self._httpx_mock.add_response(
            method="GET",
            url=f"{GITHUB_API_URL}/repos/{repo}/git/ref/heads/{branch}",
            status_code=status_code,
            json={"message": "GitHub error"},
        )

    def mock_directory(
        self,
        path: str,
        names: Iterable[str],
        *,
        repo: str = GITHUB_REPO,
        ref: str | None = None,
        optional: bool = False,
    ) -> None:
        """Mock one request for the listing of a directory.

        Parameters
        ----------
        path
            Path of the directory in the repository.
        names
            Names of the files in the directory.
        repo
            Full name (``owner/repo``) of the repository.
        ref
            Git ref that the directory must be listed at, through the ``ref``
            query parameter, or `None` if the request must not set a ref
            (listing the default branch).
        optional
            Whether the test passes even if the directory is not requested.
        """
        self._httpx_mock.add_response(
            method="GET",
            url=f"{GITHUB_API_URL}/repos/{repo}/contents/{path}",
            match_params={"ref": ref} if ref is not None else None,
            json=[
                {"type": "file", "name": name, "path": f"{path}/{name}"}
                for name in names
            ],
            is_optional=optional,
        )

    def mock_file(
        self,
        path: str,
        text: str,
        *,
        repo: str = GITHUB_REPO,
        ref: str | None = None,
        optional: bool = False,
    ) -> None:
        """Mock one request for the raw contents of a file.

        Mock the same file again to serve different contents to a later
        request.

        Parameters
        ----------
        path
            Path of the file in the repository.
        text
            Contents of the file.
        repo
            Full name (``owner/repo``) of the repository.
        ref
            Git ref that the file must be requested at, through the ``ref``
            query parameter, or `None` if the request must not set a ref
            (reading the default branch).
        optional
            Whether the test passes even if the file is not requested.
        """
        self._httpx_mock.add_response(
            method="GET",
            url=f"{GITHUB_API_URL}/repos/{repo}/contents/{path}",
            match_params={"ref": ref} if ref is not None else None,
            text=text,
            is_optional=optional,
        )

    def mock_contents_error(
        self,
        path: str,
        *,
        status_code: int,
        repo: str = GITHUB_REPO,
        ref: str | None = None,
        times: int = 1,
    ) -> None:
        """Mock requests for a file or directory that GitHub fails.

        A status code of 404 is how GitHub reports a file that does not exist
        at the requested ref.

        Each of the next ``times`` requests for the file or directory fails,
        and each must be made. Mock the file or directory afterwards with
        `mock_file` or `mock_directory` to serve it to the request after
        those, as when GitHub recovers.

        Parameters
        ----------
        path
            Path of the file or directory in the repository.
        status_code
            HTTP status code of GitHub's error response.
        repo
            Full name (``owner/repo``) of the repository.
        ref
            Git ref that the file or directory must be requested at, through
            the ``ref`` query parameter, or `None` if the request must not set
            a ref (reading the default branch).
        times
            Number of requests that fail.
        """
        for _ in range(times):
            self._httpx_mock.add_response(
                method="GET",
                url=f"{GITHUB_API_URL}/repos/{repo}/contents/{path}",
                match_params={"ref": ref} if ref is not None else None,
                status_code=status_code,
                json={"message": "GitHub error"},
            )

    def mock_contents_timeout(
        self,
        path: str,
        *,
        repo: str = GITHUB_REPO,
        times: int = 1,
    ) -> None:
        """Mock requests for a file or directory that time out.

        Each of the next ``times`` requests for the file or directory, on the
        default branch, raises `httpx.ReadTimeout`, and each must be made.
        Mock the file or directory afterwards to serve it to the request after
        those.

        Parameters
        ----------
        path
            Path of the file or directory in the repository.
        repo
            Full name (``owner/repo``) of the repository.
        times
            Number of requests that time out.
        """
        for _ in range(times):
            self._httpx_mock.add_exception(
                httpx.ReadTimeout("Timed out reading from GitHub"),
                method="GET",
                url=f"{GITHUB_API_URL}/repos/{repo}/contents/{path}",
            )

    def get_token_requests(
        self, installation_id: int = GITHUB_INSTALLATION_ID
    ) -> list[httpx.Request]:
        """Return the requests to create access tokens for an installation.

        Each one is made for a new client authorized as the installation.
        """
        url = (
            f"{GITHUB_API_URL}/app/installations/{installation_id}"
            "/access_tokens"
        )
        return [r for r in self.get_requests() if str(r.url) == url]

    def get_requests(self) -> list[httpx.Request]:
        """Return the requests made to the GitHub API."""
        return self._httpx_mock.get_requests()
