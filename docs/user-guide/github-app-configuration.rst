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

- A malformed broadcast file is skipped with a warning, and the other broadcasts are still loaded.
  Fix the file and push the fix to publish it.
- A repository with no ``broadcasts`` directory has no broadcasts.
  Git doesn't store empty directories, so a repository whose broadcasts have all been deleted looks like this.
- If GitHub fails or can't be reached while Semaphore loads a repository (for example, with a server error, a timeout, an exceeded rate limit, or an authentication error), Semaphore logs a warning and still starts.
  It then retries loading that repository in the background, waiting longer after each failed attempt (up to five minutes), until it succeeds.
  The repository's broadcasts are served as soon as it loads, without restarting Semaphore.
  Each Semaphore replica loads and retries on its own, so replicas may briefly serve different broadcasts while GitHub recovers.

After loading, Semaphore logs a ``Loaded broadcasts from GitHub`` summary.
Its ``failed_repo_count`` field is the number of repositories that are being retried, and ``skipped_file_count`` is the number of skipped files.
Each retry logs ``Retrying loading broadcasts from a repository``, and a repository that loads after retrying logs ``Loaded broadcasts from a repository after retrying``.

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

Install the app in the source repository
========================================

Once the app is created and the Semaphore app is configured, you need to *install* the app in the source repository (or repositories, if there are several).
From the app's GitHub settings page, click :guilabel:`Install App`, choose the organization or user account, and select :guilabel:`Only select repositories`.
Then pick the source repositories.

Install the app on selected repositories only.
Don't choose :guilabel:`All repositories`: Semaphore would skip the extra repositories anyway, but the app's credentials would still be able to read them, and each new repository in the account would be added automatically.

Every repository you install the app on should also be listed in :envvar:`SEMAPHORE_GITHUB_REPOS` (see :ref:`github-app-allowlist`).
A repository that the app is installed on but that isn't in the list is skipped with a warning.

Protect the source repository's default branch
==============================================

Semaphore publishes a broadcast as soon as the change reaches the source repository's default branch.
Merging a pull request into the default branch is therefore the same as publishing its broadcasts to the RSP environments they target.

Protect the default branch with a branch protection rule or ruleset that requires an approving pull request review before merging, and that blocks direct pushes and force pushes.
That way, at least one other person reviews each broadcast before it's published.
