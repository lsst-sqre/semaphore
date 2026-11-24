"""Tests for the semaphore.config module."""

import pytest
from pydantic import ValidationError

from semaphore.config import Config


@pytest.fixture
def github_app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the GitHub App credentials, but not the allowlist."""
    monkeypatch.setenv("SEMAPHORE_GITHUB_APP_ID", "1234")
    monkeypatch.setenv("SEMAPHORE_GITHUB_APP_PRIVATE_KEY", "some-key")
    monkeypatch.setenv("SEMAPHORE_GITHUB_WEBHOOK_SECRET", "some-secret")
    monkeypatch.delenv("SEMAPHORE_ENABLE_GITHUB_APP", raising=False)
    monkeypatch.delenv("SEMAPHORE_GITHUB_REPOS", raising=False)


@pytest.mark.usefixtures("github_app_env")
def test_github_repos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SEMAPHORE_GITHUB_REPOS", "lsst-sqre/rsp_broadcast,lsst-sqre/other"
    )

    config = Config()

    assert config.github_repos == [
        "lsst-sqre/rsp_broadcast",
        "lsst-sqre/other",
    ]
    assert config.enable_github_app is True
    assert config.github_app_disabled_reason is None


@pytest.mark.usefixtures("github_app_env")
def test_github_repos_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SEMAPHORE_GITHUB_REPOS", " lsst-sqre/rsp_broadcast , lsst-sqre/other,"
    )

    config = Config()

    assert config.github_repos == [
        "lsst-sqre/rsp_broadcast",
        "lsst-sqre/other",
    ]


@pytest.mark.usefixtures("github_app_env")
def test_github_repos_owner_with_underscore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enterprise Managed User logins, such as ``mona_acme``, contain an
    underscore.
    """
    monkeypatch.setenv("SEMAPHORE_GITHUB_REPOS", "mona_acme/broadcasts")

    config = Config()

    assert config.github_repos == ["mona_acme/broadcasts"]
    assert config.enable_github_app is True


@pytest.mark.usefixtures("github_app_env")
@pytest.mark.parametrize(
    "entry",
    [
        "lsst-sqre",
        "lsst-sqre/*",
        "*/rsp_broadcast",
        "mona_*/broadcasts",
        "a/b/c",
        "lsst-sqre/rsp_broadcast/extra",
        "https://github.com/lsst-sqre/rsp_broadcast",
        "/rsp_broadcast",
        "lsst-sqre/",
        "lsst sqre/rsp_broadcast",
        "lsst-sqre/rsp broadcast",
        "mona_acme\t/broadcasts",
    ],
)
def test_github_repos_invalid_entry(
    monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    monkeypatch.setenv(
        "SEMAPHORE_GITHUB_REPOS", f"lsst-sqre/rsp_broadcast,{entry}"
    )

    with pytest.raises(ValidationError) as excinfo:
        Config()

    message = str(excinfo.value)
    assert "SEMAPHORE_GITHUB_REPOS" in message
    assert repr(entry) in message
    assert "owner/repo" in message


@pytest.mark.usefixtures("github_app_env")
def test_github_app_disabled_without_repos() -> None:
    config = Config()

    assert config.github_repos == []
    assert config.enable_github_app is False
    assert config.github_app_disabled_reason is not None
    assert "SEMAPHORE_GITHUB_REPOS" in config.github_app_disabled_reason


@pytest.mark.usefixtures("github_app_env")
def test_github_app_disabled_with_empty_repos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEMAPHORE_GITHUB_REPOS", "")

    config = Config()

    assert config.github_repos == []
    assert config.enable_github_app is False
    assert config.github_app_disabled_reason is not None
    assert "SEMAPHORE_GITHUB_REPOS" in config.github_app_disabled_reason


@pytest.mark.usefixtures("github_app_env")
def test_github_app_disabled_by_setting() -> None:
    """Turning the app off on purpose is not reported as a problem."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SEMAPHORE_ENABLE_GITHUB_APP", "false")
        config = Config()

    assert config.enable_github_app is False
    assert config.github_app_disabled_reason is None


@pytest.mark.usefixtures("github_app_env")
def test_github_app_disabled_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leaving the app unconfigured is not reported as a problem."""
    monkeypatch.delenv("SEMAPHORE_GITHUB_APP_ID")

    config = Config()

    assert config.enable_github_app is False
    assert config.github_app_disabled_reason is None
