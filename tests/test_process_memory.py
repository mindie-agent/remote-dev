"""Tier 0/1 memory-controller contracts; kernel enforcement needs native acceptance."""
from __future__ import annotations

import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from remote_dev.core import _posix_owner, cgroup_memory, host_spec, local_process


def test_cgroup_capacity_uses_tightest_ancestor_headroom_and_cpu_quota(monkeypatch):
    values = {
        '/cg/worker/cpu.max': 'max 100000',
        '/cg/cpu.max': '250000 100000',
        '/cg/worker/memory.max': str(12 * 1024**3),
        '/cg/worker/memory.current': str(2 * 1024**3),
        '/cg/memory.max': str(16 * 1024**3),
        '/cg/memory.current': str(13 * 1024**3),
    }
    monkeypatch.setattr(Path, 'read_text', lambda path, **_: values[path.as_posix()])
    assert cgroup_memory.capacity_limits((Path('/cg/worker'), Path('/cg'))) == (2.5, 3072)
    values['/cg/memory.current'] = str(17 * 1024**3)
    assert cgroup_memory.capacity_limits((Path('/cg/worker'), Path('/cg'))) == (2.5, 0)


def test_cgroup_discovery_resolves_a_subtree_mount(monkeypatch):
    values = {
        '/proc/self/cgroup': '0::/user.slice/session.scope/job\n',
        '/proc/self/mountinfo': '31 26 0:28 /user.slice/session.scope /sys/fs/cgroup rw - cgroup2 cgroup rw\n',
    }
    monkeypatch.setattr(Path, 'read_text', lambda path, **_: values[path.as_posix()])
    assert cgroup_memory.current_cgroup() == (Path('/sys/fs/cgroup/job'), Path('/sys/fs/cgroup'))


def test_local_capacity_caps_host_observation_with_cgroup_limits(monkeypatch):
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(total=32768 * 1024**2, available=24000 * 1024**2)))
    monkeypatch.setattr(host_spec, 'capacity_limits', lambda: (2.5, 1536))
    monkeypatch.setattr(os, 'cpu_count', lambda: 16)
    monkeypatch.setattr(os, 'getloadavg', lambda: (0, 0, 0), raising=False)
    spec = host_spec.probe_local_hardware_spec(desktop_reserve_ratio=0, min_desktop_reserve_mb=0)
    assert spec['usable_cpu_cores'] == 2.5
    assert spec['mem_available_mb'] == spec['mem_usable_mb'] == 1536
    assert host_spec.recommend_worker_count(spec, per_worker_memory_mb=1024) == 1
    assert host_spec.recommend_worker_count(spec, per_worker_memory_mb=2048) == 0


def test_large_host_reserve_is_not_deducted_twice_from_small_container(monkeypatch):
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(
        virtual_memory=lambda: SimpleNamespace(total=1048576 * 1024**2, available=800000 * 1024**2)))
    monkeypatch.setattr(host_spec, 'capacity_limits', lambda: (4, 4096))
    monkeypatch.setattr(os, 'cpu_count', lambda: 192)
    monkeypatch.setattr(os, 'getloadavg', lambda: (16, 0, 0), raising=False)
    spec = host_spec.probe_local_hardware_spec()
    assert spec['usable_cpu_cores'] == 4
    assert spec['mem_available_mb'] == spec['mem_usable_mb'] == 4096


def test_placement_rejects_when_declared_working_set_fits_but_one_worker_does_not():
    decision = host_spec.evaluate_placement(
        {'memory_mb': 256, 'per_worker_memory_mb': 1024},
        local_spec={'cpu_cores': 8, 'usable_cpu_cores': 8, 'mem_total_mb': 4096,
                    'mem_available_mb': 512, 'mem_usable_mb': 512})
    assert decision['decision'] == 'reject'
    assert decision['shortfall']['workers'] == {'required': 1, 'available': 0}
    assert decision['recommended_workers'] == 0


def test_cgroup_requires_a_delegated_controller_and_preserves_timeouts(monkeypatch):
    monkeypatch.setattr(cgroup_memory, 'current_cgroup', lambda: (Path('/cg'), Path('/cg')))
    def denied(_path):
        raise PermissionError(errno.EACCES, 'cgroup delegation denied')
    monkeypatch.setattr(Path, 'mkdir', denied)
    group, reason = cgroup_memory.MemoryCgroup.create(512)
    assert group is None
    assert 'delegation denied' in reason
    def timed_out(_path):
        raise TimeoutError('kernel write timed out')
    monkeypatch.setattr(Path, 'mkdir', timed_out)
    with pytest.raises(TimeoutError, match='kernel write timed out'):
        cgroup_memory.MemoryCgroup.create(512)


def test_memory_controller_is_written_and_read_back_before_becoming_usable(monkeypatch):
    monkeypatch.setattr(cgroup_memory, 'current_cgroup', lambda: (Path('/cg'), Path('/cg')))
    calls = []
    class ControlFile(io.StringIO):
        def write(self, value):
            calls.append(('memory.max', value))
            return super().write(value)
    monkeypatch.setattr(Path, 'mkdir', lambda path: calls.append(('create', path)))
    monkeypatch.setattr(Path, 'open', lambda path, mode: ControlFile())
    monkeypatch.setattr(Path, 'read_text', lambda path: calls[-1][1])
    group, reason = cgroup_memory.MemoryCgroup.create(512)
    assert reason is None
    assert group.max_memory_mb == 512
    assert calls[0] == ('create', group.path)
    assert calls[1] == ('memory.max', str(512 * 1024**2))


@pytest.mark.skipif(os.name != 'posix', reason='POSIX startup contract')
def test_posix_owner_joins_memory_cgroup_before_launch_and_confirms_membership(tmp_path, monkeypatch):
    members = tmp_path / 'cgroup.procs'
    members.touch()
    owner_read, owner_write = os.pipe()
    read_receipt, notify = os.pipe()
    monkeypatch.setattr(sys, 'argv', ['owner', str(owner_read), str(notify),
                        json.dumps({'pass_fds': [], 'memory_cgroup': str(tmp_path)}), 'target'])
    monkeypatch.setattr(os, 'getpgrp', os.getpid)
    monkeypatch.setattr(os, 'fork', lambda: 999)
    def launch(command, **_):
        assert command == ['target']
        assert members.read_text() == str(os.getpid())
        return SimpleNamespace(wait=lambda: None, returncode=0)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    try:
        assert _posix_owner.main() == 0
        assert json.loads(os.read(read_receipt, 1024)) == {
            'started': True, 'memory_cgroup': str(tmp_path)}
    finally:
        os.close(owner_write)
        os.close(read_receipt)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX owner capabilities')
def test_requested_limit_without_cgroup_permission_is_observed_only(monkeypatch):
    monkeypatch.setattr(cgroup_memory.MemoryCgroup, 'create', lambda _: (None, 'cgroup is read-only'))
    with local_process.OwnedProcess([sys.executable, '-c', 'pass'], max_memory_mb=128,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE) as owner:
        owner.process.communicate(timeout=3)
        assert owner.memory_limit_mode == 'observed-only'
        assert owner.memory_limit == {'mode': 'observed-only', 'requested_mb': 128,
                                     'enforced_mb': None, 'mechanism': None,
                                     'reason': 'cgroup is read-only'}


@pytest.mark.skipif(os.name != 'posix', reason='POSIX startup contract')
def test_cgroup_join_failure_never_launches_user_code(tmp_path, monkeypatch):
    owner_read, owner_write = os.pipe()
    read_receipt, notify = os.pipe()
    monkeypatch.setattr(sys, 'argv', ['owner', str(owner_read), str(notify),
                        json.dumps({'pass_fds': [], 'memory_cgroup': str(tmp_path)}), 'target'])
    monkeypatch.setattr(os, 'getpgrp', os.getpid)
    def denied(path, mode):
        raise PermissionError(errno.EACCES, 'cgroup migration denied')
    monkeypatch.setattr(Path, 'open', denied)
    monkeypatch.setattr(os, 'fork', lambda: pytest.fail('forked before memory ownership succeeded'))
    try:
        assert _posix_owner.main() == 125
        receipt = json.loads(os.read(read_receipt, 1024))
        assert receipt['started'] is False
        assert 'migration denied' in receipt['memory_error']
    finally:
        os.close(owner_read)
        os.close(owner_write)
        os.close(read_receipt)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX startup receipt')
def test_startup_timeout_drains_the_spawned_owner(monkeypatch):
    launched = []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', launch)
    monkeypatch.setattr(local_process.select, 'select', lambda *_: ([], [], []))
    with pytest.raises(TimeoutError, match='startup receipt timed out'):
        local_process.OwnedProcess([sys.executable, '-c', 'import time;time.sleep(10)'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert len(launched) == 1
    assert launched[0].returncode is not None
    assert launched[0].stdout.closed and launched[0].stderr.closed


def test_hard_limit_report_requires_successful_owner_start(monkeypatch):
    group = SimpleNamespace(path=Path('/cg/owned'))
    monkeypatch.setattr(cgroup_memory.MemoryCgroup, 'create', lambda _: (group, None))
    monkeypatch.setattr(local_process.os, 'name', 'posix')
    def start(owner, argv, options):
        assert owner.memory_limit_mode == 'observed-only'
        assert owner._memory_cgroup is group
        owner.process = SimpleNamespace(returncode=0)
    monkeypatch.setattr(local_process.OwnedProcess, '_spawn_posix', start)
    owner = local_process.OwnedProcess(['target'], max_memory_mb=256)
    assert owner.memory_limit_mode == 'hard-limit'
    assert owner.memory_limit['enforced_mb'] == 256
    assert owner.memory_limit['mechanism'] == 'cgroup-v2-memory.max'


def test_cleanup_failure_retains_exit_status_and_can_be_retried():
    calls = []
    def close(**_):
        calls.append('close')
        if len(calls) == 1:
            raise TimeoutError('group still populated')
    owner = local_process.OwnedProcess.__new__(local_process.OwnedProcess)
    owner._closed = True
    owner._memory_cgroup = SimpleNamespace(close=close)
    owner.memory_limit = {'mode': 'hard-limit'}
    owner.process = SimpleNamespace(returncode=17)
    with pytest.raises(TimeoutError, match='still populated'):
        owner.stop(timeout=.01)
    assert owner.memory_limit['exit_code'] == 17
    assert owner.stop(timeout=.01) == 17
    assert 'cleanup_error' not in owner.memory_limit
    assert calls == ['close', 'close']
