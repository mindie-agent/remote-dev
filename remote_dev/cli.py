from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from remote_dev import package_version
from remote_dev.core.endpoint import EndpointError, has_selector, resolve_endpoint
from remote_dev.mcp.schemas import TOOL_SCHEMAS, normalize_arguments
from remote_dev.result import make_result
from remote_dev.observability import observed_tool
from remote_dev.core.errors import error_details

TOOL_NAMES = tuple(name.removeprefix("remote.") for name in TOOL_SCHEMAS)


def configure_cli_streams() -> None:
    """The JSON CLI protocol uses UTF-8, including redirected Windows pipes."""
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")


def add_endpoint_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--user", default=None)
    parser.add_argument("--container", help="Existing Docker container name or ID on the SSH host; results return its fixed full ID.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--cwd", "--workdir", dest="cwd", default=None, help="Working directory (--workdir is the Codex exec_command habit).")
    parser.add_argument("--runtime-env", dest="runtime_env", action="store_true", default=None)
    parser.add_argument("--no-runtime-env", dest="runtime_env", action="store_false")
    parser.add_argument("--runtime-env-file", dest="runtime_env_file", help="Remote profile script sourced before commands when runtime env is enabled.")
    parser.add_argument("--identity-file")
    parser.add_argument("--connect-timeout-ms", type=int)
    parser.add_argument("--alias", help="Endpoint alias from the endpoint alias files.")
    parser.add_argument(
        "--selector",
        action="append",
        metavar="KEY=VALUE",
        help="Extra selector field for a registered endpoint resolver (repeatable), e.g. --selector lab=gpu-1.",
    )


def parse_selectors(items: list[str] | None) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"bad --selector item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError(f"bad --selector item {item!r}; empty key")
        selectors[key] = value
    return selectors


def endpoint_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "host",
        "port",
        "user",
        "container",
        "root",
        "cwd",
        "runtime_env",
        "runtime_env_file",
        "identity_file",
        "connect_timeout_ms",
        "alias",
    ):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    payload.update(parse_selectors(getattr(args, "selector", None)))
    return payload


def parse_env(items: list[str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"bad --env item {item!r}; expected KEY=VALUE")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def print_payload(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload.get("result", {}).get("outcome") in {"success", "cancelled"} else 1


def error_payload(tool: str, *, outcome: str, status: str, error: str, exception=None) -> dict[str, Any]:
    result = make_result(
        tool=f"remote.{tool}",
        target={"kind": "unresolved"},
        outcome=outcome,  # type: ignore[arg-type]
        status=status,
        summary=f"remote.{tool} {status}.",
        preview={"stderr": error[-4000:]},
        extra={"error": error, **({"error_details": error_details(exception)} if exception is not None else {})},
    )
    operation = getattr(exception, "_diagnostic_operation", None)
    if operation is not None:
        summary = operation.summary()
        result.update(invocation_id=summary["operation_id"], started_at=summary["started_at"],
                      duration_ms=summary["duration_ms"], diagnostics=summary)
    return {"text": result["summary"] + "\n" + error + "\n", "result": result}


def build_parser(tool: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"remote-dev {tool.replace('_', '-')}")
    add_endpoint_args(parser)
    parser.add_argument("--input-json", help="Read complete tool arguments from a JSON file, or '-' for stdin.")
    parser.add_argument("--timeout-ms", type=int, default=None if tool == "bash" else 120000)
    parser.add_argument("--client-context-id")
    if tool == "bash":
        parser.add_argument("--command", "--cmd", dest="command", required=False, help="Shell command (--cmd is the Codex exec_command habit).")
        parser.add_argument("--description")
        parser.add_argument("--yield-time-ms", type=int, default=None, help="Wait for new output or completion, default 10000 ms, then return a session id.")
        parser.add_argument("--max-output-tokens", type=int, default=None, help="Cap returned output, 4 UTF-8 bytes per token across both output projections; metadata separate.")
        parser.add_argument("--tty", action="store_true", help="Allocate a real remote PTY (24 rows, 80 columns); Ctrl-C signals the foreground group.")
        parser.add_argument("--env", action="append")
    elif tool == "read":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--offset", type=int, default=1)
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--allow-symlink", action="store_true")
        parser.add_argument("--no-verify-content", dest="verify_content", action="store_false", default=True, help="Read a bounded log window without computing a full content hash or updating the edit guard.")
    elif tool == "ls":
        parser.add_argument("--path")
        parser.add_argument("--limit", type=int, default=200)
        parser.add_argument("--all", action="store_true")
    elif tool == "write":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--content")
        parser.add_argument("--content-file")
        parser.add_argument("--overwrite", action="store_true")
        parser.add_argument("--append", action="store_true", help="Append content at end of file instead of replacing it (mutually exclusive with --overwrite).")
        parser.add_argument("--create-dirs", action="store_true")
    elif tool == "edit":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--old-string")
        parser.add_argument("--new-string")
        parser.add_argument("--replace-all", action="store_true")
    elif tool == "multi_edit":
        parser.add_argument("--file-path", required=False)
        parser.add_argument("--edits-json")
    elif tool == "glob":
        parser.add_argument("--pattern", required=False)
        parser.add_argument("--path")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument(
            "--respect-gitignore",
            action="store_true",
            help="Omit paths ignored by .gitignore (git check-ignore, or in-process rules).",
        )
    elif tool == "grep":
        parser.add_argument("--pattern", required=False)
        parser.add_argument("--path")
        parser.add_argument("--glob")
        parser.add_argument("--type")
        parser.add_argument("--output-mode", default="files_with_matches", choices=["files_with_matches", "content", "count", "count_matches"])
        parser.add_argument("--multiline", action="store_true")
        parser.add_argument("--ignore-case", dest="case_insensitive", action="store_true")
        parser.add_argument("--context-lines", type=int, default=0)
        parser.add_argument("--before-context", type=int, default=0)
        parser.add_argument("--after-context", type=int, default=0)
        parser.add_argument("--no-line-numbers", dest="line_numbers", action="store_false", default=None)
        parser.add_argument("--include-ignored", action="store_true", help="Also search hidden and .gitignore-d paths (grep fallback approximates; see docs).")
        parser.add_argument("--offset", type=int, default=0)
        parser.add_argument("--head-limit", type=int, default=None, help="Alias of --limit.")
        parser.add_argument("--limit", type=int, default=100)
    elif tool == "apply_patch":
        parser.add_argument("--patch")
        parser.add_argument("--patch-file")
        parser.add_argument("--command")
    elif tool in {"job_status", "job_tail", "job_stop", "job_stdin"}:
        parser.add_argument("--job-id", "--session-id", dest="job_id", required=False)
        if tool == "job_tail":
            parser.add_argument("--lines", type=int, default=80)
            parser.add_argument("--stream", default="both", choices=["stdout", "stderr", "both"])
        if tool == "job_stdin":
            parser.add_argument("--chars", help="Bytes to write to the job's stdin; omit to only poll new output.")
            parser.add_argument("--eof", action="store_true", help="Close the job's stdin after writing.")
            parser.add_argument("--yield-time-ms", type=int, default=None, help="Poll up to this long before returning new output.")
            parser.add_argument("--max-output-tokens", type=int, default=None, help="Cap returned new output, 4 UTF-8 bytes per token across both output projections; metadata separate.")
        if tool == "job_stop":
            parser.add_argument("--force", action="store_true")
    elif tool in {"artifact_manifest", "artifact_pull", "artifact_push"}:
        parser.add_argument("--remote-path", required=False)
        if tool == "artifact_pull":
            parser.add_argument("--local-dir")
        if tool == "artifact_push":
            parser.add_argument("--local-path")
    elif tool == "context_snapshot":
        parser.add_argument("--no-live-probe", action="store_true")
    elif tool == "probe":
        parser.add_argument("--diagnose-connection", action="store_true")
        parser.add_argument("--module", dest="modules", action="append", help="Explicit module to import during probe; omitted means no module imports.")
    return parser


def load_input_json(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("--input-json must be a JSON object")
    return data


@observed_tool(lambda tool, args: "remote." + tool)
def run_tool(tool: str, args: argparse.Namespace) -> dict[str, Any]:
    data = {key: value for key, value in vars(args).items() if value is not None}
    data.update(endpoint_payload(args))
    if hasattr(args, "env"):
        data["env"] = parse_env(args.env)
    for filename_key, value_key in (("content_file", "content"), ("patch_file", "patch")):
        if data.get(filename_key) and data.get(value_key) is None:
            data[value_key] = Path(data[filename_key]).read_text(encoding="utf-8")
    if data.get("edits_json"):
        data["edits"] = json.loads(data["edits_json"])
    if data.get("head_limit") is not None:
        data["limit"] = data["head_limit"]
    if "no_live_probe" in data:
        data["live_probe"] = not data.pop("no_live_probe")
    # These flags are consumed by the CLI itself, not remote tool arguments.
    for key in ("input_json", "selector", "long_stream", "content_file", "patch_file", "edits_json", "head_limit"):
        data.pop(key, None)
    # JSON aliases override CLI defaults before the canonical dispatcher sees
    # them; otherwise an argparse default could shadow an explicit alias.
    explicit = normalize_arguments(f"remote.{tool}", load_input_json(args.input_json)) if args.input_json else {}
    data.update(explicit)
    if tool not in {"job_status", "job_tail", "job_stdin", "job_stop"} or has_selector(data):
        resolve_endpoint(data)
    from remote_dev.mcp.tools import call_tool
    return call_tool(f"remote.{tool}", data)


def run_tool_main(tool: str, argv: list[str] | None = None) -> int:
    configure_cli_streams()
    parser = build_parser(tool)
    args = parser.parse_args(argv)
    try:
        return print_payload(run_tool(tool, args))
    except EndpointError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="endpoint_required", error=str(exc), exception=exc))
    except FileNotFoundError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="not_found", error=str(exc), exception=exc))
    except ValueError as exc:
        return print_payload(error_payload(tool, outcome="needs_input", status="invalid_input", error=str(exc), exception=exc))
    except Exception as exc:  # noqa: BLE001
        return print_payload(error_payload(tool, outcome="failed", status="exception", error=f"{type(exc).__name__}: {exc}", exception=exc))


def _status_main() -> int:
    from remote_dev.core.state_store import state_root

    payload = {
        "name": "remote-dev",
        "version": package_version(),
        "package": "remote_dev",
        "state_dir": str(state_root()),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _build_root_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="remote-dev",
        description="SSH-backed remote twins of local agent tools.",
    )
    parser.add_argument("--version", action="version", version=package_version())
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("server", help="Run the MCP server on stdio.")
    sub.add_parser("status", help="Print package version and local runtime facts.")
    sub.add_parser("validate", help="Run local contract gates and optional live endpoint checks.")
    for name in TOOL_NAMES:
        sub.add_parser(name.replace("_", "-"), help=f"Invoke remote.{name}.")
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_cli_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in {"-h", "--help"}:
        _build_root_parser().print_help()
        return 0
    if argv[0] in {"-V", "--version"}:
        print(package_version())
        return 0
    command = argv[0]
    rest = argv[1:]
    if command == "server":
        from remote_dev.mcp.server import main as server_main

        return server_main()
    if command == "status":
        return _status_main()
    if command == "validate":
        from remote_dev.tools.validate_remote_dev_scaffold import main as validate_main

        return validate_main(rest)
    tool = command.replace("-", "_")
    if tool in TOOL_NAMES:
        return run_tool_main(tool, rest)
    _build_root_parser().print_help()
    print(f"\nunknown command: {command}", file=sys.stderr)
    return 2
