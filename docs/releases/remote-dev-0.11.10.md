# remote-dev 0.11.10：Diagnostics 2.0 Triage 阶段归因与状态契约违规观测

[English](remote-dev-0.11.10.en.md)

- **精确源码根目录与本地状态存储契约违规观测（`observability.py` + `core/state_store.py`）**：升级 `mindie-diagnostics` 依赖至正式版 `v0.5.11`（`171623584aa812677df907518dcb55b06493241b`），在故障记录中绑定 `remote_dev` 源码根目录并透传结构化 `reason_code`、`substage` 与 `contract_name`，严格排除远端主机名、IP、路径及凭据。
