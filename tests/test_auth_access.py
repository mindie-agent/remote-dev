"""Behavioral SSH enrollment/repair contracts with fixed authentication outcomes."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from remote_dev.auth import (
    CredentialStore, ProfileStore, auth_status, connection_identity, forget_access,
    prepare_auth, setup_access,
)
from remote_dev.auth import access
from remote_dev.core.endpoint import Endpoint
from remote_dev.core.errors import RemoteExecutionError


@pytest.fixture
def auth_home(tmp_path, monkeypatch):
    root = tmp_path / "auth"
    secret = root / "credentials.json"
    monkeypatch.setenv("REMOTE_DEV_AUTH_DIR", str(root))
    monkeypatch.delenv("REMOTE_DEV_CREDENTIAL_FILE", raising=False)
    return root, secret


@pytest.fixture
def endpoint():
    return Endpoint("auth.example", 22, user="alice", runtime_env=False)


def _outcome(code=0, stderr=""):
    return access._Outcome(code, "", stderr)


def _deny():
    return _outcome(255, "alice@auth.example: Permission denied (publickey,password).\n")


def _probes(monkeypatch, outcomes):
    calls = []
    results = iter(outcomes)

    def probe(endpoint, **kwargs):
        calls.append(kwargs)
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(access, "_probe", probe)
    return calls


def _key(monkeypatch, tmp_path):
    private = str(tmp_path / "managed-key")
    monkeypatch.setattr(access, "ensure_key", lambda identity_file=None, **kwargs: (identity_file or private, "ssh-ed25519 AAAATEST fixture"))
    return private


def test_key_enrollment_retains_password_reference_without_serializing_secret(auth_home, endpoint, monkeypatch, tmp_path):
    calls = _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    private = _key(monkeypatch, tmp_path)
    result = setup_access(endpoint, password="fixture-secret")
    assert result["auth_method"] == "publickey"
    assert result["bootstrap"]["installation"] == "completed"
    assert [call["method"] for call in calls] == ["publickey", "password", "password", "publickey"]
    assert calls[2]["command"].count("authorized_keys") >= 1
    profile = ProfileStore().resolve(endpoint)
    assert profile["identity_file"] == private
    assert CredentialStore().get(profile["credential_ref"]) == "fixture-secret"
    saved_text = next((auth_home[0] / "profiles").glob("*.json")).read_text()
    plan = prepare_auth(endpoint, repair=False)
    assert "fixture-secret" not in saved_text + json.dumps(asdict(plan.endpoint)) + json.dumps(result)
    assert plan.endpoint.auth_ref == result["auth_ref"]


def test_existing_agent_identity_is_preserved_without_generating_a_key(auth_home, endpoint, monkeypatch):
    _probes(monkeypatch, [_outcome()])
    monkeypatch.setattr(access, "ensure_key", lambda *args, **kwargs: pytest.fail("existing SSH identity must remain usable"))
    result = setup_access(endpoint)
    plan = prepare_auth(endpoint)
    assert result["identity_file"] is None
    assert plan.env is None
    assert "IdentitiesOnly=yes" not in plan.options


def test_short_auth_deadline_still_runs_a_successful_key_probe(auth_home, endpoint, monkeypatch):
    _probes(monkeypatch, [_outcome(), _outcome()])
    setup_access(endpoint, password="fixture-secret")

    def local_probe(target, **kwargs):
        return access._execute([sys.executable, "-I", "-c", "pass"], budget=kwargs["budget"])

    monkeypatch.setattr(access, "_probe", local_probe)
    plan = prepare_auth(endpoint, timeout_ms=500)
    assert plan.env is None


def test_key_rejection_after_install_keeps_verified_password_mode(auth_home, endpoint, monkeypatch, tmp_path):
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _deny()])
    _key(monkeypatch, tmp_path)
    result = setup_access(endpoint, password="fixture-secret")
    assert result["auth_method"] == "password"
    assert result["bootstrap"]["status"] == "failed"
    assert result["bootstrap"]["installation"] == "completed"
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: pytest.fail("password profile must not reinstall on each connection"))
    plan = prepare_auth(endpoint)
    assert "BatchMode=no" in plan.options
    assert "fixture-secret" not in json.dumps(plan.env)


def test_removed_key_is_repaired_before_returning_business_plan(auth_home, endpoint, monkeypatch, tmp_path):
    _key(monkeypatch, tmp_path)
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    initial = setup_access(endpoint, password="fixture-secret")
    identity = connection_identity(endpoint)
    calls = _probes(monkeypatch, [_deny(), _deny(), _outcome(), _outcome(), _outcome()])
    plan = prepare_auth(endpoint)
    assert [call["method"] for call in calls] == ["publickey", "publickey", "password", "password", "publickey"]
    assert plan.endpoint.identity_file == initial["identity_file"]
    assert plan.identity == identity
    assert "BatchMode=yes" in plan.options


def test_diagnostic_plan_can_fall_back_in_one_handshake_without_probing_or_repairing(auth_home, endpoint, monkeypatch, tmp_path):
    _key(monkeypatch, tmp_path)
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    setup_access(endpoint, password="fixture-secret")
    profile_path = ProfileStore.path(ProfileStore.reference(endpoint))
    saved_profile = profile_path.read_bytes()
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: pytest.fail("diagnostics cannot change keys"))
    plan = prepare_auth(replace(endpoint, ssh_mux=True), repair=False)
    assert plan.endpoint.ssh_mux is False
    assert plan.env["REMOTE_DEV_ASKPASS_REF"] == ProfileStore().resolve(endpoint)["credential_ref"]
    assert "PreferredAuthentications=publickey,password,keyboard-interactive" in plan.options
    assert profile_path.read_bytes() == saved_profile


def test_container_inspection_uses_read_only_auth_before_normal_business_repair(auth_home, endpoint, monkeypatch, tmp_path):
    from remote_dev.core import rpc_transport
    from remote_dev.core.container_endpoint import pin_container_endpoint
    from remote_dev.auth.policy import repair_allowed

    _key(monkeypatch, tmp_path)
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    setup_access(endpoint, password="fixture-secret")
    monkeypatch.setattr(access, "_probe", lambda *args, **kwargs: pytest.fail("container inspection must not repair keys"))

    def inspect(host, kind, source, payload, **kwargs):
        assert repair_allowed() is False
        plan = prepare_auth(host)
        assert "PreferredAuthentications=publickey,password,keyboard-interactive" in plan.options
        return {"returncode": 0, "stdout": json.dumps({"container": "a" * 64})}

    monkeypatch.setattr(rpc_transport, "request", inspect)
    pinned = pin_container_endpoint(replace(endpoint, container="fixture-container"))
    assert pinned.container == "a" * 64
    assert repair_allowed() is True
    probes = _probes(monkeypatch, [_outcome()])
    prepare_auth(pinned)
    assert len(probes) == 1


def test_read_only_rpc_pool_cannot_replace_a_normal_repairing_connection(auth_home, endpoint, monkeypatch):
    from remote_dev.auth import read_only
    from remote_dev.core import rpc_transport

    keys = []

    class AcquisitionObserved(Exception):
        pass

    def acquire(target, key, **kwargs):
        keys.append(key)
        raise AcquisitionObserved()

    monkeypatch.setattr(rpc_transport, "_acquire", acquire)
    with read_only(), pytest.raises(AcquisitionObserved):
        rpc_transport.request(endpoint, "python", "pass", {})
    with pytest.raises(AcquisitionObserved):
        rpc_transport.request(endpoint, "python", "pass", {})
    assert keys[0] != keys[1]


def test_profile_keeps_known_hosts_scope_across_callers(auth_home, endpoint, monkeypatch, tmp_path):
    trusted = str(tmp_path / "npu-top-known-hosts")
    _probes(monkeypatch, [_outcome()])
    setup_access(replace(endpoint, known_hosts_file=trusted), password="fixture-secret", install_key=False)
    plan = prepare_auth(endpoint)
    assert plan.endpoint.known_hosts_file == trusted
    assert trusted in connection_identity(endpoint)
    with pytest.raises(RemoteExecutionError, match="different known_hosts_file"):
        prepare_auth(replace(endpoint, known_hosts_file=str(tmp_path / "different-known-hosts")))


def test_managed_key_is_reused_without_replacing_an_existing_keypair(auth_home):
    first_identity, first_public = access.ensure_key()
    second_identity, second_public = access.ensure_key()
    assert first_identity == second_identity
    assert first_public == second_public
    assert Path(first_identity).parent == auth_home[0] / "keys"


@pytest.mark.parametrize("message", [
    "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!\nHost key verification failed.",
    "ssh: connect to host auth.example port 22: Connection refused",
])
def test_transport_failure_does_not_try_password_or_mutate_host(auth_home, endpoint, monkeypatch, message):
    calls = _probes(monkeypatch, [_outcome(255, message)])
    with pytest.raises(RemoteExecutionError, match="verification failed"):
        setup_access(endpoint, password="fixture-secret")
    assert len(calls) == 1
    assert ProfileStore().resolve(endpoint) is None
    assert json.loads(auth_home[1].read_text()) == {}


def test_uncertain_key_install_is_not_retried_or_reported_as_complete(auth_home, endpoint, monkeypatch, tmp_path):
    _key(monkeypatch, tmp_path)
    calls = _probes(monkeypatch, [_deny(), _outcome(), _outcome(255, "Connection closed by remote host")])
    with pytest.raises(RemoteExecutionError, match="outcome is unknown") as caught:
        setup_access(endpoint, password="fixture-secret")
    assert caught.value.submission_state == "not_sent"
    assert caught.value.bootstrap["installation"] == "unknown"
    assert CredentialStore().get(caught.value.bootstrap["credential_ref"]) == "fixture-secret"
    assert len(calls) == 3
    assert ProfileStore().resolve(endpoint) is None


def test_key_verification_timeout_preserves_completed_installation(auth_home, endpoint, monkeypatch, tmp_path):
    _key(monkeypatch, tmp_path)
    timeout = access.auth_error("SSH authentication deadline expired", "authentication_timeout")
    calls = _probes(monkeypatch, [_deny(), _outcome(), _outcome(), timeout])
    with pytest.raises(RemoteExecutionError, match="deadline expired") as caught:
        setup_access(endpoint, password="fixture-secret")
    assert caught.value.bootstrap["installation"] == "completed"
    assert caught.value.bootstrap["verification"] == "unknown"
    assert caught.value.submission_state == "not_sent"
    assert len(calls) == 4


def test_profile_recording_failure_keeps_installed_key_facts_and_recovery_credential(auth_home, endpoint, monkeypatch, tmp_path):
    _key(monkeypatch, tmp_path)
    calls = _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])

    def unable_to_save(*args, **kwargs):
        raise OSError("fixture disk full")

    monkeypatch.setattr(ProfileStore, "save", unable_to_save)
    with pytest.raises(RemoteExecutionError, match="saving its local auth profile failed") as caught:
        setup_access(endpoint, password="fixture-secret")
    facts = caught.value.bootstrap
    assert facts["installation"] == "completed"
    assert facts["key_verified"] is True
    assert facts["profile_saved"] is False
    assert facts["credential_retained"] is True
    assert CredentialStore().get(facts["credential_ref"]) == "fixture-secret"
    assert caught.value.submission_state == "not_sent"
    assert len(calls) == 4


def test_rotation_changes_connection_identity_and_forget_is_local(auth_home, endpoint, monkeypatch):
    _probes(monkeypatch, [_outcome(), _outcome()])
    first = setup_access(endpoint, password="first-fixture", install_key=False)
    old_identity = connection_identity(endpoint)
    old_credential = ProfileStore().resolve(endpoint)["credential_ref"]
    second = setup_access(endpoint, password="second-fixture", install_key=False)
    assert first["auth_ref"] == second["auth_ref"]
    assert connection_identity(endpoint) != old_identity
    with pytest.raises(RemoteExecutionError, match="unavailable"):
        CredentialStore().get(old_credential)
    forgotten = forget_access(endpoint)
    assert forgotten["remote_key_revoked"] is False
    assert auth_status(endpoint)["configured"] is False
    assert json.loads(auth_home[1].read_text()) == {}


def test_repair_reloads_profile_after_a_concurrent_credential_rotation(auth_home, endpoint, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    _key(monkeypatch, tmp_path)
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    setup_access(endpoint, password="old-fixture")
    old_ref = ProfileStore().resolve(endpoint)["credential_ref"]
    observed, resume = threading.Event(), threading.Event()
    def probe(target, **kwargs):
        if threading.current_thread().name.startswith("repair"):
            observed.set()
            assert resume.wait(5), "rotation did not finish"
            return _deny()
        return _outcome()

    monkeypatch.setattr(access, "_probe", probe)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="repair") as pool:
        repair = pool.submit(prepare_auth, endpoint)
        assert observed.wait(5), "repair did not probe the old profile"
        try:
            setup_access(endpoint, password="new-fixture", install_key=False)
        finally:
            resume.set()
        plan = repair.result(timeout=5)
    current = ProfileStore().resolve(endpoint)
    assert current["auth_method"] == "password"
    assert CredentialStore().get(current["credential_ref"]) == "new-fixture"
    assert plan.identity == connection_identity(endpoint)
    with pytest.raises(RemoteExecutionError, match="unavailable"):
        CredentialStore().get(old_ref)


def test_missing_local_key_uses_verified_recovery_password(auth_home, endpoint, monkeypatch, tmp_path):
    from remote_dev.observability import _authentication_event
    private = _key(monkeypatch, tmp_path)
    _probes(monkeypatch, [_deny(), _outcome(), _outcome(), _outcome()])
    setup_access(endpoint, password="fixture-secret")
    calls = _probes(monkeypatch, [_deny(), _deny(), _outcome()])

    def missing_key(*args, **kwargs):
        raise access.auth_error("SSH identity file does not exist: " + private, "configuration")

    monkeypatch.setattr(access, "ensure_key", missing_key)
    token = _authentication_event.set(None)
    try:
        plan = prepare_auth(endpoint)
    finally:
        event = _authentication_event.get()
        _authentication_event.reset(token)
    assert plan.endpoint.ssh_mux is False
    assert "BatchMode=no" in plan.options
    assert len(calls) == 3
    assert auth_status(endpoint)["bootstrap"]["installation"] == "not_sent"
    assert event["auth_method"] == "password"
    assert event["bootstrap"]["installation"] == "not_sent"
    assert "fixture-secret" not in json.dumps(event)


def test_explicit_profile_must_exist_and_match_principal(auth_home, endpoint, monkeypatch):
    _probes(monkeypatch, [_outcome()])
    result = setup_access(endpoint, password="fixture-secret", install_key=False)
    with pytest.raises(RemoteExecutionError, match="different host"):
        prepare_auth(replace(endpoint, user="bob", auth_ref=result["auth_ref"]))
    with pytest.raises(RemoteExecutionError, match="does not exist"):
        prepare_auth(replace(endpoint, auth_ref="ssh-" + "0" * 24))


def test_password_profile_survives_another_process_and_job_state_root(auth_home, endpoint, monkeypatch, tmp_path):
    _probes(monkeypatch, [_outcome()])
    monkeypatch.setenv("ONBOARD_PASSWORD", "fixture-secret")
    result = setup_access(endpoint, password_env="ONBOARD_PASSWORD", install_key=False)
    monkeypatch.delenv("ONBOARD_PASSWORD")
    monkeypatch.setenv("REMOTE_DEV_STATE_DIR", str(tmp_path / "unrelated-job-state"))
    code = (
        "import json; from remote_dev.auth import prepare_auth,CredentialStore,ProfileStore; "
        "from remote_dev.core.endpoint import Endpoint; "
        "ep=Endpoint('auth.example',22,user='alice'); p=prepare_auth(ep); "
        "secret=CredentialStore().get(ProfileStore().resolve(ep)['credential_ref']); "
        "print(json.dumps({'ref':p.endpoint.auth_ref,'options':p.options,'env':p.env,'secret_available':bool(secret)}))"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=5, check=True)
    payload = json.loads(child.stdout)
    assert payload["ref"] == result["auth_ref"]
    assert payload["secret_available"]
    assert "BatchMode=no" in payload["options"]
    assert "fixture-secret" not in child.stdout + child.stderr


def test_askpass_reads_shared_provider_without_secret_in_helper(auth_home, endpoint, monkeypatch):
    _probes(monkeypatch, [_outcome()])
    secret = "fixture-\u5bc6\u7801-\u00e9"
    setup_access(endpoint, password=secret, install_key=False)
    plan = prepare_auth(endpoint)
    import os
    helper = Path(plan.env["SSH_ASKPASS"])
    assert secret.encode("utf-8") not in helper.read_bytes()
    environment = {**os.environ, **plan.env, "PYTHONIOENCODING": "ascii"}
    output = subprocess.run([str(helper), "alice@auth.example's password:"],
                            env=environment, capture_output=True, timeout=5, check=True)
    assert output.stdout == (secret + "\n").encode("utf-8")
    challenge = subprocess.run([str(helper), "Verification code:"],
                               env=environment, capture_output=True, timeout=5)
    assert challenge.returncode != 0
    assert challenge.stdout == b""
    expired = subprocess.run([str(helper), "New password:"],
                             env=environment, capture_output=True, timeout=5)
    assert expired.returncode != 0
    assert expired.stdout == b""


@pytest.mark.parametrize("ending", ["\n", ""])
@pytest.mark.skipif(shutil.which("sh") is None, reason="The remote POSIX installer is also covered by real Linux SSH tests")
def test_authorized_keys_install_is_idempotent_and_preserves_existing_key(tmp_path, ending):
    import os
    directory = tmp_path / ".ssh"
    directory.mkdir()
    existing = "ssh-ed25519 AAAAEXISTING original" + ending
    keys = directory / "authorized_keys"
    keys.write_text(existing)
    command = access._install_command("ssh-ed25519 AAAANEW added")
    for _ in range(2):
        subprocess.run(["sh", "-c", command], env={**os.environ, "HOME": str(tmp_path)},
                       capture_output=True, text=True, timeout=3, check=True)
    assert keys.read_text() == existing + "\nssh-ed25519 AAAANEW added\n"

