# remote-dev 0.11.13: Cross-Session Job Handles, Concurrent Resident Pool, `64 KiB` Streaming, and Symlink/Windows Path Hardening

[中文](remote-dev-0.11.13.md)

`remote-dev v0.11.13` completes control-plane clean-architecture hardening and pins `mindie-diagnostics v0.5.16`:

- **Cross-Session Handle Addressing & Zero Session-Binding Gate (`P2.7`)**: Background jobs are addressed directly by stable `job_id` across MCP reconnections and sessions without prior hooks or `_meta` bindings, with zero import-time environment mutation.
- **Concurrent Resident Dispatch, `64 KiB` Streaming I/O & Path Safety**: Serves read/status/tail probes concurrently without blocking behind long foreground commands, streams large file transfers in `64 KiB` chunks (`<= 512 MiB` RSS), rejects escaping symlinks, preserves POSIX `"/"` remote root semantics on Windows hosts, and propagates `TimeoutError` before `OSError`.
- **Dependency Pin & `control-plane` Governance**: Advances `mindie-diagnostics` to `v0.5.16` and aligns contributor docs and CI with `--profile control-plane`.
