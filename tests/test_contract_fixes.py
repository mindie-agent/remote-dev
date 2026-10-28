from __future__ import annotations

import os
import subprocess
import shutil
import shlex
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.core.job_ops as job_ops
import remote_dev.core.state_store as state_store
from remote_dev.core.endpoint import Endpoint, _direct_endpoint, resolve_endpoint
from remote_dev.core.errors import CALLER_ERROR_HINTS, RemoteExecutionError, error_details
from remote_dev.core.path_policy import PathPolicyError
from remote_dev.mcp import tools
from remote_dev.mcp.native_handlers_codex import _allocate_session_id, _native_session_id, JS_MAX_SAFE_INTEGER
from remote_dev.mcp.native_spec import HarnessProfileError
from remote_dev.mcp.schemas import ENDPOINT_SELECTOR_DESCRIPTION, harness_catalog
from remote_dev.mcp.profiles_codex import _codex_session_id, _codex_write_stdin


class CallerErrorContractTests(unittest.TestCase):
    def _details(self, payload):
        with self.assertRaises(Exception) as caught:
            resolve_endpoint(payload)
        return error_details(caught.exception)

    def test_host_only_is_endpoint_incomplete_without_echo(self):
        details = self._details({"host": "secret-host.invalid"})
        self.assertEqual((details["error_code"], details["field"]), ("endpoint_incomplete", "port"))
        self.assertNotIn("secret-host", repr(details))
        self.assertEqual(details["submission_state"], "not_sent")

    def test_port_only_identifies_host(self):
        with self.assertRaises(Exception) as caught:
            _direct_endpoint({"port": 2222}, caller_fields=set())
        details = error_details(caught.exception)
        self.assertEqual((details["error_code"], details["field"]), ("endpoint_incomplete", "host"))
        self.assertEqual(details["submission_state"], "not_sent")
        self.assertNotIn("2222", repr(details))

    def test_existing_alias_without_port_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            alias_file = Path(tmp) / "endpoints.json"
            alias_file.write_text('{"endpoints": {"half": {"host": "secret-host.invalid"}}}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"REMOTE_DEV_ENDPOINTS_FILE": str(alias_file)}):
                details = self._details({"alias": "half"})
        self.assertEqual((details["error_code"], details["field"]), ("endpoint_incomplete", "port"))
        self.assertEqual(details["submission_state"], "not_sent")
        self.assertNotIn("secret-host", repr(details))

    def test_no_target_gives_selector_hint(self):
        details = self._details({})
        self.assertEqual((details["error_code"], details["field"]), ("endpoint_incomplete", "host"))
        self.assertEqual(details["submission_state"], "not_sent")
        self.assertIn("alias", CALLER_ERROR_HINTS[details["error_code"]])
        self.assertIn("together", CALLER_ERROR_HINTS[details["error_code"]])

    def test_unknown_alias(self):
        details = self._details({"alias": "no-such-alias-x"})
        self.assertEqual((details["error_code"], details["field"]), ("endpoint_alias_unknown", "alias"))
        self.assertNotIn("no-such-alias", repr(details))

    def test_hints_are_closed_and_static(self):
        self.assertEqual(set(CALLER_ERROR_HINTS), {
            "invalid_argument", "endpoint_incomplete", "endpoint_alias_unknown",
            "unknown_job", "path_policy", "unsupported_shell_for_runtime_env"})
        self.assertTrue(all(isinstance(v, str) and v for v in CALLER_ERROR_HINTS.values()))

    def test_native_validation_field(self):
        with self.assertRaises(HarnessProfileError) as caught:
            tools.call_tool("remote_exec_command", {"cmd": 3}, profile="codex")
        details = error_details(caught.exception)
        self.assertEqual((details["error_code"], details["field"]), ("invalid_argument", "cmd"))

    def test_path_policy(self):
        details = error_details(PathPolicyError("x"))
        self.assertEqual((details["error_code"], details["field"]), ("path_policy", "path"))

    def test_plain_value_error_not_marked_not_sent(self):
        details = error_details(ValueError("boom"))
        self.assertNotIn("submission_state", details)
        self.assertNotIn("error_code", details)

    def test_uncertain_submission_preserved(self):
        exc = RemoteExecutionError("lost", category="remote_execution", submission_state="uncertain")
        details = error_details(ValueError("wrapper"))
        self.assertNotIn("submission_state", details)
        try:
            try:
                raise exc
            except RemoteExecutionError as inner:
                raise ValueError("wrapped") from inner
        except ValueError as outer:
            self.assertEqual(error_details(outer)["submission_state"], "uncertain")

    def test_unknown_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_store, "substrate_root", lambda: Path(tmp)):
                with self.assertRaises(Exception) as caught:
                    job_ops.remote_job_tail(Endpoint(host="h", port=1, root="/r", cwd="/r"), job_id="nope")
        details = error_details(caught.exception)
        self.assertEqual((details.get("error_code"), details.get("field")), ("unknown_job", "job_id"))


class CatalogGuidanceTests(unittest.TestCase):
    def test_native_schemas_show_selector_guidance(self):
        catalog = {tool["name"]: tool for tool in harness_catalog("codex")}
        for name in ("remote_exec_command", "remote_apply_patch", "remote_view_image"):
            schema = catalog[name]["inputSchema"]
            self.assertIn(ENDPOINT_SELECTOR_DESCRIPTION, schema["description"], name)
            self.assertIn("together", schema["properties"]["host"]["description"])
            self.assertIn("together", schema["properties"]["port"]["description"])

    def test_write_stdin_guidance_is_job_record_truthful(self):
        catalog = {tool["name"]: tool for tool in harness_catalog("codex")}
        text = catalog["remote_write_stdin"]["inputSchema"]["description"]
        self.assertIn("job record", text)
        self.assertIn("optional", text)
        self.assertNotIn("at least one endpoint selector", text.lower())

    def test_token_budget_text_matches_policy(self):
        catalog = {tool["name"]: tool for tool in harness_catalog()}
        for name in ("remote_job_tail", "remote_job_output", "remote_job_status"):
            text = catalog[name]["inputSchema"]["properties"]["max_output_tokens"]["description"]
            self.assertNotIn("4 UTF-8 bytes per token", text)
            self.assertIn("2 * tokens", text)

    def test_job_status_stream_property(self):
        catalog = {tool["name"]: tool for tool in harness_catalog()}
        stream = catalog["remote_job_status"]["inputSchema"]["properties"]["stream"]
        self.assertEqual(stream["enum"], ["stdout", "stderr", "both"])


class SessionIdTests(unittest.TestCase):
    def test_allocated_ids_are_js_safe_and_collision_checked(self):
        taken = set()
        first = _allocate_session_id(lambda x: None)
        taken.add(first)
        second = _allocate_session_id(lambda x: object() if x in taken else None)
        self.assertNotEqual(first, second)
        self.assertLessEqual(int(second), JS_MAX_SAFE_INTEGER)

    def test_native_session_id_type(self):
        self.assertEqual(_native_session_id("123"), 123)
        self.assertEqual(_native_session_id("9" * 19), "9" * 19)
        self.assertEqual(_native_session_id("job-abc"), "job-abc")

    def test_string_session_ids_rejected_by_native_schema(self):
        with self.assertRaises(HarnessProfileError):
            tools.call_tool("remote_write_stdin", {"session_id": "9" * 19}, profile="codex")
        with self.assertRaises(Exception) as caught:
            _codex_session_id("9" * 19)
        self.assertEqual(error_details(caught.exception)["field"], "session_id")

    def test_native_description_documents_recovery_route(self):
        catalog = {tool["name"]: tool for tool in harness_catalog("codex")}
        schema = catalog["remote_write_stdin"]["inputSchema"]
        self.assertEqual(schema["properties"]["session_id"]["type"], "number")
        text = schema["properties"]["session_id"]["description"]
        for token in ("remote_job_status", "remote_job_tail", "remote_job_stdin", "string"):
            self.assertIn(token, text)

    def test_legacy_numeric_string_job_id_usable_on_canonical_tools(self):
        legacy = "9" * 19
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(state_store, "substrate_root", lambda: Path(tmp)):
                endpoint = resolve_endpoint({"host": "127.0.0.1", "port": 46000})
                record = {"schema_version": "remote-dev.job.v1", "job_id": legacy,
                          "target": endpoint.to_result_target(), "remote_dir": "/srv/.remote-dev/jobs/" + legacy,
                          "started_at": "2026-09-01T00:00:00Z", "state": "running"}
                state_store.atomic_write_json(state_store.job_record_path(endpoint, legacy), record)
                sup = {"state": "succeeded", "stdout": "out", "stderr": ""}
                with mock.patch.object(job_ops, "control", return_value=sup):
                    tail = tools.call_tool("remote_job_tail", {"session_id": legacy, "host": "127.0.0.1", "port": 46000})
                    status = tools.call_tool("remote_job_status", {"job_id": legacy, "host": "127.0.0.1", "port": 46000})
                self.assertEqual(tail["result"]["job_id"], legacy)
                self.assertIn(legacy, repr(status))

    def test_integral_float_and_rejections(self):
        self.assertEqual(_codex_session_id(12345.0), "12345")
        for bad in (True, 1.5, -1, float("inf"), float("nan"), 2**53):
            with self.subTest(bad=bad), self.assertRaises(Exception) as caught:
                _codex_session_id(bad)
            self.assertEqual(error_details(caught.exception)["field"], "session_id")

    def test_write_stdin_clamps_and_default_budget(self):
        out = _codex_write_stdin({"session_id": 5, "chars": "x", "yield_time_ms": 999999})
        self.assertEqual((out["yield_time_ms"], out["max_output_tokens"]), (30000, 10000))
        self.assertEqual(_codex_write_stdin({"session_id": 5, "chars": "x", "yield_time_ms": 1})["yield_time_ms"], 250)
        self.assertEqual(_codex_write_stdin({"session_id": 5, "yield_time_ms": 1})["yield_time_ms"], 5000)
        self.assertEqual(_codex_write_stdin({"session_id": 5, "yield_time_ms": 10**9})["yield_time_ms"], 300000)


class RuntimeEnvShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = os.path.join(self.tmp.name, "env.sh")
        Path(self.env_file).write_text("PLAIN=hidden\nmyfn() { echo fn-$PLAIN; }\n")
        self.endpoint = Endpoint(host="h", port=1, root="/r", cwd="/r", runtime_env_file=self.env_file)

    def _run(self, script):
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash") and shutil.which("sh"), "local POSIX shell execution")
    def test_function_and_nonexported_var_visible(self):
        for shell, login in ((None, None), ("bash", False), ("sh", False)):
            with self.subTest(shell=shell):
                script = job_ops._job_command(self.endpoint, 'myfn; echo "$PLAIN"', True, shell, login)
                done = self._run(script)
                self.assertEqual(done.stdout, "fn-hidden\nhidden\n", done.stderr)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "local bash login execution")
    def test_login_function_visible_and_failing_env_blocks_command(self):
        home = os.path.join(self.tmp.name, "home")
        os.makedirs(home)
        marker = os.path.join(self.tmp.name, "ran")
        env = {**os.environ, "HOME": home}
        good = job_ops._job_command(self.endpoint, 'myfn; echo "$PLAIN"', True, "bash", True)
        done = subprocess.run(["bash", "-c", good], capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(done.stdout, "fn-hidden\nhidden\n", done.stderr)
        failing = os.path.join(self.tmp.name, "bad.sh")
        Path(failing).write_text("return 7\n")
        broken = Endpoint(host="h", port=1, root="/r", cwd="/r", runtime_env_file=failing)
        script = job_ops._job_command(broken, f"touch {marker}", True, "bash", True)
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=20, env=env)
        self.assertEqual(done.returncode, 7)
        self.assertFalse(os.path.exists(marker))

    def test_no_selected_shell_preserves_existing_preamble(self):
        command = "echo a"
        expected = "\n".join([*job_ops.runtime_env_lines(self.endpoint, True), f"eval -- {shlex.quote(command)}"])
        self.assertEqual(job_ops._job_command(self.endpoint, command, True), expected)

    def test_no_preamble_unchanged(self):
        self.assertEqual(job_ops._job_command(self.endpoint, "echo a", False), "echo a")
        self.assertEqual(job_ops._job_command(self.endpoint, "echo a", False, "bash", False),
                         "exec bash -c 'echo a'")

    def test_non_posix_shell_rejected(self):
        with self.assertRaises(Exception) as caught:
            job_ops._job_command(self.endpoint, "echo a", True, "fish", False)
        details = error_details(caught.exception)
        self.assertEqual((details["error_code"], details["field"]),
                         ("unsupported_shell_for_runtime_env", "shell"))


class TailBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = state_store.substrate_root
        state_store.substrate_root = lambda: Path(self.tmp.name)
        self.addCleanup(setattr, state_store, "substrate_root", original)
        self.endpoint = resolve_endpoint({"host": "127.0.0.1", "port": 46000})
        record = {"schema_version": "remote-dev.job.v1", "job_id": "job-t1",
                  "target": self.endpoint.to_result_target(), "remote_dir": "/.remote-dev/jobs/job-t1",
                  "started_at": "2026-09-01T00:00:00Z", "state": "running"}
        state_store.atomic_write_json(state_store.job_record_path(self.endpoint, "job-t1"), record)

    def test_low_budget_keeps_newest_utf8_safe(self):
        sup = {"state": "succeeded", "stdout": "头" * 2000, "stderr": ""}
        with mock.patch.object(job_ops, "control", return_value=sup):
            for call in (job_ops.remote_job_tail, job_ops.remote_job_output):
                payload = call(self.endpoint, job_id="job-t1", max_output_tokens=10)
                result = payload["result"]
                self.assertTrue(result["truncated"])
                self.assertGreater(result["omitted_bytes"], 0)
                self.assertLessEqual(len(result["preview"]["tail"].encode()), 40)

    def _fake_tail(self, timeout_ms, *, yield_ms=1000, jump_after_first=None, state="running"):
        now = [0.0]
        reads = [0]
        sleeps = []
        calls = []

        def tick():
            reads[0] += 1
            if jump_after_first is not None and calls:
                now[0] = max(now[0], jump_after_first)
            return now[0]

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        def control(*args, **kwargs):
            calls.append({"at": now[0], "timeout_ms": kwargs.get("timeout_ms")})
            return {"state": state, "stdout": "data", "stderr": ""}

        clock = types.SimpleNamespace(monotonic=tick, sleep=sleep)
        record_path = Path(self.tmp.name) / "record.json"
        with mock.patch.object(job_ops, "time", clock), \
                mock.patch.object(job_ops, "_load_record", return_value=(self.endpoint, {}, record_path)), \
                mock.patch.object(job_ops, "job_log_refs", return_value={}), \
                mock.patch.object(job_ops, "control", control):
            payload = job_ops._job_tail_impl(self.endpoint, job_id="job-t1", timeout_ms=timeout_ms,
                                             yield_time_ms=yield_ms)
        return payload, calls, sleeps, now[0]

    def test_one_ms_deadline_bounds_sleep_and_controls(self):
        payload, calls, sleeps, elapsed = self._fake_tail(1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["timeout_ms"], 1)
        self.assertTrue(all(0.0 <= value <= 0.001 + 1e-9 for value in sleeps), sleeps)
        self.assertLessEqual(elapsed, 0.001 + 1e-9)
        self.assertEqual(payload["result"]["state"], "running")

    def test_polling_sleeps_never_exceed_remaining_and_pass_remaining_timeout(self):
        payload, calls, sleeps, elapsed = self._fake_tail(2500, yield_ms=10000)
        self.assertLessEqual(elapsed, 2.5 + 1e-9)
        self.assertTrue(all(value <= 1.0 for value in sleeps))
        self.assertAlmostEqual(sum(sleeps), 2.5, places=6)
        for call in calls:
            self.assertLessEqual(call["timeout_ms"], 2500 - int(call["at"] * 1000) + 1)
            self.assertGreaterEqual(call["timeout_ms"], 1)
        self.assertEqual(payload["result"]["state"], "running")

    def test_no_control_call_after_deadline(self):
        payload, calls, sleeps, elapsed = self._fake_tail(1, jump_after_first=0.002)
        self.assertEqual(len(calls), 1)
        self.assertEqual(payload["result"]["state"], "running")

    def test_first_call_expiry_makes_no_control_call(self):
        calls = []
        clock = types.SimpleNamespace(monotonic=iter([0.0, 5.0, 5.0, 5.0]).__next__, sleep=lambda seconds: None)
        record_path = Path(self.tmp.name) / "record.json"
        with mock.patch.object(job_ops, "time", clock), \
                mock.patch.object(job_ops, "_load_record", return_value=(self.endpoint, {}, record_path)), \
                mock.patch.object(job_ops, "control", side_effect=lambda *a, **k: calls.append(k)):
            with self.assertRaises(TimeoutError):
                job_ops._job_tail_impl(self.endpoint, job_id="job-t1", timeout_ms=1)
        self.assertEqual(calls, [])

    def test_timeout_passthrough(self):
        sup = {"state": "succeeded", "stdout": "a", "stderr": ""}
        with mock.patch.object(job_ops, "control", return_value=sup) as mocked:
            job_ops.remote_job_tail(self.endpoint, job_id="job-t1", timeout_ms=5000)
        self.assertLessEqual(mocked.call_args.kwargs["timeout_ms"], 5000)
        self.assertGreaterEqual(mocked.call_args.kwargs["timeout_ms"], 1)

    def test_real_budget_sizes(self):
        sup = {"state": "succeeded", "stdout": "x" * 500, "stderr": ""}
        with mock.patch.object(job_ops, "control", return_value=sup):
            for tokens, limit in ((10, 20), (1, 4)):
                payload = job_ops.remote_job_tail(self.endpoint, job_id="job-t1", max_output_tokens=tokens)
                self.assertEqual(len(payload["result"]["preview"]["tail"].encode()), limit)

    def test_manage_tail_stream_stderr_only(self):
        sup = {"state": "succeeded", "stdout": "OUTTEXT", "stderr": "ERRTEXT"}
        with mock.patch.object(job_ops, "control", return_value=sup):
            payload = tools.call_tool("remote.job_status",
                                      {"job_id": "job-t1", "action": "tail", "stream": "stderr",
                                       "host": "127.0.0.1", "port": 46000})
        self.assertIn("ERRTEXT", payload["text"])
        self.assertNotIn("OUTTEXT", payload["text"])

    def test_manage_tail_propagates_budget(self):
        with mock.patch.object(tools, "remote_job_tail", return_value={"text": "", "result": {}}) as mocked:
            tools.call_tool("remote.job_status",
                            {"job_id": "job-t1", "action": "tail", "max_output_tokens": 7, "timeout_ms": 900,
                             "host": "127.0.0.1", "port": 46000})
        self.assertEqual(mocked.call_args.kwargs["max_output_tokens"], 7)
        self.assertEqual(mocked.call_args.kwargs["timeout_ms"], 900)


if __name__ == "__main__":
    unittest.main()
