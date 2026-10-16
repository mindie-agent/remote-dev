# `remote-dev` Documentation

[中文](./README.md) · **English** · [Back to Project Homepage](../README.en.md)

Welcome to the `remote-dev` documentation hub. Here you will find guides on the internal architecture, multi-client MCP setup, container execution, SSH connection pooling, and diagnostics.

## Guides & References

| Topic | 中文 | English | What You'll Learn |
| --- | --- | --- | --- |
| Architecture & Layered Design | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | How the tool surface, shared SSH/container substrate, and upper-layer consumers fit together |
| Client Setup & Parameter Compatibility | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | How to configure Claude Code, Codex, Kimi Code, Cursor, and Grok Build, plus automatic parameter alias mapping |
| Containers, SSH Transport & Diagnostics | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | Working inside running Docker containers, writing custom endpoint resolvers, streaming SSH primitives, and connection diagnostics |
