# Remote-Dev Validation Record

Last updated: 2026-09-13 (existing-container endpoints).

## Version 0.8 validation (2026-09-13)

- Container endpoint tests enumerate public operation/record boundaries and
  host-only helpers. They cover fresh names, full-ID lookup bypass, replacement
  isolation for optional read guards and pooled connections, retained old job
  IDs, unknown launch outcomes without replay, CLI/alias/resolver fields,
  diagnostics, script and binary argv, and explicit bootstrap/forward rejection.
- Real local Linux protocol adapters validate Docker argv before running the
  shipped RPC worker and binary artifact workers. These exercise owned command
  exit/output and binary roundtrips. Bash pipe, redirection, variable and stdin
  script tests preserve caller shell semantics. The WSL run uses uid 1000 and
  verifies inherited command identity with no Docker user override.
- Native Windows Python 3.13 full suite: 439 passed, 97 skipped, 152 subtests
  passed. Linux/WSL Python 3.12 full suite: 522 passed, 2 skipped, 189 subtests
  passed; the final container suite adds an explicit uid-1000 assertion and
  passes all 74 cases. Package sources also parse with Python 3.9
  grammar; this is a syntax check, not a Python 3.9 runtime test.
- After merging canonical main `4da7bbd`, operation roots share one RPC
  transport within a container or the host while different container IDs
  remain isolated. The combined regression checks this partition and retains
  upstream tests for per-root job ownership, cancellation and reconnects.
  Windows full suite: 439 passed, 101 skipped, 152 subtests passed. Linux/WSL
  full suite: 526 passed, 2 skipped, 189 subtests passed. The new ownership
  test waits for verified quiet because result publication can precede the
  supervisor's own exit; no launch or start gate is replayed.
- These local adapters do not establish real container namespace isolation or
  NPU/model correctness. Live existing-container acceptance is a separate
  consumer test. The package does not install missing container prerequisites
  or grant managed device leases; jobs retain the existing writable-root
  requirement for scratch/log files.

## Version 0.7 validation (2026-09-12)

- Synchronous SDK capture waits past early output until completion or the
  existing yield deadline, retaining preview limits and complete durable logs.
  Final-change checks: 27 native Windows session/job tests; 50 WSL worker,
  session and job tests plus 9 subtests. These cover cancellation, command
  timeout, early output, yield deadlines and UTF-8 output cursors.
  Three alternating small-command pairs on one warmed Windows-to-Linux RPC
  connection reduced control calls from two to one: baseline median 0.802 s
  (0.729–1.230), candidate 0.532 s (0.498–0.956). Ranges overlap; this is
  endpoint-specific evidence, not an absolute latency guarantee.

- Hosted Windows Python 3.9/3.12 exposed equal monotonic timer ticks in the
  pool's LRU selection. A completion-order counter now breaks this ambiguity
  without changing idle-expiry deadlines. A fixed-clock regression confirms
  that reusing an entry protects it from eviction; the 17 affected pool/RPC
  tests pass on Windows Python 3.9/3.13 and WSL Python 3.13.

- WSL full suite: 428 passed, 2 skipped, 174 subtests passed. Native Windows
  full suite: 353 passed, 89 skipped, 146 subtests passed. The pool tests
  cover concurrent first calls, unrelated endpoint progress during slow open
  and close, forty sequential endpoint replacements without evicting active
  requests, bounded wait/cancellation and idle expiration.
- Saturation tests fill all 32 ordinary RPC slots and confirm status, stop,
  tail and stdin can use reserved workers. The MCP test uses the actual
  underscore wire name. Long output waits release the cursor lock; concurrent
  input and output consumption cannot duplicate input or replay consumed bytes.
- Real Bash tests retain runtime functions, non-exported variables, PATH order,
  pipefail/nounset/errexit and command exit status. Initialization happens on
  every command; BASH_ENV runs once in the applicable Bash startup mode.
  Missing/failed explicit runtime scripts prevent user-command execution.
- Installed 0.7.0 wheels on native Windows Python 3.9 and WSL Python 3.12 passed
  four-container SDK tests for runtime functions/PATH/cwd/env, pipeline exit
  codes, real PTY input, cancellation and quiet owned-job completion. Eight
  alternating baseline/candidate short-command samples per endpoint gave
  medians around 0.50 s on Windows and 0.52–0.53 s on WSL. These containers
  have inexpensive startup; timing differences were within noise.
- Four Windows-to-host paired runs also passed. Median baseline/candidate
  milliseconds were 2545/1501, 714/494, 707/493 and 831/497; removing the extra
  runtime Bash startup saves measurable time where host startup scripts cost
  time. One initial scratch-directory setup returned no directory and was
  repeated as an independent scratch probe; the successful endpoint rerun is
  recorded separately. No submitted business job was replayed.
- The baseline comparison substitutes only the 0.6 runtime command wrapper
  inside the installed SDK, retaining the connection and supervisor. It does
  not skip Bash initialization. Performance depends on the endpoint startup
  scripts; there is no general absolute-latency guarantee.
- Unsupported tool arguments (including MCP `wait`) now fail before endpoint
  resolution or execution. CLI-local input-file flags are consumed before the
  same validation; native aliases and registered endpoint selectors remain valid.
- Removed the redundant CI `validate --local-only` step (compileall and schema
  assertions already exercised by tests). Platform tests and clean wheel
  installation remain required.
- A managed-group follow-up adds `spec.prepared_timeout_seconds`, a separate
  1–86400 second gate wait (default 120). The affected Linux worker/control/
  session suite passes 43 tests, including expiry without command execution,
  a prepared wait longer than the command timeout followed by successful
  activation, and default/long/invalid timeout contracts.

## Version 0.6 validation (2026-09-11)

This version has 18 tools. It replaces the separate monitor/background flags
with execute/yield/poll, writable stdin and real remote PTYs. Both native
Windows Python and WSL use the shipped binary SSH RPC and artifact workers.

- Full native Windows suite: 342 passed, 82 skipped, 146 subtests passed.
- Full WSL suite: 410 passed, 2 skipped, 174 subtests passed.
- After the real-MCP cancellation check identified a stale running reply,
  cancellation now waits for the owned family to drain. The affected Linux
  worker/session suite passed 29 tests, including two additional cases for
  cancellation completion and invalid-UTF-8 output budgeting.
- Both clients passed live SSH scaffold validation, including three concurrent
  scratch workers, read/edit/write, search, patches, artifacts, stdin/EOF,
  process timeouts and status/log resources. Test-created jobs were stopped
  and verified quiet. No NPU workload was used.
- Transport tests execute the actual shipped worker source through local
  subprocess pipes; this exercises both client OS implementations without
  requiring SSH fixtures. They cover concurrent replies, scoped cancellation,
  unknown outcomes without replay, bounded source caches and mutation locks.
- A 32 MiB artifact download stays below 12 MiB of measured Python allocations;
  a 32 MiB single-line text read stays below 8 MiB. Fast log windows stop early
  and intentionally omit the content hash/read ledger.
- Clean 0.6.0 wheel installations on Windows and WSL passed actual MCP over
  JSON lines and Content-Length framing against the same explicit Linux SSH
  endpoint. Both passed fast-before-slow concurrent replies, cancellation with
  quiet completion, PTY terminal detection/Chinese input/Ctrl-C, a shared
  64-token output budget with complete later drain, and a hash-checked 4 MiB
  binary roundtrip. Every job created by the harness was verified quiet.
- In these paired live runs, warm short commands took approximately 1.5–2.0 s;
  first calls took 6.7–7.3 s. A fast command returned in 1.5–1.6 s while a
  two-second command completed in about 3.5–3.6 s. These are endpoint-specific
  observations, including network and remote shell startup, not a promise of
  local-shell latency or a measurement of the desktop application's scheduling.

The initial Windows suite encountered a transient file-replacement denial;
the isolated reproduction passed. A later session-record test reproduced the
same Windows error. Local atomic publication now retries only the prepared
replacement for up to 500 ms on Windows sharing/access errors, preserving the
existing destination and never replaying remote execution.

Older sections below are dated evidence for prior releases, including their
former tool counts and no-PTY boundary.

## Client-parity batch (2026-09-10, this checkout)

Nineteen `remote_*` tools (new: `remote_job_stdin`), a shared client-native
alias layer (`path`, `line_offset`/`n_lines`, `cmd`/`workdir`, grep
`-i`/`-A`/`-B`/`-C`/`-n`/`head_limit`), read-from-end via negative offset,
write append mode, grep `count` vs `count_matches` distinction, and
interactive pipe sessions (writable stdin FIFO→pipe proxy, eof, incremental
output cursors, `yield_time_ms`, `max_output_tokens` budgets, explicit
no-PTY capability boundary).

Local gates on macOS (Apple Silicon), Python 3.11.13:

- `python3 -m pytest` — **357 passed, 9 skipped** (Linux-only worker cases,
  including the new interactive stdin roundtrip which runs on Linux CI),
  plus 153 subtests.
- `python3 -m remote_dev validate --local-only` — **ok**: 19 MCP tools, 19
  CLI subcommands, max 2 tool-specific required fields (aliased fields are
  server-enforced so providers cannot reject alias-only calls).
- Real MCP stdio chain (framed JSON-RPC): tools/list advertises the portable
  names with the new schemas; an alias-only `remote_read` call
  (`path`/`line_offset`/`n_lines`) passes normalization and dispatch;
  `remote_bash tty=true` returns `unsupported_capability` without
  connecting; a missing `file_path` returns an actionable server-side error.
- The remote executor scripts (`REMOTE_FILE_PY`, `REMOTE_SEARCH_PY`) were
  executed for real through local `python3 -c` subprocesses (negative offset,
  binary detection, append, grep flags, count vs count_matches on a
  two-matches-one-line input, hidden-dir handling, grep fallback with masked
  PATH). The worker's `stdin` control action and incremental tail were
  exercised against real FIFOs and log files locally.
- Kimi 0.42.0 native semantics were confirmed against the installed client:
  `count_matches` maps to `rg --count-matches` (not `rg -c`), and negative
  `line_offset` reads from the end of the file.

Not run: live SSH endpoint checks (no disposable endpoint in this
environment — the live section of `remote-dev validate` now covers the
interactive session roundtrip and tty boundary), native Windows, and the
per-client E2E model sessions. Those remain recorded as unverified here.

## Session-semantics review batch (2026-09-11, this checkout)

Fixes for the independent acceptance review of the client-parity batch:

- Partially accepted stdin writes combined with `eof=true` no longer close
  stdin: worker writes are chunked at `PIPE_BUF` on UTF-8 character
  boundaries (all-or-nothing nonblocking writes), and the EOF marker is only
  published once every byte of the operation has been accepted. The response
  reports `written`/`written_chars`/`stdin_buffer_full`/`eof_deferred` so the
  exact remainder is retryable; `written_chars` is the character-level slice
  point because byte counts cannot slice Python/JSON Unicode strings.
- Incremental tail reads hold back a UTF-8 character split by the byte budget
  for the next poll instead of decoding it into permanent U+FFFD. Genuinely
  invalid bytes still flush as replacements (the cursor always advances),
  and a terminal job at end of file flushes an unfinished trailing sequence.
- `remote.bash` initial yield now uses the same incremental cursor path as
  `remote.job_stdin` polls: it returns the first bytes up to the budget and
  persists `stdin_cursors`, so the first follow-up poll continues where the
  yield stopped instead of replaying from offset 0, and bytes the yield
  skipped are delivered by later polls instead of being dropped by a
  last-lines tail.

Local gates on macOS (Apple Silicon), Python 3.11.13:

- `python3 -m pytest` — **364 passed, 9 skipped** (7 new regression tests:
  deferred-EOF retry over a real 1 MiB FIFO roundtrip, Unicode
  `written_chars` accounting, split-UTF-8 paged reads, invalid-byte flush,
  terminal tail flush, tiny-budget progress, initial-yield→poll
  continuation).
- `python3 .mindie-local/kimi-handoff/review-repro.py kimi1` (reviewer's repro
  against this checkout) — UTF-8 pagination now reassembles exactly
  (`combined_matches_original: true`, 0 replacement characters), and the 1
  MiB + `eof=true` write reports `eof: false` (deferred) with the remainder
  retry accepted.
- `python3 -m remote_dev validate --local-only` — **ok** (19 tools / 19 CLI
  subcommands).
- Real MCP stdio chain re-run: 19 tools listed; `remote_job_stdin` schema
  documents the `written_chars` retry contract and deferred `eof`;
  `remote_bash` yield description documents the first-bytes/continuation
  semantics.

Not run (unchanged from the previous batch): live SSH endpoint checks, Linux
worker roundtrip on a real Linux host, native Windows, per-client E2E model
sessions.

---

Previous record:

## v0.5.0 process control and local client portability (2026-09-10)

Generic remote process supervision lives in this package:
`remote_dev.processes.control(endpoint, job_id, action, **parameters)`.
The Linux worker (child-subreaper, identity/marker/start-time checks,
start gate, descendant drain, timeout/stop/logs) is shipped as package
source. Ordinary `remote.bash --run-in-background` and `remote.job_*`
use that same path; the previous nohup/PID-only runner is gone. Default
cwd is the endpoint root. The local SSH/stream client handles macOS,
Linux, and native Windows subprocess pipes without claiming remote
Windows process support.

Independent acceptance on macOS, Python 3.12.13:

- Built the wheel and installed it with pytest into a clean environment.
- Full test suite against that install: **312 passed, 8 skipped** (Linux
  worker cases), plus 152 subtests. Local contract gates passed: 18 MCP
  tools and 18 CLI fallbacks.
- The installed wheel controlled CPU-only jobs over SSH on a Linux host
  with Python 3.12.3. A prepared gate stayed closed until authorized;
  repeated preparation retained the same supervisor. Jobs survived the
  launching local frontend's exit. Stopping a family drained its detached,
  clean-environment child while a separate job stayed running. A background
  descendant timed out with a drained completion receipt. All four test
  jobs were confirmed quiet after cleanup.
- Native Windows argument selection was tested by simulation; actual
  Windows execution is covered separately by the CI portability job.

This evidence covers the generic process substrate. It does not establish
NPU execution, model correctness, or performance.

---

Previous record:

## Standalone Extraction (2026-09-07)

remote-dev was extracted from `.remote-dev/` in the vllm-ascend-workspace
scaffold with `git filter-repo` (history preserved, files moved to the repo
root) and then decoupled in reviewable commits: explicit-only endpoint
resolution with a resolver plugin interface, removal of the VAWS task facade
and the coordinator job supervisor, and environment-configured runtime
preamble / mux dir / state dir. Local gates at extraction time on macOS with
Python 3.11:

- `python3 -m compileall -q .` passes.
- `python3 -m unittest discover -s tests` passed at extraction (the
  extraction handoff note was removed when the repo became an installable
  package).
- `python3 tools/validate_remote_dev_scaffold.py --local-only` passed:
  18 MCP tools, 18 CLI fallbacks, max 3 tool-specific required fields.

Nothing below this line was rerun on the standalone checkout. All live
endpoint, managed-session, parity, serving, benchmark and profiling evidence
was collected inside the scaffold and depends on scaffold components
(`session-management`, `remote-code-parity`, VAWS sessions) that are not part
of this repository. The remote-endpoint behaviour of the standalone code is
therefore **unproven without hardware** until the live smoke is rerun with
`--host/--port` against a reachable SSH host.

## Current Client Compatibility Evidence (2026-08-27)

See [client compatibility](CLIENT_COMPATIBILITY.md) for client-native config
locations, common object schemas, portable wire names, approval behavior, and
version-specific real-client smoke results. Kimi Code, Claude + DeepSeek V4,
Codex, Grok, and Cursor IDE all passed real MCP patch/read/multi-edit/read checks
on code/config revision `1f93400`. Cursor CLI separately loaded all 18 tools but
its model request needs a fresh login and is not counted as passing. The focused
regression set passes 63 tests remotely and in Python 3.9/3.12 CI. The full
82-test run retains 8 failures in two existing Claude skill-shim checks; base
commit `2176b48` reproduces the same failures in 73 tests. It is not a fully
passing suite.

## Historical Evidence (2026-05-25)

The following records describe the earlier validation snapshot, not a rerun on
the current checkout. Its endpoint `anyOf` representation is superseded by the
portable schemas documented above; endpoint validation remains server-enforced.

- Local contract gates pass:
  - `python3 -m compileall -q .remote-dev .agents`
  - `python3 -m unittest discover -s .remote-dev/tests` -> 71 tests
  - `python3 -m unittest discover -s .agents/tests` -> 15 tests
  - `python3 .remote-dev/tools/sync_claude_skills.py --check`
  - `git diff --check -- .remote-dev .agents AGENTS.md CLAUDE.md .mcp.json .codex .claude .gitignore`
- `validate_remote_dev_scaffold.py --local-only` passes and reports:
  - 18 MCP tools
  - 18 CLI fallbacks
  - endpoint selector `anyOf` expressed by normal remote tool schemas
  - max tool-specific required fields: 3
- Direct live endpoint validation passed on `<direct-validation-endpoint>` with 3 parallel
  scratch workers. Covered probe, context snapshot, bash success/failure/timeout,
  cwd guards, read/edit/write, ls, glob, grep, apply_patch, artifact
  manifest/pull/push, background jobs, MCP job stdout resource, MCP artifact
  manifest resource, and cleanup.
- Two managed VAWS sessions were created concurrently on `<managed-validation-host>`:
  - `<validation-session-a>` with isolated worktree, container, and SSH port.
  - `<validation-session-b>` with isolated worktree, container, and SSH port.
- Both sessions passed `validate_remote_dev_scaffold.py --session-id ...` with
  2 parallel scratch workers each.
- Repo-root `.mindie-local/current-session.json` hash stayed unchanged:
  `2d6fdc38c2fae31b165177210ccbfb974863777d7b7d6273edbdcb18b9146525`.
- Both scratch sessions were removed with container, worktree, and lease cleanup;
  validation session records now show `status=removed`, and lease maps are empty.
- Subagent-driven stress validation created three additional managed sessions
  across two remote hosts, each with 10 parallel scratch workers. The initial
  run exposed a Codex-format `remote.apply_patch` virtual-state bug for patches
  that add a file and update/move it in the same payload. After the fix, a
  10-worker managed-session rerun and a 10-worker direct-endpoint rerun both
  passed.
- Remote-toolbox stress passed on both stress hosts with parallelism 8, 24
  concurrent `remote_exec` checks, 12 background jobs, and 256 artifact files
  per host. Stress sessions and leases were cleaned up afterward.
- Two NPU-leased managed sessions were created concurrently on `<npu-validation-host>`
  for heavy parallel validation:
  - `<heavy-session-a>` with isolated container SSH port and one leased NPU.
  - `<heavy-session-b>` with isolated container SSH port and one leased NPU.
- The two heavy sessions passed `remote-code-parity` in `source-only` and
  `materialize` modes from distinct local worktrees. Both runs used isolated
  workspace ids, cache lock paths, and manifest paths. Distinct worktree
  markers were materialized and verified remotely:
  - A: `remote_dev_parity_marker.txt` contained
    `<heavy-session-a>` identity and root commit `63cc52f`.
  - B: `remote_dev_parity_marker.txt` contained
    `<heavy-session-b>` identity and root commit `9185598`.
- Parallel service lifecycle passed with `/home/weights/Qwen3-0.6B`:
  - A: ready on its leased device and service port; stopping A left B ready.
  - B: ready on its leased device and service port; after A stopped, B still
    reported `alive=true`, `health=true`, and `models_ok=true`.
- Parallel benchmark passed in both sessions with a tiny random workload
  (`num_prompts=2`, `max_concurrency=1`, `input_len=8`, `output_len=8`):
  - A result was written under session-local `.mindie-local/sessions/<session-id>/benchmark/runs/`,
    status `ok`, output throughput about `2.13`.
  - B result was written under session-local `.mindie-local/sessions/<session-id>/benchmark/runs/`,
    status `ok`, output throughput about `2.25`.
  - After benchmark cleanup, both sessions reported `service_alive.ok=false`
    and `live_leases.service_ports=[]`.
- Parallel profiling collection passed in both sessions with the same tag
  `remote-dev-same-tag`, proving run directories do not collide:
  - A and B manifests were written under distinct
    `.mindie-local/ascend-profiling-collection/runs/<timestamp>_<tag>_<session>_<pid>_<uuid>/`
    directories.
  - Both manifests ended with `status=ok`, `workload_status.status=ok`,
    `rank_count=1`, `analysis_status=ok`, and verified
    `kernel_details.csv` plus `trace_view.json`.
- Final host probe on `<npu-validation-host>` after benchmark and profiling cleanup
  showed all 8 NPU devices free and no busy HBM entries.
- The two heavy sessions were removed with container, worktree, and lease
  cleanup. Post-cleanup status showed `status=removed`, both worktree paths
  absent, and central leases for `<npu-validation-host>` empty.

## Fixes Made During Validation

- CLI fallback errors now return a JSON `remote-dev.result.v1` result instead of
  leaking tracebacks.
- `remote.apply_patch` schema now requires either `patch` or `command`.
- Artifact pull blocks unsafe manifest relpaths before writing local files.
- Hook wrappers are covered by subprocess tests for permissive Claude/Codex
  allow behavior.
- Unified `remote.apply_patch` records before sha and real diffstat.
- Codex-format `remote.apply_patch` validates all ops before writing and rolls
  back best-effort if commit fails.
- Codex-format `remote.apply_patch` now lets later ops read virtual files added
  earlier in the same patch payload.
- Unified-diff `remote.apply_patch` rejects symlink and non-regular targets in
  remote preflight before `git apply`.
- Background `remote.bash` validates cwd before launching a job and preserves the
  same cwd error statuses as foreground bash.
- Direct endpoints default to full remote-path permission (`root=/`) while
  keeping `/vllm-workspace` as the default cwd; pass an explicit narrower root
  when validating path isolation.
- Read ledgers are scoped by client context/session and now act as optimistic
  concurrency checks when present; they are not required for default edit/write
  permission.
- Claude/Codex hook examples now cover permissive MCP remote-tool and raw shell
  behavior; hooks default to allow.
- Remote read, grep, job-tail, and bash text output are capped; full logs remain
  available through refs/resources.
- Claude project skills are lightweight generated shims that point back to the
  canonical `.agents/skills/<name>/SKILL.md` sources instead of full mirrors.
- The MCP server sets a process-local `REMOTE_DEV_SESSION_ID` so default read
  ledgers are isolated per server process.
- Remote toolbox explicit `--job-id` duplicates are blocked before remote process
  launch; non-ok `remote_job_start.py` statuses now exit nonzero.
- Added `validate_remote_dev_scaffold.py` as a repeatable JSON-reporting local
  and live validation entry point.
- Memory profiling and profiling collection run directories now include safe
  tags, target/session identity, pid, and a uuid suffix instead of only
  second-level timestamp plus tag.
- Benchmark results are now persisted under session-local
  `.mindie-local/sessions/<session-id>/benchmark/runs/` paths.
- `session_status.py` now reports `live_leases` from the central lease map so
  active service ports are visible even though the session creation record is
  static.

## Remaining High-Value Validation

- Full `remote-code-parity --apply-mode install` was intentionally not run in
  the two scratch sessions because it would replace image-provided editable
  packages and trigger remote rebuild/install work. `source-only` and
  `materialize` were validated against distinct session worktrees.
- `ascend-memory-profiling` was not run end-to-end because profiling collection
  already covered real profiler artifacts, and the memory-profiling collision
  risk is now covered by local run-dir regression tests.

Runtime feedback coverage: `tests/test_diagnostics.py` checks read-only mux comparison, explicit direct HTTP, HTTP 502 classification, URL diagnostic redaction, and immutable startup identity across installation changes. Transport fixtures remain local; these tests do not establish an Ascend runtime result.

## CLI feedback (2026-09-11)

On Windows/Python 3.13.12, 20 alternating baseline/candidate fresh-process samples
against `34a460d` measured root-help median 189 to 161 ms (p95 203 to 173 ms),
read-help 178 to 152 ms, invalid options 191 to 163 ms, and missing endpoints
177 to 151 ms. Both revisions used source worktrees and identical dependencies.
Output and exit codes matched; generated result metadata was excluded from the
structured endpoint-error comparison. OS caches were not reset.

Parser and client parity tests: 46 passed, 13 platform skips, 41 subtests. Fresh
subprocess guards cover all 19 tool help paths plus parser failures, rejecting
execution imports, sockets and child processes and checking for home-directory
side effects. These are client feedback measurements, not SSH or NPU results.

## Connection feedback (2026-09-11)

Ten alternating pairs on a Windows client and one Linux SSH endpoint compared
three separate related read-only queries with one request gathering the same
OS/cwd/Python facts. Native independent connections were used in both arms.
Median total latency was 13.768 versus 4.601 seconds (67% lower); sample p95
was 14.167 versus 4.740 seconds. At ten samples p95 is the sample maximum;
this is exploratory endpoint-specific evidence, not a general network SLA.
Returned facts matched in every pair. The new diagnostic batches those facts
in its one fixed request, without importing model/device libraries.

Three real diagnostic probes succeeded. Total SSH duration ranged 4.587-4.818
seconds, TCP milestone 0.300-0.584 seconds, and authentication milestone
2.891-3.170 seconds. The fixed remote facts query took about 0.012 ms;
stream drain/exit took 5.7-7.1 ms. Milestones are local receive timestamps,
not packet-level measurements. Unknown transfer/decode time in traced mode is
not fabricated. Raw private endpoint identities remain in local receipts.

Mocked/local-pipe tests cover successful and timed-out transport, milestone
ordering, output preservation, absent phase information, fixed-probe batching,
and no business-command replay. These tests do not establish NPU behavior.
