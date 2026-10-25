# remote-dev 0.10.7

Pins mindie-diagnostics 0.5.4 (`5ae643e`): the recorder allocates the first/free log slot with `O_EXCL` before reader-registry/cursor coordination, fixing the intermittent CI failure that the old real-code setup hit at about 101 ms; full-window reader and pressure semantics are unchanged. The 100 ms recycle scan budget and 3 s SQLite limitation remain, and no asynchronous optimization is claimed.

Retains model-visible head/tail summaries for large logs: native and canonical ordinary command/task output shows a bounded UTF-8 head/tail with an explicit omission note, readable full remote raw-log refs, and truthful local-cache status. Background/incremental pages stay full within their budget and cursors never skip unreturned bytes.

A one-shot terminal command reads only bounded real remote edges; preview reads share a small deadline. A preview failure does not turn a completed command into an MCP transport error or replay it: the real exit is preserved and `preview_degraded` is explicit. For very large redirected logs use `remote.read` on the raw remote log.

Verified for canonical under canonical/Codex/Claude/Kimi profiles and four native foreground/background paths; CI covers Ubuntu 3.11/3.12, macOS, and Windows. Formal tags/releases are produced separately.
