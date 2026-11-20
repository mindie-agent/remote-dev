# `remote-dev` Documentation

[中文](./README.md) · **English** · [Back to Project Homepage](../README.en.md)

Welcome to the `remote-dev` documentation hub. Here you will find guides on the internal architecture, multi-client MCP setup, container execution, SSH connection pooling, diagnostics, and release notes.

## Guides & References

| Topic | 中文 | English | What You'll Learn |
| --- | --- | --- | --- |
| Architecture & Layered Design | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | How the tool surface, shared SSH/container substrate, and upper-layer consumers fit together |
| Client Setup & Parameter Compatibility | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | How to configure Claude Code, Codex, Kimi Code, Cursor, and Grok Build, plus automatic parameter alias mapping |
| Containers, SSH Transport & Diagnostics | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | Working inside running Docker containers, writing custom endpoint resolvers, streaming SSH primitives, and connection diagnostics |

## Release Notes

| Version | 中文 | English | Highlights |
| --- | --- | --- | --- |
| **`v0.11.15` (Current)** | [remote-dev-0.11.15.md](./releases/remote-dev-0.11.15.md) | [remote-dev-0.11.15.en.md](./releases/remote-dev-0.11.15.en.md) | Lock-free `ContextVar` `session_scope` per-request state/ledger/auth isolation, import-time purity in `remote_dev.mcp.server`, Windows `> 260`-character extended-path support, and stream cleanup hardening |
| `v0.11.14` | [remote-dev-0.11.14.md](./releases/remote-dev-0.11.14.md) | [remote-dev-0.11.14.en.md](./releases/remote-dev-0.11.14.en.md) | Strict 1:1 7-Harness native tool surfaces (adding `agy` and `oc` profiles) and frictionless single-step SSH onboarding |
| `v0.11.13` | [remote-dev-0.11.13.md](./releases/remote-dev-0.11.13.md) | [remote-dev-0.11.13.en.md](./releases/remote-dev-0.11.13.en.md) | Cross-session background job handles, concurrent resident worker pool, `64 KiB` streaming I/O, and symlink/timeout hardening |
| `v0.11.12` | [remote-dev-0.11.12.md](./releases/remote-dev-0.11.12.md) | [remote-dev-0.11.12.en.md](./releases/remote-dev-0.11.12.en.md) | Bounded remote NPU job ring logs & safe-ownership terminal GC, local endpoint state retention, and test temp-dir cleanup |
