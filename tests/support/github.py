"""Test utilities for Semaphore's GitHub App integration.

These helpers configure the GitHub App, sign and post webhook events to
Semaphore, and mock the GitHub REST API with pytest-httpx.
"""

import hashlib
import hmac
import json
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
    "GITHUB_APP_ID",
    "GITHUB_INSTALLATION_ID",
    "GITHUB_REPO",
    "GITHUB_WEBHOOK_SECRET",
    "GitHubMock",
    "configure_github_app",
    "make_message_id",
    "make_push_payload",
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


def make_message_id(
    path: str, *, repo: str = GITHUB_REPO, ref: str = "refs/heads/main"
) -> str:
    """Return the identifier of a broadcast message sourced from GitHub."""
    return f"github.com/{repo}/{path}/?ref={ref}"


def make_push_payload(
    *,
    repo: str = GITHUB_REPO,
    fork: bool = False,
    ref: str = "refs/heads/main",
    default_branch: str = "main",
    before: str = DEFAULT_BEFORE_SHA,
    after: str = DEFAULT_AFTER_SHA,
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
    owner, name = repo.split("/")
    contents_url = f"{GITHUB_API_URL}/repos/{repo}/contents/{{+path}}"
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
        "deleted": False,
        "forced": False,
        "repository": {
            "id": 1,
            "name": name,
            "full_name": repo,
            "owner": {"name": owner, "login": owner},
            "private": False,
            "fork": fork,
            "default_branch": default_branch,
            "master_branch": default_branch,
            "url": f"https://github.com/{repo}",
            "contents_url": contents_url,
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

    def mock_installation_token(
        self, installation_id: int = GITHUB_INSTALLATION_ID
    ) -> None:
        """Mock creating access tokens for an installation of the app.

        Any number of tokens may be created, but at least one must be.
        """
        self._httpx_mock.add_response(
            method="POST",
            url=(
                f"{GITHUB_API_URL}/app/installations/{installation_id}"
                "/access_tokens"
            ),
            status_code=201,
            json={
                "token": "ghs_test-installation-token",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            is_reusable=True,
        )

    def mock_file(
        self, path: str, text: str, *, repo: str = GITHUB_REPO
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
        """
        self._httpx_mock.add_response(
            method="GET",
            url=f"{GITHUB_API_URL}/repos/{repo}/contents/{path}",
            text=text,
        )

    def get_requests(self) -> list[httpx.Request]:
        """Return the requests made to the GitHub API."""
        return self._httpx_mock.get_requests()
