# Semaphore

Semaphore is the user messaging service for the Rubin Science Platform.
At the moment, it implements a broadcast service where Markdown-formatted messages in a GitHub repository can be displayed on the Rubin Science Platform (such in the homepage, [Squareone](https://github.com/lsst-sqre/squareone)).

The broadcast repository used by the Rubin Science Platform is [lsst-sqre/rsp_broadcast](https://github.com/lsst-sqre/rsp_broadcast).

## Documentation

The primary documentation for Semaphore operators and developers is https://semaphore.lsst.io.

Semaphore's configuration in Phalanx is documented at https://phalanx.lsst.io/applications/semaphore/index.html

The design of Semaphore's broadcast system is written up in [SQR-060: Design of the Semaphore user broadcast message system for the Rubin Science Platform](https://sqr-060.lsst.io/).

## Future plans

Semaphore is intended to be a general-purpose user messaging service for the Rubin Science Platform.
Besides the broadcast service, we plan to implement other communication APIs:

- Direct messages, where Rubin Science Platform operators and services can send messages to users.
- A blogging service, where Rubin Science Platform operators and observatory staff can post news and updates into a standard JSON/RSS feed.
