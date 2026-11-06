"""Actual distribution metadata and interpreter selection, without NPU imports."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import venv

import pytest

from remote_dev.core import context_snapshot
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.ssh_transport import RemoteCompleted
from remote_dev.mcp.tools import call_tool
from remote_dev.processes.probe import distribution_info


def metadata(site, name, source=None, direct=None):
    info = site / (name + "-1.0.dist-info")
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: 1.0\n", encoding="utf-8", newline="\n")
    if source is not None:
        direct = {"url": source.as_uri(), "dir_info": {"editable": True}}
    if direct is not None:
        (info / "direct_url.json").write_text(json.dumps(direct), encoding="utf-8", newline="\n")
    return info


def test_editable_source_found_from_metadata_without_import(tmp_path, monkeypatch):
    site = tmp_path / "site"
    source = tmp_path / "existing sources"
    package = source / "probe_example"
    package.mkdir(parents=True)
    # Importing the package is a test failure, not simulated hardware behavior.
    (package / "__init__.py").write_text("raise AssertionError('must not import workload')\n", encoding="utf-8")
    metadata(site, "probe_example", source)
    monkeypatch.syspath_prepend(str(source))
    monkeypatch.syspath_prepend(str(site))
    before = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    result = distribution_info("probe-example")
    assert result["status"] == "ok"
    assert result["editable"] is True
    assert result["source_path"] == str(source.resolve())
    assert result["source_exists"] is True
    assert "probe_example" not in sys.modules
    assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before


def test_missing_noneditable_and_invalid_metadata_are_distinct(tmp_path, monkeypatch):
    metadata(tmp_path, "probe_wheel")
    metadata(tmp_path, "probe_broken", direct={"url": "file:///src", "dir_info": []})
    null_info = metadata(tmp_path, "probe_null")
    (null_info / "direct_url.json").write_text("null", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert distribution_info("probe-no-such-package")["status"] == "not_installed"
    wheel = distribution_info("probe-wheel")
    assert wheel["status"] == "ok" and wheel["editable"] is None and wheel["source_path"] is None
    broken = distribution_info("probe-broken")
    assert broken["status"] == "error"
    assert "dir_info" in broken["error"]
    assert distribution_info("probe-null")["status"] == "error"


def test_mcp_probe_uses_selected_interpreter_metadata_and_remote_cwd(tmp_path, monkeypatch):
    environment = tmp_path / "business-python"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    query = subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                           capture_output=True, text=True, check=True, timeout=5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    site = Path(query.stdout.strip())
    source = tmp_path / "original-source"
    source.mkdir()
    metadata(site, "probe_example", source)
    # The Git boundary root can be / while the explicit remote cwd is elsewhere.
    endpoint = Endpoint(host="example.invalid", port=22, root="/", cwd="/existing/source",
                        runtime_env_file="/existing/runtime.sh")
    monkeypatch.setattr("remote_dev.mcp.tools.resolve_endpoint", lambda args: endpoint)
    scripts = []

    def local_probe(ep, script, *, timeout_ms):
        assert ep == endpoint
        assert timeout_ms == 10000
        assert ". /existing/runtime.sh" in script
        assert "cd -- /existing/source" in script
        argv = shlex.split(script[script.index("exec "):])[1:]
        assert argv[0] == "/business/bin/python"
        scripts.append(script)
        # Exercise the shipped worker in a real independent interpreter, using
        # local paths in place of the SSH endpoint's paths.
        payload = json.loads(argv[-1])
        payload.update(root=str(tmp_path), cwd=str(source))
        proc = subprocess.run([str(python), *argv[1:-1], json.dumps(payload)], cwd=source,
                              capture_output=True, text=True, timeout=10,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return RemoteCompleted(proc.returncode, proc.stdout, proc.stderr, timed_out=False)

    monkeypatch.setattr(context_snapshot, "run_script", local_probe)
    unrelated = tmp_path / "unrelated-local-directory"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)
    result = call_tool("remote_probe", {"host": "example.invalid", "port": 22,
                       "python_executable": "/business/bin/python", "distributions": ["probe-example"],
                       "timeout_ms": 10000})["result"]
    assert result["outcome"] == "success"
    summary = result["probe"]["summary"]
    assert Path(summary["python_prefix"]).resolve() == environment.resolve()
    assert summary["distributions"]["probe-example"]["source_path"] == str(source.resolve())
    assert summary["isolated_python"] is True
    assert len(scripts) == 1


@pytest.mark.parametrize("response", [
    RemoteCompleted(7, "", "interpreter not found", timed_out=False),
    RemoteCompleted(0, "not JSON", "", timed_out=False),
    RemoteCompleted(None, "", "deadline", timed_out=True),
])
def test_probe_transport_failures_do_not_look_successful(monkeypatch, response):
    monkeypatch.setattr(context_snapshot, "run_script", lambda *args, **kwargs: response)
    result = context_snapshot.remote_probe(Endpoint(host="example.invalid", port=22))["result"]
    assert result["outcome"] in ("failed", "timeout")
    assert result["snapshot"] is None


@pytest.mark.parametrize("args", [
    {"python_executable": "relative/python"}, {"distributions": "vllm"},
    {"distributions": ["x"] * 33}, {"modules": [""]},
])
def test_invalid_discovery_arguments_fail_before_remote_execution(monkeypatch, args):
    def unexpected(*a, **kw):
        raise AssertionError("must not execute invalid discovery")
    monkeypatch.setattr(context_snapshot, "run_script", unexpected)
    with pytest.raises(ValueError):
        context_snapshot.remote_probe(Endpoint(host="example.invalid", port=22), **args)


def test_failed_probe_leaves_custom_script_route_operational(tmp_path, monkeypatch):
    from remote_dev.core import shell_ops
    endpoint = Endpoint(host="example.invalid", port=22, root="/", cwd="/chosen/new-source")
    monkeypatch.setattr(context_snapshot, "run_script", lambda *a, **kw:
                        RemoteCompleted(3, "", "discovery unavailable", timed_out=False))
    result = context_snapshot.remote_probe(endpoint)["result"]
    assert result["outcome"] == "failed"
    arguments = result["next"]["endpoint_arguments"]
    assert arguments["host"] == endpoint.host and arguments["port"] == endpoint.port
    assert arguments["cwd"] == "/chosen/new-source"

    # Execute an independent user task script at the transport boundary. A
    # probe failure must not register a local lockout or become a prerequisite.
    output = tmp_path / "task-result.txt"
    script = tmp_path / "custom_task.py"
    script.write_text("from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text('completed')\n", encoding="utf-8")
    command = shlex.join([sys.executable, "-I", str(script), str(output)])

    def local_job(ep, *, command, cwd, **kwargs):
        assert ep == endpoint and cwd == arguments["cwd"]
        proc = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=5,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"text": proc.stdout, "result": {"exit_code": proc.returncode}}

    monkeypatch.setattr(shell_ops, "start_remote_job", local_job)
    reply = shell_ops.remote_bash(endpoint, command=command, cwd=arguments["cwd"])
    assert reply["result"]["exit_code"] == 0
    assert output.read_text() == "completed"
