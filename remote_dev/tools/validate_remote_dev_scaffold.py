#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


from remote_dev.cli import TOOL_NAMES, build_parser
from remote_dev.core.endpoint import DEFAULT_CWD, DEFAULT_ROOT, has_selector, selector_fields
from remote_dev.mcp.schemas import ALIASES, ENDPOINT_PROPS, TOOL_SCHEMAS
from remote_dev.mcp.tools import call_tool, list_resources, list_tools, read_resource


def progress(message: str) -> None:
    print(f"__REMOTE_DEV_VALIDATE_PROGRESS__={message}", file=sys.stderr, flush=True)


def run_command(name: str, argv: list[str]) -> dict[str, Any]:
    started = time.monotonic()
    proc = subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "name": name,
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def local_checks() -> list[dict[str, Any]]:
    import remote_dev

    package_dir = str(Path(remote_dev.__file__).resolve().parent)
    commands = [
        ("compileall", [sys.executable, "-m", "compileall", "-q", package_dir]),
    ]
    results = []
    for name, argv in commands:
        progress(f"local:{name}")
        results.append(run_command(name, argv))
    return results


def mcp_and_burden_checks() -> dict[str, Any]:
    tools = list_tools()
    names = [tool["name"] for tool in tools]
    expected_tools = {name.removeprefix("remote.") for name in TOOL_SCHEMAS}
    endpoint_fields = set(ENDPOINT_PROPS)
    required_by_tool = {name: set(schema.get("required", [])) for name, schema in TOOL_SCHEMAS.items()}
    endpoint_required = {
        name: sorted(required & endpoint_fields)
        for name, required in required_by_tool.items()
        if required & endpoint_fields
    }
    own_required_counts = {
        name: len(required - endpoint_fields)
        for name, required in required_by_tool.items()
    }
    has_native_shape_names = all(name.startswith("remote_") for name in names)
    all_have_schema = all("inputSchema" in tool for tool in tools)
    resources = list_resources()
    status = "ok"
    failures: list[str] = []
    if set(names) != set(ALIASES) or len(names) != len(set(names)):
        failures.append("tools/list does not match unique portable aliases")
    if set(TOOL_NAMES) != expected_tools:
        failures.append("CLI tool subcommands do not match TOOL_SCHEMAS")
    else:
        for name in TOOL_NAMES:
            build_parser(name)
    if endpoint_required:
        failures.append("endpoint fields should not be top-level required by tool schemas")
    if not has_native_shape_names:
        failures.append("wire tool names should retain remote_<native-tool> shape")
    if not all_have_schema:
        failures.append("every tool must expose inputSchema")
    if failures:
        status = "failed"
    return {
        "status": status,
        "tool_count": len(tools),
        "tools": names,
        "resource_count": len(resources),
        "cli_wrapper_count": len(TOOL_NAMES),
        "endpoint_required": endpoint_required,
        "own_required_counts": own_required_counts,
        "max_own_required_fields": max(own_required_counts.values()) if own_required_counts else 0,
        "failures": failures,
    }


def require_outcome(name: str, payload: dict[str, Any], *, statuses: set[str] | None = None, outcomes: set[str] | None = None) -> dict[str, Any]:
    result = payload.get("result", {})
    outcome = str(result.get("outcome"))
    status = str(result.get("status"))
    if outcomes is None:
        outcomes = {"success"}
    if outcome not in outcomes or (statuses is not None and status not in statuses):
        raise RuntimeError(f"{name} returned outcome={outcome} status={status}: {payload.get('text', '')[:1000]}")
    return {
        "name": name,
        "outcome": outcome,
        "status": status,
        "duration_ms": result.get("duration_ms"),
        "summary": result.get("summary"),
    }


def endpoint_payload(args: argparse.Namespace) -> dict[str, Any]:
    cwd = args.cwd
    if cwd is None and args.root != "/":
        cwd = args.root
    payload = {
        "host": args.host,
        "port": args.port,
        "user": args.user,
        "root": args.root,
        "cwd": cwd,
        "connect_timeout_ms": args.connect_timeout_ms,
        "runtime_env_file": args.runtime_env_file,
        "alias": args.alias,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    for item in args.selector or []:
        if "=" not in item:
            raise SystemExit(f"bad --selector item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        payload[key] = value
    return payload


def finish_session(payload, endpoint, timeout_ms):
    """Validate the actual execute/poll interface, retaining a bounded preview."""
    deadline = time.monotonic() + timeout_ms / 1000 + 45
    output = {"stdout": "", "stderr": ""}
    truncated = False
    while True:
        result = payload["result"]
        for name in output:
            text = str(result.get("preview", {}).get(name) or "")
            output[name] = (output[name] + text)[:8192]
            truncated = truncated or bool(result.get("bytes_remaining", {}).get(name))
        if not result.get("session_id") or result.get("status") in {"job_start_failed", "cwd_not_found", "cwd_outside_root", "cwd_not_directory", "job_id_exists"}:
            break
        if time.monotonic() >= deadline:
            call_tool("remote.job_stop", {**endpoint, "job_id": result["job_id"], "force": True})
            raise RuntimeError("validation command did not finish within its deadline")
        payload = call_tool("remote.job_stdin", {**endpoint, "job_id": result["job_id"], "yield_time_ms": 1000})
    result["preview"] = output
    result["output_truncated"] = truncated
    payload["text"] = result["summary"] + "\n" + output["stdout"] + output["stderr"]
    return payload


def completed_bash(arguments):
    return finish_session(call_tool("remote.bash", arguments), arguments, int(arguments.get("timeout_ms") or 30000))


def run_parallel_worker(endpoint: dict[str, Any], scratch: str, index: int, timeout_ms: int) -> dict[str, Any]:
    worker_dir = f"{scratch}/parallel-{index}"
    file_path = f"{worker_dir}/task.txt"
    command = f"mkdir -p {worker_dir!r} && printf 'worker-{index}\\ninitial\\n' > {file_path!r}"
    checks = [
        require_outcome(f"parallel_{index}_create", completed_bash({**endpoint, "command": command, "timeout_ms": timeout_ms})),
        require_outcome(f"parallel_{index}_read", call_tool("remote.read", {**endpoint, "file_path": file_path, "timeout_ms": timeout_ms}), statuses={"ok"}),
        require_outcome(
            f"parallel_{index}_edit",
            call_tool(
                "remote.edit",
                {
                    **endpoint,
                    "file_path": file_path,
                    "old_string": "initial",
                    "new_string": f"done-{index}",
                    "timeout_ms": timeout_ms,
                },
            ),
            statuses={"edited"},
        ),
    ]
    return {"worker": index, "status": "ok", "checks": checks}


def live_endpoint_checks(args: argparse.Namespace) -> dict[str, Any]:
    endpoint = endpoint_payload(args)
    if not has_selector(endpoint):
        return {"status": "skipped", "reason": f"no endpoint selector was provided (known selector fields: {', '.join(selector_fields())})"}
    timeout_ms = args.timeout_ms
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    scratch_root = (endpoint.get("cwd") or DEFAULT_CWD or endpoint.get("root") or DEFAULT_ROOT).rstrip("/")
    scratch = f"{scratch_root}/.remote-dev/validation/{stamp}"
    narrow_endpoint = {**endpoint, "root": scratch, "cwd": scratch}
    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    try:
        progress("remote:probe")
        checks.append(require_outcome("probe", call_tool("remote.probe", {**endpoint, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("context_snapshot", call_tool("remote.context_snapshot", {**endpoint, "timeout_ms": timeout_ms, "live_probe": True})))
        checks.append(require_outcome("cwd_blocked", completed_bash({**narrow_endpoint, "cwd": "/tmp", "command": "pwd", "timeout_ms": timeout_ms}), outcomes={"blocked"}, statuses={"cwd_outside_root"}))
        checks.append(require_outcome("cwd_not_found", completed_bash({**endpoint, "cwd": f"{scratch}/missing", "command": "pwd", "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"cwd_not_found"}))
        checks.append(require_outcome("nonzero_exit", completed_bash({**endpoint, "command": "exit 7", "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"failed"}))
        checks.append(require_outcome("timeout", completed_bash({**endpoint, "command": "sleep 2", "timeout_ms": 500}), outcomes={"timeout"}, statuses={"timeout"}))

        setup = f"mkdir -p {scratch!r} && printf 'alpha\\nbeta\\n' > {scratch!r}/file.txt"
        checks.append(require_outcome("bash_create", completed_bash({**endpoint, "command": setup, "timeout_ms": timeout_ms})))
        big = completed_bash({**endpoint, "command": "python3 - <<'PY'\nprint('x' * 50000)\nPY", "timeout_ms": timeout_ms})
        checks.append(require_outcome("large_output_preview", big))
        if not big.get("result", {}).get("output_truncated"):
            raise RuntimeError("large_output_preview did not mark stdout as truncated")
        checks.append(require_outcome("ls", call_tool("remote.ls", {**endpoint, "path": scratch, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("read", call_tool("remote.read", {**endpoint, "file_path": f"{scratch}/file.txt", "offset": 1, "limit": 10, "timeout_ms": timeout_ms}), statuses={"ok"}))
        checks.append(require_outcome("directory_read_rejected", call_tool("remote.read", {**endpoint, "file_path": scratch, "timeout_ms": timeout_ms}), outcomes={"failed"}, statuses={"is_directory"}))
        checks.append(require_outcome("edit", call_tool("remote.edit", {**endpoint, "file_path": f"{scratch}/file.txt", "old_string": "beta", "new_string": "gamma", "timeout_ms": timeout_ms}), statuses={"edited"}))
        checks.append(require_outcome("read_after_edit", call_tool("remote.read", {**endpoint, "file_path": f"{scratch}/file.txt", "timeout_ms": timeout_ms}), statuses={"ok"}))
        checks.append(require_outcome("write", call_tool("remote.write", {**endpoint, "file_path": f"{scratch}/write.txt", "content": "created\\n", "create_dirs": True, "timeout_ms": timeout_ms}), statuses={"written"}))
        checks.append(require_outcome("glob", call_tool("remote.glob", {**endpoint, "pattern": "*.txt", "path": scratch, "timeout_ms": timeout_ms})))
        checks.append(require_outcome("grep_content", call_tool("remote.grep", {**endpoint, "pattern": "gamma", "path": scratch, "glob": "*.txt", "output_mode": "content", "timeout_ms": timeout_ms})))
        patch = f"""*** Begin Patch
*** Add File: {scratch}/patch-old.txt
+old
*** Update File: {scratch}/patch-old.txt
*** Move to: {scratch}/patch-new.txt
@@
-old
+new
*** End of File
*** End Patch
"""
        checks.append(require_outcome("apply_patch", call_tool("remote.apply_patch", {**endpoint, "patch": patch, "timeout_ms": timeout_ms}), statuses={"applied"}))
        checks.append(require_outcome("artifact_manifest", call_tool("remote.artifact_manifest", {**endpoint, "remote_path": scratch, "timeout_ms": timeout_ms}), statuses={"ok"}))
        with tempfile.TemporaryDirectory() as tmp:
            checks.append(require_outcome("artifact_pull", call_tool("remote.artifact_pull", {**endpoint, "remote_path": f"{scratch}/file.txt", "local_dir": tmp, "timeout_ms": timeout_ms}), statuses={"ok"}))
            local_push = Path(tmp) / "push.txt"
            local_push.write_text("pushed\n", encoding="utf-8")
            checks.append(require_outcome("artifact_push", call_tool("remote.artifact_push", {**endpoint, "local_path": str(local_push), "remote_path": f"{scratch}/pushed.txt", "timeout_ms": timeout_ms}), statuses={"ok"}))

        job_payload = completed_bash({**endpoint, "command": "printf 'job-out\\n'; printf 'job-err\\n' >&2", "cwd": scratch, "timeout_ms": timeout_ms})
        checks.append(require_outcome("background_job_start", job_payload, statuses={"succeeded"}))
        job_id = job_payload["result"]["job_id"]
        time.sleep(2)
        checks.append(require_outcome("job_status", call_tool("remote.job_status", {**endpoint, "job_id": job_id, "timeout_ms": timeout_ms}), statuses={"succeeded"}))
        checks.append(require_outcome("job_tail", call_tool("remote.job_tail", {**endpoint, "job_id": job_id, "lines": 20, "timeout_ms": timeout_ms})))

        interactive_payload = call_tool("remote.bash", {**endpoint, "command": "read -r line; printf 'got:%s\\n' \"$line\"", "cwd": scratch, "yield_time_ms": 1500, "timeout_ms": timeout_ms})
        checks.append(require_outcome("interactive_job_start", interactive_payload))
        interactive_id = interactive_payload["result"]["job_id"]
        stdin_payload = call_tool("remote.job_stdin", {**endpoint, "job_id": interactive_id, "chars": "hello-stdin\x0a", "eof": True, "yield_time_ms": 5000, "timeout_ms": timeout_ms})
        stdin_payload = finish_session(stdin_payload, endpoint, timeout_ms)
        checks.append(require_outcome("job_stdin", stdin_payload))
        if "got:hello-stdin" not in json.dumps(stdin_payload.get("result", {})):
            raise RuntimeError("job_stdin did not surface the echoed stdin line")
        replay = call_tool("remote.job_stdin", {**endpoint, "job_id": interactive_id, "timeout_ms": timeout_ms})
        checks.append(require_outcome("job_stdin_poll_after_eof", replay))
        if "got:hello-stdin" in json.dumps(replay.get("result", {})):
            raise RuntimeError("job_stdin replayed earlier output; incremental cursors are broken")
        checks.append(require_outcome("interactive_job_done", call_tool("remote.job_status", {**endpoint, "job_id": interactive_id, "timeout_ms": timeout_ms}), statuses={"succeeded"}))
        tty_payload = completed_bash({**endpoint, "command": "true", "tty": True, "timeout_ms": timeout_ms})
        checks.append(require_outcome("bash_tty", tty_payload, statuses={"succeeded"}))
        noninteractive = call_tool("remote.job_stdin", {**endpoint, "job_id": job_id, "chars": "x", "timeout_ms": timeout_ms})
        checks.append(require_outcome("job_stdin_rejects_plain_job", noninteractive, outcomes={"failed"}, statuses={"stdin_rejected"}))
        resource_uris = {item["uri"] for item in list_resources()}
        stdout_uri = next((uri for uri in resource_uris if uri.endswith(f"/job/{job_id}/stdout")), None)
        if not stdout_uri:
            raise RuntimeError(f"MCP job stdout resource missing for {job_id}")
        stdout_resource = read_resource(stdout_uri)
        if "job-out" not in stdout_resource.get("text", ""):
            raise RuntimeError("MCP job stdout resource did not include remote log content")
        checks.append({"name": "mcp_job_stdout_resource", "outcome": "success", "status": "ok"})
        artifact_resource_count = len([uri for uri in resource_uris if "/artifacts/" in uri and uri.endswith("/manifest")])
        if artifact_resource_count < 1:
            raise RuntimeError("MCP artifact manifest resource was not registered")
        checks.append({"name": "mcp_artifact_manifest_resource", "outcome": "success", "status": "ok", "count": artifact_resource_count})

        progress("remote:parallel_workers")
        parallel_results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel_workers) as executor:
            futures = [executor.submit(run_parallel_worker, endpoint, scratch, index, timeout_ms) for index in range(args.parallel_workers)]
            for future in concurrent.futures.as_completed(futures):
                parallel_results.append(future.result())
        checks.append({"name": "parallel_workers", "outcome": "success", "status": "ok", "workers": sorted(parallel_results, key=lambda item: item["worker"])})
    except Exception as exc:  # noqa: BLE001
        failures.append(str(exc))
    finally:
        cleanup = completed_bash({**endpoint, "command": f"rm -rf {scratch!r}", "timeout_ms": timeout_ms})
        checks.append({
            "name": "cleanup",
            "outcome": cleanup.get("result", {}).get("outcome"),
            "status": cleanup.get("result", {}).get("status"),
        })
    return {
        "status": "ok" if not failures else "failed",
        "target": endpoint,
        "scratch": scratch,
        "parallel_workers": args.parallel_workers,
        "checks": checks,
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the remote-dev contract and optional live endpoint behavior.")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user", default="root")
    parser.add_argument("--root", default="/")
    parser.add_argument("--cwd")
    parser.add_argument("--connect-timeout-ms", type=int, default=10000)
    parser.add_argument("--runtime-env-file", dest="runtime_env_file")
    parser.add_argument("--alias", help="Endpoint alias from the endpoint alias files.")
    parser.add_argument("--selector", action="append", metavar="KEY=VALUE", help="Selector field for a registered endpoint resolver (repeatable).")
    parser.add_argument("--timeout-ms", type=int, default=30000)
    parser.add_argument("--parallel-workers", type=int, default=3)
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--local-only", action="store_true")
    args = parser.parse_args(argv)
    if args.parallel_workers < 1:
        parser.error("--parallel-workers must be >= 1")

    report: dict[str, Any] = {
        "schema_version": "remote-dev.validation.v1",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "local_checks": [],
        "mcp_and_burden": {},
        "live_endpoint": {},
    }
    if not args.skip_local:
        report["local_checks"] = local_checks()
    progress("local:mcp_and_burden")
    report["mcp_and_burden"] = mcp_and_burden_checks()
    if not args.local_only:
        report["live_endpoint"] = live_endpoint_checks(args)
    else:
        report["live_endpoint"] = {"status": "skipped", "reason": "--local-only"}

    failed = False
    failed = failed or any(item.get("status") != "ok" for item in report["local_checks"])
    failed = failed or report["mcp_and_burden"].get("status") != "ok"
    failed = failed or report["live_endpoint"].get("status") == "failed"
    report["status"] = "failed" if failed else "ok"
    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
