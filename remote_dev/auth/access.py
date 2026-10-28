"""[Role] SSH enrollment, reusable authentication plans and public-key repair.
[Invariants]
- Only pre-dispatch authentication probes run here; business commands are never retried.
- Password success and key installation/verification are separate recorded facts.
[Timeouts & Locks]
- One deadline covers probes, key generation and repair; cleanup reserves at most three seconds.
- Profile/key locks are bounded; SSH children belong to OwnedProcess.
[Side-Effect Policy]
- Enrollment/repair may append one public key; never changes sshd or removes other keys.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import sysconfig
import time
import uuid

from remote_dev.core.errors import RemoteExecutionError, record_cleanup_failure
from remote_dev.core.local_process import OwnedProcess
from remote_dev.core.locking import record_lock
from .store import CredentialStore, ProfileStore, auth_error, auth_root, ensure_auth_root, prepare_auth_directory
from .public_key import INSTALL_PUBLIC_KEY_SH

_KEY_OPTIONS = ["-o", "BatchMode=yes", "-o", "PreferredAuthentications=publickey",
                "-o", "PubkeyAuthentication=yes"]
_PASSWORD_OPTIONS = ["-o", "BatchMode=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
                     "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=yes",
                     "-o", "KbdInteractiveAuthentication=yes", "-o", "NumberOfPasswordPrompts=1"]
_READ_ONLY_OPTIONS = ["-o", "BatchMode=no", "-o", "PreferredAuthentications=publickey,password,keyboard-interactive",
                      "-o", "PubkeyAuthentication=yes", "-o", "PasswordAuthentication=yes",
                      "-o", "KbdInteractiveAuthentication=yes", "-o", "NumberOfPasswordPrompts=1"]


@dataclass
class AuthPlan:
    endpoint: object
    options: list[str]
    env: dict[str, str] | None
    identity: tuple


@dataclass
class _Outcome:
    returncode: int
    stdout: str
    stderr: str
    cleanup_error: str | None = None


class _Budget:
    def __init__(self, timeout_ms):
        milliseconds = 30000 if timeout_ms is None else timeout_ms
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)) or milliseconds <= 0:
            raise ValueError("SSH authentication timeout_ms must be positive")
        self.deadline = time.monotonic() + milliseconds / 1000
        self.cleanup_grace = min(3.0, milliseconds / 4000)

    def remaining(self, *, cleanup=False):
        seconds = self.deadline - time.monotonic() - (0 if cleanup else self.cleanup_grace)
        if seconds <= 0:
            raise auth_error("SSH authentication deadline expired before business submission", "authentication_timeout")
        return seconds

    def milliseconds(self, *, cleanup=False):
        return max(1, int(self.remaining(cleanup=cleanup) * 1000))


def _execute(argv, *, env=None, budget):
    """Execute only bounded fixed probes/bootstrap scripts, preserving SSH stderr."""
    budget.remaining()
    owner = OwnedProcess(argv, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    failure = None
    completed = None
    try:
        stdout, stderr = owner.process.communicate(timeout=budget.remaining())
        completed = _Outcome(owner.process.returncode, stdout.decode("utf-8", "replace"),
                             stderr.decode("utf-8", "replace"))
        return completed
    except subprocess.TimeoutExpired as exc:
        failure = auth_error("SSH authentication deadline expired before business submission", "authentication_timeout")
        failure.ssh_started = True
        raise failure from exc
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            owner.stop(timeout=max(0.001, min(2, budget.deadline - time.monotonic())))
        except Exception as exc:
            if completed is not None:
                completed.cleanup_error = f"{type(exc).__name__}: {exc}"
            elif failure is None:
                raise
            else:
                record_cleanup_failure(failure, exc)
        for stream in (owner.process.stdout, owner.process.stderr):
            if stream is not None:
                stream.close()


def ensure_key(identity_file=None, *, timeout_ms=10000):
    """Return the selected private-key path and public line, generating only our default."""
    from remote_dev.core.ssh_transport import _ssh_executable
    ssh = Path(_ssh_executable())
    keygen = str(ssh.with_name("ssh-keygen" + ssh.suffix)) if ssh.is_absolute() else "ssh-keygen"
    budget = _Budget(timeout_ms)
    path = Path(identity_file).expanduser() if identity_file else auth_root() / "keys" / "id_ed25519"
    if path.suffix == ".pub":
        path = path.with_suffix("")
    path = path.resolve()
    public_path = Path(str(path) + ".pub")
    prepare_auth_directory(path)
    with record_lock(path, timeout_seconds=budget.remaining()):
        if not path.exists():
            if identity_file:
                raise auth_error(f"SSH identity file does not exist: {path}", "configuration")
            path.parent.mkdir(parents=True, exist_ok=True)
            result = _execute([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(path)], budget=budget)
            _require_success(result, "SSH key generation")
        if not public_path.exists():
            result = _execute([keygen, "-y", "-P", "", "-f", str(path)], budget=budget)
            _require_success(result, "SSH public-key derivation; provide the .pub file for an encrypted key")
            public_path.write_text(result.stdout.strip() + "\n", encoding="utf-8", newline="\n")
        public_key = public_path.read_text(encoding="utf-8").strip()
        if "\n" in public_key or len(public_key.split()) < 2:
            raise auth_error("SSH public key must contain one public-key line", "configuration")
        return str(path), public_key


def _askpass_env(reference, *, timeout_ms=10000):
    script = Path(__file__).with_name("askpass.py").resolve()
    root = ensure_auth_root()
    if os.name == "nt":
        launcher = Path(sysconfig.get_path("scripts")) / "remote-dev-askpass.exe"
        if not launcher.is_file():
            raise auth_error(
                "Windows SSH password access requires the native remote-dev-askpass.exe launcher; "
                "reinstall this remote-dev distribution into the active Python environment",
                "configuration",
            )
    else:
        runtime = hashlib.sha256((sys.executable + "\0" + str(script)).encode()).hexdigest()[:16]
        launcher = root / "helpers" / ("askpass-" + runtime)
        text = "#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(script)]) + ' "$@"\n'
        prepare_auth_directory(launcher)
        with record_lock(launcher, timeout_seconds=min(5, timeout_ms / 1000)):
            if not launcher.exists() or launcher.read_text(encoding="utf-8") != text:
                launcher.write_text(text, encoding="utf-8", newline="\n")
                launcher.chmod(0o700)
    environment = {
        "SSH_ASKPASS": str(launcher), "SSH_ASKPASS_REQUIRE": "force",
        "DISPLAY": os.environ.get("DISPLAY") or "mindie:0",
        "REMOTE_DEV_AUTH_DIR": str(root), "REMOTE_DEV_ASKPASS_REF": reference,
    }
    if os.environ.get("REMOTE_DEV_CREDENTIAL_FILE"):
        environment["REMOTE_DEV_CREDENTIAL_FILE"] = str(Path(os.environ["REMOTE_DEV_CREDENTIAL_FILE"]).expanduser().resolve())
    return environment


def _probe(endpoint, *, method, identity_file=None, credential_ref=None, command="true", budget):
    from remote_dev.core.ssh_transport import _ssh_cmd
    raw = replace(endpoint, auth_ref=None, identity_file=identity_file, ssh_mux=False,
                  container=None, container_selector=None)
    options = _key_options(identity_file) if method == "publickey" else list(_PASSWORD_OPTIONS)
    environment = None if method == "publickey" else _askpass_env(
        credential_ref, timeout_ms=budget.milliseconds())
    connect_seconds = max(1, int(budget.remaining()))
    if endpoint.connect_timeout_ms is not None:
        connect_seconds = min(connect_seconds, max(1, endpoint.connect_timeout_ms // 1000))
    argv = _ssh_cmd(raw)
    argv[1:1] = [*options, "-o", f"ConnectTimeout={connect_seconds}", "-o", "ConnectionAttempts=1"]
    return _execute([*argv, command], env=environment, budget=budget)


def _auth_rejected(result):
    if result.returncode != 255:
        return False
    error = result.stderr.casefold()
    if "host key" in error or "host identification has changed" in error:
        return False
    return "permission denied (" in error or "too many authentication failures" in error or "authentication failed" in error


def _hostkey_error(result):
    error = result.stderr.casefold()
    return "host key verification failed" in error or "host identification has changed" in error


def _require_success(result, stage):
    if result.returncode == 0:
        return
    category = "host_key" if _hostkey_error(result) else (
        "authentication_rejected" if _auth_rejected(result) else "connection_unavailable")
    error = auth_error(f"{stage} failed: {result.stderr.strip()[-2000:] or 'exit ' + str(result.returncode)}", category)
    error.exit_code = result.returncode
    error.stderr_tail = result.stderr[-2000:]
    raise error


def _install_command(public_key):
    return INSTALL_PUBLIC_KEY_SH + "\nremote_dev_install_public_key " + shlex.quote(public_key)


def _bootstrap(endpoint, identity_file, public_key, credential_ref, budget):
    try:
        installed = _probe(endpoint, method="password", credential_ref=credential_ref,
                           command=_install_command(public_key), budget=budget)
    except RemoteExecutionError as exc:
        # This fixed command may have appended the key before its reply was lost.
        unknown = bool(getattr(exc, "ssh_started", False))
        # Business dispatch has not happened; uncertainty belongs only to this
        # separate authentication maintenance write.
        exc.submission_state = "not_sent"
        exc.bootstrap = {"status": "unknown" if unknown else "failed",
                         "installation": "unknown" if unknown else "not_sent", "key_verified": False}
        raise
    facts = {"status": "failed", "installation": "completed" if installed.returncode == 0 else "failed",
             "key_verified": False, "exit_code": installed.returncode}
    if installed.cleanup_error:
        facts["cleanup_error"] = installed.cleanup_error
    if installed.returncode != 0:
        if installed.returncode == 255 and (_auth_rejected(installed) or _hostkey_error(installed)):
            _require_success(installed, "SSH public-key installation")
        if installed.returncode == 255:
            failure = auth_error("SSH public-key installation outcome is unknown; no business command was sent: " + installed.stderr[-2000:], "connection_unavailable")
            failure.bootstrap = {**facts, "status": "unknown", "installation": "unknown"}
            raise failure
        facts["error"] = installed.stderr[-2000:]
        return facts
    try:
        checked = _probe(endpoint, method="publickey", identity_file=identity_file, budget=budget)
    except RemoteExecutionError as exc:
        exc.bootstrap = {**facts, "verification": "unknown", "error": str(exc)}
        raise
    if checked.returncode and not _auth_rejected(checked):
        try:
            _require_success(checked, "SSH public-key verification after installation")
        except RemoteExecutionError as exc:
            exc.bootstrap = {**facts, "verification": "failed", "exit_code": checked.returncode,
                             "error": checked.stderr[-2000:]}
            raise
    facts.update(key_verified=checked.returncode == 0, exit_code=checked.returncode,
                 status="completed" if checked.returncode == 0 else "failed",
                 verification="completed" if checked.returncode == 0 else "failed")
    if checked.cleanup_error:
        facts["cleanup_error"] = checked.cleanup_error
    if checked.returncode:
        facts["error"] = checked.stderr[-2000:]
    return facts


def _enroll_key(endpoint, identity_file, credential_ref, budget):
    try:
        identity_file, public_key = ensure_key(
            identity_file, timeout_ms=max(1, int(budget.remaining(cleanup=True) * 1000)))
    except RemoteExecutionError as exc:
        if exc.category != "configuration":
            raise
        return identity_file, {"status": "failed", "installation": "not_sent",
                               "key_verified": False, "error": str(exc)}
    except (FileNotFoundError, PermissionError) as exc:
        return identity_file, {"status": "failed", "installation": "not_sent",
                               "key_verified": False, "error": str(exc)}
    return identity_file, _bootstrap(endpoint, identity_file, public_key, credential_ref, budget)


def _identity(profile):
    return (profile["auth_ref"], profile["revision"], profile["auth_method"],
            profile.get("identity_file"), profile.get("known_hosts_file"))


def _key_options(identity_file):
    return [*_KEY_OPTIONS, *(["-o", "IdentitiesOnly=yes"] if identity_file else [])]


def connection_identity(endpoint):
    """Non-secret pool identity; no credentials, SSH probes or filesystem writes."""
    profile = ProfileStore().resolve(endpoint)
    if profile:
        _validate_profile_overrides(endpoint, profile)
        return _identity(profile)
    return ("unmanaged", endpoint.identity_file, getattr(endpoint, "known_hosts_file", None))


def _validate_profile_overrides(endpoint, profile):
    for field in ("identity_file", "known_hosts_file"):
        requested = getattr(endpoint, field, None)
        if requested and requested != profile.get(field):
            raise auth_error(f"SSH auth profile uses a different {field}; re-enroll to change authentication settings", "configuration")


def _plan(endpoint, profile, *, allow_password_fallback=False, timeout_ms=10000):
    _validate_profile_overrides(endpoint, profile)
    target = replace(endpoint, auth_ref=profile["auth_ref"],
                     identity_file=profile.get("identity_file"),
                     known_hosts_file=profile.get("known_hosts_file"))
    if allow_password_fallback and profile["auth_method"] == "publickey" and profile.get("credential_ref"):
        # One SSH handshake may choose the stored password without modifying
        # remote keys or retrying a business command during diagnostics.
        target = replace(target, ssh_mux=False)
        options = [*_READ_ONLY_OPTIONS, *(["-o", "IdentitiesOnly=yes"] if target.identity_file else [])]
        return AuthPlan(target, options,
                        _askpass_env(profile["credential_ref"], timeout_ms=timeout_ms),
                        (*_identity(profile), "read_only_fallback"))
    if profile["auth_method"] == "password":
        reference = profile.get("credential_ref")
        if not reference:
            raise auth_error("Password SSH profile has no credential reference", "credential_unavailable")
        target = replace(target, ssh_mux=False)
        return AuthPlan(target, list(_PASSWORD_OPTIONS),
                        _askpass_env(reference, timeout_ms=timeout_ms), _identity(profile))
    return AuthPlan(target, _key_options(target.identity_file), None, _identity(profile))


def setup_access(endpoint, *, password=None, password_env=None, identity_file=None,
                 install_key=True, timeout_ms=30000):
    """Enroll one SSH principal; keep a verified password for future key repair."""
    budget = _Budget(timeout_ms)
    lock = ProfileStore.path(ProfileStore.reference(endpoint)).with_suffix(".access")
    prepare_auth_directory(lock)
    with record_lock(lock, timeout_seconds=budget.remaining()):
        return _setup_access(endpoint, password=password, password_env=password_env,
                             identity_file=identity_file, install_key=install_key, budget=budget)


def _setup_access(endpoint, *, password, password_env, identity_file, install_key, budget):
    profiles, credentials = ProfileStore(), CredentialStore()
    previous = profiles.resolve(endpoint)
    known_hosts_file = getattr(endpoint, "known_hosts_file", None) or (previous.get("known_hosts_file") if previous else None)
    endpoint = replace(endpoint, known_hosts_file=known_hosts_file)
    if password is not None and password_env:
        raise auth_error("Pass password or password_env, not both", "configuration")
    if password_env:
        password = credentials.get("env:" + password_env, timeout_ms=budget.milliseconds())
    credential_ref = previous.get("credential_ref") if previous else None
    new_reference = None
    saved_profile = False
    credential_verified = False
    bootstrap = None
    try:
        if password is not None:
            credential_ref = new_reference = credentials.put(password, timeout_ms=budget.milliseconds())
        selected_identity = identity_file or (previous.get("identity_file") if previous else None) or endpoint.identity_file
        if selected_identity:
            path = Path(selected_identity).expanduser()
            selected_identity = str((path.with_suffix("") if path.suffix == ".pub" else path).resolve())
        if install_key:
            key_check = _probe(endpoint, method="publickey", identity_file=selected_identity, budget=budget)
            if key_check.returncode and not _auth_rejected(key_check):
                _require_success(key_check, "SSH public-key verification")
        else:
            key_check = None
        if credential_ref:
            password_check = _probe(endpoint, method="password", credential_ref=credential_ref, budget=budget)
            _require_success(password_check, "SSH password verification")
            credential_verified = True
        elif key_check is None or key_check.returncode:
            raise auth_error("SSH password is required to configure access", "credential_required")
        if key_check is not None and key_check.returncode == 0:
            method, bootstrap = "publickey", {"status": "already_configured", "key_verified": True}
        elif install_key:
            selected_identity, bootstrap = _enroll_key(endpoint, selected_identity, credential_ref, budget)
            method = "publickey" if bootstrap["key_verified"] else "password"
        else:
            method, bootstrap = "password", {"status": "not_requested", "key_verified": False}
        profile = {
            "schema_version": 1, "auth_ref": profiles.reference(endpoint), "target": profiles.target(endpoint),
            "auth_method": method, "identity_file": selected_identity,
            "known_hosts_file": known_hosts_file,
            "credential_ref": credential_ref, "revision": uuid.uuid4().hex, "bootstrap": bootstrap,
        }
        try:
            profiles.save(profile, timeout_ms=budget.milliseconds())
        except Exception as exc:
            raise auth_error(
                "SSH access was verified but saving its local auth profile failed",
                "local_recording_failed",
            ) from exc
        saved_profile = True
        result = {"auth_ref": profile["auth_ref"], "auth_method": method,
                  "identity_file": selected_identity, "bootstrap": bootstrap}
        if new_reference and previous and previous.get("credential_ref") and previous["credential_ref"] != new_reference:
            try:
                credentials.delete(previous["credential_ref"], timeout_ms=budget.milliseconds(cleanup=True))
            except Exception as cleanup:
                result["credential_cleanup_error"] = f"{type(cleanup).__name__}: {cleanup}"
        return result
    except BaseException as error:
        if not saved_profile and credential_verified:
            facts = getattr(error, "bootstrap", None) or bootstrap or {
                "status": "failed", "installation": "not_sent", "key_verified": False,
            }
            error.bootstrap = {**facts, "profile_saved": False,
                               "credential_retained": bool(credential_ref)}
            if credential_ref:
                error.bootstrap["credential_ref"] = credential_ref
        elif new_reference and not saved_profile:
            try:
                credentials.delete(new_reference, timeout_ms=budget.milliseconds(cleanup=True))
            except Exception as cleanup:
                record_cleanup_failure(error, cleanup)
        raise


def prepare_auth(endpoint, *, timeout_ms=None, repair=True):
    """Prepare a new connection, repairing a removed key before any business dispatch."""
    from .policy import repair_allowed
    repair = repair and repair_allowed()
    profiles = ProfileStore()
    profile = profiles.resolve(endpoint)
    if profile is None:
        return AuthPlan(endpoint, [], None, ("unmanaged", endpoint.identity_file, getattr(endpoint, "known_hosts_file", None)))
    initial_plan = _plan(endpoint, profile, allow_password_fallback=not repair,
                         timeout_ms=10000 if timeout_ms is None else timeout_ms)
    if repair and profile["auth_method"] == "publickey" and profile.get("credential_ref"):
        budget = _Budget(timeout_ms)
        key_check = _probe(initial_plan.endpoint, method="publickey",
                           identity_file=profile.get("identity_file"), budget=budget)
        if key_check.returncode == 0:
            return initial_plan
        if not _auth_rejected(key_check):
            _require_success(key_check, "SSH public-key verification")
        lock = profiles.path(profile["auth_ref"]).with_suffix(".access")
        with record_lock(lock, timeout_seconds=budget.remaining()):
            # Setup/forget and repair share this lock so a stale repair cannot
            # restore the credential reference deleted by a password rotation.
            profile = profiles.resolve(endpoint)
            if profile is None:
                return AuthPlan(endpoint, [], None, ("unmanaged", endpoint.identity_file, getattr(endpoint, "known_hosts_file", None)))
            plan = _plan(endpoint, profile, timeout_ms=budget.milliseconds())
            if profile["auth_method"] != "publickey" or not profile.get("credential_ref"):
                return plan
            # Recheck while holding the mutation lock: another caller may
            # already have restored the same key after our failed probe.
            profile = _repair_profile(plan.endpoint, profile, profiles, budget)
        return _plan(endpoint, profile, timeout_ms=budget.milliseconds())
    return initial_plan


def _repair_profile(endpoint, profile, profiles, budget):
    key_check = _probe(endpoint, method="publickey", identity_file=profile.get("identity_file"), budget=budget)
    if key_check.returncode:
        if not _auth_rejected(key_check):
            _require_success(key_check, "SSH public-key verification")
        password_check = _probe(endpoint, method="password", credential_ref=profile["credential_ref"], budget=budget)
        _require_success(password_check, "SSH recovery-password verification")
        identity, facts = _enroll_key(endpoint, profile.get("identity_file"), profile["credential_ref"], budget)
        profile = {**profile, "bootstrap": facts, "identity_file": identity}
        if not facts["key_verified"]:
            profile.update(auth_method="password", revision=uuid.uuid4().hex)
        try:
            profiles.save(profile, timeout_ms=budget.milliseconds())
        except Exception as exc:
            error = auth_error("SSH key maintenance finished but saving its local auth profile failed",
                               "local_recording_failed")
            error.bootstrap = {**facts, "profile_saved": False}
            raise error from exc
        from remote_dev.observability import _authentication_event
        _authentication_event.set({
            "auth_ref": profile["auth_ref"], "auth_method": profile["auth_method"],
            "bootstrap": facts,
        })
    return profile


def auth_status(endpoint):
    profile = ProfileStore().resolve(endpoint)
    if profile is None:
        return {"configured": False, "target": ProfileStore.target(endpoint)}
    return {"configured": True, "auth_ref": profile["auth_ref"], "target": profile["target"],
            "auth_method": profile["auth_method"], "identity_file": profile.get("identity_file"),
            "known_hosts_file": profile.get("known_hosts_file"),
            "credential_configured": bool(profile.get("credential_ref")),
            "bootstrap": profile.get("bootstrap")}


def forget_access(endpoint, *, timeout_ms=30000):
    budget = _Budget(timeout_ms)
    profiles = ProfileStore()
    lock = profiles.path(profiles.reference(endpoint)).with_suffix(".access")
    prepare_auth_directory(lock)
    with record_lock(lock, timeout_seconds=budget.remaining()):
        profile = profiles.resolve(endpoint)
        if profile is None:
            return {"removed": False, "remote_key_revoked": False}
        if profile.get("credential_ref"):
            CredentialStore().delete(profile["credential_ref"], timeout_ms=budget.milliseconds())
        profiles.delete(profile["auth_ref"], timeout_ms=budget.milliseconds())
        return {"removed": True, "auth_ref": profile["auth_ref"], "remote_key_revoked": False}
