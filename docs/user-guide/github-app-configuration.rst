##########################
Configuring the GitHub App
##########################

Semaphore uses GitHub to source certain content, such as broadcasts.
To authenticate with GitHub, Semaphore uses a GitHub App.
Each installation of Semaphore has its own GitHub App in order to receive webhook events.
Semaphore installations in different RSP/Phalanx environments can share the same source repository, however.

To learn more about installing GitHub Apps in general, see the `GitHub Apps documentation <https://docs.github.com/en/apps/creating-github-apps/setting-up-a-github-app/creating-a-github-app>`__.

.. important::

    The GitHub App must be created in the GitHub organization or user that owns the source repository.
    For example, if the source repository is `lsst-sqre/rsp_broadcast`_, the GitHub App must be created in the ``lsst-sqre`` organization.

.. _github-app-allowlist:

Which repositories Semaphore accepts
====================================

Semaphore only reads broadcast messages from the GitHub repositories listed in the :envvar:`SEMAPHORE_GITHUB_REPOS` environment variable.
Set it to the source repositories for the environment, as a comma-separated list of ``owner/repo`` names (for example, ``lsst-sqre/rsp_broadcast``).

- A repository is accepted only if its name is in :envvar:`SEMAPHORE_GITHUB_REPOS`.
  Names are matched case-insensitively and wildcards are not supported.
- A fork is never used as a source, even if its name is in the list.
- Only pushes to a repository's default branch publish broadcasts.
  Pushes to other branches are ignored.

Pushes from any other repository are acknowledged, ignored, and logged as a warning.
When Semaphore starts, it likewise skips, with a warning, any repository that the GitHub App can see but that isn't accepted.

:envvar:`SEMAPHORE_GITHUB_REPOS` is required to use the GitHub App.
If the GitHub App credentials are set but :envvar:`SEMAPHORE_GITHUB_REPOS` is unset or empty, Semaphore disables the GitHub App and logs a warning at startup.
While the GitHub App is disabled, the webhook endpoint returns ``501 Not Implemented`` and Semaphore loads no broadcasts from GitHub.

The allowlist is Semaphore's own check on where broadcasts come from.
The GitHub settings recommended on this page (a private app, installed on selected repositories, with a protected default branch) add further layers of defense around it.

.. _github-app-startup:

Loading broadcasts at startup
=============================

When Semaphore starts, it loads the broadcasts in the ``broadcasts`` directory of each accepted repository's default branch.
Later changes arrive through push webhooks.

- An installation of the GitHub App that is suspended is skipped with a warning.
  So is an installation on an account that owns none of the repositories in :envvar:`SEMAPHORE_GITHUB_REPOS`.
  Semaphore doesn't request an access token for a skipped installation.
- A malformed broadcast file is skipped with a warning, and the other broadcasts are still loaded.
  Fix the file and push the fix to publish it.
- A repository with no ``broadcasts`` directory has no broadcasts.
  Git doesn't store empty directories, so a repository whose broadcasts have all been deleted looks like this.
- If GitHub fails or can't be reached while Semaphore loads a repository, or while it authorizes an installation of the GitHub App or lists the installation's repositories (for example, with a server error, a timeout, an exceeded rate limit, or an authentication error), Semaphore logs a warning and still starts.
  The other installations and repositories are still loaded.
  Semaphore then loads that repository, or that installation's repositories, in the background (see :ref:`github-app-reloads`).
  The broadcasts are served as soon as they load, without restarting Semaphore.
- Likewise, if GitHub fails or can't be reached while Semaphore lists the installations of the GitHub App, Semaphore logs a ``Could not list the GitHub App's installations, will retry`` warning and still starts.
  GitHub lists the installations a page at a time, and Semaphore keeps the broadcasts of the installations on the pages that GitHub returned before it failed.
  If the first page failed, Semaphore starts with no broadcasts from GitHub.
  Semaphore then lists the installations again in the background and loads each one, so the broadcasts of every installation are served once GitHub recovers.

After loading, Semaphore logs a ``Loaded broadcasts from GitHub`` summary.
Its ``failed_installation_count`` and ``failed_repo_count`` fields are the numbers of installations and repositories that are queued to be loaded in the background, and ``skipped_file_count`` is the number of skipped files.
The summary doesn't count the installations that Semaphore couldn't list.

.. _github-app-unseen-repos:

Troubleshooting a repository that isn't loaded
----------------------------------------------

If no installation of the GitHub App can see a repository in :envvar:`SEMAPHORE_GITHUB_REPOS`, Semaphore logs a ``No GitHub App installation can see these allowed repositories`` warning at startup.
Its ``github_repos`` field lists those repositories.
For each one, check that:

- The name in :envvar:`SEMAPHORE_GITHUB_REPOS` is spelled correctly.
- The GitHub App is installed on the repository (see :ref:`github-app-install`).
- The installation isn't suspended. Semaphore logs a ``Skipping a suspended GitHub App installation`` warning for a suspended installation.

The warning leaves out the repositories of an account whose installation is being retried, because Semaphore doesn't yet know which repositories that installation can see.
If GitHub failed to list the installations of the GitHub App at startup, Semaphore doesn't log the warning at startup.
It logs the warning, if any repository is still unseen, after it has listed the installations in the background and tried to load each of them.

.. _github-app-reloads:

Reloading broadcasts after GitHub fails
=======================================

GitHub doesn't redeliver a webhook on its own, so Semaphore doesn't drop a push that it can't process because of GitHub.
If GitHub fails or can't be reached while Semaphore processes a push, or while it loads a repository at startup (for example, with a server error, a timeout, an exceeded rate limit, or an authentication error), Semaphore logs a warning and queues the repository to be reloaded in the background.
It still acknowledges the push to GitHub.

- Until the repository is reloaded, Semaphore keeps serving each broadcast whose file it couldn't read from GitHub as it was before.
  This includes a broadcast loaded from the repository's former default branch, on the first push after the default branch is changed.
- Semaphore reloads a queued repository in full from the head of its current default branch.
  It loads each broadcast file in the ``broadcasts`` directory and stops serving the repository's other broadcasts, including any whose file was deleted and any loaded from a former default branch.
  So a push's changes are published once GitHub recovers, without another push or a restart.
- If GitHub fails again, Semaphore retries, waiting longer after each failed attempt (from 10 seconds up to five minutes), until the repository reloads.
  A repository that fails several times while it's queued is reloaded once per attempt.
  Once nothing is queued, the next failure starts again with a 10 second wait.
- If GitHub no longer finds a queued repository, or the repository is no longer accepted (see :ref:`github-app-allowlist`), Semaphore drops it from the queue and logs a ``Dropping a repository that GitHub cannot find from the reload queue`` or ``Dropping a repository that may not publish broadcasts from the reload queue`` warning.
- An installation of the GitHub App that GitHub failed to authorize, or whose repositories GitHub failed to list, at startup is retried the same way.
  Once GitHub lists the installation's repositories, Semaphore reloads each accepted repository, and queues any that fails.
- If GitHub failed to list the installations of the GitHub App at startup, Semaphore retries the listing the same way, with a new app token for each attempt, until GitHub returns every page.
  It then skips the installations that it skips at startup (suspended installations, and installations on an account that owns none of the repositories in :envvar:`SEMAPHORE_GITHUB_REPOS`) and loads each other installation as above, right away.
  A repository that already loaded at startup is reloaded in full, so it serves one copy of each broadcast.

Each Semaphore replica keeps its own queue and reloads on its own, so replicas may briefly serve different broadcasts while GitHub recovers.
A replica's queue is lost when it restarts, which is harmless because the replica loads every repository again at startup.

Each attempt to reload a repository logs ``Retrying loading broadcasts from a repository``, and a repository that reloads logs ``Loaded broadcasts from a repository after retrying``.
Likewise, each attempt to load an installation's repositories logs ``Retrying loading broadcasts from a GitHub App installation``, and an installation whose repositories GitHub lists logs ``Loaded broadcasts from a GitHub App installation after retrying``.
Each attempt to list the installations logs ``Retrying listing the GitHub App's installations``, and a listing that succeeds logs ``Listed the GitHub App's installations after retrying``, whose ``installation_count`` field is the number of installations that Semaphore then loads.

Create an app with a template URL
=================================

You can create the GitHub App by customizing and visiting the following URL (replace ``lsst-sqre`` with the GitHub organization or user that owns the source repository):

.. literalinclude:: _github-app-url-org.txt

Alternatively, the app can be installed in a personal account (not recommended for production use):

.. literalinclude:: _github-app-url-personal.txt

Once you follow the link, you will be able to make further customizations to the GitHub App before creating it.
These settings are described in the following sections.

GitHub App settings
===================

Name
----

The name of the GitHub App should be "Semaphore (env)".
For example, ``Semaphore (data.lsst.cloud)``.

This naming convention distinguishes the Semaphore installations for each Phalanx environment.

Description
-----------

Use the description provided with the GitHub App template URL, and modify it as needed.

Homepage URL
------------

Set this to the documentation URL, https://semaphore.lsst.io.

Identifying and authorizing users
---------------------------------

Not applicable.

Post installation
-----------------

Not applicable.

Webhook
-------

The webhook should be enabled.
Set the webhook URL to the ``/semaphore/github/webhook`` endpoint in the RSP/Phalanx environment.
For example, ``https://data.lsst.cloud/semaphore/github/webhook``.

Create a webhook secret and store it in the :envvar:`SEMAPHORE_GITHUB_WEBHOOK_SECRET` environment variable (through Vault/1Password).

Permissions
-----------

The GitHub App needs the following repository permissions:

- **Checks**: Read & write
- **Contents**: Read-only
- **Metadata**: Read-only

Events
------

The GitHub App needs to subscribe to the following events:

- Check Run
- Push

Where can this GitHub App be installed?
---------------------------------------

Select :guilabel:`Only on this account`.
The template URL selects this option for you.

.. warning::

    **Do not make the GitHub App public.**
    A public GitHub App (:guilabel:`Any account`) can be installed in any GitHub organization or user account, which could then send events to Semaphore.
    Semaphore ignores broadcasts from repositories that aren't in :envvar:`SEMAPHORE_GITHUB_REPOS`, so the allowlist already rejects them.
    Keeping the app private is defense in depth: it stops other accounts from installing the app at all, so a mistake in the allowlist can't be exploited by an outside account.

.. _github-app-secrets:

Create the app and secrets
==========================

Once the GitHub App is configured, you can click the :guilabel:`Create GitHub App` button to create it in your GitHub organization or user account.

When you do this, you can create the secret keys that Semaphore needs to authenticate with GitHub and verify webhook events.
These are provided to Semaphore as environment variables:

- :envvar:`SEMAPHORE_GITHUB_APP_ID`: The GitHub App ID. This is shown on the GitHub App's :guilabel:`General` page under the :guilabel:`About` heading.
- :envvar:`SEMAPHORE_GITHUB_APP_PRIVATE_KEY`: The GitHub App's private key. This is shown on the GitHub App's :guilabel:`General` page under :guilabel:`Client secrets`.
- :envvar:`SEMAPHORE_GITHUB_WEBHOOK_SECRET`: The webhook secret you created in the GitHub App's :guilabel:`General` page under :guilabel:`Webhooks`.

See :doc:`environment-variables` for more information on Phalanx's environment variable settings.

.. _github-app-install:

Install the app in the source repository
========================================

Once the app is created and the Semaphore app is configured, you need to *install* the app in the source repository (or repositories, if there are several).
From the app's GitHub settings page, click :guilabel:`Install App`, choose the organization or user account, and select :guilabel:`Only select repositories`.
Then pick the source repositories.

Install the app on selected repositories only.
Don't choose :guilabel:`All repositories`: Semaphore would skip the extra repositories anyway, but the app's credentials would still be able to read them, and each new repository in the account would be added automatically.

Every repository you install the app on should also be listed in :envvar:`SEMAPHORE_GITHUB_REPOS` (see :ref:`github-app-allowlist`).
A repository that the app is installed on but that isn't in the list is skipped with a warning.
Conversely, a repository in the list that the app isn't installed on is reported with a warning at startup (see :ref:`github-app-unseen-repos`).

Protect the source repository's default branch
==============================================

Semaphore publishes a broadcast as soon as the change reaches the source repository's default branch.
Merging a pull request into the default branch is therefore the same as publishing its broadcasts to the RSP environments they target.

Protect the default branch with a branch protection rule or ruleset that requires an approving pull request review before merging, and that blocks direct pushes and force pushes.
That way, at least one other person reviews each broadcast before it's published.
