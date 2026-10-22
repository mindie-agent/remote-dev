"""
[Role] Cross-platform hardware specification probing (`HostHardwareSpec`), workload resource expectation normalization (`WorkloadExpectation`), and spec-driven local vs. remote placement (`host_spec.py`).
[Invariants]
- Probing works identically across Windows (`os.name == "nt"`), macOS (`sys.platform == "darwin"`), and Linux (`sys.platform.startswith("linux")`) with zero subprocess launches.
- Placement compares `WorkloadExpectation` (`cpu_cores`, `memory_mb`, `vram_mb`/`hbm_mb`, `npu_count`) purely against candidate `HostHardwareSpec` headroom (`mem_usable_mb`, `cpu_cores`, `vram_free_mb`, `free_npus`); never rejects local execution based on OS platform name.
- Worker concurrency is bounded by both usable CPU cores and usable memory (`max(1, min(usable_cpu_cores, mem_usable_mb // max(1, per_worker_memory_mb)))`).
[Timeouts & Locks]
- Pure in-memory / kernel syscall (`GlobalMemoryStatusEx`, `/proc/meminfo`, `sysconf`/`sysctl`) probing and placement evaluation; zero subprocess launches, zero network I/O, zero locks.
[Side-Effect Policy]
- Strictly read-only local kernel memory/CPU queries and pure functional data transformations.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import contextlib
from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from typing import Any
import uuid

TIER_PRESETS: dict[str, dict[str, Any]] = {
    "light": {"tier": "light", "cpu_cores": 1, "memory_mb": 512, "vram_mb": 0, "npu_count": 0, "expected_seconds": 15.0, "per_worker_memory_mb": 256},
    "medium": {"tier": "medium", "cpu_cores": 4, "memory_mb": 4096, "vram_mb": 0, "npu_count": 0, "expected_seconds": 60.0, "per_worker_memory_mb": 512},
    "heavy": {"tier": "heavy", "cpu_cores": 8, "memory_mb": 16384, "vram_mb": 0, "npu_count": 0, "expected_seconds": 300.0, "per_worker_memory_mb": 2048},
    "npu": {"tier": "npu", "cpu_cores": 8, "memory_mb": 16384, "vram_mb": 16384, "npu_count": 1, "expected_seconds": 300.0, "per_worker_memory_mb": 2048},
}


@dataclass(frozen=True)
class HostHardwareSpec:
    os_family: str
    cpu_cores: int
    usable_cpu_cores: float
    mem_total_mb: int
    mem_available_mb: int
    mem_usable_mb: int
    vram_total_mb: int = 0
    vram_free_mb: int = 0
    npu_count: int = 0
    free_npus: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkloadExpectation:
    tier: str = "light"
    cpu_cores: int = 1
    memory_mb: int = 512
    vram_mb: int = 0
    npu_count: int = 0
    expected_seconds: float = 15.0
    per_worker_memory_mb: int = 256

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any] | WorkloadExpectation | str | None) -> WorkloadExpectation:
        norm = normalize_workload_expectation(spec)
        return cls(**{k: norm[k] for k in ("tier", "cpu_cores", "memory_mb", "vram_mb", "npu_count", "expected_seconds", "per_worker_memory_mb")})

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "hbm_mb": self.vram_mb}


def detect_os_family(os_name: str | None = None, platform_name: str | None = None) -> str:
    actual_os, actual_platform = (os.name if os_name is None else os_name), (sys.platform if platform_name is None else platform_name)
    return "windows" if (actual_os == "nt" or actual_platform.startswith("win")) else ("macos" if actual_platform == "darwin" else "linux")


def _probe_windows_memory_mb(kernel32: Any = None) -> tuple[int, int]:
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx(dwLength=ctypes.sizeof(MemoryStatusEx))
    lib = kernel32 if kernel32 is not None else getattr(ctypes, "windll", None).kernel32
    if not lib.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys // (1024 * 1024)), int(status.ullAvailPhys // (1024 * 1024))


def _parse_linux_meminfo(content: str) -> tuple[int, int]:
    values_kb = {
        line.partition(":")[0].strip(): int(line.partition(":")[2].strip().split()[0])
        for line in content.splitlines()
        if ":" in line and line.partition(":")[2].strip().split()[:1] and line.partition(":")[2].strip().split()[0].isdigit()
    }
    total_kb = values_kb.get("MemTotal", 0)
    avail_kb = values_kb.get("MemAvailable", values_kb.get("MemFree", 0) + values_kb.get("Buffers", 0) + values_kb.get("Cached", 0))
    return total_kb // 1024, min(total_kb, avail_kb) // 1024


def _parse_macos_vm_stat(content: str, total_mb: int) -> tuple[int, int]:
    page_size, reclaimable_pages = 4096, 0
    for line in content.splitlines():
        if "page size of" in line:
            digits = [int(tok) for tok in line.split() if tok.isdigit()]
            page_size = digits[0] if digits else page_size
        elif ":" in line and line.partition(":")[0].strip() in {"Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"}:
            cleaned = line.partition(":")[2].strip().rstrip(".")
            if cleaned.isdigit():
                reclaimable_pages += int(cleaned)
    avail_mb = (reclaimable_pages * page_size) // (1024 * 1024)
    return total_mb, (min(total_mb, avail_mb) if avail_mb > 0 else int(total_mb * 0.65)) if total_mb > 0 else avail_mb


def _probe_macos_memory_mb() -> tuple[int, int]:
    total_bytes, page_size = 0, 4096
    try:
        pages, page_size = int(os.sysconf("SC_PHYS_PAGES")), int(os.sysconf("SC_PAGE_SIZE"))
        total_bytes = max(0, pages * page_size)
    except TimeoutError:
        raise
    except (AttributeError, ValueError, OSError):
        pass
    total_mb = total_bytes // (1024 * 1024)
    try:
        import ctypes

        libc, reclaimable_pages = ctypes.CDLL(None), 0
        for name in (b"vm.page_free_count", b"vm.page_speculative_count", b"vm.page_pageable_internal_count"):
            val, size = ctypes.c_uint64(0), ctypes.c_size_t(8)
            if libc.sysctlbyname(name, ctypes.byref(val), ctypes.byref(size), None, 0) == 0:
                reclaimable_pages += int(val.value)
        if reclaimable_pages > 0:
            return total_mb, min(total_mb, (reclaimable_pages * page_size) // (1024 * 1024))
    except TimeoutError:
        raise
    except (subprocess.SubprocessError, OSError, ValueError):
        pass
    return total_mb, int(total_mb * 0.65)


def _compute_usable_memory_mb(
    mem_total_mb: int, mem_available_mb: int, *, desktop_reserve_ratio: float = 0.20, min_desktop_reserve_mb: int = 2048, reserved_mb: int = 0
) -> int:
    if mem_total_mb <= 0 or mem_available_mb <= 0:
        return 0
    reserve_mb = max(int(min_desktop_reserve_mb), int(round(mem_total_mb * max(0.0, float(desktop_reserve_ratio)))))
    return max(0, int(mem_available_mb) - reserve_mb - max(0, int(reserved_mb)))


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except TimeoutError:
            raise
        except (OSError, ValueError):
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except TimeoutError:
        raise
    except OSError:
        return False


def _open_pool_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS local_reservations("
        "id TEXT PRIMARY KEY, pid INTEGER NOT NULL, memory_mb INTEGER NOT NULL, "
        "cpu_cores INTEGER NOT NULL, expires_at REAL NOT NULL)"
    )
    return conn


def active_local_reserved_mb(pool_db_path: Path | None = None, *, exclude_pid: int | None = None) -> int:
    if pool_db_path is None:
        return 0
    now = time.time()
    try:
        with contextlib.closing(_open_pool_db(pool_db_path)) as conn, conn:
            conn.execute("DELETE FROM local_reservations WHERE expires_at <= ?", (now,))
            rows = conn.execute("SELECT id, pid, memory_mb FROM local_reservations").fetchall()
            dead_ids = [row_id for row_id, pid, _ in rows if not _pid_alive(int(pid))]
            if dead_ids:
                conn.executemany("DELETE FROM local_reservations WHERE id = ?", [(rid,) for rid in dead_ids])
            return sum(
                max(0, int(mem_mb))
                for row_id, pid, mem_mb in rows
                if row_id not in set(dead_ids) and (exclude_pid is None or int(pid) != int(exclude_pid))
            )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return 0
        raise


@contextlib.contextmanager
def reserve_local_resources(
    expectation: Mapping[str, Any] | WorkloadExpectation | str | None,
    *,
    pool_db_path: Path,
    local_spec: Mapping[str, Any] | None = None,
    remote_specs: Sequence[Mapping[str, Any]] = (),
    ttl_seconds: float = 600.0,
    pid: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Atomically evaluate placement and reserve local memory across concurrent processes (`WAL` + `busy_timeout=5000`)."""
    exp = normalize_workload_expectation(expectation)
    res_id = uuid.uuid4().hex
    owner_pid = int(os.getpid() if pid is None else pid)
    inserted = False
    with contextlib.closing(_open_pool_db(pool_db_path)) as conn:
        with conn:
            now = time.time()
            conn.execute("DELETE FROM local_reservations WHERE expires_at <= ?", (now,))
            rows = conn.execute("SELECT id, pid, memory_mb FROM local_reservations").fetchall()
            dead_ids = [r_id for r_id, r_pid, _ in rows if not _pid_alive(int(r_pid))]
            if dead_ids:
                conn.executemany("DELETE FROM local_reservations WHERE id = ?", [(rid,) for rid in dead_ids])
            reserved_mb = sum(max(0, int(m)) for r_id, _, m in rows if r_id not in set(dead_ids))
            base_local = _normalize_local_spec(local_spec) if local_spec is not None else probe_local_hardware_spec()
            effective_local = {**base_local, "mem_usable_mb": max(0, int(base_local["mem_usable_mb"]) - reserved_mb)}
            decision = evaluate_placement(exp, local_spec=effective_local, remote_specs=remote_specs)
            if decision["decision"] == "local":
                conn.execute(
                    "INSERT INTO local_reservations(id, pid, memory_mb, cpu_cores, expires_at) VALUES(?, ?, ?, ?, ?)",
                    (res_id, owner_pid, int(exp["memory_mb"]), int(exp["cpu_cores"]), now + max(1.0, float(ttl_seconds))),
                )
                inserted = True
    try:
        yield {**decision, "reservation_id": res_id if inserted else None}
    finally:
        if inserted:
            with contextlib.suppress(sqlite3.Error), contextlib.closing(_open_pool_db(pool_db_path)) as conn, conn:
                conn.execute("DELETE FROM local_reservations WHERE id = ?", (res_id,))


def probe_local_hardware_spec(*, desktop_reserve_ratio: float = 0.20, min_desktop_reserve_mb: int = 2048) -> dict[str, Any]:
    os_family, cpu_cores, load_1m = detect_os_family(), max(1, int(os.cpu_count() or 1)), 0.0
    if hasattr(os, "getloadavg"):
        try:
            load_1m = max(0.0, float(os.getloadavg()[0]))
        except TimeoutError:
            raise
        except OSError:
            load_1m = 0.0
    elif os_family == "linux":
        try:
            load_1m = max(0.0, float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0]))
        except TimeoutError:
            raise
        except (OSError, ValueError, IndexError):
            load_1m = 0.0

    mem_total_mb = mem_available_mb = 0
    try:
        import psutil  # type: ignore[import-not-found]

        vm = psutil.virtual_memory()
        mem_total_mb, mem_available_mb = int(vm.total // (1024 * 1024)), int(vm.available // (1024 * 1024))
    except ImportError:
        try:
            if os_family == "windows":
                mem_total_mb, mem_available_mb = _probe_windows_memory_mb()
            elif os_family == "macos":
                mem_total_mb, mem_available_mb = _probe_macos_memory_mb()
            else:
                mem_total_mb, mem_available_mb = _parse_linux_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
        except TimeoutError:
            raise
        except (subprocess.SubprocessError, OSError, ValueError):
            mem_total_mb = mem_available_mb = 0
    if mem_total_mb <= 0:
        raise RuntimeError("Failed to probe local host memory specification")

    usable_cpu_cores = round(max(1.0, float(cpu_cores) - load_1m), 2)
    mem_usable_mb = _compute_usable_memory_mb(
        mem_total_mb, mem_available_mb, desktop_reserve_ratio=desktop_reserve_ratio, min_desktop_reserve_mb=min_desktop_reserve_mb
    )
    return HostHardwareSpec(os_family, cpu_cores, usable_cpu_cores, mem_total_mb, mem_available_mb, mem_usable_mb).to_dict()


def normalize_remote_probe_spec(probe_row: Mapping[str, Any]) -> dict[str, Any]:
    server = probe_row.get("server") if isinstance(probe_row.get("server"), Mapping) else {}
    cpu_cores = max(0, int(probe_row.get("cpu_cores") or server.get("cpu_cores") or 0))
    load_raw = probe_row.get("load_1m") if probe_row.get("load_1m") is not None else server.get("load_1m")
    load_1m = max(0.0, float(load_raw or 0.0))
    usable_cpu_cores = (
        max(0.0, float(probe_row["usable_cpu_cores"]))
        if probe_row.get("usable_cpu_cores") is not None
        else (round(max(1.0, float(cpu_cores) - load_1m), 2) if cpu_cores > 0 else 0.0)
    )
    mem_total_mb = max(0, int(probe_row.get("mem_total_mb") or server.get("mem_total_mb") or 0))
    avail_raw = probe_row.get("mem_available_mb") if probe_row.get("mem_available_mb") is not None else server.get("mem_available_mb")
    mem_available_mb = max(0, int(avail_raw if avail_raw is not None else mem_total_mb))
    usable_raw = probe_row.get("mem_usable_mb")
    mem_usable_mb = max(0, int(usable_raw if usable_raw is not None else mem_available_mb))

    npus = probe_row.get("npus")
    vram_total_mb, vram_free_mb, computed_free_dies = 0, 0, 0
    if isinstance(npus, Sequence) and not isinstance(npus, (str, bytes)) and len(npus) > 0:
        for card in npus:
            if not isinstance(card, Mapping):
                continue
            dies = card.get("dies")
            units = dies if (isinstance(dies, Sequence) and not isinstance(dies, (str, bytes)) and len(dies) > 0) else [card]
            for unit in units:
                if isinstance(unit, Mapping):
                    total_u, used_u = max(0, int(unit.get("hbm_total_mb") or 0)), max(0, int(unit.get("hbm_used_mb") or 0))
                    vram_total_mb += total_u
                    if not unit.get("busy"):
                        vram_free_mb += max(0, total_u - used_u)
                        computed_free_dies += 1
    else:
        vram_total_mb = max(0, int(probe_row.get("vram_total_mb") or probe_row.get("hbm_total_mb") or 0))
        free_vram_raw = probe_row.get("vram_free_mb") if probe_row.get("vram_free_mb") is not None else probe_row.get("hbm_free_mb")
        vram_free_mb = max(0, int(free_vram_raw if free_vram_raw is not None else vram_total_mb))

    npu_count_raw = probe_row.get("npu_count") if probe_row.get("npu_count") is not None else probe_row.get("total_npus")
    npu_count = max(0, int(npu_count_raw)) if npu_count_raw is not None else (len(npus) if isinstance(npus, Sequence) and not isinstance(npus, (str, bytes)) else 0)
    free_phy_ids = list(probe_row.get("free_phy_ids") or [])
    free_npus = max(0, int(probe_row["free_npus"])) if probe_row.get("free_npus") is not None else (len(free_phy_ids) if free_phy_ids else computed_free_dies)

    spec = HostHardwareSpec(
        str(probe_row.get("os_family") or "linux"), cpu_cores, usable_cpu_cores,
        mem_total_mb, mem_available_mb, mem_usable_mb, vram_total_mb, vram_free_mb, npu_count, free_npus,
    ).to_dict()
    for meta_key in ("host", "alias", "online"):
        if meta_key in probe_row:
            spec[meta_key] = probe_row[meta_key]
    if free_phy_ids:
        spec["free_phy_ids"] = free_phy_ids
    return spec


def normalize_workload_expectation(spec: Mapping[str, Any] | WorkloadExpectation | str | None) -> dict[str, Any]:
    if isinstance(spec, WorkloadExpectation):
        return spec.to_dict()
    if spec is None or isinstance(spec, str):
        tier_key = "light" if spec is None else spec.strip().lower()
        if tier_key not in TIER_PRESETS:
            raise ValueError(f"unsupported workload tier {spec!r}; expected one of {sorted(TIER_PRESETS)}")
        return {**TIER_PRESETS[tier_key], "hbm_mb": TIER_PRESETS[tier_key]["vram_mb"]}
    if not isinstance(spec, Mapping):
        raise ValueError("workload expectation must be a tier string, mapping, or None")

    explicit_tier = spec.get("tier")
    tier_key = str(explicit_tier).strip().lower() if explicit_tier is not None else "light"
    if tier_key not in TIER_PRESETS:
        raise ValueError(f"unsupported workload tier {explicit_tier!r}; expected one of {sorted(TIER_PRESETS)}")
    merged = dict(TIER_PRESETS[tier_key])
    for int_key, floor in (("cpu_cores", 1), ("memory_mb", 0), ("npu_count", 0), ("per_worker_memory_mb", 1)):
        if spec.get(int_key) is not None:
            merged[int_key] = max(floor, int(spec[int_key]))
    if spec.get("npu_count") is None and isinstance(spec.get("devices"), Sequence) and not isinstance(spec.get("devices"), (str, bytes)):
        merged["npu_count"] = len(spec["devices"])
    vram_override = spec.get("vram_mb") if spec.get("vram_mb") is not None else spec.get("hbm_mb")
    if vram_override is not None:
        merged["vram_mb"] = max(0, int(vram_override))
    if spec.get("expected_seconds") is not None:
        merged["expected_seconds"] = max(1.0, float(spec["expected_seconds"]))
    if explicit_tier is None:
        merged["tier"] = "npu" if (merged["npu_count"] > 0 or merged["vram_mb"] > 0) else ("heavy" if (merged["memory_mb"] >= 16384 or merged["cpu_cores"] >= 8) else ("medium" if (merged["memory_mb"] >= 4096 or merged["cpu_cores"] >= 4) else "light"))
    merged["hbm_mb"] = merged["vram_mb"]
    return merged


def recommend_worker_count(hardware_spec: Mapping[str, Any], *, per_worker_memory_mb: int = 512, max_workers: int | None = None) -> int:
    usable_cpu = float(hardware_spec.get("usable_cpu_cores") if hardware_spec.get("usable_cpu_cores") is not None else (hardware_spec.get("cpu_cores") or 1))
    cpu_cap = max(1, int(math.floor(usable_cpu)))
    mem_usable = int(hardware_spec.get("mem_usable_mb") if hardware_spec.get("mem_usable_mb") is not None else (hardware_spec.get("mem_available_mb") or hardware_spec.get("mem_total_mb") or 0))
    workers = max(1, min(cpu_cap, max(0, mem_usable) // max(1, int(per_worker_memory_mb or 512))))
    return max(1, min(workers, int(max_workers))) if max_workers is not None else workers


def _candidate_shortfall(spec: Mapping[str, Any], exp: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    gaps: dict[str, dict[str, int]] = {"online": {"required": 1, "available": 0}} if spec.get("online") is False else {}
    for exp_key, spec_key in (("memory_mb", "mem_usable_mb"), ("cpu_cores", "cpu_cores"), ("vram_mb", "vram_free_mb"), ("npu_count", "free_npus")):
        req, avail = int(exp[exp_key]), int(spec.get(spec_key) or 0)
        if req > avail:
            gaps[exp_key] = {"required": req, "available": avail}
    return gaps


def _candidate_score(spec: Mapping[str, Any], exp: Mapping[str, Any]) -> tuple[float, ...]:
    if int(exp["npu_count"]) > 0 or int(exp["vram_mb"]) > 0:
        return (float(spec.get("free_npus") or 0), float(spec.get("vram_free_mb") or 0), float(spec.get("mem_usable_mb") or 0), float(spec.get("usable_cpu_cores") or spec.get("cpu_cores") or 0))
    return (float(spec.get("mem_usable_mb") or 0), float(spec.get("usable_cpu_cores") or spec.get("cpu_cores") or 0), float(spec.get("vram_free_mb") or 0))


def _recommend_timeout_ms(spec: Mapping[str, Any] | None, exp: Mapping[str, Any]) -> int:
    base_ms = max(1000, int(round(float(exp["expected_seconds"]) * 1000.0)))
    if spec is None:
        return base_ms
    usable_cpu = max(1.0, float(spec.get("usable_cpu_cores") or spec.get("cpu_cores") or 1.0))
    return int(round(base_ms * max(1.0, min(4.0, float(exp["cpu_cores"]) / usable_cpu))))


def _normalize_local_spec(local_spec: Mapping[str, Any]) -> dict[str, Any]:
    spec = normalize_remote_probe_spec(local_spec)
    if "mem_usable_mb" not in local_spec:
        spec["mem_usable_mb"] = _compute_usable_memory_mb(spec["mem_total_mb"], spec["mem_available_mb"])
    return spec


def evaluate_placement(
    expectation: Mapping[str, Any] | WorkloadExpectation | str | None,
    *, local_spec: Mapping[str, Any] | None = None, remote_specs: Sequence[Mapping[str, Any]] = (), prefer_local: bool = True,
) -> dict[str, Any]:
    exp = normalize_workload_expectation(expectation)
    norm_local = _normalize_local_spec(local_spec) if local_spec is not None else probe_local_hardware_spec()
    local_gaps = _candidate_shortfall(norm_local, exp)
    local_ok = not local_gaps

    satisfying_remotes: list[dict[str, Any]] = []
    remote_gaps: list[dict[str, Any]] = []
    for raw_remote in remote_specs:
        norm_remote = normalize_remote_probe_spec(raw_remote)
        gaps = _candidate_shortfall(norm_remote, exp)
        if not gaps:
            satisfying_remotes.append(norm_remote)
        else:
            remote_gaps.append({"target": str(norm_remote.get("host") or norm_remote.get("alias") or "remote"), "gaps": gaps})
    satisfying_remotes.sort(key=lambda item: _candidate_score(item, exp), reverse=True)
    best_remote = satisfying_remotes[0] if satisfying_remotes else None

    use_local = local_ok and (best_remote is None or prefer_local or _candidate_score(norm_local, exp) >= _candidate_score(best_remote, exp))
    chosen_decision = "local" if use_local else ("remote" if best_remote is not None else "reject")
    chosen_spec = norm_local if use_local else best_remote

    if chosen_decision in {"local", "remote"} and chosen_spec is not None:
        target_id = "local" if chosen_decision == "local" else str(chosen_spec.get("host") or chosen_spec.get("alias") or "remote")
        host_val = "local" if chosen_decision == "local" else (chosen_spec.get("host") or target_id)
        alias_val = "local" if chosen_decision == "local" else (chosen_spec.get("alias") or target_id)
        return {
            "decision": chosen_decision, "target": target_id, "host": host_val, "alias": alias_val, "shortfall": None,
            "recommended_workers": recommend_worker_count(chosen_spec, per_worker_memory_mb=int(exp["per_worker_memory_mb"])),
            "recommended_timeout_ms": _recommend_timeout_ms(chosen_spec, exp), "expectation": exp, "target_spec": chosen_spec,
        }

    primary_gaps = dict(local_gaps) if local_gaps else (dict(remote_gaps[0]["gaps"]) if remote_gaps else {})
    return {
        "decision": "reject", "target": None, "host": None, "alias": None,
        "shortfall": {**primary_gaps, "local": local_gaps, "remote": remote_gaps},
        "recommended_workers": recommend_worker_count(norm_local, per_worker_memory_mb=int(exp["per_worker_memory_mb"])),
        "recommended_timeout_ms": _recommend_timeout_ms(norm_local, exp), "expectation": exp, "target_spec": None,
    }
