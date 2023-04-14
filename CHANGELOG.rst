##########
Change log
##########

0.4.0 (2023-04-14)
==================

- Summaries will now be interpreted using the first provided body paragraph unless defined in the `summary` property in the post metadata

0.3.0 (2022-04-14)
==================

- Broadcast messages can now have a "category," which you can set in the YAML front-matter.
  The default behavior is ``category: maintenance``, which matches the idiomatic use of broadcasts up to this point.
  However, you can also set ``category: info`` to publish informational messages, like general announcements.
  The category is present in the JSON data model for broadcasts published from the API.
  Note that "category" is an enumeration: only ``info`` or ``maintenance`` are allowed values.
- Semaphore is now cross-published to the GitHub Container Registry, ``ghcr.io/lsst-sqre/semaphore``.
- Semaphore now runs on Python 3.10.

0.2.1 (2021-08-12)
==================

This release prevents the ``/v1/broadcasts`` endpoint from showing disabled messages.

0.2.0 (2021-08-06)
==================

This is the initial working version of Semaphore, featuring the ability to source broadcast messages from GitHub, and get updates for those messages through GitHub webhooks.
