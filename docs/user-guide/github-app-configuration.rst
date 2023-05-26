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

    **Do not create a public GitHub App.**
    If the GitHub App is public, it can be installed in any GitHub organization or user.
    Semaphore does not yet support an "accept" list to ensure that only authorized organizations or users can install a public GitHub App.

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

The webhook should be enabled. Set the webhook URL to the ``/semaphore/github/webhook`` endpoint in the RSP/Phalanx environment.
For example ``https://data.lsst.cloud/semaphore/github/webhook``.

Create a webhook secret and store it in the :envvar:`SEMAPHORE_GITHUB_WEBHOOK_SECRET` environment variable (though Vault/1Password).

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

.. _github-app-secrets:

Create the app and secrets
==========================

Once the GitHub App is configured, you can click the "Create GitHub App" button to create it in your GitHub organization or user account.

When you do this, you can create the secret keys that Semaphore needs to authenticate with GitHub and verify webhook events.
These are provided to Semaphore as environment variables:

- :envvar:`SEMAPHORE_GITHUB_APP_ID`: The GitHub App ID. This is shown on the GitHub App's "General" page under the "About" heading.
- :envvar:`SEMAPHORE_GITHUB_APP_PRIVATE_KEY`: The GitHub App's private key. This is shown on the GitHub App's "General" page under "Client secrets".
- :envvar:`SEMAPHORE_GITHUB_WEBHOOK_SECRET`: The webhook secret you created in the GitHub App's "General" page under "Webhooks".

See :doc:`environment-variables` for more information on Phalanx's environment variable settings.

Install the app in the source repository
========================================

Once the app is created and the Semaphore app is configured, you need to *install* the app in the source repository (or repositories, if there are several).
From the app's GitHub settings page, click "Install App" and select the repositories to install it in.
Avoid installing the app in repositories that do not use Semaphore.

.. _lsst-sqre/rsp_broadcast: https://github.com/lsst-sqre/rsp_broadcast
