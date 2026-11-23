"""Tests for the per-repository locks of broadcast sources."""

from semaphore.github.repolocks import RepoLocks


def test_same_repo_shares_lock() -> None:
    locks = RepoLocks()

    assert locks.get("lsst-sqre/rsp_broadcast") is locks.get(
        "LSST-SQRE/RSP_Broadcast"
    )


def test_other_repo_has_own_lock() -> None:
    locks = RepoLocks()

    assert locks.get("lsst-sqre/rsp_broadcast") is not locks.get(
        "lsst-sqre/other"
    )
