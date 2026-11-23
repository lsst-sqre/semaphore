"""Tests for the semaphore.github.sourcepolicy module."""

from typing import Any

from semaphore.github.sourcepolicy import (
    BroadcastSourcePolicy,
    SourceRejectionReason,
)


def make_repository(full_name: str, *, fork: bool = False) -> dict[str, Any]:
    """Make the parts of a GitHub repository resource the policy reads."""
    owner, name = full_name.split("/")
    return {
        "name": name,
        "full_name": full_name,
        "owner": {"login": owner},
        "fork": fork,
    }


def test_allowed() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast"])

    decision = policy.evaluate(make_repository("lsst-sqre/rsp_broadcast"))

    assert decision.allowed
    assert decision.reason is None


def test_not_in_allowlist() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast"])

    decision = policy.evaluate(make_repository("lsst-sqre/other"))

    assert not decision.allowed
    assert decision.reason == SourceRejectionReason.not_in_allowlist


def test_fork() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast"])

    decision = policy.evaluate(
        make_repository("someone/rsp_broadcast", fork=True)
    )

    assert not decision.allowed
    assert decision.reason == SourceRejectionReason.fork


def test_allowlisted_fork() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast-fork"])

    decision = policy.evaluate(
        make_repository("lsst-sqre/rsp_broadcast-fork", fork=True)
    )

    assert not decision.allowed
    assert decision.reason == SourceRejectionReason.fork


def test_case_insensitive_match() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast"])

    decision = policy.evaluate(make_repository("LSST-SQRE/RSP_Broadcast"))

    assert decision.allowed


def test_case_insensitive_allowlist_entry() -> None:
    policy = BroadcastSourcePolicy(["LSST-SQRE/RSP_Broadcast"])

    decision = policy.evaluate(make_repository("lsst-sqre/rsp_broadcast"))

    assert decision.allowed


def test_empty_allowlist() -> None:
    policy = BroadcastSourcePolicy([])

    decision = policy.evaluate(make_repository("lsst-sqre/rsp_broadcast"))

    assert decision.reason == SourceRejectionReason.not_in_allowlist


def test_missing_full_name() -> None:
    policy = BroadcastSourcePolicy(["lsst-sqre/rsp_broadcast"])

    decision = policy.evaluate({"name": "rsp_broadcast", "fork": False})

    assert decision.reason == SourceRejectionReason.not_in_allowlist
