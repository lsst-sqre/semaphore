"""Tests for identifying broadcast messages sourced from GitHub."""

import pytest

from semaphore.broadcast.repository import BroadcastMessageRepository
from semaphore.github.broadcastservices import (
    GitHubMessageRef,
    get_repo_message_refs,
)

from ..support.broadcasts import create_active_message

MESSAGE_REF = GitHubMessageRef(
    path="broadcasts/hello.md",
    repo_name="rsp_broadcast",
    repo_owner="lsst-sqre",
    ref="refs/heads/tickets/DM-1",
)
"""A message reference whose ref contains slashes."""


def test_from_id_parses_as_id() -> None:
    assert GitHubMessageRef.from_id(MESSAGE_REF.as_id()) == MESSAGE_REF


@pytest.mark.parametrize(
    "identifier",
    [
        "1",
        "gitlab.com/lsst-sqre/rsp_broadcast/broadcasts/hello.md/?ref=main",
        "github.com/lsst-sqre/rsp_broadcast",
        "github.com/lsst-sqre/rsp_broadcast/broadcasts/hello.md",
        "github.com/lsst-sqre/rsp_broadcast/broadcasts/hello.md/?ref=",
    ],
)
def test_from_id_rejects_other_ids(identifier: str) -> None:
    assert GitHubMessageRef.from_id(identifier) is None


def test_get_repo_message_refs() -> None:
    """Only messages from the repository are found, whatever their ref and
    the case of the repository's name.
    """
    main_ref = GitHubMessageRef(
        path="broadcasts/a.md",
        repo_name="RSP_Broadcast",
        repo_owner="LSST-SQRE",
        ref="refs/heads/main",
    )
    trunk_ref = GitHubMessageRef(
        path="broadcasts/b.md",
        repo_name="rsp_broadcast",
        repo_owner="lsst-sqre",
        ref="refs/heads/trunk",
    )
    other_repo_ref = GitHubMessageRef(
        path="broadcasts/a.md",
        repo_name="rsp_broadcast_extra",
        repo_owner="lsst-sqre",
        ref="refs/heads/main",
    )
    broadcast_repo = BroadcastMessageRepository(
        [
            create_active_message(main_ref.as_id()),
            create_active_message(trunk_ref.as_id()),
            create_active_message(other_repo_ref.as_id()),
            create_active_message("1"),
        ]
    )

    message_refs = get_repo_message_refs(
        broadcast_repo=broadcast_repo, repo_full_name="lsst-sqre/rsp_broadcast"
    )

    assert message_refs == [main_ref, trunk_ref]
