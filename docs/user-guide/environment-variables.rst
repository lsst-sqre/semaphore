#####################
Environment variables
#####################

Semaphore uses environment variables for configuration.
In practice, these variables are typically set as Helm values and 1Password/Vault secrets that are injected into the container as environment variables.
See the `Phalanx documentation for Semaphore <https://phalanx.lsst.io/applications/semaphore/index.html>`__ for more information on the Phalanx-specific configurations.

.. envvar:: SAFIR_NAME

   (string, default: "semaphore") The name of the application.
   This is used in the metadata endpoint and the application's URL path prefix.

.. envvar:: SAFIR_PROFILE

   (string enum: "production" [default], "development") The application run profile.
   Use production to enable JSON structured logging.

.. envvar:: SAFIR_LOG_LEVEL

   (string enum: "debug", "info" [default], "warning", "error", "critical") The application log level.

.. envvar:: SEMAPHORE_ENABLE_GITHUB_APP

   (boolean, default: "true") Whether to enable the Semaphore GitHub App.
   If true, the app will be enabled and the ``SEMAPHORE_GITHUB_*`` variables must be set.
   See :doc:`github-app-configuration` for more information.

.. envvar:: SEMAPHORE_GITHUB_APP_ID

   (**secret** string) The GitHub App ID for the Semaphore GitHub App.
   Can be an empty string if the app is not configured.
   See :ref:`github-app-secrets` for more information.

.. envvar:: SEMAPHORE_GITHUB_APP_PRIVATE_KEY

   (**secret** string) The GitHub App private key for the Semaphore GitHub App.
   Can be an empty string if the app is not configured.
   See :ref:`github-app-secrets` for more information.

.. envvar:: SEMAPHORE_GITHUB_REPOS

   (string) Comma-separated list of the GitHub repositories that may publish broadcast messages, each written as ``owner/repo`` (for example, ``lsst-sqre/rsp_broadcast``).
   Names are matched case-insensitively and wildcards are not supported.
   A repository that is a fork is never accepted, even if it is listed.
   Pushes from any other repository are acknowledged, ignored, and logged as a warning.

   This variable is required to use the GitHub App.
   If the GitHub App credentials are set but this variable is unset or empty, the GitHub App is disabled and a warning is logged at startup.
   An entry that isn't of the form ``owner/repo`` is a configuration error.
   See :ref:`github-app-allowlist` for more information.

.. envvar:: SEMAPHORE_GITHUB_WEBHOOK_SECRET

   (**secret** string) The GitHub webhook secret for the Semaphore GitHub App.
   Can be an empty string if the app is not configured.
   See :ref:`github-app-secrets` for more information.

.. envvar:: SEMAPHORE_PHALANX_ENV

   (string) The name of Phalanx environment this Semaphore installation is running in (e.g. ``idfprod``).
   This configuration aids in determining which broadcast messages from a shared GitHub repository to index, based on the ``env`` YAML/markdown front-matter keyword.
   For a list of environments, see https://phalanx.lsst.io/environments/index.html.
