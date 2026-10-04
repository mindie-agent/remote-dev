"""Property tests for endpoint resolution (``core.endpoint``).

Property under test: resolution is deterministic and total. Every payload
either yields a well-formed ``Endpoint`` (all fields typed, port in range,
absolute root, destination safe to hand to ``ssh``) or raises ``EndpointError``
with a precise message — never a partially populated endpoint and never a
foreign exception type.

Consumer-owned resolution (sessions, machine inventories, ...) reaches
remote-dev only through the resolver plugin interface; it is exercised here
through fake resolvers so the wrapping and field mapping are covered without
any consumer state or host.

Recovered from the scaffold's property-test campaign
(maoxx241/vllm-ascend-workspace#89). The managed-resolution class of that
campaign targeted ``_endpoint_from_managed``, which no longer exists in the
extracted repository; it is rewritten here against ``register_resolver``.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

import remote_dev.core.endpoint as endpoint_mod  # noqa: E402
from remote_dev.core.endpoint import (  # noqa: E402
    Endpoint,
    EndpointError,
    clear_resolvers,
    register_resolver,
    resolve_endpoint,
    unregister_resolver,
)
from remote_dev.core.ssh_transport import ssh_base_cmd  # noqa: E402
from test_property_support import DOC_HOSTS, Gen, run_cases  # noqa: E402

GARBAGE_VALUES: tuple[Any, ...] = (None, "", 0, -1, 1.5, True, False, [], {}, "0", "x", " ", "1e3", b"bytes", ["a"], {"k": "v"})


def well_formed_payload(gen: Gen) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "host": gen.choice(DOC_HOSTS),
        "port": gen.choice((gen.integer(1, 65535), str(gen.integer(1, 65535)))),
    }
    if gen.boolean():
        payload["user"] = gen.choice(("root", "dev", "agent01"))
    if gen.boolean():
        payload["root"] = gen.choice(("/", "/vllm-workspace", "/vllm-workspace/", "/data/a/b"))
    if gen.boolean():
        payload["cwd"] = gen.choice(("/vllm-workspace", "/vllm-workspace/src", None, ""))
    if gen.boolean():
        payload["runtime_env"] = gen.choice((True, False, 0, 1, "yes"))
    if gen.boolean():
        payload["identity_file"] = gen.choice(("~/.ssh/id_example", None, ""))
    if gen.boolean():
        payload["connect_timeout_ms"] = gen.choice((1, 1000, 10000, 2500, None, 7200000))
    if gen.boolean():
        payload["ssh_mux"] = gen.choice((True, False, None))
    if gen.boolean():
        payload["keepalive"] = gen.choice((True, False, None))
    if gen.boolean():
        payload["alias"] = gen.choice(("dev", None, ""))
    if gen.boolean():
        payload["source"] = gen.choice(({"origin": "test"}, "not-a-dict", None))
    if gen.boolean():
        payload["kind"] = gen.choice(("direct-endpoint", None, ""))
    return payload


def garbage_payload(gen: Gen) -> dict[str, Any]:
    keys = (
        "host",
        "port",
        "user",
        "root",
        "cwd",
        "alias",
        "session_id",
        "session_file",
        "machine",
        "runtime_env",
        "identity_file",
        "kind",
        "source",
        "ssh_mux",
        "keepalive",
        "unknown_field",
    )
    payload: dict[str, Any] = {}
    for key in gen.subset(keys):
        payload[key] = gen.one_of(lambda: gen.choice(GARBAGE_VALUES), lambda: gen.word(0, 5), lambda: gen.integer(-5, 70000))
    return payload


def assert_well_formed(test: unittest.TestCase, endpoint: Endpoint) -> None:
    test.assertIsInstance(endpoint.host, str)
    test.assertTrue(endpoint.host, "host must be non-empty")
    test.assertIsInstance(endpoint.port, int)
    test.assertNotIsInstance(endpoint.port, bool)
    test.assertIsInstance(endpoint.user, str)
    test.assertTrue(endpoint.user)
    test.assertIsInstance(endpoint.root, str)
    test.assertTrue(endpoint.root.startswith("/"), f"root must be absolute: {endpoint.root!r}")
    test.assertTrue(endpoint.effective_cwd.startswith("/"), f"cwd must be absolute: {endpoint.effective_cwd!r}")
    if endpoint.connect_timeout_ms is not None:
        test.assertIs(type(endpoint.connect_timeout_ms), int)
        test.assertGreater(endpoint.connect_timeout_ms, 0)
    test.assertIsInstance(endpoint.runtime_env, bool)
    test.assertIn(endpoint.ssh_mux, (True, False, None))
    test.assertIsInstance(endpoint.keepalive, bool)
    test.assertTrue(endpoint.kind == "direct-endpoint" or endpoint.kind.startswith("resolver:"), endpoint.kind)
    target = endpoint.to_result_target()
    for key in ("kind", "endpoint_id", "host", "port", "user", "root", "cwd", "runtime_env"):
        test.assertIn(key, target)
        test.assertIsNotNone(target[key], f"result target {key} is None")
    test.assertRegex(endpoint.endpoint_id, r"^[0-9a-f]{16}$")


class ResolverIsolation(unittest.TestCase):
    """Run with no registered resolvers and restore the registry afterwards."""

    def setUp(self) -> None:
        self._saved = list(endpoint_mod._RESOLVERS)
        clear_resolvers()
        self.addCleanup(self._restore)
        patcher = mock.patch.dict(endpoint_mod.os.environ, {endpoint_mod.RESOLVERS_ENV: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore(self) -> None:
        clear_resolvers()
        endpoint_mod._RESOLVERS.extend(self._saved)


class DirectEndpointProperties(ResolverIsolation):
    def setUp(self) -> None:
        super().setUp()
        # An empty substrate directory: no alias files can leak in from the
        # developer's real (gitignored) endpoints.local.json.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_well_typed_payloads_always_resolve_to_well_formed_endpoints(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            payload = well_formed_payload(gen)
            endpoint = resolve_endpoint(payload)
            assert_well_formed(self, endpoint)
            self.assertEqual(endpoint.kind, "direct-endpoint")
            self.assertEqual(endpoint.host, payload["host"])
            self.assertEqual(endpoint.port, int(payload["port"]))
            self.assertEqual(endpoint.user, payload.get("user") or endpoint_mod.DEFAULT_USER)
            self.assertEqual(endpoint.root, payload.get("root") or endpoint_mod.DEFAULT_ROOT)
            self.assertEqual(endpoint.effective_cwd, payload.get("cwd") or endpoint_mod.DEFAULT_CWD or endpoint.root)
            # Determinism: the same payload resolves to an identical endpoint.
            self.assertEqual(resolve_endpoint(dict(payload)), endpoint)
            self.assertEqual(resolve_endpoint(dict(payload)).endpoint_id, endpoint.endpoint_id)

        run_cases(400, body, label="direct endpoint well-formed")

    def test_endpoint_id_depends_only_on_identity_fields(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            base = {"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": gen.word(1, 6), "root": gen.choice(("/", "/vllm-workspace"))}
            a = resolve_endpoint({**base, "cwd": "/vllm-workspace", "connect_timeout_ms": 1000, "runtime_env": True})
            b = resolve_endpoint(
                {
                    **base,
                    "cwd": "/vllm-workspace/other",
                    "connect_timeout_ms": 9000,
                    "runtime_env": False,
                    "identity_file": "~/.ssh/k",
                    "ssh_mux": False,
                    "keepalive": True,
                }
            )
            self.assertEqual(
                a.endpoint_id,
                b.endpoint_id,
                "cwd/timeout/runtime_env/identity/ssh_mux/keepalive must not change endpoint identity",
            )
            for field, value in (("host", gen.choice([h for h in DOC_HOSTS if h != base["host"]])), ("port", (base["port"] % 65535) + 1), ("user", base["user"] + "x"), ("root", "/elsewhere")):
                other = resolve_endpoint({**base, field: value})
                self.assertNotEqual(other.endpoint_id, a.endpoint_id, f"changing {field} must change endpoint identity")
            self.assertNotIn(base["host"], a.endpoint_id)

        run_cases(150, body, label="endpoint_id identity")

    def test_garbage_payloads_raise_only_endpoint_error(self) -> None:
        # No resolver is registered, so payloads without host+port or alias
        # must end in the "no endpoint target" EndpointError.
        def body(gen: Gen, _index: int) -> None:
            payload = garbage_payload(gen)
            with mock.patch.object(endpoint_mod, "substrate_root", return_value=Path(self._tmp.name)):
                try:
                    endpoint = resolve_endpoint(payload)
                except EndpointError as exc:
                    self.assertTrue(str(exc), "error must carry a message")
                    return
                except Exception as exc:  # noqa: BLE001
                    self.fail(f"non-EndpointError {type(exc).__name__} for payload {payload!r}: {exc}")
            # Any accepted endpoint must be fully typed, even if the payload was odd.
            self.assertIsInstance(endpoint.host, str)
            self.assertIsInstance(endpoint.port, int)
            self.assertIsInstance(endpoint.user, str)
            self.assertIsInstance(endpoint.root, str)

        run_cases(600, body, label="garbage payload totality")

    def test_missing_target_message_lists_every_selector(self) -> None:
        with self.assertRaises(EndpointError) as ctx:
            resolve_endpoint({})
        message = str(ctx.exception)
        self.assertIn("no endpoint target", message)
        for token in ("host", "port", "alias"):
            self.assertIn(token, message)

    def test_port_range_is_validated_at_resolution(self) -> None:
        """``port=70000`` / ``port=-1`` must fail at resolution, not later
        inside ``ssh``."""
        for port in (70000, -1, 65536):
            with self.assertRaises(EndpointError, msg=f"port {port} accepted"):
                resolve_endpoint({"host": "203.0.113.5", "port": port})

    def test_connect_timeout_garbage_is_an_endpoint_error(self) -> None:
        """A non-numeric ``connect_timeout_ms`` must raise ``EndpointError``,
        not leak ``ValueError`` to the MCP caller."""
        for value in ("abc", "2500", 0, -1, True, False, 1.5, float("inf"), float("nan"), [], {}):
            with self.subTest(value=value), self.assertRaises(EndpointError):
                resolve_endpoint({"host": "203.0.113.5", "port": 22, "connect_timeout_ms": value})

    def test_relative_root_or_cwd_is_rejected_at_resolution(self) -> None:
        """A relative ``root`` (or ``cwd``) must fail at resolution. Accepting
        it made every later path check raise ``PathPolicyError('remote path
        must be absolute')``, which surfaced as ``path_outside_root`` on
        unrelated tool calls — the symptom appeared far from the cause."""
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "root": "relative"})
        with self.assertRaises(EndpointError):
            resolve_endpoint({"host": "203.0.113.5", "port": 22, "cwd": "relative"})

    def test_user_field_cannot_inject_ssh_options(self) -> None:
        """A ``user`` beginning with ``-`` (from a tool argument or an alias
        file) used to be spliced into argv as ``user@host`` and parsed by
        ``ssh`` as an option — e.g. ``-oProxyCommand=...`` ran a local
        command. ``ssh_base_cmd`` now uses ``-l user`` and ``-- host``.
        Evidence: the last destination token is the host, not an option."""
        endpoint = resolve_endpoint({"host": "203.0.113.5", "port": 22, "user": "-oProxyCommand=marker"})
        argv = ssh_base_cmd(endpoint)
        self.assertEqual(argv[argv.index("-l") + 1], "-oProxyCommand=marker")
        self.assertEqual(argv[argv.index("--") + 1], "203.0.113.5")
        destination = argv[-1]
        self.assertFalse(destination.startswith("-"), f"destination is parsed as an ssh option: {argv}")

    def test_ssh_argv_shape_is_stable_for_safe_users(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            user = gen.choice(("root", "dev", "agent01")) + gen.text("abc0123", 0, 3)
            endpoint = resolve_endpoint({"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": user})
            argv = ssh_base_cmd(endpoint)
            self.assertEqual(argv[0], "ssh")
            self.assertEqual(argv[argv.index("-l") + 1], user)
            self.assertEqual(argv[argv.index("-p") + 1], str(endpoint.port))
            self.assertEqual(argv[argv.index("--") + 1], endpoint.host)
            timeout_options = [item for item in argv if item.startswith("ConnectTimeout=")]
            self.assertEqual(timeout_options, [])

        run_cases(100, body, label="ssh argv shape")


class AliasResolutionProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.substrate = Path(self._tmp.name)
        patcher = mock.patch.object(endpoint_mod, "substrate_root", return_value=self.substrate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_aliases(self, entries: dict[str, Any], *, local: dict[str, Any] | None = None) -> None:
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": entries}), encoding="utf-8")
        if local is not None:
            (self.substrate / "endpoints.local.json").write_text(json.dumps({"endpoints": local}), encoding="utf-8")

    def test_alias_fields_are_used_unless_payload_overrides_with_a_value(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            alias_name = gen.word(1, 8)
            entry = {"host": gen.choice(DOC_HOSTS), "port": gen.integer(1, 65535), "user": "aliasuser", "root": "/vllm-workspace"}
            self._write_aliases({alias_name: entry})
            payload: dict[str, Any] = {"alias": alias_name}
            override_user = gen.boolean()
            override_root = gen.boolean()
            if override_user:
                payload["user"] = "override"
            if override_root:
                payload["root"] = "/data"
            endpoint = resolve_endpoint(payload)
            self.assertEqual(endpoint.host, entry["host"])
            self.assertEqual(endpoint.port, entry["port"])
            self.assertEqual(endpoint.user, "override" if override_user else "aliasuser")
            self.assertEqual(endpoint.root, "/data" if override_root else "/vllm-workspace")
            self.assertEqual(endpoint.alias, alias_name)
            self.assertEqual(resolve_endpoint(dict(payload)), endpoint)

        run_cases(120, body, label="alias merge")

    def test_local_alias_file_overrides_shared_entries(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}}, local={"dev": {"host": DOC_HOSTS[1], "port": 2}})
        endpoint = resolve_endpoint({"alias": "dev"})
        self.assertEqual((endpoint.host, endpoint.port), (DOC_HOSTS[1], 2))

    def test_unknown_or_malformed_alias_files_raise_endpoint_error(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}})
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "missing"})
        (self.substrate / "endpoints.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": ["list"]}), encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})
        (self.substrate / "endpoints.json").write_text(json.dumps({"endpoints": {"dev": {"host": DOC_HOSTS[0]}}}), encoding="utf-8")
        with self.assertRaises(EndpointError):
            resolve_endpoint({"alias": "dev"})

    def test_direct_host_port_wins_over_alias(self) -> None:
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 1}})
        endpoint = resolve_endpoint({"alias": "dev", "host": DOC_HOSTS[2], "port": 46000})
        self.assertEqual((endpoint.host, endpoint.port), (DOC_HOSTS[2], 46000))

    def test_explicit_null_fields_do_not_override_alias_values(self) -> None:
        """Alias merge drops ``None`` so an MCP client that sends optional
        fields as explicit ``null`` (``host: null, port: null, alias: 'dev'``)
        still uses the configured alias. The scaffold campaign recorded this
        as a defect; the extracted resolver already filters ``None``."""
        self._write_aliases({"dev": {"host": DOC_HOSTS[0], "port": 46000, "user": "dev"}})
        endpoint = resolve_endpoint({"alias": "dev", "host": None, "port": None, "user": None, "root": None})
        self.assertEqual((endpoint.host, endpoint.port, endpoint.user), (DOC_HOSTS[0], 46000, "dev"))


class ResolverMappingProperties(ResolverIsolation):
    """Consumer resolvers stand in for the scaffold's managed-resolution path."""

    def test_resolver_targets_map_to_well_formed_endpoints(self) -> None:
        def body(gen: Gen, index: int) -> None:
            has_session = gen.boolean()
            session_id = "sess-" + gen.text("abcdef0123", 4, 8) if has_session else None
            alias = "machine-" + gen.text("abc", 1, 3)
            host = gen.choice(DOC_HOSTS)
            port = gen.integer(1, 65535)

            def resolve(payload: dict[str, Any]) -> dict[str, Any] | None:
                if has_session:
                    if not payload.get("session_id"):
                        return None
                    chosen_alias = session_id
                    source_session = session_id
                else:
                    if not payload.get("machine"):
                        return None
                    chosen_alias = alias
                    source_session = None
                return {
                    "host": host,
                    "port": port,
                    "user": "root",
                    "root": "/vllm-workspace",
                    "cwd": "/vllm-workspace",
                    "alias": chosen_alias,
                    "source": {
                        "vaws_target": {
                            "alias": alias,
                            "session_id": source_session,
                            "kwargs": {"session_id": source_session},
                        }
                    },
                }

            name = f"consumer-{index}"
            register_resolver(resolve, name=name, fields=("session_id", "machine"))
            try:
                payload: dict[str, Any] = {"session_id": session_id} if has_session else {"machine": alias}
                if gen.boolean():
                    payload["root"] = "/vllm-workspace"
                if gen.boolean():
                    payload["cwd"] = "/vllm-workspace/src"
                endpoint = resolve_endpoint(payload)
                assert_well_formed(self, endpoint)
                self.assertEqual(endpoint.kind, f"resolver:{name}")
                self.assertEqual(endpoint.alias, session_id if has_session else alias)
                self.assertEqual(endpoint.effective_cwd, payload.get("cwd") or "/vllm-workspace")
                self.assertIn("vaws_target", endpoint.source or {})
                self.assertEqual(endpoint.to_result_target()["source"]["vaws_target"]["kwargs"]["session_id"], session_id)
            finally:
                unregister_resolver(name)

        run_cases(120, body, label="resolver endpoint mapping")

    def test_resolver_failures_are_wrapped_as_endpoint_errors(self) -> None:
        for error in (RuntimeError("boom"), KeyError("session"), ValueError("bad"), FileNotFoundError("state")):
            with self.subTest(error=type(error).__name__):
                def broken(_payload: dict[str, Any], _error: Exception = error) -> None:
                    raise _error

                name = f"broken-{type(error).__name__}"
                register_resolver(broken, name=name)
                try:
                    with self.assertRaises(EndpointError) as ctx:
                        resolve_endpoint({})
                    self.assertIn(name, str(ctx.exception))
                    self.assertIn(str(error), str(ctx.exception))
                finally:
                    unregister_resolver(name)


if __name__ == "__main__":
    unittest.main()
