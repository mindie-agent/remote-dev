# remote-dev 0.11.9

A local selected-binding failure now reports execution=not_started, retaining the
actual selection stage and leaving independent RPC work available. Only a helper
that was dispatched can have an unconfirmed tool outcome; no request is replayed.

Selected-helper deadlines reserve the existing five-second owned-process drain
inside the native MCP envelope. Standard catalog notifications and active-call
generation continuity introduced in 0.11.8 remain intact.
