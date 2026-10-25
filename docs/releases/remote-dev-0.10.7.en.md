# remote-dev 0.10.7

Retains model-visible head/tail summaries for large logs: native and canonical ordinary command/task output shows a bounded UTF-8 head/tail with an explicit omission note, readable full remote raw-log refs, and truthful local-cache status. Background/incremental pages stay full within their budget and cursors never skip unreturned bytes.

A one-shot terminal command reads only bounded real remote edges; preview reads share a small deadline. A preview failure does not turn a completed command into an MCP transport error or replay it: the real exit is preserved and `preview_degraded` is explicit. For very large redirected logs use `remote.read` on the raw remote log.

Verified for canonical under canonical/Codex/Claude/Kimi profiles and four native foreground/background paths; CI covers Ubuntu 3.11/3.12, macOS, and Windows. Formal tags/releases are produced separately.
