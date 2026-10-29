"""Tier 0 unit tests for cross-platform HostHardwareSpec, WorkloadExpectation, and Spec-Driven Placement."""
from __future__ import annotations

import copy
import ctypes
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from remote_dev.core import host_spec, local_process
from remote_dev.core.host_spec import (
    WorkloadExpectation,
    _compute_usable_memory_mb,
    _parse_linux_meminfo,
    _parse_macos_vm_stat,
    _probe_windows_memory_mb,
    detect_os_family,
    evaluate_placement,
    normalize_remote_probe_spec,
    normalize_workload_expectation,
    probe_local_hardware_spec,
    recommend_worker_count,
)


def test_cross_platform_os_family_and_stdlib_memory_parsers():
    assert detect_os_family(os_name="nt", platform_name="win32") == "windows"
    assert detect_os_family(os_name="posix", platform_name="darwin") == "macos"
    assert detect_os_family(os_name="posix", platform_name="linux") == "linux"

    # Windows GlobalMemoryStatusEx ctypes fallback
    class FakeKernel32:
        def GlobalMemoryStatusEx(self, ptr):
            struct_val = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ulonglong * 9)).contents
            # Fields: dwLength+dwMemoryLoad (8 bytes), ullTotalPhys, ullAvailPhys
            # Better: set via attribute on the underlying ctypes structure object
            obj = ptr._obj
            obj.ullTotalPhys = 32768 * 1024 * 1024
            obj.ullAvailPhys = 20480 * 1024 * 1024
            return 1

    win_total, win_avail = _probe_windows_memory_mb(kernel32=FakeKernel32())
    assert (win_total, win_avail) == (32768, 20480)

    # Linux /proc/meminfo parser
    linux_meminfo = (
        "MemTotal:       65536000 kB\n"
        "MemFree:        12000000 kB\n"
        "MemAvailable:   49152000 kB\n"
        "Buffers:         1024000 kB\n"
    )
    assert _parse_linux_meminfo(linux_meminfo) == (64000, 48000)

    # macOS vm_stat reclaimable pages parser (16384-byte Apple Silicon pages)
    macos_vm_stat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:                              524288.\n"
        "Pages active:                           1048576.\n"
        "Pages inactive:                          524288.\n"
        "Pages speculative:                        65536.\n"
        "Pages purgeable:                         196608.\n"
    )
    # Reclaimable = (524288 + 524288 + 65536 + 196608) * 16384 B = 1310720 * 16 KB = 20480 MB
    assert _parse_macos_vm_stat(macos_vm_stat, 65536) == (65536, 20480)


def test_probe_local_hardware_spec_respects_desktop_reserve():
    spec = probe_local_hardware_spec(desktop_reserve_ratio=0.20, min_desktop_reserve_mb=2048)
    assert spec["os_family"] in {"windows", "macos", "linux"}
    assert spec["cpu_cores"] >= 1
    assert spec["usable_cpu_cores"] >= 0.0
    assert spec["mem_total_mb"] > 0
    assert 0 <= spec["mem_usable_mb"] <= spec["mem_available_mb"] <= spec["mem_total_mb"]
    # Verify exact reserve math: 8 GB laptop with 4 GB available leaves 2 GB usable after 2 GB reserve
    assert _compute_usable_memory_mb(8192, 4096, desktop_reserve_ratio=0.20, min_desktop_reserve_mb=2048) == 2048
    # 64 GB workstation with 56 GB available reserves 20% of 64 GB (13107 MB) -> 42893 MB usable
    assert _compute_usable_memory_mb(65536, 56000, desktop_reserve_ratio=0.20, min_desktop_reserve_mb=2048) == 42893


PROBE_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "npu_capacity_probe.json").read_text(encoding="utf-8"))


def fleet_row(cards, **server):
    """Assemble one `fleet_probe` row, deriving every count from the card rows.

    Mirrors `fleet_probe_parser.parse_fleet_probe_output`: card-level
    `free_npus`/`total_npus` and Die-level `free_phy_ids`/`total_phy_devices`
    are all computed from the same `npus` records, so a fixture can never
    claim a free Phy-ID that its own Die rows report busy.
    """
    dies = [(card, die) for card in cards for die in (card.get("dies") or [card])]
    return {
        "alias": server.pop("alias", "a3-165"),
        "host": server.pop("host", "192.168.13.165"),
        "online": True,
        "npus": cards,
        "total_npus": len(cards),
        "free_npus": len([card for card in cards if not card["busy"]]),
        "total_phy_devices": len(dies),
        "free_phy_ids": [die["phy_id"] for _card, die in dies if not die["busy"]],
        "busy_phy_ids": [die["phy_id"] for _card, die in dies if die["busy"]],
        "server": {"cpu_cores": 192, "load_1m": 12.0, "mem_total_mb": 1048576,
                   "mem_available_mb": 900000, **server},
    }


def half_busy_dual_die_cards():
    """One Die of a free A3 card occupied, as a single-Die (TP=1) lease looks.

    `parse_npu_smi_info` marks a Die busy from its own HBM/AICore/process
    attribution, so one card can carry one busy and one free Die.
    """
    cards = copy.deepcopy(PROBE_FIXTURE["a3_dual_die_partial"])
    free_card = next(card for card in cards if not card["busy"])
    free_card["dies"][0]["busy"] = True
    free_card["dies"][0]["hbm_used_mb"] = 60000
    free_card["busy"] = True  # card-level aggregation follows its busiest Die
    free_card["hbm_used_mb"] = sum(die["hbm_used_mb"] for die in free_card["dies"])
    return cards


def test_dual_die_capacity_counts_physical_execution_units_not_cards():
    row = fleet_row(copy.deepcopy(PROBE_FIXTURE["a3_dual_die_partial"]))
    norm = normalize_remote_probe_spec(row)
    assert norm["host"] == "192.168.13.165"
    assert norm["cpu_cores"] == 192
    assert norm["usable_cpu_cores"] == 180.0
    assert norm["mem_usable_mb"] == 900000
    # Two real A3 cards -> four physical Dies, not two execution units.
    assert (row["total_npus"], row["free_npus"]) == (2, 1)
    assert norm["npu_count"] == 4
    assert norm["free_npus"] == 2
    assert norm["free_phy_ids"] == [4, 5]
    assert norm["vram_total_mb"] == 4 * 65536
    # Free HBM is per-Die available capacity: (65536-3130) + (65536-3128).
    assert norm["vram_free_mb"] == (65536 - 3130) + (65536 - 3128)
    assert norm["vram_free_observed"] is True


def test_half_busy_dual_die_card_still_offers_its_free_die():
    row = fleet_row(half_busy_dual_die_cards())
    norm = normalize_remote_probe_spec(row)
    # Every card is card-level busy, yet one Die remains leasable.
    assert row["free_npus"] == 0
    assert row["free_phy_ids"] == [5]
    assert norm["free_npus"] == 1
    assert norm["vram_free_mb"] == 65536 - 3128
    placed = evaluate_placement(
        {"tier": "npu", "npu_count": 1, "hbm_mb": 60000, "memory_mb": 4096, "cpu_cores": 8},
        local_spec={"os_family": "macos", "cpu_cores": 8, "usable_cpu_cores": 7.0,
                    "mem_total_mb": 16384, "mem_available_mb": 8192},
        remote_specs=[row],
    )
    assert placed["decision"] == "remote"
    assert placed["host"] == "192.168.13.165"


def test_all_busy_a3_reports_authoritative_zero_free_dies():
    row = fleet_row(copy.deepcopy(PROBE_FIXTURE["a3_all_busy"]), host="192.168.13.154", alias="a3-154")
    assert row["free_phy_ids"] == []
    norm = normalize_remote_probe_spec(row)
    assert norm["npu_count"] == 4
    assert norm["free_npus"] == 0
    assert norm["free_phy_ids"] == []
    # An empty Phy-ID list outranks a stale card-level count rather than
    # falling back to it.
    assert normalize_remote_probe_spec({**row, "free_npus": 2})["free_npus"] == 0


def test_legacy_single_die_a2_and_card_only_rows_keep_card_counts():
    row = fleet_row(copy.deepcopy(PROBE_FIXTURE["a2_single_die"]), host="173.125.1.2", alias="a2-2")
    norm = normalize_remote_probe_spec(row)
    # A2 910B4 is one Die per card: Die and card counts coincide.
    assert (row["total_npus"], row["free_npus"]) == (2, 2)
    assert norm["npu_count"] == 2
    assert norm["free_npus"] == 2
    assert norm["vram_free_mb"] == (32768 - 2874) + (32768 - 2873)

    # Legacy probe rows that predate per-Die records stay card-shaped.
    card_only = [{"npu_id": 0, "busy": False, "hbm_total_mb": 32768, "hbm_used_mb": 2874},
                 {"npu_id": 1, "busy": True, "hbm_total_mb": 32768, "hbm_used_mb": 30000}]
    legacy = normalize_remote_probe_spec(
        {"host": "10.0.0.7", "npus": card_only, "total_npus": 2, "free_npus": 1,
         "server": {"cpu_cores": 64, "mem_total_mb": 32768, "mem_available_mb": 20000}})
    assert legacy["npu_count"] == 2
    assert legacy["free_npus"] == 1
    assert legacy["vram_free_mb"] == 32768 - 2874


def test_unmeasured_die_hbm_is_leasable_but_carries_no_headroom():
    """A `0 / 0` HBM Die is still a free execution unit with unknown headroom.

    `fleet_probe.probe_machine_record` keeps `hbm_total_mb == 0` on cards whose
    `npu-smi info` table reported no HBM and whose `npu-smi info -t usages`
    fallback failed (the snapshot then carries `usage_parse_error`). That is the
    reachable shape here: the production parser always emits both HBM keys, so
    only the unmeasured-total arm occurs in a real fleet row.
    """
    row = {
        "host": "10.0.0.9", "free_phy_ids": [0, 1],
        "usage_parse_error": "TimeoutError: npu-smi info -t usages timed out",
        "server": {"cpu_cores": 192, "mem_total_mb": 1048576, "mem_available_mb": 900000},
        "npus": [{"npu_id": 0, "busy": False, "hbm_used_mb": 0, "hbm_total_mb": 0, "die_count": 1,
                  "dies": [{"phy_id": 0, "busy": False, "hbm_used_mb": 0, "hbm_total_mb": 0}]},
                 {"npu_id": 1, "busy": False, "hbm_used_mb": 4778, "hbm_total_mb": 98304, "die_count": 1,
                  "dies": [{"phy_id": 1, "busy": False, "hbm_used_mb": 4778, "hbm_total_mb": 98304}]}],
    }
    norm = normalize_remote_probe_spec(row)
    assert norm["free_npus"] == 2
    # Only the measured Die contributes headroom; the unmeasured one adds none
    # and is not credited with a nameplate capacity it never reported.
    assert norm["vram_free_mb"] == 98304 - 4778
    assert norm["vram_total_mb"] == 98304
    assert norm["vram_free_observed"] is False


def test_unobserved_free_hbm_is_not_total_capacity():
    # No `npus` inventory and no free figure: nothing may be claimed available.
    unobserved = normalize_remote_probe_spec(
        {"host": "10.0.0.8", "hbm_total_mb": 131072,
         "server": {"cpu_cores": 64, "mem_total_mb": 32768, "mem_available_mb": 20000}})
    assert unobserved["vram_total_mb"] == 131072
    assert unobserved["vram_free_mb"] == 0
    assert unobserved["vram_free_observed"] is False

    observed = normalize_remote_probe_spec(
        {"host": "10.0.0.8", "hbm_total_mb": 131072, "hbm_free_mb": 65536,
         "server": {"cpu_cores": 64, "mem_total_mb": 32768, "mem_available_mb": 20000}})
    assert observed["vram_free_mb"] == 65536
    assert observed["vram_free_observed"] is True

    rejected = evaluate_placement(
        {"tier": "npu", "npu_count": 1, "hbm_mb": 65536},
        local_spec={"os_family": "linux", "cpu_cores": 8, "usable_cpu_cores": 8.0,
                    "mem_total_mb": 16384, "mem_available_mb": 8192},
        remote_specs=[{"host": "10.0.0.8", "hbm_total_mb": 131072, "free_npus": 4,
                       "server": {"cpu_cores": 64, "mem_total_mb": 32768, "mem_available_mb": 20000}}],
    )
    assert rejected["decision"] == "reject"
    assert rejected["shortfall"]["remote"][0]["gaps"]["vram_mb"] == {"required": 65536, "available": 0}


def test_high_spec_macos_or_windows_workstation_executes_heavy_workload_locally():
    # High-spec macOS Studio / Windows Workstation (128 GB RAM, 24 cores) vs smaller remote VM (32 GB RAM, 8 cores)
    local_macos_studio = {
        "os_family": "macos",
        "cpu_cores": 24,
        "usable_cpu_cores": 22.0,
        "mem_total_mb": 131072,
        "mem_available_mb": 110000,
    }
    small_remote = {
        "alias": "small-vm",
        "host": "10.0.0.12",
        "server": {"cpu_cores": 8, "load_1m": 2.0, "mem_total_mb": 32768, "mem_available_mb": 20000},
        "total_npus": 0,
        "free_npus": 0,
        "npus": [],
    }
    # Even with prefer_local=False, local workstation has higher usable RAM and CPU than small_remote
    decision = evaluate_placement(
        {"tier": "heavy", "cpu_cores": 8, "memory_mb": 32768, "per_worker_memory_mb": 2048},
        local_spec=local_macos_studio,
        remote_specs=[small_remote],
        prefer_local=False,
    )
    assert decision["decision"] == "local"
    assert decision["target"] == "local"
    assert decision["shortfall"] is None
    assert decision["recommended_workers"] == 22


def test_low_ram_windows_or_mac_laptop_routes_heavy_and_npu_tasks_to_remote():
    low_ram_windows_laptop = {
        "os_family": "windows",
        "cpu_cores": 8,
        "usable_cpu_cores": 6.0,
        "mem_total_mb": 8192,
        "mem_available_mb": 4096,  # 2048 MB usable after 2048 MB desktop reserve
        "vram_total_mb": 0,
        "vram_free_mb": 0,
        "npu_count": 0,
        "free_npus": 0,
    }
    remote_a2 = {
        "alias": "a2-2",
        "host": "173.125.1.2",
        "online": True,
        "total_npus": 8,
        "free_npus": 4,
        "server": {"cpu_cores": 192, "load_1m": 8.0, "mem_total_mb": 524288, "mem_available_mb": 400000},
        "npus": [{"npu_id": i, "busy": False, "hbm_total_mb": 65536, "hbm_used_mb": 0} for i in range(4)],
    }

    # Light workload (512 MB) still runs locally on the laptop
    light_res = evaluate_placement("light", local_spec=low_ram_windows_laptop, remote_specs=[remote_a2])
    assert light_res["decision"] == "local"

    # Heavy CPU/RAM compile workload (16 GB RAM) routes to remote_a2
    heavy_res = evaluate_placement("heavy", local_spec=low_ram_windows_laptop, remote_specs=[remote_a2])
    assert heavy_res["decision"] == "remote"
    assert heavy_res["target"] == "173.125.1.2"
    assert heavy_res["alias"] == "a2-2"

    # NPU/VRAM workload routes to remote_a2 even if local has plenty of CPU/RAM
    high_ram_cpu_only_local = {
        "os_family": "linux",
        "cpu_cores": 64,
        "usable_cpu_cores": 60.0,
        "mem_total_mb": 262144,
        "mem_available_mb": 220000,
        "vram_free_mb": 0,
        "free_npus": 0,
    }
    npu_res = evaluate_placement(
        WorkloadExpectation.from_spec({"tier": "npu", "npu_count": 2, "hbm_mb": 65536}),
        local_spec=high_ram_cpu_only_local,
        remote_specs=[remote_a2],
    )
    assert npu_res["decision"] == "remote"
    assert npu_res["host"] == "173.125.1.2"


def test_recommend_worker_count_and_shortfall_rejection():
    # 16 usable cores, but only 3072 MB usable memory with 1024 MB per worker -> capped at 3 workers
    spec = {"cpu_cores": 16, "usable_cpu_cores": 16.0, "mem_usable_mb": 3072}
    assert recommend_worker_count(spec, per_worker_memory_mb=1024) == 3
    # 4 usable cores, 65536 MB usable memory -> capped at 4 workers by CPU
    assert recommend_worker_count({"cpu_cores": 8, "usable_cpu_cores": 4.5, "mem_usable_mb": 65536}, per_worker_memory_mb=512) == 4
    # Fewer than one worker worth of memory must never force a worker to start
    assert recommend_worker_count({"cpu_cores": 4, "usable_cpu_cores": 4.0, "mem_usable_mb": 128}, per_worker_memory_mb=1024) == 0

    # Reject with structured shortfall when neither local nor remote satisfies
    rejected = evaluate_placement(
        {"memory_mb": 131072, "vram_mb": 131072, "npu_count": 4, "expected_seconds": 20, "cpu_cores": 16},
        local_spec={"os_family": "macos", "cpu_cores": 8, "usable_cpu_cores": 4.0, "mem_total_mb": 16384, "mem_available_mb": 8192},
        remote_specs=[
            {
                "alias": "small-npu",
                "host": "10.0.0.99",
                "total_npus": 1,
                "free_npus": 1,
                "server": {"cpu_cores": 16, "mem_total_mb": 32768, "mem_available_mb": 24000},
                "npus": [{"npu_id": 0, "busy": False, "hbm_total_mb": 32768, "hbm_used_mb": 0}],
            }
        ],
    )
    assert rejected["decision"] == "reject"
    assert rejected["target"] is None
    assert "memory_mb" in rejected["shortfall"]
    assert "vram_mb" in rejected["shortfall"]
    assert "npu_count" in rejected["shortfall"]
    assert len(rejected["shortfall"]["remote"]) == 1
    # 16 required cores / 4 usable local cores = 4x timeout scale on 20s -> 80000ms
    assert rejected["recommended_timeout_ms"] == 80000


def test_windows_job_and_owned_process_max_memory_mb_fuse():
    with pytest.raises(ValueError, match="max_memory_mb must be a positive integer"):
        local_process.OwnedProcess([sys.executable, "-c", "pass"], max_memory_mb=0)
    with pytest.raises(ValueError, match="max_memory_mb must be a positive integer"):
        local_process.OwnedProcess([sys.executable, "-c", "pass"], max_memory_mb=True)

    # Verify _WindowsJob configures JOB_OBJECT_LIMIT_JOB_MEMORY (0x200) | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000)
    captured: dict[str, int] = {}

    fake_kernel = mock.MagicMock()
    fake_kernel.CreateJobObjectW.return_value = 42

    def fake_set_info(handle, info_class, ptr, size):
        ext = ptr._obj
        captured["handle"] = handle
        captured["info_class"] = info_class
        captured["flags"] = int(ext.basic.flags)
        captured["job_memory"] = int(ext.job_memory)
        captured["size"] = int(size)
        return 1

    fake_kernel.SetInformationJobObject.side_effect = fake_set_info
    fake_kernel.CloseHandle.return_value = 1

    with mock.patch.object(ctypes, "WinDLL", return_value=fake_kernel, create=True):
        job = local_process._WindowsJob(max_memory_mb=4096)
        assert job.memory_limit["mode"] == "hard-limit"
        assert job.memory_limit["enforced_mb"] == 4096
        job.close()

    assert captured["handle"] == 42
    assert captured["info_class"] == 9
    assert captured["flags"] == (local_process.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | local_process.JOB_OBJECT_LIMIT_JOB_MEMORY)
    assert captured["job_memory"] == 4096 * 1024 * 1024

    with mock.patch.object(ctypes, "WinDLL", return_value=fake_kernel, create=True):
        unlimited = local_process._WindowsJob()
        assert unlimited.memory_limit["mode"] == "observed-only"
        assert unlimited.memory_limit["enforced_mb"] is None
        unlimited.close()
    assert captured["flags"] == local_process.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert captured["job_memory"] == 0


def test_concurrent_local_reservations_prevent_toctou_overcommit_and_reclaim_dead_pids(tmp_path):
    db_path = tmp_path / "pool.sqlite3"
    local_24g = {
        "os_family": "macos",
        "cpu_cores": 16,
        "usable_cpu_cores": 14.0,
        "mem_total_mb": 32768,
        "mem_available_mb": 26000,
        "mem_usable_mb": 24000,
    }
    remote_a3 = [
        {
            "alias": "a3-165",
            "host": "192.168.13.165",
            "online": True,
            "server": {"cpu_cores": 192, "load_1m": 4.0, "mem_total_mb": 1048576, "mem_available_mb": 900000},
        }
    ]
    task_10g = {"cpu_cores": 4, "memory_mb": 10000}

    with host_spec.reserve_local_resources(task_10g, pool_db_path=db_path, local_spec=local_24g, remote_specs=remote_a3) as first:
        assert first["decision"] == "local"
        assert host_spec.active_local_reserved_mb(db_path) == 10000
        with host_spec.reserve_local_resources(task_10g, pool_db_path=db_path, local_spec=local_24g, remote_specs=remote_a3) as second:
            assert second["decision"] == "local"
            assert host_spec.active_local_reserved_mb(db_path) == 20000
            # Third concurrent 10 GiB task sees only 4 GiB usable locally and routes to remote a3-165:
            with host_spec.reserve_local_resources(task_10g, pool_db_path=db_path, local_spec=local_24g, remote_specs=remote_a3) as third:
                assert third["decision"] == "remote"
                assert third["host"] == "192.168.13.165"
                assert third["reservation_id"] is None

    # After context exit, local reservations are released:
    assert host_spec.active_local_reserved_mb(db_path) == 0

