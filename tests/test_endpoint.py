from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import remote_dev.core.endpoint as endpoint_module  # noqa: E402
from remote_dev.core.errors import error_details
from remote_dev.core.endpoint import (  # noqa: E402
    BUILTIN_SELECTOR_FIELDS,
    Endpoint,
    EndpointError,
    clear_resolvers,
    has_selector,
    register_resolver,
    registered_resolvers,
    resolve_endpoint,
    resolver_setup,
    selector_fields,
    unregister_resolver,
)


class EndpointTests(unittest.TestCase):
    def test_endpoint_id_is_stable_and_redacts_from_state_path(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        self.assertEqual(endpoint.endpoint_id, Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace").endpoint_id)
        self.assertEqual(len(endpoint.endpoint_id), 16)
        self.assertNotIn("1.2.3.4", endpoint.endpoint_id)

    def test_direct_endpoint_defaults(self) -> None:
        endpoint = resolve_endpoint({"host": "1.2.3.4", "port": 46000})
        self.assertEqual(endpoint.user, "root")
        self.assertEqual(endpoint.root, "/")
        self.assertEqual(endpoint.effective_cwd, "/")
        self.assertEqual(endpoint.kind, "direct-endpoint")
        self.assertIsNone(endpoint.ssh_mux)
        self.assertFalse(endpoint.keepalive)
        self.assertNotIn("ssh_mux", endpoint.to_result_target())
        self.assertNotIn("keepalive", endpoint.to_result_target())

    def test_direct_endpoint_accepts_ssh_mux_and_keepalive(self) -> None:
        independent = resolve_endpoint({"host": "192.0.2.10", "port": 22, "ssh_mux": False, "keepalive": True})
        self.assertIs(independent.ssh_mux, False)
        self.assertTrue(independent.keepalive)
        target = independent.to_result_target()
        self.assertIs(target["ssh_mux"], False)
        self.assertIs(target["keepalive"], True)
        shared = resolve_endpoint({"host": "192.0.2.10", "port": 22, "ssh_mux": True})
        self.assertIs(shared.ssh_mux, True)
        self.assertFalse(shared.keepalive)
        self.assertIs(shared.to_result_target()["ssh_mux"], True)
        self.assertNotIn("keepalive", shared.to_result_target())

    def test_direct_endpoint_rejects_non_boolean_ssh_mux_or_keepalive(self) -> None:
        for key, value in (("ssh_mux", "0"), ("ssh_mux", 0), ("keepalive", "true"), ("keepalive", 1)):
            with self.assertRaises(EndpointError):
                resolve_endpoint({"host": "192.0.2.10", "port": 22, key: value})

    def test_for_long_stream_sets_independent_keepalive_and_cannot_be_half_configured(self) -> None:
        endpoint = Endpoint.for_long_stream("192.0.2.10", 46000, identity_file="/keys/a")
        self.assertIs(endpoint.ssh_mux, False)
        self.assertTrue(endpoint.keepalive)
        self.assertEqual(endpoint.identity_file, "/keys/a")
        forced = Endpoint.for_long_stream("192.0.2.10", 46000, ssh_mux=False, keepalive=False)
        self.assertIs(forced.ssh_mux, False)
        self.assertTrue(forced.keepalive)
        with self.assertRaises(EndpointError) as raised:
            Endpoint.for_long_stream("192.0.2.10", 46000, ssh_mux=True)
        message = str(raised.exception)
        self.assertIn("rc=0", message)
        self.assertIn("first-option-wins", message)
        self.assertIn("ControlPath", message)

    def test_direct_endpoint_rejects_non_integer_port(self) -> None:
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "1.2.3.4", "port": "not-a-port"})

    def test_direct_endpoint_rejects_garbage_connect_timeout(self) -> None:
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "connect_timeout_ms": "abc"})

    def test_direct_endpoint_rejects_out_of_range_port(self) -> None:
        for port in (70000, -1, 65536, 0):
            with self.assertRaises(EndpointError):
                resolve_endpoint({"host": "203.0.113.5", "port": port})

    def test_direct_endpoint_rejects_relative_root_or_cwd(self) -> None:
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "root": "relative"})
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "cwd": "relative"})

    def test_runtime_env_file_is_explicit_and_recorded_in_target(self) -> None:
        plain = resolve_endpoint({"host": "1.2.3.4", "port": 22})
        self.assertIsNone(plain.runtime_env_file)
        self.assertNotIn("runtime_env_file", plain.to_result_target())
        configured = resolve_endpoint({"host": "1.2.3.4", "port": 22, "runtime_env_file": "/etc/profile.d/toolchain.sh"})
        self.assertEqual(configured.runtime_env_file, "/etc/profile.d/toolchain.sh")
        self.assertEqual(configured.to_result_target()["runtime_env_file"], "/etc/profile.d/toolchain.sh")


class AliasFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.alias_file = Path(self.temp.name) / "endpoints.json"
        self.alias_file.write_text(
            json.dumps({"endpoints": {"lab": {"host": "10.0.0.5", "port": 2222, "root": "/srv", "cwd": "/srv/app"}}}),
            encoding="utf-8",
        )
        patcher = mock.patch.dict(os.environ, {"REMOTE_DEV_ENDPOINTS_FILE": str(self.alias_file)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_alias_resolves_and_caller_fields_override(self) -> None:
        endpoint = resolve_endpoint({"alias": "lab"})
        self.assertEqual((endpoint.host, endpoint.port, endpoint.root, endpoint.cwd), ("10.0.0.5", 2222, "/srv", "/srv/app"))
        self.assertEqual(endpoint.alias, "lab")
        narrowed = resolve_endpoint({"alias": "lab", "root": "/srv/app"})
        self.assertEqual(narrowed.root, "/srv/app")

    def test_unknown_alias_is_an_endpoint_error(self) -> None:
        with self.assertRaises(EndpointError) as ctx:
            resolve_endpoint({"alias": "missing"})
        self.assertIn("missing", str(ctx.exception))

    def test_alias_file_env_precedes_checkout_local_files(self) -> None:
        files = endpoint_module.alias_files()
        self.assertEqual(
            files[:2],
            [
                Path("~/.mindie/endpoints.json").expanduser(),
                Path("~/.mindie-coordinator/endpoints.json").expanduser(),
            ],
        )
        self.assertEqual(files[2], self.alias_file)
        cwd = Path.cwd()
        self.assertEqual(files[-2:], [cwd / "endpoints.json", cwd / "endpoints.local.json"])

    def test_zero_arg_resolves_default_alias_and_container_dict_inherits_workdir(self) -> None:
        coord_file = Path(self.temp.name) / "coordinator-endpoints.json"
        coord_file.write_text(
            json.dumps(
                {
                    "default_alias": "a2",
                    "endpoints": {
                        "a2": {
                            "host": "173.125.1.2",
                            "port": 22,
                            "user": "root",
                            "container": {
                                "name": "mindie-bench-a2-core-20261017",
                                "id": "b2bb20466bf7",
                                "user": "root",
                                "workdir": "/workspace/mindie",
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"REMOTE_DEV_ENDPOINTS_FILE": str(coord_file)}):
            auto_ep = resolve_endpoint({})
            self.assertEqual(auto_ep.host, "173.125.1.2")
            self.assertEqual(auto_ep.port, 22)
            self.assertEqual(auto_ep.alias, "a2")
            self.assertEqual(auto_ep.container, "b2bb20466bf7")
            self.assertEqual(auto_ep.root, "/workspace/mindie")
            self.assertEqual(auto_ep.cwd, "/workspace/mindie")
            self.assertEqual(auto_ep.effective_cwd, "/workspace/mindie")

            default_ep = resolve_endpoint({"alias": "default"})
            self.assertEqual(default_ep.host, "173.125.1.2")
            self.assertEqual(default_ep.container, "b2bb20466bf7")
            self.assertEqual(default_ep.effective_cwd, "/workspace/mindie")

            overridden_ep = resolve_endpoint({"cwd": "/workspace/mindie/sub"})
            self.assertEqual(overridden_ep.root, "/workspace/mindie")
            self.assertEqual(overridden_ep.cwd, "/workspace/mindie/sub")


class ResolverPluginTests(unittest.TestCase):
    """Consumers inject their own alias/session semantics; remote-dev never
    imports the consumer."""

    def setUp(self) -> None:
        self._saved = list(endpoint_module._RESOLVERS)
        clear_resolvers()
        self.addCleanup(self._restore)
        alias_patcher = mock.patch.object(endpoint_module, "alias_files", return_value=[])
        alias_patcher.start()
        self.addCleanup(alias_patcher.stop)

    def _restore(self) -> None:
        clear_resolvers()
        endpoint_module._RESOLVERS.extend(self._saved)

    def test_no_selector_and_no_resolver_is_rejected_with_guidance(self) -> None:
        with self.assertRaises(EndpointError) as ctx:
            resolve_endpoint({})
        message = str(ctx.exception)
        self.assertIn("no endpoint target", message)
        self.assertIn("registered resolvers: none", message)
        # The former consumer-specific selectors are gone from the substrate.
        self.assertEqual(selector_fields(), BUILTIN_SELECTOR_FIELDS)
        for legacy in ("session_id", "session_file", "machine"):
            self.assertFalse(has_selector({legacy: "x"}), legacy)

    def test_resolver_dict_result_becomes_endpoint_and_caller_overrides_shape(self) -> None:
        seen: list[dict] = []

        def by_session(payload):
            seen.append(payload)
            if not payload.get("session_id"):
                return None
            return {"host": "10.1.1.1", "port": 40000, "cwd": "/work/" + payload["session_id"], "source": {"session": payload["session_id"]}}

        register_resolver(by_session, name="sessions", fields=("session_id",))
        self.assertIn("session_id", selector_fields())
        self.assertTrue(has_selector({"session_id": "s1"}))

        endpoint = resolve_endpoint({"session_id": "s1", "root": "/work"})
        self.assertEqual((endpoint.host, endpoint.port), ("10.1.1.1", 40000))
        self.assertEqual(endpoint.root, "/work")
        self.assertEqual(endpoint.cwd, "/work/s1")
        self.assertEqual(endpoint.kind, "resolver:sessions")
        self.assertEqual(endpoint.source, {"session": "s1", "resolver": "sessions"})
        self.assertEqual(seen[-1]["session_id"], "s1")

        overridden = resolve_endpoint({"session_id": "s1", "ssh_mux": False, "keepalive": True})
        self.assertIs(overridden.ssh_mux, False)
        self.assertTrue(overridden.keepalive)

    def test_resolver_may_return_endpoint_instance(self) -> None:
        register_resolver(lambda payload: Endpoint(host="10.2.2.2", port=22, kind="managed") if payload.get("machine") else None, name="machines", fields=("machine",))
        endpoint = resolve_endpoint({"machine": "npu-a"})
        self.assertEqual(endpoint.kind, "managed")
        self.assertEqual(endpoint.host, "10.2.2.2")

    def test_resolvers_run_in_order_and_declining_resolver_is_skipped(self) -> None:
        calls: list[str] = []

        def first(payload):
            calls.append("first")
            return None

        def second(payload):
            calls.append("second")
            return {"host": "10.3.3.3", "port": 22}

        register_resolver(first, name="first")
        register_resolver(second, name="second")
        endpoint = resolve_endpoint({})
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual(endpoint.host, "10.3.3.3")

    def test_resolver_is_consulted_for_empty_payload_so_consumer_can_auto_bind(self) -> None:
        # The former cwd-upward worktree auto-bind now lives in the consumer.
        register_resolver(lambda payload: {"host": "10.4.4.4", "port": 22} if not payload else None, name="auto-bind")
        self.assertEqual(resolve_endpoint({}).host, "10.4.4.4")

    def test_direct_and_alias_take_precedence_over_resolvers(self) -> None:
        register_resolver(lambda payload: {"host": "10.9.9.9", "port": 9}, name="greedy")
        self.assertEqual(resolve_endpoint({"host": "1.2.3.4", "port": 22}).host, "1.2.3.4")

    def test_resolver_exception_is_reported_as_endpoint_error(self) -> None:
        def broken(payload):
            raise RuntimeError("inventory unreadable")

        register_resolver(broken, name="broken")
        with self.assertRaises(EndpointError) as ctx:
            resolve_endpoint({})
        self.assertIn("broken", str(ctx.exception))
        self.assertIn("inventory unreadable", str(ctx.exception))

    def test_resolver_bad_return_type_is_rejected(self) -> None:
        register_resolver(lambda payload: "10.0.0.1:22", name="stringy")
        with self.assertRaises(EndpointError):
            resolve_endpoint({})

    def test_duplicate_names_rejected_and_unregister_works(self) -> None:
        register_resolver(lambda payload: None, name="dup")
        with self.assertRaises(EndpointError):
            register_resolver(lambda payload: None, name="dup")
        self.assertTrue(unregister_resolver("dup"))
        self.assertFalse(unregister_resolver("dup"))
        self.assertEqual(registered_resolvers(), ())


class EndpointErrorProvenanceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(endpoint_module, "_RESOLVERS", [])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(endpoint_module, "_load_env_resolvers")
        patcher.start()
        self.addCleanup(patcher.stop)

    def resolve(self, source, configured, overrides):
        if source == "direct":
            return resolve_endpoint({**configured, **overrides})
        if source == "alias":
            with mock.patch.object(endpoint_module, "_read_endpoint_aliases",
                                   return_value={"fixture": configured}):
                return resolve_endpoint({"alias": "fixture", **overrides})
        with mock.patch.object(endpoint_module, "_RESOLVERS", []):
            register_resolver(lambda _: configured, name="fixture")
            return resolve_endpoint(overrides)

    def test_explicit_invalid_values_are_caller_not_sent(self):
        invalid = {"root": r"C:\local", "cwd": "relative",
                   "connect_timeout_ms": 0, "ssh_mux": "false", "keepalive": 1}
        for source in ("direct", "alias", "resolver"):
            for key, value in invalid.items():
                with self.subTest(source=source, key=key), self.assertRaises(EndpointError) as caught:
                    self.resolve(source, {"host": "example.invalid", "port": 22}, {key: value})
                self.assertEqual(error_details(caught.exception), {
                    "type": "EndpointError", "category": "caller",
                    "submission_state": "not_sent", "retryable": False,
                    "error_code": "invalid_argument",
                })
        for source in ("direct", "alias"):
            for value in ("bad", -1, True):
                with self.subTest(source=source, port=value), self.assertRaises(EndpointError) as caught:
                    self.resolve(source, {"host": "example.invalid", "port": 22}, {"port": value})
                self.assertEqual(error_details(caught.exception)["category"], "caller")
                self.assertEqual(error_details(caught.exception)["submission_state"], "not_sent")

    def test_invalid_configured_values_are_not_blame_on_caller(self):
        for source in ("alias", "resolver"):
            for key, value in (("root", r"C:\local"), ("cwd", "relative"),
                               ("connect_timeout_ms", 0), ("ssh_mux", "false"),
                               ("keepalive", 1), ("port", -1)):
                with self.subTest(source=source, key=key), self.assertRaises(EndpointError) as caught:
                    self.resolve(source, {"host": "example.invalid", "port": 22, key: value},
                                 {"user": "fixture"})
                self.assertEqual(error_details(caught.exception)["category"], "internal")
                self.assertNotIn("submission_state", error_details(caught.exception))

    def test_empty_paths_retain_default_source_even_when_explicit(self):
        for source in ("direct", "alias", "resolver"):
            for key, default in (("root", "DEFAULT_ROOT"), ("cwd", "DEFAULT_CWD")):
                for value in (None, "", False, 0):
                    with self.subTest(source=source, key=key, value=value):
                        with mock.patch.object(endpoint_module, default, "bad-default"):
                            with self.assertRaises(EndpointError) as caught:
                                self.resolve(source, {"host": "example.invalid", "port": 22},
                                             {key: value})
                        self.assertEqual(error_details(caught.exception)["category"], "internal")
                        self.assertNotIn("submission_state", error_details(caught.exception))

    def test_resolver_endpoint_is_not_reinterpreted_or_overridden(self):
        configured = Endpoint(host="example.invalid", port=22, root="/configured")
        actual = self.resolve("resolver", configured, {
            "root": r"C:\ignored", "cwd": "ignored", "keepalive": "ignored"})
        self.assertIs(actual, configured)

    def test_resolver_port_does_not_gain_caller_override_semantics(self):
        actual = self.resolve("resolver", {"host": "example.invalid", "port": 22}, {"port": -1})
        self.assertEqual(actual.port, 22)
        with self.assertRaises(EndpointError) as caught:
            self.resolve("resolver", {"host": "example.invalid", "port": -1}, {"port": 22})
        self.assertEqual(error_details(caught.exception)["category"], "internal")

    def test_invalid_alias_json_and_resolver_failure_keep_original_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoints.json"
            path.write_text("{invalid", encoding="utf-8")
            with mock.patch.object(endpoint_module, "alias_files", return_value=[path]):
                with self.assertRaises(EndpointError) as caught:
                    resolve_endpoint({"alias": "fixture", "root": r"C:\caller"})
            self.assertNotEqual(error_details(caught.exception)["category"], "caller")
        def failed(_):
            raise RuntimeError("resolver configuration unreadable")
        register_resolver(failed, name="failed")
        with self.assertRaises(EndpointError) as caught:
            resolve_endpoint({"root": r"C:\caller"})
        self.assertEqual(error_details(caught.exception)["category"], "internal")


class EnvResolverLoadingTests(unittest.TestCase):
    """``REMOTE_DEV_RESOLVERS`` lets a consumer load plugins into the MCP
    server process without remote-dev importing anything by name."""

    PLUGIN = '''
from remote_dev.core.endpoint import register_resolver, resolver_setup

def plain(payload):
    if payload.get("box"):
        return {"host": "10.5.5.5", "port": 5, "cwd": "/box/" + payload["box"]}
    return None

@resolver_setup
def setup():
    register_resolver(plain, name="boxes", fields=("box",))
'''

    def _run(self, spec: str, payload: dict) -> dict:
        code = (
            "import json\n"
            "from remote_dev.core.endpoint import resolve_endpoint, selector_fields, registered_resolvers, EndpointError\n"
            "try:\n"
            "    ep = resolve_endpoint(%r)\n"
            "    print(json.dumps({'host': ep.host, 'cwd': ep.cwd, 'kind': ep.kind, 'fields': list(selector_fields()), 'resolvers': [r.name for r in registered_resolvers()]}))\n"
            "except EndpointError as exc:\n"
            "    print(json.dumps({'error': str(exc)}))\n"
        ) % (payload,)
        env = {**os.environ, "REMOTE_DEV_RESOLVERS": spec}
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_setup_hook_from_file_registers_fields_and_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "consumer_plugin.py"
            plugin.write_text(self.PLUGIN, encoding="utf-8")
            data = self._run(f"{plugin}:setup", {"box": "b1"})
        self.assertEqual(data["host"], "10.5.5.5")
        self.assertEqual(data["cwd"], "/box/b1")
        self.assertEqual(data["kind"], "resolver:boxes")
        self.assertIn("box", data["fields"])
        self.assertEqual(data["resolvers"], ["boxes"])

    def test_plain_callable_from_file_is_registered_under_its_spec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "consumer_plugin.py"
            plugin.write_text(self.PLUGIN, encoding="utf-8")
            data = self._run(f"{plugin}:plain", {"box": "b2"})
            self.assertEqual(data["resolvers"], [f"{plugin}:plain"])
        self.assertEqual(data["cwd"], "/box/b2")

    def test_missing_plugin_file_is_a_clear_endpoint_error(self) -> None:
        data = self._run("/nonexistent/plugin.py:setup", {})
        self.assertIn("plugin file does not exist", data["error"])

    def test_module_import_failure_is_a_clear_endpoint_error(self) -> None:
        data = self._run("remote_dev_no_such_module_xyz:setup", {})
        self.assertIn("failed to import", data["error"])


if __name__ == "__main__":
    unittest.main()
