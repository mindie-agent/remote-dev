"""Independent native-contract differential tests (Agent C).

Derived from the installed-artifact baseline in ``native-baseline.json`` and
``evidence/``.  Nothing here is copied from the implementation's constants or
from the implementation's own tests.  The native side is the baseline; the
implementation under test is the clean ``6ed0c70`` checkout on PYTHONPATH.

Run from this directory:
    PYTHONPATH=/Users/maoxx241/code/remote-dev \
        python3 -m unittest -v test_native_contracts

Tier 1 only: no SSH, no live cluster, no model calls.  Worker tests execute the
real standalone ``remote_file_worker.py`` / ``search_worker.py`` scripts and the
real embedded image-sniff script under a temporary local directory.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev
from remote_dev.core.errors import caller_error
from remote_dev.mcp.harness_profiles import (
    CANONICAL_PROFILE,
    HarnessProfileError,
    NATIVE_PROFILES,
    PROFILE_ENV,
    resolve_profile,
)
from remote_dev.mcp.schemas import (
    ALIASES,
    ENDPOINT_PROPS,
    TOOL_SCHEMAS,
    harness_catalog,
)
import remote_dev.mcp.tools as tools
import remote_dev.mcp.native_common as native_common


HERE = Path(__file__).resolve().parent

# The oracle can be supplied explicitly (preferred when this file is copied
# into the implementation's tests/ directory), discovered next to the test or
# in ../baseline, and otherwise falls back to the embedded copy below.  The
# embedded copy is generated from native-baseline.json and contains only the
# fields these tests consume.
BASELINE_ENV = "REMOTE_DEV_NATIVE_BASELINE"


def _load_baseline() -> dict:
    env_path = os.environ.get(BASELINE_ENV)
    candidates = [Path(env_path)] if env_path else []
    candidates += [
        HERE / "native-baseline.json",
        HERE.parent / "native-baseline.json",
        HERE.parent / "baseline" / "native-baseline.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return json.loads(EMBEDDED_BASELINE_JSON)


EMBEDDED_BASELINE_JSON = r'''{"harnesses":[{"harness":"codex","tools":[{"name":"exec_command","schema_kind":"json","required":["cmd"],"defaults":{"tty":false,"yield_time_ms":10000,"max_output_tokens":10000,"sandbox_permissions":"use_default","login":true},"input_schema":{"properties":{"cmd":{"type":"string","description":"Shell command to execute."},"workdir":{"type":"string","description":"Working directory for the command. Defaults to the turn cwd."},"tty":{"type":"boolean","description":"True allocates a PTY for the command; false or omitted uses plain pipes."},"yield_time_ms":{"type":"number","description":"Wait before yielding output. Defaults to 10000 ms; effective range is 250-30000 ms."},"max_output_tokens":{"type":"number","description":"Output token budget. Defaults to 10000 tokens; larger requests may be capped by policy."},"shell":{"type":"string","description":"Shell binary to launch. Defaults to the user's default shell. CONDITIONAL: omitted under local ZshFork mode; present in Direct mode."},"login":{"type":"boolean","description":"True runs the shell with -l/-i semantics; false disables them. Defaults to true. CONDITIONAL: present only when login shells are allowed."},"sandbox_permissions":{"type":"string","enum":["use_default","require_escalated"],"description":"Per-command sandbox override. Defaults to `use_default`; use `require_escalated` for unsandboxed execution."},"justification":{"type":"string","description":"User-facing approval question for `require_escalated`; omit otherwise."},"prefix_rule":{"type":"array","description":"Reusable approval prefix for `cmd`, only with `sandbox_permissions: \"require_escalated\"`; for example [\"git\", \"pull\"]."},"environment_id":{"type":"string","description":"Environment id from <environment_context>. Omit to use the primary environment. CONDITIONAL: only when multiple environments are attached."},"additional_permissions":{"type":"object","description":"Sandboxed filesystem or network access for this command; only with `sandbox_permissions: \"with_additional_permissions\"`. CONDITIONAL: feature exec_permission_approvals (default false)."}},"required":["cmd"],"additionalProperties":false}},{"name":"write_stdin","schema_kind":"json","required":["session_id"],"defaults":{"chars":"","yield_time_ms":"250 for non-empty writes; 5000-300000 for empty polls","max_output_tokens":10000},"input_schema":{"properties":{"session_id":{"type":"number","description":"Identifier of the running unified exec session."},"chars":{"type":"string","description":"Bytes to write to stdin. Defaults to empty, which polls without writing."},"yield_time_ms":{"type":"number","description":"Wait before yielding output. Non-empty writes default to 250 ms and cap at 30000 ms; empty polls wait 5000-300000 ms by default."},"max_output_tokens":{"type":"number","description":"Output token budget. Defaults to 10000 tokens; larger requests may be capped by policy."}},"required":["session_id"],"additionalProperties":false}},{"name":"apply_patch","schema_kind":"freeform_lark_grammar","required":["patch body matching the freeform grammar"],"defaults":{},"input_schema":null},{"name":"view_image","schema_kind":"json","required":["path"],"defaults":{"detail":"high"},"input_schema":{"properties":{"path":{"type":"string","description":"Local filesystem path to an image file."},"detail":{"type":"string","enum":["high","original"],"description":"Image detail level. Defaults to `high`; use `original` to preserve exact resolution. CONDITIONAL: only when the model supports original image detail and unified image budget is off."},"environment_id":{"type":"string","description":"Environment id from <environment_context>. Omit to use the primary environment. CONDITIONAL: multiple environments."}},"required":["path"],"additionalProperties":false}}]},{"harness":"claude","tools":[{"name":"Bash","schema_kind":"json","required":["command"],"defaults":{"timeout":120000,"run_in_background":false,"dangerouslyDisableSandbox":false},"input_schema":{"properties":{"command":{"type":"string","description":"The command to execute"},"timeout":{"type":"number","description":"Optional timeout in milliseconds (max 600000)"},"description":{"type":"string","description":"Clear, concise description of what this command does in active voice. Never use words like \"complex\" or \"risk\" in the description - just describe what it does."},"run_in_background":{"type":"boolean","description":"Set to true to run this command in the background."},"dangerouslyDisableSandbox":{"type":"boolean","description":"Set this to true to dangerously override sandbox mode and run commands without sandboxing."}},"required":["command"]}},{"name":"Read","schema_kind":"json","required":["file_path"],"defaults":{"offset":1,"limit":2000,"pages":"whole PDF, but PDFs over 10 pages require pages"},"input_schema":{"properties":{"file_path":{"type":"string","description":"The absolute path to the file to read"},"offset":{"type":"integer","description":"The line number to start reading from. Only provide if the file is too large to read at once"},"limit":{"type":"integer","description":"The number of lines to read. Only provide if the file is too large to read at once."},"pages":{"type":"string","description":"Page range for PDF files (e.g., \"1-5\", \"3\", \"10-20\"). Only applicable to PDF files. Maximum 20 pages per request."}},"required":["file_path"]}},{"name":"Write","schema_kind":"json","required":["file_path","content"],"defaults":{},"input_schema":{"properties":{"file_path":{"type":"string","description":"The absolute path to the file to write (must be absolute, not relative)"},"content":{"type":"string","description":"The content to write to the file"}},"required":["file_path","content"]}},{"name":"Edit","schema_kind":"json","required":["file_path","old_string","new_string"],"defaults":{"replace_all":false},"input_schema":{"properties":{"file_path":{"type":"string","description":"The absolute path to the file to modify"},"old_string":{"type":"string","description":"The text to replace"},"new_string":{"type":"string","description":"The text to replace it with (must be different from old_string)"},"replace_all":{"type":"boolean","description":"Replace all occurrences of old_string (default false)"}},"required":["file_path","old_string","new_string"]}},{"name":"NotebookEdit","schema_kind":"json","required":["notebook_path","new_source"],"defaults":{"edit_mode":"replace"},"input_schema":{"properties":{"notebook_path":{"type":"string","description":"The absolute path to the Jupyter notebook file to edit (must be absolute, not relative)"},"cell_id":{"type":"string","description":"The ID of the cell to edit. When inserting a new cell, the new cell will be inserted after the cell with this ID, or at the beginning if not specified."},"new_source":{"type":"string","description":"The new source for the cell"},"cell_type":{"type":"string","enum":["code","markdown"],"description":"The type of the cell (code or markdown). If not specified, it defaults to the current cell type. If using edit_mode=insert, this is required."},"edit_mode":{"type":"string","enum":["replace","insert","delete"],"description":"The type of edit to make (replace, insert, delete). Defaults to replace."}},"required":["notebook_path","new_source"]}},{"name":"Glob","schema_kind":"json","required":["pattern"],"defaults":{"path":"current working directory"},"input_schema":{"properties":{"pattern":{"type":"string","description":"The glob pattern to match files against"},"path":{"type":"string","description":"The directory to search in. If not specified, the current working directory will be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter \"undefined\" or \"null\" - simply omit it for the default behavior. Must be a valid directory path if provided."}},"required":["pattern"]}},{"name":"Grep","schema_kind":"json","required":["pattern"],"defaults":{"output_mode":"files_with_matches","-n":true,"-i":false,"-o":false,"head_limit":250,"offset":0,"multiline":false},"input_schema":{"properties":{"pattern":{"type":"string","description":"The regular expression pattern to search for in file contents"},"path":{"type":"string","description":"File or directory to search in (rg PATH). Defaults to current working directory."},"glob":{"type":"string","description":"Glob pattern to filter files (e.g. \"*.js\", \"*.{ts,tsx}\") - maps to rg --glob"},"output_mode":{"type":"string","enum":["content","files_with_matches","count"],"description":"Output mode: \"content\" shows matching lines (supports -A/-B/-C context, -n line numbers, head_limit), \"files_with_matches\" shows file paths (supports head_limit), \"count\" shows match counts (supports head_limit). Defaults to \"files_with_matches\"."},"-B":{"type":"number","description":"Number of lines to show before each match (rg -B). Requires output_mode: \"content\", ignored otherwise."},"-A":{"type":"number","description":"Number of lines to show after each match (rg -A). Requires output_mode: \"content\", ignored otherwise."},"-C":{"type":"number","description":"Alias for context."},"context":{"type":"number","description":"Number of lines to show before and after each match (rg -C). Requires output_mode: \"content\", ignored otherwise."},"-n":{"type":"boolean","description":"Show line numbers in output (rg -n). Requires output_mode: \"content\", ignored otherwise. Defaults to true."},"-i":{"type":"boolean","description":"Case insensitive search (rg -i)"},"-o":{"type":"boolean","description":"Print only the matched (non-empty) parts of each matching line, one match per output line (rg -o / --only-matching). Requires output_mode: \"content\", ignored otherwise. Defaults to false."},"type":{"type":"string","description":"File type to search (rg --type). Common types: js, py, rust, go, java, etc. More efficient than include for standard file types."},"head_limit":{"type":"number","description":"Limit output to first N lines/entries, equivalent to \"| head -N\". Works across all output modes. Defaults to 250 when unspecified. Pass 0 for unlimited (use sparingly - large result sets waste context)."},"offset":{"type":"number","description":"Skip first N lines/entries before applying head_limit, equivalent to \"| tail -n +N | head -N\". Works across all output modes. Defaults to 0."},"multiline":{"type":"boolean","description":"Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall). Default: false."}},"required":["pattern"]}},{"name":"TaskOutput","schema_kind":"json","required":["task_id"],"defaults":{"block":true,"timeout":30000},"input_schema":{"properties":{"task_id":{"type":"string","description":"The task ID to get output from"},"block":{"type":"boolean","description":"Whether to wait for completion"},"timeout":{"type":"number","description":"Max wait time in ms"}},"required":["task_id"]}},{"name":"TaskStop","schema_kind":"json","required":[],"defaults":{},"input_schema":{"properties":{"task_id":{"type":"string","description":"The ID of the background task to stop. Agent-team teammates and named background agents are also accepted by agent ID or name."},"shell_id":{"type":"string","description":"Deprecated: use task_id instead"}},"required":[]}},{"name":"TaskList","schema_kind":"json","required":[],"defaults":{},"input_schema":{"properties":{},"required":[]}}]},{"harness":"kimi","tools":[{"name":"Bash","schema_kind":"json","required":["command"],"defaults":{"timeout":"60 s foreground, 600 s background","run_in_background":false,"disable_timeout":false},"input_schema":{"properties":{"command":{"type":"string","description":"The command to execute."},"cwd":{"type":"string","description":"The working directory in which to run the command. When omitted, the command runs in the session's working directory."},"timeout":{"type":"integer","description":"Optional timeout in seconds for the command to execute. Foreground default 60s, max 300s. Background default 600s, max 86400s. Ignored for background commands when disable_timeout=true."},"description":{"type":"string","description":"A short description for the background task. Required when run_in_background is true."},"run_in_background":{"type":"boolean","description":"Whether to run the command as a background task."},"disable_timeout":{"type":"boolean","description":"If true, do not apply a timeout to the command. Only applies when run_in_background is true."}},"required":["command"],"additionalProperties":false}},{"name":"Read","schema_kind":"json","required":["path"],"defaults":{"line_offset":1,"max_chars":100000},"input_schema":{"properties":{"path":{"type":"string","description":"Path to a text file or a kimi-file:// attachment reference in the current session. Relative filesystem paths resolve against the working directory; a path outside the working directory must be absolute. Directories are not supported; use `ls` via Bash for a known directory, or Glob for pattern search."},"line_offset":{"type":"integer","description":"The line number to start reading from. Omit to start at line 1. Negative values read from the end of the file (for example, -100 reads the last 100 lines)."},"column_offset":{"type":"integer","description":"Zero-based character offset within the first line of a forward read, excluding its line-number prefix. Uses JavaScript string length in the displayed text. Copy continuation arguments from the previous result to resume a long line."},"n_lines":{"type":"integer","description":"The number of lines to read. Omit to read toward the end of the file. Results are bounded by max_chars, with continuation arguments when the requested range is incomplete."},"max_chars":{"type":"integer","description":"Maximum characters in the returned text, including line numbers and status. Omit for the configured default; requests above the configured maximum are capped."}},"required":["path"],"additionalProperties":false}},{"name":"Write","schema_kind":"json","required":["path","content"],"defaults":{"mode":"overwrite"},"input_schema":{"properties":{"path":{"type":"string","description":"Path to the file to create, append to, or completely overwrite. Relative paths resolve against the working directory; a path outside the working directory must be absolute. Missing parent directories are created automatically."},"content":{"type":"string","description":"Raw full file content to write exactly as provided. This does not use the Read/Edit text view."},"mode":{"type":"string","enum":["overwrite","append"],"description":"Write mode. Defaults to overwrite. append adds content to the end exactly as provided and does not add a newline."}},"required":["path","content"],"additionalProperties":false}},{"name":"Edit","schema_kind":"json","required":["path","old_string","new_string"],"defaults":{"replace_all":false},"input_schema":{"properties":{"path":{"type":"string","description":"Path to the text file to edit. Relative paths resolve against the working directory; a path outside the working directory must be absolute."},"old_string":{"type":"string","description":"Exact content to replace from the Read output view, without the line-number prefix. Use LF for pure CRLF files; use actual \\r escapes where Read shows \\r."},"new_string":{"type":"string","description":"Replacement text in the same Read output view. LF is written back as CRLF only for pure CRLF files."},"replace_all":{"type":"boolean","description":"Set true only when every occurrence of old_string should be replaced."}},"required":["path","old_string","new_string"],"additionalProperties":false}},{"name":"Glob","schema_kind":"json","required":["pattern"],"defaults":{"head_limit":100,"offset":0,"path":"current working directory","include_ignored":false},"input_schema":{"properties":{"pattern":{"type":"string","description":"Glob pattern to match files."},"head_limit":{"type":"integer","description":"Maximum number of matching paths to return after offset. Defaults to 100. Pass 0 to remove the match-count limit. The character limit still applies: large pages are saved for Read, and a continuation offset is provided when more paths remain. Search time and output capture limits still apply."},"offset":{"type":"integer","description":"Number of matching paths to skip. Defaults to 0. Each call searches the current filesystem again; changes can shift results between pages."},"path":{"type":"string","description":"Directory to search. Accepts an absolute path, or a path relative to the current working directory. Defaults to the current working directory."},"include_ignored":{"type":"boolean","description":"Also match files excluded by ignore files such as `.gitignore`, `.ignore`, and `.rgignore` (for example `node_modules` or build outputs). Sensitive files (such as `.env`) remain filtered out for safety. VCS metadata directories (`.git` and similar) are always skipped, even when this is true. Defaults to false."},"include_dirs":{"type":"boolean","description":"Deprecated and ignored. Results are always files-only - directories are never listed. Accepted only so older calls that still pass this flag are not rejected by parameter validation."}},"required":["pattern"],"additionalProperties":false}},{"name":"Grep","schema_kind":"json","required":["pattern"],"defaults":{"output_mode":"files_with_matches","-i":false,"-n":true,"head_limit":250,"offset":0,"multiline":false,"include_ignored":false},"input_schema":{"properties":{"pattern":{"type":"string","description":"Regular expression to search for."},"path":{"type":"string","description":"File or directory to search. Accepts an absolute path, or a path relative to the current working directory. Omit to search the current working directory. Use Read instead when you already know a concrete file path and need its contents."},"glob":{"type":"string","description":"Optional glob filter for which files to search, e.g. `*.ts`. Matched against each file's full absolute path, so a path-anchored pattern like `src/**/*.ts` silently matches nothing - use a basename pattern (`*.ts`), or anchor with `**/` (`**/src/**/*.ts`). To scope the search to a directory, use `path` instead."},"type":{"type":"string","description":"Optional ripgrep file type filter, such as ts or py. Prefer this over `glob` when filtering by language or file kind: it is more efficient and less error-prone than an equivalent glob pattern."},"output_mode":{"type":"string","enum":["content","files_with_matches","count_matches"],"description":"Shape of the result. `content` shows matching lines (honors `-A`, `-B`, `-C`, `-n`, and `head_limit`); `files_with_matches` shows only the paths of files that contain a match, most-recently-modified first (honors `head_limit`); `count_matches` shows per-file match counts as `path:count` lines, preceded by an aggregate total line. Defaults to `files_with_matches`."},"-i":{"type":"boolean","description":"Perform a case-insensitive search. Defaults to false."},"-n":{"type":"boolean","description":"Prefix each matching line with its line number. Applies only when `output_mode` is `content`. Defaults to true."},"-A":{"type":"integer","description":"Number of lines to show after each match. Applies only when `output_mode` is `content`."},"-B":{"type":"integer","description":"Number of lines to show before each match. Applies only when `output_mode` is `content`."},"-C":{"type":"integer","description":"Number of lines to show before and after each match. Applies only when `output_mode` is `content`; takes precedence over `-A` and `-B`."},"head_limit":{"type":"integer","description":"Limit output to the first N lines/entries after offset. Defaults to 250. Pass 0 for unlimited."},"offset":{"type":"integer","description":"Number of leading lines/entries to skip before applying `head_limit`. Use it together with `head_limit` to page through large result sets. Defaults to 0."},"multiline":{"type":"boolean","description":"Enable multiline matching, where the pattern can span line boundaries and `.` also matches newlines. Defaults to false."},"include_ignored":{"type":"boolean","description":"Also search files excluded by ignore files such as `.gitignore`, `.ignore`, and `.rgignore` (for example `node_modules` or build outputs). Sensitive files (such as `.env`) remain filtered out for safety. VCS metadata directories (`.git` and similar) are always skipped, even when this is true. Defaults to false."}},"required":["pattern"],"additionalProperties":false}},{"name":"TaskList","schema_kind":"json","required":[],"defaults":{"active_only":true,"limit":20},"input_schema":{"properties":{"active_only":{"type":"boolean","description":"Whether to list only non-terminal background tasks."},"limit":{"type":"integer","description":"Maximum number of tasks to return."}},"required":[],"additionalProperties":false}},{"name":"TaskOutput","schema_kind":"json","required":["task_id"],"defaults":{},"input_schema":{"properties":{"task_id":{"type":"string","description":"The background task ID to inspect."}},"required":["task_id"],"additionalProperties":false}},{"name":"TaskStop","schema_kind":"json","required":["task_id"],"defaults":{"reason":"Stopped by TaskStop"},"input_schema":{"properties":{"task_id":{"type":"string","description":"The background task ID to stop."},"reason":{"type":"string","description":"Short reason recorded when the task is stopped."}},"required":["task_id"],"additionalProperties":false}},{"name":"ReadMediaFile","schema_kind":"json","required":["path"],"defaults":{"full_resolution":false},"input_schema":{"properties":{"path":{"type":"string","description":"Path to an image or video file, or a kimi-file:// attachment reference in the current session. Relative filesystem paths resolve against the working directory; a path outside the working directory must be absolute. Directories and text files are not supported."},"region":{"type":"object"},"full_resolution":{"type":"boolean","description":"Images only: skip the default downscaling and view at native resolution. Fails with an explicit error when the payload would exceed the per-image byte limit; use region for files that large."}},"required":["path"],"additionalProperties":false}}]},{"harness":"dsh","tools":[{"name":"bash","schema_kind":"dsh_parameter_dsl","required":["command","description"],"defaults":{"timeoutMs":120000,"run_in_background":false},"input_schema":{"properties":{"command":{"type":"string","description":"The bash command to execute."},"description":{"type":"string","description":"Clear, concise description of what this command does in active voice, 5-10 words (shown in the UI). Examples: \"ls\" -> \"List files in current directory\"; \"git status\" -> \"Show working tree status\"; \"npm install\" -> \"Install package dependencies\"."},"timeoutMs":{"type":"number","description":"Timeout in milliseconds. The executor applies its configured default and cap; on expiry the command moves to the background as a job instead of being killed."},"workdir":{"type":"string","description":"Working directory for this command. Defaults to the session workspace; a relative path is resolved against it."},"run_in_background":{"type":"boolean","description":"Run in the background and return a job id immediately (collect with job_output, stop with job_kill). No timeout applies."},"sandbox_permissions":{"type":"string","enum":["workspace-write","danger-full-access"],"description":"Sandbox escalation target; only advertised when the shell executor confines. CONDITIONAL."},"justification":{"type":"string","description":"Required with sandbox_permissions: one sentence for the user explaining why this exact command needs the wider access. Use the language of the user's current request. CONDITIONAL."}},"required":["command","description"],"additionalProperties":true}},{"name":"read","schema_kind":"dsh_parameter_dsl","required":["file_path"],"defaults":{"offset":1,"limit":2000},"input_schema":{"properties":{"file_path":{"type":"string","description":"Path to read, resolved by the filesystem backend."},"offset":{"type":"number","description":"1-based first line to return. Defaults to 1."},"limit":{"type":"number","description":"Maximum number of lines to return. Defaults to 2000."}},"required":["file_path"],"additionalProperties":true}},{"name":"write","schema_kind":"dsh_parameter_dsl","required":["file_path","content"],"defaults":{},"input_schema":{"properties":{"file_path":{"type":"string","description":"Path to write, resolved by the filesystem backend."},"content":{"type":"string","description":"Full UTF-8 text content to write."}},"required":["file_path","content"],"additionalProperties":true}},{"name":"edit","schema_kind":"dsh_parameter_dsl","required":["file_path","old_string","new_string"],"defaults":{"replace_all":false},"input_schema":{"properties":{"file_path":{"type":"string","description":"Path to edit, resolved by the filesystem backend."},"old_string":{"type":"string","description":"Literal text to replace."},"new_string":{"type":"string","description":"Literal replacement text. Use an empty string to delete the match."},"replace_all":{"type":"boolean","description":"Replace all matches. Defaults to false; when false, old_string must appear exactly once."}},"required":["file_path","old_string","new_string"],"additionalProperties":true}},{"name":"read_image","schema_kind":"dsh_parameter_dsl","required":["file_path"],"defaults":{},"input_schema":{"properties":{"file_path":{"type":"string","description":"Path to the image file, resolved by the filesystem backend."}},"required":["file_path"],"additionalProperties":true}},{"name":"glob","schema_kind":"dsh_parameter_dsl","required":["pattern"],"defaults":{"maxResults":100},"input_schema":{"properties":{"pattern":{"type":"string","description":"Glob pattern to match file paths against (e.g. \"**/*.ts\", \"src/**/*.test.js\"). A pattern with no \"/\" matches the basename at any depth, so \"*\" and \"*.ts\" both search the whole tree; include a separator to anchor the depth."},"path":{"type":"string","description":"Directory to search in. Defaults to the session workspace; a relative path resolves against it."}},"required":["pattern"],"additionalProperties":true}},{"name":"grep","schema_kind":"dsh_parameter_dsl","required":["pattern"],"defaults":{"maxMatches":250,"maxLineBytes":2000},"input_schema":{"properties":{"pattern":{"type":"string","description":"Regular expression to search for (ripgrep syntax)."},"path":{"type":"string","description":"File or directory to search. Defaults to the session workspace; a relative path resolves against it."},"include":{"type":"string","description":"One glob filter for which files to search (e.g. \"*.ts\", \"*.{js,jsx}\"). Not a list; negation is not supported."}},"required":["pattern"],"additionalProperties":true}},{"name":"job_output","schema_kind":"dsh_parameter_dsl","required":["job_id"],"defaults":{"wait":false,"timeout_ms":30000,"maxWaitTimeoutMs":600000},"input_schema":{"properties":{"job_id":{"type":"string","description":"Job id returned by the tool that started the background work."},"wait":{"type":"boolean","description":"Block until the job finishes or the timeout expires; a timed-out wait leaves the job running. Defaults to false."},"timeout_ms":{"type":"number","description":"Max wait in milliseconds with wait: true. Defaults to and is capped by configuration."}},"required":["job_id"],"additionalProperties":true}},{"name":"job_list","schema_kind":"dsh_parameter_dsl","required":[],"defaults":{},"input_schema":{"properties":{},"required":[],"additionalProperties":true}},{"name":"job_kill","schema_kind":"dsh_parameter_dsl","required":["job_id"],"defaults":{},"input_schema":{"properties":{"job_id":{"type":"string","description":"Job id returned by the tool that started the background work."},"reason":{"type":"string","description":"Optional short reason, recorded in the log and forwarded to the job."}},"required":["job_id"],"additionalProperties":true}}]}]}'''

BASELINE = _load_baseline()
BY_HARNESS = {item["harness"]: item for item in BASELINE["harnesses"]}
# Claude TaskList is a host session task/plan registry (high-level host
# interface), intentionally not projected as a remote-job list. Kimi TaskList
# is a background-process roster and remains in scope.
BY_HARNESS["claude"]["tools"] = [
    tool for tool in BY_HARNESS["claude"]["tools"] if tool["name"] != "TaskList"
]

CORE_DIR = Path(remote_dev.__file__).resolve().parent / "core"
FILE_WORKER = CORE_DIR / "remote_file_worker.py"
SEARCH_WORKER = CORE_DIR / "search_worker.py"

ENDPOINT_KEYS = set(ENDPOINT_PROPS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog(profile: str) -> dict[str, dict]:
    return {tool["name"]: tool for tool in harness_catalog(profile)}


def _is_conditional(description: object) -> bool:
    return isinstance(description, str) and "CONDITIONAL" in description.upper()


def _types_compatible(expected: object, actual: object) -> bool:
    """JSON Schema integer is a subtype of number; everything else is exact."""
    if expected == actual:
        return True
    if expected == "number" and actual == "integer":
        return True
    if isinstance(expected, list):
        return actual in expected
    return False


def _required_for_baseline(tool: dict) -> list[str]:
    if tool.get("schema_kind") == "freeform_lark_grammar":
        return ["patch"]  # MCP transport wraps raw freeform text in `patch`
    return list(tool.get("required") or [])


def _props_for_baseline(tool: dict) -> dict:
    schema = tool.get("input_schema") or {}
    return dict(schema.get("properties") or {})


def _run_worker(worker: Path, payload: dict, cwd: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, "-I", str(worker)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=str(cwd),
        timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"worker failed rc={proc.returncode}: {proc.stderr}")
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        raise AssertionError(f"worker produced no JSON: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    return json.loads(lines[-1])


class FakeCanonical:
    """Capture canonical dispatches; this is a seam test, not native evidence."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, name: str, args: dict | None, *, profile: str = CANONICAL_PROFILE) -> dict:
        self.calls.append((name, dict(args or {})))
        return {"text": "ok", "result": {"outcome": "success", "status": "ok"}}

    @property
    def last(self) -> tuple[str, dict]:
        return self.calls[-1]


# ---------------------------------------------------------------------------
# 1. Catalog names and schema contracts
# ---------------------------------------------------------------------------

class CatalogContractTests(unittest.TestCase):
    def test_canonical_catalog_matches_dispatcher_and_schemas(self) -> None:
        catalog = harness_catalog("canonical")
        self.assertEqual({tool["name"] for tool in catalog}, set(ALIASES))
        self.assertEqual({tool["name"] for tool in catalog}, {
            name.replace(".", "_") for name in TOOL_SCHEMAS
        })

    def test_exact_native_wire_names_are_present(self) -> None:
        for profile in NATIVE_PROFILES:
            with self.subTest(profile=profile):
                actual = set(_catalog(profile))
                expected = {
                    "remote_" + tool["name"]
                    for tool in BY_HARNESS[profile]["tools"]
                }
                missing = sorted(expected - actual)
                self.assertEqual(missing, [], f"{profile} missing native tools: {missing}")

    def test_native_schema_required_properties_types_enums_defaults(self) -> None:
        for profile in NATIVE_PROFILES:
            cat = _catalog(profile)
            for tool in BY_HARNESS[profile]["tools"]:
                name = "remote_" + tool["name"]
                with self.subTest(profile=profile, tool=name):
                    if name not in cat:
                        self.fail(f"{name} absent from {profile} catalog")
                    actual_schema = cat[name]["inputSchema"]
                    actual_props = dict(actual_schema.get("properties") or {})
                    expected_props = _props_for_baseline(tool)
                    expected_defaults = dict(tool.get("defaults") or {})

                    for prop_name, expected in expected_props.items():
                        if _is_conditional(expected.get("description")):
                            continue
                        self.assertIn(prop_name, actual_props, f"{name} missing property {prop_name}")
                        actual = actual_props[prop_name]
                        if "type" in expected:
                            self.assertTrue(
                                _types_compatible(expected["type"], actual.get("type")),
                                f"{name}.{prop_name} type expected {expected['type']!r} got {actual.get('type')!r}",
                            )
                        if "enum" in expected:
                            self.assertEqual(
                                actual.get("enum"), expected["enum"],
                                f"{name}.{prop_name} enum mismatch",
                            )
                        if (
                            "default" in actual
                            and prop_name in expected_defaults
                            and isinstance(expected_defaults[prop_name], (bool, int, float))
                        ):
                            self.assertEqual(
                                actual["default"], expected_defaults[prop_name],
                                f"{name}.{prop_name} default mismatch",
                            )
                        for bound in ("minimum", "maximum", "minLength", "maxLength"):
                            if bound in expected and bound in actual:
                                self.assertEqual(
                                    actual[bound], expected[bound],
                                    f"{name}.{prop_name} {bound} mismatch",
                                )

                    # Required lists are exact, except freeform apply_patch is
                    # transported over MCP as a single `patch` string.
                    self.assertEqual(
                        list(actual_schema.get("required") or []),
                        _required_for_baseline(tool),
                        f"{name} required list mismatch",
                    )

    def test_native_schema_has_no_extra_native_properties(self) -> None:
        """Strict native parity: report compatibility/extra fields, do not allowlist."""
        problems: list[str] = []
        for profile in NATIVE_PROFILES:
            cat = _catalog(profile)
            for tool in BY_HARNESS[profile]["tools"]:
                if tool.get("schema_kind") == "freeform_lark_grammar":
                    # Freeform apply_patch is wrapped in a single MCP `patch`
                    # string; its native schema is the Lark grammar, not JSON.
                    continue
                name = "remote_" + tool["name"]
                if name not in cat:
                    continue
                actual = set(cat[name]["inputSchema"].get("properties") or {}) - ENDPOINT_KEYS
                expected = set(_props_for_baseline(tool))
                extras = sorted(actual - expected)
                if extras:
                    problems.append(f"{profile}/{name}: extra native-incompatible properties {extras}")
                expected_ap = (tool.get("input_schema") or {}).get("additionalProperties")
                actual_ap = cat[name]["inputSchema"].get("additionalProperties")
                if expected_ap is not None and actual_ap != expected_ap:
                    if profile == "dsh" and actual_ap is None:
                        continue
                    problems.append(
                        f"{profile}/{name}: additionalProperties expected {expected_ap!r} got {actual_ap!r}"
                    )
        self.assertEqual(problems, [], "\n".join(["strict native schema deviations:"] + problems))

    def test_native_defaults_are_advertised_where_native_has_them(self) -> None:
        required_defaults = {
            ("claude", "remote_TaskOutput"): {"block": True, "timeout": 30000},
            ("kimi", "remote_Bash"): {"timeout": 60},
            ("kimi", "remote_TaskList"): {"active_only": True, "limit": 20},
            ("kimi", "remote_TaskStop"): {"reason": "Stopped by TaskStop"},
        }
        cat_cache: dict[str, dict] = {}
        for (profile, name), defaults in required_defaults.items():
            cat_cache.setdefault(profile, _catalog(profile))
            props = cat_cache[profile][name]["inputSchema"]["properties"]
            for key, value in defaults.items():
                with self.subTest(profile=profile, tool=name, field=key):
                    self.assertEqual(props[key].get("default"), value)


# ---------------------------------------------------------------------------
# 2. Profile selection and canonical boundary
# ---------------------------------------------------------------------------

class ProfileSelectionTests(unittest.TestCase):
    def test_explicit_profile_wins_and_is_case_insensitive(self) -> None:
        self.assertEqual(resolve_profile("KIMI"), "kimi")
        self.assertEqual(resolve_profile("Claude"), "claude")
        self.assertEqual(resolve_profile("canonical"), "canonical")
        with mock.patch.dict(os.environ, {PROFILE_ENV: "dsh"}):
            self.assertEqual(resolve_profile("kimi"), "kimi")
            self.assertEqual(resolve_profile(), "dsh")

    def test_environment_profile_is_used_and_unknown_is_rejected(self) -> None:
        with mock.patch.dict(os.environ, {PROFILE_ENV: "dsh"}):
            names = {tool["name"] for tool in harness_catalog()}
            self.assertIn("remote_job_list", names)
            self.assertNotIn("remote_exec_command", names)
        with mock.patch.dict(os.environ, {PROFILE_ENV: "claude-code"}):
            with self.assertRaises(HarnessProfileError):
                resolve_profile()
            with self.assertRaises(HarnessProfileError):
                harness_catalog()

    def test_unknown_explicit_profile_rejected(self) -> None:
        for bad in ("claude-code", "kimi2", ""):
            with self.subTest(profile=bad):
                if bad == "":
                    # Empty means "unset", which is canonical, not unknown.
                    self.assertEqual(resolve_profile(""), "canonical")
                else:
                    with self.assertRaises(HarnessProfileError):
                        resolve_profile(bad)
                    with self.assertRaises(HarnessProfileError):
                        harness_catalog(bad)

    def test_canonical_rejects_native_case_names(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PROFILE_ENV, None)
            for name, args in (
                ("remote_Read", {"path": "/tmp/x"}),
                ("remote_Bash", {"command": "true"}),
                ("remote_exec_command", {"cmd": "true"}),
                ("remote_job_list", {}),
            ):
                with self.subTest(name=name):
                    with self.assertRaises((ValueError, HarnessProfileError)):
                        tools.call_tool(name, args, profile="canonical")

    def test_native_name_of_another_profile_is_rejected(self) -> None:
        with self.assertRaises((ValueError, HarnessProfileError)):
            tools.call_tool("remote_Read", {"path": "/tmp/x"}, profile="dsh")
        with self.assertRaises((ValueError, HarnessProfileError)):
            tools.call_tool("remote_job_list", {}, profile="claude")
        with self.assertRaises((ValueError, HarnessProfileError)):
            tools.call_tool("remote_ReadMediaFile", {"path": "/tmp/x"}, profile="claude")
        with self.assertRaises((ValueError, HarnessProfileError)):
            tools.call_tool("remote_exec_command", {"cmd": "true"}, profile="kimi")


# ---------------------------------------------------------------------------
# 3. Normalization: units, defaults, validation, permissions
# ---------------------------------------------------------------------------

class NormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCanonical()
        self.patch = mock.patch.object(tools, "_call_canonical", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def _call(self, name: str, args: dict, profile: str) -> tuple[str, dict]:
        tools.call_tool(name, args, profile=profile)
        return self.fake.last

    def test_kimi_bash_seconds_to_milliseconds_and_background_defaults(self) -> None:
        canonical, args = self._call(
            "remote_Bash",
            {"command": "sleep 1", "timeout": 60, "description": "wait", "run_in_background": True},
            "kimi",
        )
        self.assertEqual(canonical, "remote.bash")
        self.assertEqual(args["timeout_ms"], 60000)
        self.assertEqual(args["yield_time_ms"], 0)

        canonical, args = self._call(
            "remote_Bash", {"command": "true", "description": "bg", "run_in_background": True}, "kimi"
        )
        self.assertEqual(args["timeout_ms"], 600000)
        self.assertEqual(args["yield_time_ms"], 0)

        canonical, args = self._call("remote_Bash", {"command": "true"}, "kimi")
        # Kimi auto-backgrounds a foreground command at its timeout, so the
        # foreground budget is a yield/promote budget, not a kill deadline.
        self.assertEqual(args["yield_time_ms"], 60000)
        self.assertNotIn("timeout_ms", args)

    def test_kimi_bash_disable_timeout_and_caps(self) -> None:
        canonical, args = self._call(
            "remote_Bash",
            {"command": "true", "description": "bg", "run_in_background": True, "disable_timeout": True},
            "kimi",
        )
        self.assertNotIn("timeout_ms", args)
        self.assertEqual(args["yield_time_ms"], 0)
        for args_in in (
            {"command": "true", "timeout": 301},
            {"command": "true", "description": "bg", "run_in_background": True, "timeout": 86401},
            {"command": "true", "run_in_background": True},  # description required
        ):
            with self.subTest(args=args_in):
                with self.assertRaises(HarnessProfileError):
                    tools.call_tool("remote_Bash", args_in, profile="kimi")

    def test_claude_bash_milliseconds_and_background(self) -> None:
        canonical, args = self._call("remote_Bash", {"command": "true"}, "claude")
        self.assertEqual(canonical, "remote.bash")
        self.assertEqual(args["yield_time_ms"], 120000)
        self.assertNotIn("timeout_ms", args)

        canonical, args = self._call("remote_Bash", {"command": "true", "timeout": 60000}, "claude")
        self.assertEqual(args["yield_time_ms"], 60000)
        self.assertNotIn("timeout_ms", args)

        canonical, args = self._call(
            "remote_Bash", {"command": "true", "run_in_background": True}, "claude"
        )
        self.assertEqual(args["yield_time_ms"], 0)
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_Bash", {"command": "true", "dangerouslyDisableSandbox": True}, profile="claude"
            )

    def test_dsh_bash_timeoutms_default_and_promote_budget(self) -> None:
        canonical, args = self._call(
            "remote_bash", {"command": "true", "description": "x"}, "dsh"
        )
        self.assertEqual(canonical, "remote.bash")
        self.assertEqual(args["yield_time_ms"], 120000)
        self.assertNotIn("timeout_ms", args)

        canonical, args = self._call(
            "remote_bash", {"command": "true", "description": "x", "timeoutMs": 1500}, "dsh"
        )
        self.assertEqual(args["yield_time_ms"], 1500)
        self.assertNotIn("timeout_ms", args)

        canonical, args = self._call(
            "remote_bash",
            {"command": "true", "description": "x", "run_in_background": True},
            "dsh",
        )
        self.assertEqual(args["yield_time_ms"], 0)
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_bash", {"command": "true", "description": "x", "timeoutMs": 600001}, profile="dsh"
            )
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_bash",
                {"command": "true", "description": "x", "sandbox_permissions": "danger-full-access"},
                profile="dsh",
            )

    def test_codex_exec_defaults_and_clamps(self) -> None:
        canonical, args = self._call("remote_exec_command", {"cmd": "echo hi"}, "codex")
        self.assertEqual(canonical, "remote.bash")
        self.assertEqual(args["command"], "echo hi")
        self.assertFalse(args["tty"])
        self.assertEqual(args["yield_time_ms"], 10000)
        self.assertEqual(args["max_output_tokens"], 10000)
        self.assertNotIn("timeout_ms", args)

        canonical, args = self._call(
            "remote_exec_command", {"cmd": "true", "yield_time_ms": 1}, "codex"
        )
        self.assertEqual(args["yield_time_ms"], 250)
        canonical, args = self._call(
            "remote_exec_command", {"cmd": "true", "yield_time_ms": 999999}, "codex"
        )
        self.assertEqual(args["yield_time_ms"], 30000)

        _canonical, args = self._call(
            "remote_exec_command", {"cmd": "true", "shell": "bash", "login": False}, "codex"
        )
        self.assertEqual(args["shell"], "bash")
        self.assertFalse(args["login"])
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_exec_command",
                {"cmd": "true", "sandbox_permissions": "require_escalated"},
                profile="codex",
            )

    def test_codex_write_stdin_defaults(self) -> None:
        canonical, args = self._call("remote_write_stdin", {"session_id": 7}, "codex")
        self.assertEqual(canonical, "remote.job_stdin")
        self.assertEqual(args["job_id"], "7")
        self.assertEqual(args["chars"], "")
        self.assertEqual(args["yield_time_ms"], 5000)

        canonical, args = self._call(
            "remote_write_stdin", {"session_id": 7, "chars": "hello"}, "codex"
        )
        self.assertEqual(args["yield_time_ms"], 250)

        canonical, args = self._call(
            "remote_write_stdin", {"session_id": 7, "chars": "", "yield_time_ms": 1234}, "codex"
        )
        self.assertEqual(args["yield_time_ms"], 1234)

    def test_write_defaults(self) -> None:
        canonical, args = self._call(
            "remote_Write", {"path": "/tmp/x", "content": "x"}, "kimi"
        )
        self.assertEqual(canonical, "remote.write")
        self.assertEqual(args["file_path"], "/tmp/x")
        self.assertTrue(args["overwrite"])
        self.assertFalse(args["append"])
        self.assertTrue(args["create_dirs"])

        canonical, args = self._call(
            "remote_Write", {"path": "/tmp/x", "content": "x", "mode": "append"}, "kimi"
        )
        self.assertFalse(args["overwrite"])
        self.assertTrue(args["append"])

        canonical, args = self._call(
            "remote_Write", {"file_path": "/tmp/x", "content": "x"}, "claude"
        )
        self.assertTrue(args["overwrite"])
        self.assertTrue(args["create_dirs"])

        canonical, args = self._call(
            "remote_write", {"file_path": "/tmp/x", "content": "x"}, "dsh"
        )
        self.assertTrue(args["overwrite"])
        self.assertTrue(args["create_dirs"])

    def test_read_defaults(self) -> None:
        canonical, args = self._call("remote_Read", {"path": "/tmp/x"}, "kimi")
        self.assertEqual(canonical, "remote.read")
        self.assertEqual(args["offset"], 1)
        self.assertGreater(args["limit"], 0)  # transport guard; native has no fixed line cap
        self.assertEqual(args["max_chars"], 100000)

        canonical, args = self._call("remote_read", {"file_path": "/tmp/x"}, "dsh")
        self.assertEqual(args["offset"], 1)
        self.assertEqual(args["limit"], 2000)

        canonical, args = self._call("remote_Read", {"file_path": "/tmp/x"}, "claude")
        self.assertEqual(args["offset"], 1)
        self.assertEqual(args["limit"], 2000)

    def test_glob_grep_defaults_and_zero_limit(self) -> None:
        canonical, args = self._call("remote_Glob", {"pattern": "*"}, "kimi")
        self.assertEqual(canonical, "remote.glob")
        self.assertEqual(args["limit"], 100)
        self.assertEqual(args["offset"], 0)
        self.assertTrue(args["respect_gitignore"])

        canonical, args = self._call(
            "remote_Glob", {"pattern": "*", "head_limit": 0}, "kimi"
        )
        self.assertEqual(args["limit"], 0)

        canonical, args = self._call("remote_Grep", {"pattern": "x"}, "kimi")
        self.assertEqual(canonical, "remote.grep")
        self.assertEqual(args["output_mode"], "files_with_matches")
        self.assertEqual(args["limit"], 250)
        self.assertEqual(args["offset"], 0)
        self.assertFalse(args["case_insensitive"])

        canonical, args = self._call(
            "remote_Grep", {"pattern": "x", "head_limit": 0}, "claude"
        )
        self.assertEqual(args["limit"], 0)

        canonical, args = self._call("remote_Grep", {"pattern": "x"}, "claude")
        self.assertEqual(args["limit"], 250)
        self.assertEqual(args["offset"], 0)

        canonical, args = self._call("remote_glob", {"pattern": "*"}, "dsh")
        self.assertEqual(args["limit"], 100)
        self.assertIn(".git", args["excludes"])

        canonical, args = self._call("remote_grep", {"pattern": "x"}, "dsh")
        self.assertEqual(args["output_mode"], "content")
        self.assertTrue(args["line_numbers"])
        self.assertEqual(args["limit"], 250)

    def test_task_output_and_stop_native_shape(self) -> None:
        claude = _catalog("claude")
        claude_task = claude["remote_TaskOutput"]["inputSchema"]["properties"]
        self.assertEqual(claude_task["block"]["default"], True)
        self.assertEqual(claude_task["timeout"]["default"], 30000)
        self.assertEqual(claude_task["timeout"]["maximum"], 600000)
        self.assertIn("task_id", claude["remote_TaskOutput"]["inputSchema"]["required"])
        self.assertEqual(claude["remote_TaskStop"]["inputSchema"].get("required"), [])

        kimi = _catalog("kimi")
        kimi_task = kimi["remote_TaskOutput"]["inputSchema"]["properties"]
        self.assertNotIn("block", kimi_task)
        self.assertNotIn("timeout", kimi_task)
        self.assertEqual(
            kimi["remote_TaskStop"]["inputSchema"]["properties"]["reason"]["default"],
            "Stopped by TaskStop",
        )

        dsh = _catalog("dsh")
        dsh_job = dsh["remote_job_output"]["inputSchema"]["properties"]
        self.assertNotIn("default", dsh_job["wait"])
        self.assertIn("timeout_ms", dsh_job)
        self.assertEqual(dsh["remote_job_list"]["inputSchema"].get("required", []), [])
        self.assertEqual(dsh["remote_job_kill"]["inputSchema"]["required"], ["job_id"])

        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_TaskOutput", {"task_id": "j", "block": False}, profile="kimi")
        with self.assertRaises((HarnessProfileError, ValueError)):
            tools.call_tool(
                "remote_TaskOutput", {"task_id": "j", "timeout": 600001}, profile="claude"
            )
        with self.assertRaises(HarnessProfileError):
            tools.call_tool(
                "remote_job_output", {"job_id": "j", "wait": "yes"}, profile="dsh"
            )

    def test_dsh_job_output_wait_and_timeout_are_forwarded(self) -> None:
        from remote_dev.mcp import harness_native

        captured: dict = {}

        def fake_incremental(endpoint, *, job_id, wait, timeout_ms):
            captured.update({"endpoint": endpoint, "job_id": job_id, "wait": wait, "timeout_ms": timeout_ms})
            return {"text": "delta", "result": {"state": "running", "job_id": job_id}}

        record = {"job_id": "j", "session_scope": "scope", "state": "running", "description": "demo"}
        import remote_dev.mcp.native_handlers_dsh as dsh_handlers

        with (
            mock.patch.object(dsh_handlers, "_job_record", return_value=record),
            mock.patch.object(dsh_handlers, "_assert_visible", return_value=None),
            mock.patch(
                "remote_dev.core.job_ops.remote_job_output_incremental",
                fake_incremental,
            ),
        ):
            payload = tools.call_tool(
                "remote_job_output",
                {"job_id": "j", "wait": True, "timeout_ms": 500},
                profile="dsh",
            )
        self.assertEqual(captured["job_id"], "j")
        self.assertTrue(captured["wait"])
        self.assertEqual(captured["timeout_ms"], 500)
        self.assertIn("delta", payload["text"])
        self.assertEqual(payload["result"]["job"]["status"], "running")

    def test_claude_task_output_block_false_does_not_wait(self) -> None:
        from remote_dev.mcp import harness_native

        calls: list[tuple[str, dict]] = []

        def fake(name, args, *, profile=CANONICAL_PROFILE):
            calls.append((name, dict(args)))
            return {"text": "", "result": {"job": {"state": "running"}}}

        record = {"job_id": "j", "session_scope": "scope", "state": "running"}
        import remote_dev.mcp.native_handlers_claude as claude_handlers

        with (
            mock.patch(
                "remote_dev.core.state_store.find_job_record",
                return_value=(Path("/tmp/fake-job.json"), record),
            ),
            mock.patch.object(claude_handlers, "_assert_visible", return_value=None),
            mock.patch.object(claude_handlers, "_job_file", return_value=Path("/tmp/fake-stdout.log")),
            mock.patch.object(claude_handlers, "_tail_text", return_value=("out", False)),
            mock.patch.object(tools, "_call_canonical", fake),
        ):
            payload = tools.call_tool(
                "remote_TaskOutput",
                {"task_id": "j", "block": False, "timeout": 600000},
                profile="claude",
            )
        self.assertEqual(calls[0][0], "remote.job_status")
        self.assertEqual(
            sum(1 for call in calls if call[0] == "remote.job_status"),
            1,
            "block=False must not poll a second time",
        )
        self.assertIn("out", payload["text"])

    def test_unknown_and_wrong_type_rejection(self) -> None:
        cases = [
            ("remote_Read", {"path": "/tmp/x", "bogus": 1}, "kimi"),
            ("remote_Bash", {"command": "x", "timeout": "60"}, "kimi"),
            ("remote_Glob", {"pattern": "*", "head_limit": -1}, "kimi"),
            ("remote_Read", {"file_path": "/tmp/x"}, "kimi"),  # Kimi uses path
            ("remote_exec_command", {"cmd": 1}, "codex"),
        ]
        for name, args, profile in cases:
            with self.subTest(name=name, profile=profile):
                with self.assertRaises((HarnessProfileError, ValueError)):
                    tools.call_tool(name, args, profile=profile)

    def test_permission_fields_rejected(self) -> None:
        cases = [
            ("remote_bash", {"command": "x", "description": "y", "justification": "z"}, "dsh"),
            ("remote_exec_command", {"cmd": "x", "sandbox_permissions": "require_escalated"}, "codex"),
            ("remote_Bash", {"command": "x", "dangerouslyDisableSandbox": True}, "claude"),
        ]
        for name, args, profile in cases:
            with self.subTest(name=name, profile=profile):
                with self.assertRaises(HarnessProfileError):
                    tools.call_tool(name, args, profile=profile)


class BackgroundHonestyTests(unittest.TestCase):
    """Unknown job ids and per-harness terminal states through real core paths."""

    def test_claude_unknown_task_id_is_a_caller_error(self) -> None:
        for name in ("remote_TaskOutput", "remote_TaskStop"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as raised:
                    tools.call_tool(name, {"task_id": "job-missing-001"}, profile="claude")
                self.assertIn("job-missing-001", str(raised.exception))

    def test_kimi_task_list_query_failure_is_not_stale_success(self) -> None:
        from remote_dev.core.endpoint import Endpoint
        from remote_dev.core.job_ops import job_record_path
        from remote_dev.core.state_store import (
            atomic_write_json, ensure_endpoint_state, resolve_ledger_scope,
        )

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(
                os.environ,
                {"REMOTE_DEV_STATE_DIR": tmp, "REMOTE_DEV_SESSION_ID": "test-session"},
            ):
                endpoint = Endpoint(
                    host="127.0.0.1", port=1, user="root",
                    root="/tmp/rnp-tl-root", cwd="/tmp/rnp-tl-root",
                    connect_timeout_ms=1000,
                )
                job_id = "job-unreachable-001"
                record = {
                    "schema_version": "remote-dev.job.v1",
                    "job_id": job_id,
                    "description": "unreachable",
                    "target": endpoint.to_result_target(),
                    "connection": {**endpoint.__dict__, "cwd": endpoint.cwd},
                    "command_preview": "sleep 1",
                    "cwd": endpoint.cwd,
                    "env_keys": [],
                    "runtime_env": False,
                    "runtime_env_file": None,
                    "remote_dir": f"/tmp/rnp-tl-root/.remote-dev/jobs/{job_id}",
                    "started_at": "2026-10-24T00:00:00Z",
                    "timeout_ms": None,
                    "tty": False,
                    "session_scope": resolve_ledger_scope(),
                    "authorization": {"token": "x", "job_id": job_id},
                }
                ensure_endpoint_state(endpoint)
                path = job_record_path(endpoint, job_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_json(path, record)
                with self.assertRaises(ValueError) as raised:
                    tools.call_tool("remote_TaskList", {}, profile="kimi")
                self.assertIn("could not query", str(raised.exception))

    def test_status_query_failure_is_not_a_terminal_state(self) -> None:
        import remote_dev.mcp.native_handlers_claude as claude_handlers
        import remote_dev.mcp.native_handlers_kimi as kimi_handlers

        failed = {
            "text": "Remote job j status failed.\n",
            "result": {
                "outcome": "failed",
                "status": "failed",
                "summary": "Remote job j status failed.",
            },
        }
        record = {"job_id": "j", "session_scope": "scope", "state": "running"}
        calls = [
            ("remote_TaskOutput", "claude", {"task_id": "j"}),
            ("remote_TaskStop", "claude", {"task_id": "j"}),
            ("remote_TaskOutput", "kimi", {"task_id": "j"}),
            ("remote_TaskStop", "kimi", {"task_id": "j"}),
        ]
        with (
            mock.patch(
                "remote_dev.core.state_store.find_job_record",
                return_value=(Path("/tmp/fake-job.json"), record),
            ),
            mock.patch.object(claude_handlers, "_assert_visible", return_value=None),
            mock.patch.object(kimi_handlers, "_assert_visible", return_value=None),
            mock.patch.object(claude_handlers, "_job_record", return_value=record),
            mock.patch.object(kimi_handlers, "_job_record", return_value=record),
            mock.patch("remote_dev.mcp.tools._call_canonical", return_value=failed),
        ):
            for name, profile, args in calls:
                with self.subTest(name=name, profile=profile):
                    with self.assertRaises(ValueError) as raised:
                        tools.call_tool(name, args, profile=profile)
                    self.assertIn("j", str(raised.exception))

    def test_live_job_strict_keeps_an_honest_absent_state(self) -> None:
        from remote_dev.mcp.native_common import _live_job

        absent = {
            "text": "Remote job j: absent\n",
            "result": {
                "outcome": "failed",
                "status": "absent",
                "job": {"job_id": "j", "state": "absent"},
            },
        }
        with mock.patch("remote_dev.mcp.tools._call_canonical", return_value=absent):
            state, job = _live_job("j", strict=True)
        self.assertEqual(state, "absent")
        self.assertEqual(job["state"], "absent")

    def test_dsh_job_list_marks_unavailable_live_status(self) -> None:
        from remote_dev.mcp import native_handlers_dsh as dsh_handlers

        record = {
            "job_id": "j", "session_scope": "scope", "description": "demo",
            "started_at": "2026-10-24T00:00:00Z", "state": "running",
        }
        with (
            mock.patch(
                "remote_dev.core.state_store.list_endpoint_records",
                return_value=[{"endpoint_id": "e"}],
            ),
            mock.patch(
                "remote_dev.core.state_store.list_job_records", return_value=[record]
            ),
            mock.patch(
                "remote_dev.core.state_store.resolve_ledger_scope", return_value="scope"
            ),
            mock.patch.object(
                dsh_handlers, "_live_job", side_effect=OSError("transport closed")
            ),
        ):
            payload = tools.call_tool("remote_job_list", {}, profile="dsh")
        job = payload["result"]["jobs"][0]
        self.assertEqual(job["status"], "running")
        self.assertIn("live status unavailable", job["detail"])
        self.assertIn("last known: running", job["detail"])
        self.assertIn("live status unavailable", payload["text"])
        self.assertIn("last known: running", payload["text"])

    def test_kimi_task_list_strips_private_job_fields(self) -> None:
        from remote_dev.mcp import native_handlers_kimi as kimi_handlers

        record = {
            "job_id": "j", "session_scope": "scope", "description": "demo",
            "command_preview": "sleep 1", "started_at": "2026-10-24T00:00:00Z",
            "state": "running", "authorization": {"token": "secret"},
            "connection": {"host": "h", "password": "secret"},
            "local_record": "/tmp/j.json",
        }
        with (
            mock.patch(
                "remote_dev.core.state_store.list_endpoint_records",
                return_value=[{"endpoint_id": "e"}],
            ),
            mock.patch(
                "remote_dev.core.state_store.list_job_records", return_value=[record]
            ),
            mock.patch(
                "remote_dev.core.state_store.resolve_ledger_scope", return_value="scope"
            ),
            mock.patch.object(
                kimi_handlers, "_live_job", return_value=("running", {})
            ),
        ):
            payload = tools.call_tool("remote_TaskList", {}, profile="kimi")
        job = payload["result"]["jobs"][0]
        self.assertEqual(job["job_id"], "j")
        self.assertEqual(job["state"], "running")
        self.assertNotIn("authorization", job)
        self.assertNotIn("connection", job)
        self.assertNotIn("local_record", job)

    def test_dsh_status_query_failure_does_not_become_completed(self) -> None:
        from remote_dev.mcp import native_handlers_dsh as dsh_handlers

        record = {
            "job_id": "j", "session_scope": "scope", "state": "running",
            "description": "demo", "started_at": "2026-10-24T00:00:00Z",
        }
        status_failure = {
            "text": "Remote job j status failed.\n",
            "result": {
                "outcome": "failed",
                "status": "failed",
                "summary": "Remote job j status failed.",
                "job": {
                    "job_id": "j", "state": "running",
                    "error": "Connection refused",
                },
                "error_details": {"category": "transport", "submission_state": "uncertain"},
            },
        }
        with (
            mock.patch.object(dsh_handlers, "_job_record", return_value=record),
            mock.patch.object(dsh_handlers, "_assert_visible", return_value=None),
            mock.patch("remote_dev.mcp.tools._call_canonical", return_value=status_failure),
        ):
            with self.assertRaises(ValueError) as raised:
                tools.call_tool("remote_job_kill", {"job_id": "j"}, profile="dsh")
        self.assertIn("status query failed", str(raised.exception))
        self.assertNotIn("cancellation-requested", str(raised.exception))

    def test_dsh_canonical_stop_failure_is_preserved(self) -> None:
        from remote_dev.mcp import native_handlers_dsh as dsh_handlers

        record = {
            "job_id": "j", "session_scope": "scope", "state": "running",
            "description": "demo", "started_at": "2026-10-24T00:00:00Z",
        }
        stop_failure = {
            "text": "Remote job j stop failed.\nConnection refused\n",
            "result": {
                "outcome": "failed", "status": "failed",
                "summary": "Remote job j stop failed.",
                "error": "Connection refused",
                "error_details": {"category": "transport", "submission_state": "not_sent"},
            },
        }
        with (
            mock.patch.object(dsh_handlers, "_job_record", return_value=record),
            mock.patch.object(dsh_handlers, "_assert_visible", return_value=None),
            mock.patch.object(dsh_handlers, "_live_job", return_value=("running", {})),
            mock.patch("remote_dev.mcp.tools._call_canonical", return_value=stop_failure),
        ):
            payload = tools.call_tool("remote_job_kill", {"job_id": "j"}, profile="dsh")
        self.assertEqual(payload["result"]["outcome"], "failed")
        self.assertEqual(payload["result"]["status"], "failed")
        self.assertIn("Connection refused", payload["result"]["error"])

    def test_dsh_background_nonzero_is_completed_with_exit_detail(self) -> None:
        from remote_dev.mcp import native_handlers_dsh as dsh_handlers

        record = {
            "job_id": "j", "session_scope": "scope", "state": "failed",
            "description": "demo", "started_at": "2026-10-24T00:00:00Z",
        }
        with (
            mock.patch.object(dsh_handlers, "_job_record", return_value=record),
            mock.patch.object(dsh_handlers, "_assert_visible", return_value=None),
            mock.patch(
                "remote_dev.core.job_ops.remote_job_output_incremental",
                return_value={
                    "text": "out",
                    "result": {"state": "failed", "exit_code": 7, "job_id": "j"},
                },
            ),
        ):
            payload = tools.call_tool(
                "remote_job_output", {"job_id": "j"}, profile="dsh"
            )
        self.assertEqual(payload["result"]["job"]["status"], "completed")
        self.assertEqual(payload["result"]["job"]["detail"], "exit code: 7")
        self.assertEqual(payload["result"]["status"], "completed")


# ---------------------------------------------------------------------------
# 4. Known native gaps that must fail loudly (not weakened)
# ---------------------------------------------------------------------------

class FinalLogStopTests(unittest.TestCase):
    def test_tail_text_reads_bounded_suffix_without_whole_file(self):
        directory = tempfile.mkdtemp(prefix="tail-bounded-")
        path = Path(directory) / "large.log"
        with path.open("wb") as stream:
            stream.seek(64 * 1024 * 1024)
            stream.write(b"TAIL-LINE\n")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("whole file read")):
            text, truncated = native_common._tail_text(path, 4096)
        self.assertTrue(truncated)
        self.assertTrue(text.endswith("TAIL-LINE\n"))
        self.assertLessEqual(len(text.encode("utf-8")), 4096)

    def test_refresh_job_output_accepts_completed_nonzero_command(self):
        completed = {
            "text": "Remote command failed. Exit code: 7.\n",
            "result": {
                "tool": "remote.job_stdin", "outcome": "failed", "status": "failed",
                "state": "failed", "exit_code": 7,
            },
        }
        with mock.patch("remote_dev.mcp.tools._call_canonical", return_value=completed):
            self.assertIsNone(native_common._refresh_job_output("j"))

    def test_refresh_job_output_preserves_local_recording_failure(self):
        recording = {
            "text": "The remote result was received, but local recording failed.\n",
            "result": {
                "tool": "remote.job_stdin", "outcome": "failed",
                "status": "local_recording_failed", "state": "failed", "exit_code": 7,
                "execution_outcome": "failed",
                "error_details": {"operation_completed": True},
                "local_recording": {"status": "failed", "stage": "save_output"},
            },
        }
        with mock.patch("remote_dev.mcp.tools._call_canonical", return_value=recording):
            self.assertIs(native_common._refresh_job_output("j"), recording)

    def test_refresh_job_output_preserves_canonical_failure(self):
        failure = {
            "text": "Remote job j: failed\n",
            "result": {"outcome": "failed", "status": "failed", "error": "pull failed"},
        }
        with mock.patch("remote_dev.mcp.tools._call_canonical", return_value=failure):
            self.assertIs(native_common._refresh_job_output("j"), failure)
        with mock.patch("remote_dev.mcp.tools._call_canonical", side_effect=OSError("down")):
            out = native_common._refresh_job_output("j")
        self.assertEqual(out["result"]["outcome"], "failed")
        self.assertIn("task output unavailable", out["text"])

    def test_native_task_output_does_not_hide_refresh_failure(self):
        import remote_dev.mcp.native_handlers_claude as claude_handlers
        import remote_dev.mcp.native_handlers_kimi as kimi_handlers

        record = {"job_id": "j", "session_scope": "scope", "state": "succeeded"}
        status = {
            "text": "Remote job j: succeeded\n",
            "result": {"outcome": "success", "status": "succeeded", "job": {"state": "succeeded"}},
        }
        failure = {
            "text": "task output unavailable: OSError: down\n",
            "result": {"outcome": "failed", "status": "output_unavailable", "error": "down"},
        }
        for name, profile, module in (
            ("remote_TaskOutput", "claude", claude_handlers),
            ("remote_TaskOutput", "kimi", kimi_handlers),
        ):
            with self.subTest(profile=profile), \
                    mock.patch("remote_dev.core.state_store.find_job_record",
                               return_value=(Path("/tmp/fake-job.json"), record)), \
                    mock.patch.object(module, "_refresh_job_output", return_value=failure), \
                    mock.patch.object(module, "_assert_visible", return_value=None), \
                    mock.patch("remote_dev.mcp.tools._call_canonical", return_value=status):
                payload = tools.call_tool(name, {"task_id": "j"}, profile=profile)
            self.assertEqual(payload["result"]["outcome"], "failed")
            self.assertIn("output unavailable", payload["text"])

    def test_task_stop_preserves_canonical_stop_failure_text(self):
        import remote_dev.mcp.native_handlers_claude as claude_handlers
        import remote_dev.mcp.native_handlers_kimi as kimi_handlers

        record = {"job_id": "j", "session_scope": "scope", "state": "running",
                  "description": "demo", "command_preview": "sleep 60"}
        status = {
            "text": "Remote job j: running\n",
            "result": {"outcome": "success", "status": "running", "job": {"state": "running"}},
        }
        failure = {
            "text": "Remote job j stop failed.\nConnection refused\n",
            "result": {
                "outcome": "failed", "status": "failed",
                "summary": "Remote job j stop failed.", "error": "Connection refused",
                "error_details": {"category": "transport", "submission_state": "not_sent"},
            },
        }

        def canonical(name, args):
            return status if name == "remote.job_status" else failure

        for name, profile, module in (
            ("remote_TaskStop", "claude", claude_handlers),
            ("remote_TaskStop", "kimi", kimi_handlers),
        ):
            with self.subTest(profile=profile), \
                    mock.patch.object(module, "_job_record", return_value=record), \
                    mock.patch.object(module, "_assert_visible", return_value=None), \
                    mock.patch("remote_dev.mcp.tools._call_canonical", side_effect=canonical):
                payload = tools.call_tool(name, {"task_id": "j"}, profile=profile)
            self.assertEqual(payload["result"]["outcome"], "failed")
            self.assertIn("stop failed", payload["text"])
            self.assertNotIn("Successfully stopped", payload["text"])

class NativeGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCanonical()
        self.patch = mock.patch.object(tools, "_call_canonical", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_claude_read_pdf_pages_are_normalized(self) -> None:
        from remote_dev.mcp.harness_profiles import spec_by_wire

        spec = spec_by_wire("claude", "remote_Read")
        self.assertIsNotNone(spec)
        args = spec.normalize({"file_path": "/tmp/doc.pdf", "pages": "1-2"})
        self.assertEqual(args["pages"], "1-2")

    def test_grep_head_limit_zero_is_unlimited(self) -> None:
        """Native Kimi/Claude `head_limit=0` means unlimited; the worker caps at 5000."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            target = root / "many.txt"
            target.write_text("\n".join(f"needle {i}" for i in range(5100)) + "\n", encoding="utf-8")
            data = _run_worker(
                SEARCH_WORKER,
                {
                    "op": "grep",
                    "root": str(root),
                    "cwd": str(root),
                    "path": str(root),
                    "pattern": "needle",
                    "output_mode": "content",
                    "line_numbers": False,
                    "limit": 0,
                    "offset": 0,
                },
                root,
            )
            self.assertEqual(data["status"], "ok")
            self.assertEqual(
                len(data["matches"]),
                5100,
                "native head_limit=0 is unlimited; worker truncated to 5000",
            )
            self.assertFalse(data["truncated"])


# ---------------------------------------------------------------------------
# 5. Real Tier-1 worker differential tests (no SSH)
# ---------------------------------------------------------------------------

class WorkerFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rd-native-worker-")
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.small = self.root / "small.txt"
        self.small.write_text("\n".join(f"line-{i:03d}" for i in range(1, 301)) + "\n", encoding="utf-8")
        self.empty = self.root / "empty.txt"
        self.empty.write_text("", encoding="utf-8")
        self.page = self.root / "page.txt"
        self.page.write_text("a\nb\nc\nd\n", encoding="utf-8")
        self.unicode = self.root / "unicode.txt"
        self.unicode.write_text("héllo 世界\n第二行\n", encoding="utf-8")
        self.long = self.root / "long.txt"
        self.long.write_text("x" * 5000 + "\n", encoding="utf-8")

    def _read(self, file_path: str, **extra) -> dict:
        payload = {"op": "read", "root": str(self.root), "cwd": str(self.root), "file_path": file_path}
        payload.update(extra)
        return _run_worker(FILE_WORKER, payload, self.root)

    def _write(self, file_path: str, content: str | None = None, **extra) -> dict:
        payload = {"op": "write", "root": str(self.root), "cwd": str(self.root), "file_path": file_path}
        if content is not None:
            payload["content"] = content
        payload.update(extra)
        return _run_worker(FILE_WORKER, payload, self.root)

    def test_read_defaults_empty_and_missing(self) -> None:
        data = self._read("small.txt")
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["offset"], 1)
        self.assertEqual(data["file"]["limit"], 200)
        self.assertEqual(data["file"]["line_start"], 1)
        self.assertEqual(data["file"]["line_end"], 200)
        self.assertEqual(data["file"]["total_lines"], 300)
        self.assertTrue(data["file"]["partial"])
        self.assertTrue(data["file"]["content"].startswith("1 | line-001\n2 | line-002"))

        data = self._read("empty.txt")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["file"]["total_lines"], 0)
        self.assertEqual(data["file"]["line_start"], 0)
        self.assertEqual(data["file"]["line_end"], 0)
        self.assertEqual(data["file"]["content"], "")
        self.assertFalse(data["file"]["partial"])

        data = self._read("missing.txt")
        self.assertEqual(data["status"], "not_found")
        self.assertIn("does not exist", data["error"])

    def test_read_pagination_non_ascii_and_long_line(self) -> None:
        data = self._read("page.txt", offset=2, limit=2)
        self.assertEqual(data["status"], "partial")
        self.assertEqual(data["file"]["content"], "2 | b\n3 | c")
        self.assertEqual(data["file"]["line_start"], 2)
        self.assertEqual(data["file"]["line_end"], 3)
        self.assertTrue(data["file"]["partial"])

        data = self._read("unicode.txt")
        self.assertEqual(data["status"], "ok")
        self.assertIn("héllo 世界", data["file"]["content"])
        self.assertIn("第二行", data["file"]["content"])

        data = self._read("long.txt")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["file"]["truncated_line_count"], 1)
        self.assertIn("<remote-dev line truncated>", data["file"]["content"])
        self.assertTrue(any("truncated to 2000 chars" in warning for warning in data["warnings"]))

    def test_write_overwrite_parents_append_and_omitted_content(self) -> None:
        data = self._write("nested/a/b/new.txt", "hello", create_dirs=True, overwrite=True)
        self.assertEqual(data["status"], "written")
        self.assertTrue((self.root / "nested/a/b/new.txt").is_file())
        self.assertEqual((self.root / "nested/a/b/new.txt").read_text(encoding="utf-8"), "hello")

        data = self._write("missing/child.txt", "hello", create_dirs=False, overwrite=True)
        self.assertEqual(data["status"], "parent_not_found")

        data = self._write("small.txt", "replace", overwrite=False)
        self.assertEqual(data["status"], "file_exists")
        data = self._write("small.txt", "replace", overwrite=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual((self.root / "small.txt").read_text(encoding="utf-8"), "replace")

        data = self._write("append.txt", "first", overwrite=True)
        self.assertEqual(data["status"], "written")
        data = self._write("append.txt", "second", append=True)
        self.assertEqual(data["status"], "written")
        self.assertTrue(data["appended"])
        self.assertEqual((self.root / "append.txt").read_text(encoding="utf-8"), "firstsecond")

        data = self._write("empty-write.txt", create_dirs=True, overwrite=True)
        self.assertEqual(data["status"], "written")
        self.assertEqual((self.root / "empty-write.txt").read_text(encoding="utf-8"), "")

    def test_edit_delete_replacement_and_errors(self) -> None:
        target = self.root / "delete.txt"
        target.write_bytes(b"a\nb\nc\n")
        payload = {
            "op": "edit",
            "root": str(self.root),
            "cwd": str(self.root),
            "file_path": "delete.txt",
            "old_string": "b\n",
            "new_string": "",
        }
        data = _run_worker(FILE_WORKER, payload, self.root)
        self.assertEqual(data["status"], "edited")
        self.assertEqual(target.read_bytes(), b"a\nc\n")

        ambiguous = self.root / "ambiguous.txt"
        ambiguous.write_bytes(b"x\nx\n")
        data = _run_worker(
            FILE_WORKER,
            {
                "op": "edit",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "ambiguous.txt",
                "old_string": "x",
                "new_string": "y",
            },
            self.root,
        )
        self.assertEqual(data["status"], "old_string_not_unique")

        data = _run_worker(
            FILE_WORKER,
            {
                "op": "edit",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "delete.txt",
                "old_string": "not-there",
                "new_string": "y",
            },
            self.root,
        )
        self.assertEqual(data["status"], "old_string_not_found")

        data = _run_worker(
            FILE_WORKER,
            {
                "op": "edit",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "delete.txt",
                "old_string": "",
                "new_string": "y",
            },
            self.root,
        )
        self.assertEqual(data["status"], "empty_old_string")

    def test_read_pdf_is_explicitly_unsupported(self) -> None:
        pdf = self.root / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
        data = self._read("doc.pdf")
        self.assertEqual(data["status"], "pdf_unsupported")
        self.assertIn("artifact_pull", data["error"])

    def test_read_image_sniff_and_binary_refusal(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        image = self.root / "tiny.png"
        image.write_bytes(png)

        data = self._read("tiny.png")
        self.assertEqual(data["status"], "binary_file")
        self.assertIn("artifact_pull", data["error"])

        from remote_dev.core.image_ops import REMOTE_IMAGE_PY

        proc = subprocess.run(
            [sys.executable, "-I", "-c", REMOTE_IMAGE_PY],
            input=json.dumps({"path": str(image), "max_bytes": 1024 * 1024}),
            text=True,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sniffed = json.loads(proc.stdout.strip())
        self.assertEqual(sniffed["status"], "ok")
        self.assertEqual(sniffed["mime_type"], "image/png")
        self.assertEqual(base64.b64decode(sniffed["data"])[:8], b"\x89PNG\r\n\x1a\n")


class WorkerSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rd-native-search-")
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.target = self.root / "m.txt"
        self.target.write_text(
            "\n".join(f"needle {i}" for i in range(1, 6)) + "\n", encoding="utf-8"
        )
        self.many = self.root / "many"
        self.many.mkdir()
        for index in range(120):
            (self.many / f"f-{index:03d}.txt").write_text(f"file {index}\n", encoding="utf-8")

    def _grep(self, **extra) -> dict:
        payload = {
            "op": "grep",
            "root": str(self.root),
            "cwd": str(self.root),
            "path": str(self.root),
            "pattern": "needle",
        }
        payload.update(extra)
        return _run_worker(SEARCH_WORKER, payload, self.root)

    def test_grep_defaults_pagination_and_zero(self) -> None:
        data = self._grep(output_mode="content", line_numbers=False, limit=2, offset=0)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["matches"]), 2)
        self.assertTrue(data["truncated"])

        data = self._grep(output_mode="content", line_numbers=False, limit=2, offset=2)
        self.assertEqual(len(data["matches"]), 2)
        self.assertIn("needle 3", data["matches"][0])
        self.assertIn("needle 4", data["matches"][1])

        data = self._grep(output_mode="content", line_numbers=False, limit=0, offset=0)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["matches"]), 5)
        self.assertFalse(data["truncated"])

        data = self._grep()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["output_mode"], "files_with_matches")
        self.assertIn("m.txt", {Path(row).name for row in data["matches"]})

    def test_glob_limit_pagination(self) -> None:
        payload = {
            "op": "glob",
            "root": str(self.root),
            "cwd": str(self.root),
            "path": str(self.many),
            "pattern": "*.txt",
            "limit": 10,
            "offset": 0,
        }
        data = _run_worker(SEARCH_WORKER, payload, self.root)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["matches"]), 10)
        self.assertTrue(data["truncated"])

        payload["offset"] = 10
        data = _run_worker(SEARCH_WORKER, payload, self.root)
        self.assertEqual(len(data["matches"]), 10)
        self.assertNotEqual(data["matches"][0], _run_worker(SEARCH_WORKER, {**payload, "offset": 0}, self.root)["matches"][0])


class WorkerCanonicalCompatibilityTests(unittest.TestCase):
    """The worker payloads use canonical field names, the compatibility contract."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rd-native-canonical-")
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def test_canonical_read_write_edit_grep_payloads(self) -> None:
        target = self.root / "canonical.txt"
        written = _run_worker(
            FILE_WORKER,
            {
                "op": "write",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "canonical.txt",
                "content": "alpha\nbeta\ngamma\n",
                "overwrite": True,
                "create_dirs": True,
            },
            self.root,
        )
        self.assertEqual(written["status"], "written")

        read = _run_worker(
            FILE_WORKER,
            {
                "op": "read",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "canonical.txt",
                "offset": 2,
                "limit": 1,
            },
            self.root,
        )
        self.assertEqual(read["status"], "partial")
        self.assertEqual(read["file"]["content"], "2 | beta")

        edited = _run_worker(
            FILE_WORKER,
            {
                "op": "edit",
                "root": str(self.root),
                "cwd": str(self.root),
                "file_path": "canonical.txt",
                "old_string": "beta",
                "new_string": "BETA",
            },
            self.root,
        )
        self.assertEqual(edited["status"], "edited")
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")

        grepped = _run_worker(
            SEARCH_WORKER,
            {
                "op": "grep",
                "root": str(self.root),
                "cwd": str(self.root),
                "path": str(self.root),
                "pattern": "BETA",
                "output_mode": "content",
                "line_numbers": False,
                "limit": 10,
                "offset": 0,
            },
            self.root,
        )
        self.assertEqual(grepped["status"], "ok")
        self.assertEqual(len(grepped["matches"]), 1)
        self.assertIn("BETA", grepped["matches"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
