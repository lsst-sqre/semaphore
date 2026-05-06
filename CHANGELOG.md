# Change log

Semaphore is versioned with [semver](https://semver.org/).
Dependencies are updated to the latest available version during each release, and aren't noted here.

Find changes for the upcoming release in the project's [changelog.d directory](https://github.com/lsst-sqre/semaphore/tree/main/changelog.d/).

<!-- scriv-insert-here -->

<a id='changelog-1.0.0'></a>
## 1.0.0 (2026-05-06)

### Backwards-incompatible changes

- Rename `config.profile` to `config.logProfile` in the Helm configuration to make it clearer what this field controls.

### New features

- Add support for configuring a Slack webhook for alerting, and send uncaught exceptions to that webhook if configured.
- Optionally report errors to Sentry.

### Other changes

- The Semaphore change log is now maintained using [scriv](https://scriv.readthedocs.io/en/latest/).
- Semaphore now uses [uv](https://github.com/astral-sh/uv) to maintain frozen dependencies and set up a development environment.

<a id='changelog-0.5.0'></a>
## 0.5.0 (2024-06-26)

### Backwards-incompatible changes

- Reorganized the broadcast message categories to be: `info`, `notice` (new), and `outage` (new). The old `maintenance` category has been removed and is now equivalent to `notice`.

### Other changes

- Added documentation for Semaphore's configuration environment variables and how to configure Semaphore's GitHub App integration.
- Added a Redoc subsite for the API documentation.

## 0.4.0 (2023-04-14)

### New features

- Summaries will now be interpreted using the first provided body paragraph unless defined in the `summary` property in the post metadata.

## 0.3.0 (2022-04-14)

### New features

- Broadcast messages can now have a "category," which you can set in the YAML front-matter. The default behavior is `category: maintenance`, which matches the idiomatic use of broadcasts up to this point. However, you can also set `category: info` to publish informational messages, like general announcements. The category is present in the JSON data model for broadcasts published from the API. Note that "category" is an enumeration: only `info` or `maintenance` are allowed values.
- Semaphore is now cross-published to the GitHub Container Registry, `ghcr.io/lsst-sqre/semaphore`.

## 0.2.1 (2021-08-12)

### Bug fixes

- Prevent the ``/v1/broadcasts`` endpoint from showing disabled messages.

## 0.2.0 (2021-08-06)

This is the initial working version of Semaphore, featuring the ability to source broadcast messages from GitHub, and get updates for those messages through GitHub webhooks.
