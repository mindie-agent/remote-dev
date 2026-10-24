# remote-dev 0.10.6：SSH 清理异常保留、A3 双 Die 物理单元计数与实测空闲 HBM 归一化

[English](https://github.com/mindie-agent/remote-dev/blob/v0.10.6/docs/releases/remote-dev-0.10.6.en.md)

`remote-dev 0.10.6` 修复了 SSH 端口转发与流式命令在主操作出错后执行本地清理时的异常覆盖问题，并改进了远端主机硬件规格归一化逻辑（`normalize_remote_probe_spec`），使昇腾 910C（A3）双 Die 物理执行单元计数与空闲 HBM 显存计算完全基于实测遥测数据。

---

## 一、核心修复与行为变更

### 1. SSH 流式与端口转发清理失败不掩盖主异常（`remote_dev/core/ssh_forward.py`、`ssh_stream.py`、`errors.py`，PR `#43`）

- **修复 `LocalForward.__exit__` 清理异常覆盖**：此前模块拆分后，`ssh_forward.py` 的 `LocalForward.__exit__` 在主操作已抛出异常且 `self.close()` 也失败时引用了未绑定的 `transport` 变量，触发 `NameError` 并掩盖原始异常。现统一通过 `record_cleanup_failure(primary_error, cleanup_error)` 将清理失败累积记录到主异常的 `cleanup_error` 字段与异常附注（`add_note`）中，并继续抛出原始主异常。
- **流式命令退出清理收敛**：`ssh_stream.py` 在流读取异常后若本地停止辅助进程失败，同样通过 `record_cleanup_failure` 保留主异常并附加 `cleanup_error` 诊断；同时移除了确认远端子进程退出（`EXIT:`）后不可达的重复清理分支。

### 2. A3 双 Die 物理单元计数与实测空闲 HBM 归一化（`remote_dev/core/host_spec.py`，PR `#44`）

- **按可独立租约的物理执行单元统计 `npu_count` 与 `free_npus`**：
  - `normalize_remote_probe_spec` 对包含多 Die 列表的物理卡（如昇腾 910C A3 单卡 2 个 Die）按独立物理执行单元展开计数（单张 A3 双 Die 卡计为 `2` 个单元），单 Die 卡或仅含整卡行的数据保持每卡 `1` 个单元；
  - 当探测结果包含 `free_phy_ids` 时（包括空列表 `[]` 表示当前 `0` 个空闲物理单元），直接以 `len(free_phy_ids)` 作为权威 `free_npus`，不再在空列表时回退到按卡估算。
- **严格基于实测已用显存计算 `vram_free_mb`**：
  - 仅在非繁忙执行单元同时具备实测 `hbm_total_mb` 与 `hbm_used_mb` 时，累加 `max(0, hbm_total_mb - hbm_used_mb)` 并将 `vram_free_observed` 设为 `True`；
  - 若 `hbm_used_mb` 未观测到，绝不把标称总显存 `hbm_total_mb` 当作可用空闲显存。

---

## 二、测试验证

- 本地单元与契约测试完整覆盖 `LocalForward.__exit__` 主异常与 `cleanup_error` 保留、A3 双 Die 单元计数、`free_phy_ids=[]` 零空闲判定及未观测 HBM 拒绝逻辑。
- 配合 `coordinator` 在常驻 A2（`173.125.1.2`）与 A3（`192.168.13.165`、`192.168.13.154`）主机上完成只读 Fleet 容量探测验证（`test_live_fleet_probe_and_capacity_across_a2_and_a3`，**`1 passed`，`8.01s`**）。
