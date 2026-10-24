"""Independent native-contract differential tests (Agent C).

Derived from the installed-artifact baseline in ``fixtures/native-baseline.json`` and
the run-dir evidence.  Nothing here is copied from the implementation's constants or
from the implementation's own tests.  The native side is the baseline; the
implementation under test is the clean ``6d33ad7`` checkout on PYTHONPATH.

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


HERE = Path(__file__).resolve().parent
BASELINE = json.loads((HERE / "fixtures" / "native-baseline.json").read_text(encoding="utf-8"))
BY_HARNESS = {item["harness"]: item for item in BASELINE["harnesses"]}

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
    def test_canonical_catalog_is_still_19(self) -> None:
        catalog = harness_catalog("canonical")
        self.assertEqual(len(catalog), 19)
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
            ("dsh", "remote_read"): {"offset": 1, "limit": 2000},
            ("dsh", "remote_job_output"): {"wait": False},
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

        for bad in (
            {"cmd": "true", "shell": "bash"},
            {"cmd": "true", "login": True},
            {"cmd": "true", "sandbox_permissions": "require_escalated"},
        ):
            with self.subTest(args=bad):
                with self.assertRaises(HarnessProfileError):
                    tools.call_tool("remote_exec_command", bad, profile="codex")

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
        self.assertEqual(dsh_job["wait"]["default"], False)
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
        with (
            mock.patch.object(harness_native, "_job_record", return_value=record),
            mock.patch.object(harness_native, "_assert_visible", return_value=None),
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
        with (
            mock.patch(
                "remote_dev.core.state_store.find_job_record",
                return_value=(Path("/tmp/fake-job.json"), record),
            ),
            mock.patch.object(harness_native, "_assert_visible", return_value=None),
            mock.patch.object(harness_native, "_job_file", return_value=Path("/tmp/fake-stdout.log")),
            mock.patch.object(harness_native, "_tail_text", return_value=("out", False)),
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


# ---------------------------------------------------------------------------
# 4. Known native gaps that must fail loudly (not weakened)
# ---------------------------------------------------------------------------

class NativeGapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeCanonical()
        self.patch = mock.patch.object(tools, "_call_canonical", self.fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_claude_read_pdf_pages_are_normalized_before_explicit_unsupported(self) -> None:
        # The profile must preserve native `pages` through normalization; the
        # canonical worker then returns an explicit `pdf_unsupported` status
        # (covered by WorkerFileTests.test_read_pdf_is_explicitly_unsupported).
        tools.call_tool(
            "remote_Read",
            {"file_path": "/tmp/doc.pdf", "pages": "1-2"},
            profile="claude",
        )
        self.assertEqual(self.fake.last[0], "remote.read")
        self.assertEqual(self.fake.last[1]["pages"], "1-2")

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
        target.write_text("a\nb\nc\n", encoding="utf-8")
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
        self.assertEqual(target.read_text(encoding="utf-8"), "a\nc\n")

        ambiguous = self.root / "ambiguous.txt"
        ambiguous.write_text("x\nx\n", encoding="utf-8")
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
        self.assertIn(str(self.target), data["matches"])

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
