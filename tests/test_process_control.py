"""Client-side process control contract. No SSH, no coordinator install."""
from __future__ import annotations

import unittest
from unittest import mock

from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError
from remote_dev.processes import control, worker_source
import remote_dev.processes.client as control_mod


class ProcessControlClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = Endpoint(host="192.0.2.10", port=46000, root="/srv/app", cwd="/srv/app")

    def test_worker_source_is_packaged_and_generic(self) -> None:
        source = worker_source()
        self.assertIn("PR_SET_CHILD_SUBREAPER", source)
        self.assertIn("REMOTE_DEV_JOB_TOKEN", source)
        self.assertIn("control_job", source)
        self.assertNotIn("MINDIE_REMOTE_JOB", source)
        self.assertNotIn(".vaws-runtime", source)
        self.assertNotIn("lease/fence", source)
        self.assertNotIn("NPU", source)

    def test_control_is_exported_at_the_package_boundary(self) -> None:
        import remote_dev.processes as processes

        self.assertTrue(callable(processes.control))
        self.assertIs(processes.control, control)
        self.assertFalse(hasattr(processes.control, "control"))

    def test_control_ships_worker_over_rpc_without_a_local_shell(self) -> None:
        response = {"state": "prepared", "quiet": False, "gate_open": False}
        with mock.patch.object(control_mod, "rpc_request", return_value=response) as execute:
            payload = control(self.endpoint, "job-abc123", "prepare",
                              spec={"command": "true", "cwd": "/srv/app", "env": {}, "timeout_seconds": 10})
        self.assertEqual(payload, response)
        endpoint, kind, source, body = execute.call_args.args
        self.assertIs(endpoint, self.endpoint)
        self.assertEqual(kind, "control")
        self.assertEqual(body["job_id"], "job-abc123")
        self.assertEqual(body["action"], "prepare")
        self.assertEqual(body["root"], "/srv/app")
        self.assertIn("PR_SET_CHILD_SUBREAPER", source)

    def test_control_accepts_an_ordinary_host_port_mapping(self) -> None:
        with mock.patch.object(control_mod, "rpc_request", return_value={"state": "absent", "quiet": True}) as execute:
            row = control({"host": "192.0.2.8", "port": 22, "root": "/work"}, "job-map", "status")
        self.assertEqual(execute.call_args.args[3]["root"], "/work")
        self.assertEqual(row, {"state": "absent", "quiet": True})

    def test_control_reraises_remote_value_errors(self) -> None:
        with mock.patch.object(control_mod, "rpc_request", side_effect=ValueError("invalid job id")):
            with self.assertRaisesRegex(ValueError, "invalid job id"):
                control(self.endpoint, "??", "status")

    def test_control_rejects_unknown_actions_locally(self) -> None:
        with self.assertRaises(ValueError):
            control(self.endpoint, "job-abc123", "lease")

    def test_control_transport_failure_is_not_replayed(self) -> None:
        with mock.patch.object(control_mod, "rpc_request", side_effect=RemoteExecutionError("ssh: connect failed")) as execute:
            with self.assertRaisesRegex(RemoteExecutionError, "ssh: connect failed"):
                control(self.endpoint, "job-abc123", "status")
        self.assertEqual(execute.call_count, 1)

    def test_control_rejects_invalid_result(self) -> None:
        with mock.patch.object(control_mod, "rpc_request", return_value={}):
            with self.assertRaisesRegex(RemoteExecutionError, "no state"):
                control(self.endpoint, "job-abc123", "status")
