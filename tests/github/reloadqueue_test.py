"""Tests for the queue of GitHub repositories whose broadcasts must be
reloaded.
"""

import asyncio

import pytest

from semaphore.github.reloadqueue import (
    QueuedInstallation,
    QueuedInstallationListing,
    QueuedRepo,
    ReloadQueue,
)


def test_queues_repo() -> None:
    queue = ReloadQueue()

    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")

    assert queue.pending() == [
        QueuedRepo(installation_id="1", full_name="lsst-sqre/a")
    ]


def test_queues_pending_repo_once() -> None:
    """Adding a repository that is already pending does nothing, whatever
    the case of its name.
    """
    queue = ReloadQueue()

    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")
    queue.add_repo(installation_id="1", repo_full_name="LSST-SQRE/A")
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")

    assert queue.pending() == [
        QueuedRepo(installation_id="1", full_name="lsst-sqre/a")
    ]


def test_queues_repo_of_each_installation() -> None:
    queue = ReloadQueue()

    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")
    queue.add_repo(installation_id="2", repo_full_name="lsst-sqre/a")

    assert len(queue) == 2


def test_queues_installation_apart_from_its_repos() -> None:
    queue = ReloadQueue()

    queue.add_installation(installation_id="1", account="lsst-sqre")
    queue.add_installation(installation_id="1", account="lsst-sqre")
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")

    assert queue.pending() == [
        QueuedInstallation(installation_id="1", account="lsst-sqre"),
        QueuedRepo(installation_id="1", full_name="lsst-sqre/a"),
    ]


def test_queues_installation_listing_once() -> None:
    """The listing of the installations is one entry, apart from the
    installations and repositories, and adding it while it is pending does
    nothing.
    """
    queue = ReloadQueue()

    queue.add_installation(installation_id="1", account="lsst-sqre")
    queue.add_installation_listing()
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")
    queue.add_installation_listing()

    assert queue.pending() == [
        QueuedInstallation(installation_id="1", account="lsst-sqre"),
        QueuedInstallationListing(),
        QueuedRepo(installation_id="1", full_name="lsst-sqre/a"),
    ]


def test_contains_pending_entry() -> None:
    queue = ReloadQueue()
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")

    assert QueuedRepo(installation_id="1", full_name="LSST-SQRE/A") in queue
    assert QueuedInstallationListing() not in queue


def test_discards_installation_listing() -> None:
    queue = ReloadQueue()
    queue.add_installation_listing()

    queue.discard(QueuedInstallationListing())

    assert len(queue) == 0


def test_discards_repo_whatever_its_case() -> None:
    queue = ReloadQueue()
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/b")

    queue.discard(QueuedRepo(installation_id="1", full_name="LSST-SQRE/A"))

    assert queue.pending() == [
        QueuedRepo(installation_id="1", full_name="lsst-sqre/b")
    ]


def test_discards_repo_that_is_not_pending() -> None:
    queue = ReloadQueue()

    queue.discard(QueuedRepo(installation_id="1", full_name="lsst-sqre/a"))

    assert len(queue) == 0


@pytest.mark.asyncio
async def test_wait_for_pending_waits_for_an_entry() -> None:
    queue = ReloadQueue()
    waiter = asyncio.create_task(queue.wait_for_pending())
    await asyncio.sleep(0)
    waited = not waiter.done()

    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")

    async with asyncio.timeout(5):
        await waiter
    assert waited


@pytest.mark.asyncio
async def test_wait_until_empty_waits_for_last_discard() -> None:
    queue = ReloadQueue()
    queue.add_repo(installation_id="1", repo_full_name="lsst-sqre/a")
    queue.add_installation(installation_id="2", account="lsst-sqre")
    waiter = asyncio.create_task(queue.wait_until_empty())
    queue.discard(QueuedRepo(installation_id="1", full_name="lsst-sqre/a"))
    await asyncio.sleep(0)
    waited = not waiter.done()

    queue.discard(QueuedInstallation(installation_id="2", account="lsst-sqre"))

    async with asyncio.timeout(5):
        await waiter
    assert waited
