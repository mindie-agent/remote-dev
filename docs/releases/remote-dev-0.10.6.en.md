# remote-dev 0.10.6: Preserved Primary SSH Exceptions Across Cleanup & Observed Dual-Die A3 / HBM Capacity Normalization

[中文](https://github.com/mindie-agent/remote-dev/blob/v0.10.6/docs/releases/remote-dev-0.10.6.md)

`remote-dev 0.10.6` fixes primary-exception masking when local cleanup fails in SSH port forwarding and streaming commands, and updates `normalize_remote_probe_spec` so Ascend 910C (A3) dual-Die physical unit counts and free HBM calculations rely strictly on observed hardware telemetry.

---

## 1. Key Fixes & Behavior Changes

### 1. Preserving Primary SSH Exceptions Across Cleanup Failures (`remote_dev/core/ssh_forward.py`, `ssh_stream.py`, `errors.py`, PR `#43`)

- **Fixed `LocalForward.__exit__` Unbound `transport` & Exception Masking**: After an earlier module split, `LocalForward.__exit__` in `ssh_forward.py` referenced an unbound `transport` variable when `self.close()` failed during exception unwinding, raising a `NameError` that masked the primary error. It now calls `record_cleanup_failure(primary_error, cleanup_error)` to record the cleanup failure on `primary_error.cleanup_error` (and via `add_note`) before re-raising the original exception.
- **Stream Cleanup Consolidation**: `ssh_stream.py` likewise attaches secondary cleanup failures onto the primary stream exception's `cleanup_error` field via `record_cleanup_failure`, and removes the unreachable duplicate post-exit cleanup branch after a confirmed `EXIT:` frame.

### 2. Dual-Die A3 Physical Unit Counting & Observed Free HBM Normalization (`remote_dev/core/host_spec.py`, PR `#44`)

- **Counting Independently Leasable Physical Execution Units (`npu_count` & `free_npus`)**:
  - `normalize_remote_probe_spec` counts each Die on multi-Die cards (such as dual-Die Ascend 910C A3 cards) as an independently leasable execution unit (`2` units per dual-Die A3 card), while single-Die cards and card-only rows remain `1` unit per card.
  - When `free_phy_ids` is present in the probe payload (including an empty list `[]` indicating `0` free units), `free_npus` is taken authoritatively from `len(free_phy_ids)` rather than falling back to card-level counts when `[]` is empty.
- **Strict Observed Free HBM Calculation (`vram_free_mb` & `vram_free_observed`)**:
  - `vram_free_mb` sums `max(0, hbm_total_mb - hbm_used_mb)` only across non-busy execution units where both `hbm_total_mb` and `hbm_used_mb` are measured, setting `vram_free_observed = True`;
  - When `hbm_used_mb` is missing or unobserved, `normalize_remote_probe_spec` never substitutes total nameplate `hbm_total_mb` as free memory.

---

## 2. Verification

- Unit and contract tests covering `LocalForward.__exit__` primary exception and `cleanup_error` preservation, dual-Die A3 unit counting, `free_phy_ids=[]` handling, and unobserved HBM refusal all passed.
- Verified with `coordinator` on standing A2 (`173.125.1.2`) and A3 (`192.168.13.165`, `192.168.13.154`) hosts via `test_live_fleet_probe_and_capacity_across_a2_and_a3` (**`1 passed` in `8.01s`**).
