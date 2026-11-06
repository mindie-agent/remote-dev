# remote-dev 0.11.6

[中文](remote-dev-0.11.6.md)

This patch fixes first-time SSH access when a user has supplied a password but the public MCP interface cannot accept it.

- `remote_auth(action="setup")` accepts an ephemeral `password` and reuses shared key generation, idempotent public-key installation, and fresh-process key verification. Passwords are excluded from configuration, logs, receipts, and persistent environment variables.
- Successful, verified SSH operations retain their primary result when local profile or receipt recording fails. Responses include `receipt_warning` / `receipt_unconfirmed` and verified direct-key connection information.
- MCP outcomes consistently distinguish success, failure, and uncertain external writes. Uncertain writes require reconciliation before another attempt.
- Real SSH acceptance on PowerShell, Windows PowerShell 5, and WSL covers the public MCP password interface, wrong passwords, idempotence, credential persistence, and fresh-process access after local recording failures. It does not claim validation of private deployment hosts or model inference.

CI verifies source contracts and platform behavior; the release workflow produces benchmarks from the exact clean release tag.
