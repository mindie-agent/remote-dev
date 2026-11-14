# remote-dev 0.11.10: Diagnostics 2.0 Triage Attribution & State Contract-Violation Observability

[中文](remote-dev-0.11.10.md)

- **Source-Root Attribution & State-Store Contract-Violation Observability (`observability.py` + `core/state_store.py`)**: Upgrades `mindie-diagnostics` to formal release `v0.5.11` (`171623584aa812677df907518dcb55b06493241b`), binds `remote_dev` `source_root`, and forwards structured `reason_code`, `substage`, and `contract_name` in failure and state-contract diagnostics while keeping hostnames, IPs, paths, and credentials strictly excluded.
