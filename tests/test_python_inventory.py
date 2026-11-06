"""Select using real virtualenv prefixes and distribution metadata only."""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from remote_dev.core.python_discovery import worker_source, inventory_command, compact_shell_script
from remote_dev.processes import python_inventory


@pytest.fixture
def environments(tmp_path):
    rows = []
    for name in ("plain", "accelerator"):
        root = tmp_path / name
        venv.EnvBuilder(with_pip=False).create(root)
        python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        proc = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                              capture_output=True, text=True, timeout=5, check=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows.append((str(python), Path(proc.stdout.strip())))
    return rows


def install_metadata(site, name):
    directory = site / (name.replace("-", "_") + "-1.0.dist-info")
    directory.mkdir()
    (directory / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n", encoding="utf-8", newline="\n")
    # The package body exists to catch accidental imports during selection.
    (site / (name.replace("-", "_") + ".py")).write_text("raise RuntimeError('must not import')\n", encoding="utf-8")


def request(tmp_path):
    return {"root": str(tmp_path), "cwd": str(tmp_path), "probe_source": worker_source("probe.py"),
            "required_distributions": ["fixture-core", "fixture-accelerator"], "timeout_ms": 10000}


def test_only_environment_with_all_dependencies_is_selected(environments, tmp_path, monkeypatch):
    plain, capable = environments
    install_metadata(plain[1], "fixture-core")
    for name in ("fixture-core", "fixture-accelerator"):
        install_metadata(capable[1], name)
    monkeypatch.setattr(python_inventory, "discover", lambda payload: ([plain[0], capable[0]], []))
    result = python_inventory.inventory(request(tmp_path))
    assert result["status"] == "ok"
    assert Path(result["summary"]["python_executable"]) == Path(capable[0])
    assert result["summary"]["interpreters"][0]["missing_distributions"] == ["fixture-accelerator"]
    assert result["summary"]["validation_scope"] == "distribution_metadata"


@pytest.mark.parametrize("bound", [False, True])
def test_multiple_venvs_stay_distinct_and_need_binding(environments, tmp_path, monkeypatch, bound):
    for _, site in environments:
        for name in ("fixture-core", "fixture-accelerator"):
            install_metadata(site, name)
    paths = [row[0] for row in environments]
    monkeypatch.setattr(python_inventory, "discover", lambda payload: (paths, paths[1:] if bound else []))
    result = python_inventory.inventory(request(tmp_path))
    assert len(result["summary"]["interpreters"]) == 2
    assert result["summary"]["interpreter_selection"] == ("selected" if bound else "ambiguous")
    if bound:
        assert Path(result["summary"]["python_executable"]) == Path(paths[1])


def test_explicit_missing_interpreter_never_uses_another(tmp_path):
    payload = {**request(tmp_path), "python_executable": str(tmp_path / "missing-python"),
               "python_candidates": [sys.executable]}
    result = python_inventory.inventory(payload)
    assert result["status"] == "failed"
    assert result["summary"]["interpreter_selection"] == "unavailable"
    assert len(result["summary"]["interpreters"]) == 1


def test_source_inspection_is_forwarded_for_selected_environment(environments, tmp_path, monkeypatch):
    python, site = environments[0]
    install_metadata(site, "fixture-core")
    install_metadata(site, "fixture-accelerator")
    directory = site / "fixture_core-1.0.dist-info"
    chosen_source = tmp_path / "user-selected-new-source"
    chosen_source.mkdir()
    (directory / "direct_url.json").write_text(json.dumps({"url": chosen_source.as_uri(),
                                                         "dir_info": {"editable": True}}), encoding="utf-8")
    monkeypatch.setattr(python_inventory, "discover", lambda payload: ([python], []))
    result = python_inventory.inventory(request(tmp_path))
    assert result["summary"]["distributions"]["fixture-core"]["source_path"] == str(chosen_source.resolve())


def test_rendered_inventory_preserves_explicit_override_and_partial_failure(tmp_path):
    command = inventory_command({"root": str(tmp_path), "cwd": str(tmp_path),
                                 "required_distributions": ["fixture-missing"]})
    argv = shlex.split(command)
    missing = str(tmp_path / "explicit-missing-python")
    proc = subprocess.run([sys.executable, *argv[1:], missing], capture_output=True,
                          text=True, timeout=5, check=True,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result = json.loads(proc.stdout)
    assert result["status"] == "failed"
    assert result["summary"]["interpreters"][0]["requested_python"] == missing
    assert len(result["summary"]["interpreters"]) == 1


@pytest.mark.skipif(os.name == "nt", reason="execvp replaces the remote Linux process; Windows exec does not retain parent pipe semantics")
def test_compact_script_executes_custom_body_with_exact_arguments(tmp_path):
    script = 'printf "%s\\n" "$1" "$2"\n' + "# task code\n" * 1500
    command = compact_shell_script(script)
    argv = shlex.split(command.rsplit(' "$@"', 1)[0])[2:]
    proc = subprocess.run([sys.executable, *argv, "new source path", "line1\nline2"],
                          capture_output=True, text=True, timeout=5,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "new source path\nline1\nline2\n"
