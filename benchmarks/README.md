# Release benchmark

`release.json` is consumed by the shared MindIE release benchmark runner.
Each repetition executes `release.py` against the current checkout.

One measured operation parses a synthetic two-file Codex patch with deletion,
replacement, and addition hunks, then translates shell command arguments through
the real MCP normalization API. Every iteration verifies complete hunk contents
and literal command preservation. Inputs contain no user or machine data.

The metric is elapsed microseconds per parser/translation pair, including the
assertions and request construction. It does not measure SSH latency, remote
execution throughput, or agent productivity. Compare runs only with their
recorded source, Python, platform, and CPU context.
