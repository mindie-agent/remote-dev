# `remote-dev` Documentation

[中文](./README.md) · **English** · [Back to Project Homepage](../README.en.md)

This directory provides architecture notes, multi-client MCP compatibility contracts, and low-level transport and diagnostics references for `remote-dev`.

## Documents

| Document | 中文 | English | Summary |
| --- | --- | --- | --- |
| Architecture & Substrate Design | [design.zh.md](./design.zh.md) | [../DESIGN.md](../DESIGN.md) | Three-layer boundary (tool surface, shared substrate, consumer), dependency direction, container coordinates, and MCP resources |
| Client Compatibility & Aliases | [client-compatibility.zh.md](./client-compatibility.zh.md) | [../CLIENT_COMPATIBILITY.md](../CLIENT_COMPATIBILITY.md) | MCP setup for Claude Code, Codex, Kimi Code, Cursor, and Grok Build, plus shared parameter alias normalization |
| Transport, Containers & Diagnostics | [transport-and-diagnostics.md](./transport-and-diagnostics.md) | [transport-and-diagnostics.en.md](./transport-and-diagnostics.en.md) | Existing-container execution, resolver plugin contract, SSH RPC pooling and streaming primitives, process supervision, and structured diagnostics |
