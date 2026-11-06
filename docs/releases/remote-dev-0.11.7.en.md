# remote-dev 0.11.7

[简体中文](remote-dev-0.11.7.md)

Source development tasks started from any directory can discover their business Python and editable sources, and continue when an automatic helper does not fit.

- `remote_probe` accepts an explicit `python_executable`, supplemental `python_candidates`, and `required_distributions`. Bounded discovery selects an eligible active environment or a sole candidate, reports ambiguity between eligible environments, and preserves failure for an explicit interpreter.
- Metadata probes return distribution versions, installation and editable source paths, Python prefixes, and ABI without importing frameworks. Distinct virtualenvs remain distinct. Package metadata does not prove native loading or NPU readiness.
- Shared MCP descriptions make source, build output, and interpreter choices independent. They honor an explicit new directory and otherwise reuse the current checkout, regardless of the agent's local working directory.
- Failure responses preserve endpoint and candidate facts and expose continuation through explicit interpreters, CLIs, native SSH, or task scripts. Uncertain external writes require reconciliation first.
- Maintained standalone workers and compressed transport keep probe and readiness payloads within Windows SSH command limits.

Clients discover this independent formal component release without a product channel update. CI covers cross-platform contracts, real isolated OpenSSH, and wheel installation; the release workflow runs tests and benchmarks against the exact tag. A2/A3 SSH was unavailable, so native rebuilds, dynamic-library loading, and NPU canary acceptance remain unverified.
