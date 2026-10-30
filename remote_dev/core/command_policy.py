"""
[Role] Command-level safety interceptor for destructive host and NPU operations (`command_policy.py`).
[Invariants]
- Intercepts three destructive command classes before any remote dispatch (`submission_state="not_sent"`):
  1. `npu_reset`: Ascend NPU reset via `npu-smi set -t reset` (or `npu-smi set ... reset`).
  2. `shutdown`: Host shutdown/poweroff/halt (`shutdown` without `-r`/`-c`, `poweroff`, `halt`, `init 0`, `telinit 0`, `systemctl poweroff|halt|shutdown`, `ipmitool ... power off|soft`).
  3. `reboot`: Host reboot (`reboot`, `shutdown -r`, `init 6`, `telinit 6`, `systemctl reboot|kexec|soft-reboot`, `ipmitool ... power reset|cycle`).
- Ignores container lifecycle commands (`docker`, `podman`, `nerdctl`, `crictl`, `kubectl`) and normal service restarts (`systemctl restart <service>`).
- Requires explicit human confirmation (`confirm=True` or `REMOTE_DEV_CONFIRM_DANGEROUS=1`) before permitting execution.
[Timeouts & Locks]
- Pure in-memory lexical command inspection with bounded recursion (`MAX_SHELL_NESTING = 4`); zero I/O, locks, or subprocesses.
[Side-Effect Policy]
- Side-effect-free command classification and `remote-dev.result.v1` blocked payload construction.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
from typing import Any, Mapping

from remote_dev.result import make_result

CONFIRM_ENV_VAR = "REMOTE_DEV_CONFIRM_DANGEROUS"
_TRUTHY_CONFIRM_VALUES = frozenset({"1", "true", "yes", "on", "confirmed"})
_ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
MAX_SHELL_NESTING = 4

_SHELL_KEYWORDS = frozenset({
    "do", "then", "else", "elif", "if", "while", "until", "!", "time", "coproc",
})
_SHELL_BINARIES = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh"})

_SUDO_VALUE_FLAGS = frozenset({
    "-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-T", "-U",
    "--user", "--group", "--close-from", "--chdir", "--host",
    "--prompt", "--role", "--type", "--command-timeout", "--other-user",
})
_ENV_VALUE_FLAGS = frozenset({
    "-u", "-C", "-S", "--unset", "--chdir", "--split-string",
})
_TIMEOUT_VALUE_FLAGS = frozenset({
    "-k", "-s", "--kill-after", "--signal",
})
_XARGS_VALUE_FLAGS = frozenset({
    "-I", "-L", "-n", "-P", "-s", "-a", "-d", "-E", "-e",
})
_SSH_VALUE_FLAGS = frozenset({
    "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J",
    "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
})


@dataclass(frozen=True)
class DangerousCommandMatch:
    category: str  # "npu_reset" | "reboot" | "shutdown"
    label: str
    matched_command: str

    @property
    def reason(self) -> str:
        return (
            f"Dangerous operation intercepted ({self.category}: {self.label}) in command "
            f"`{self.matched_command}`. Ask the user for explicit confirmation first. "
            f"Only after the user confirms, rerun with confirm=true (or --confirm on CLI, "
            f"or prefix the command with {CONFIRM_ENV_VAR}=1)."
        )


def is_confirmed(
    *,
    confirm: bool = False,
    env: Mapping[str, str] | None = None,
) -> bool:
    if confirm is True:
        return True
    if env:
        val = str(env.get(CONFIRM_ENV_VAR) or "").strip().lower()
        if val in _TRUTHY_CONFIRM_VALUES:
            return True
    return False


def _split_shell_segments(command: str) -> list[str]:
    """Split a shell command string on unquoted control operators and extract substitutions."""
    segments: list[str] = []
    buf: list[str] = []
    sub_stack: list[list[str]] = []
    quote: str | None = None
    i = 0
    n = len(command)

    def flush_buf() -> None:
        text = "".join(buf).strip()
        buf.clear()
        if text:
            segments.append(text)

    while i < n:
        ch = command[i]
        current = sub_stack[-1] if sub_stack else buf

        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue

        if quote == '"':
            if ch == "\\" and i + 1 < n:
                current.append(ch)
                current.append(command[i + 1])
                i += 2
                continue
            if ch == "$" and i + 1 < n and command[i + 1] == "(":
                sub_stack.append([])
                i += 2
                continue
            if ch == ")" and sub_stack:
                sub_text = "".join(sub_stack.pop()).strip()
                if sub_text:
                    segments.extend(_split_shell_segments(sub_text))
                i += 1
                continue
            current.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue

        # Unquoted context
        if ch == "\\" and i + 1 < n:
            current.append(ch)
            current.append(command[i + 1])
            i += 2
            continue

        if ch == "#" and (not current or current[-1].isspace()):
            while i < n and command[i] != "\n":
                i += 1
            continue

        if ch in {"'", '"'}:
            quote = ch
            current.append(ch)
            i += 1
            continue

        if ch == "$" and i + 1 < n and command[i + 1] == "(":
            sub_stack.append([])
            i += 2
            continue

        if ch == ")" and sub_stack:
            sub_text = "".join(sub_stack.pop()).strip()
            if sub_text:
                segments.extend(_split_shell_segments(sub_text))
            i += 1
            continue

        if ch == "`":
            end = command.find("`", i + 1)
            if end != -1:
                sub_text = command[i + 1 : end].strip()
                if sub_text:
                    segments.extend(_split_shell_segments(sub_text))
                i = end + 1
                continue

        if not sub_stack:
            if ch in {";", "\n", "&", "|", "(", ")", "{", "}"}:
                flush_buf()
                i += 1
                continue

        current.append(ch)
        i += 1

    while sub_stack:
        sub_text = "".join(sub_stack.pop()).strip()
        if sub_text:
            segments.extend(_split_shell_segments(sub_text))
    flush_buf()
    return segments


def _tokenize_segment(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _unwrap_tokens(tokens: list[str]) -> tuple[list[str], bool]:
    """Strip shell keywords, env assignments, and command wrappers (`sudo`, `env`, `nohup`, `timeout`, etc.)."""
    items = list(tokens)
    confirmed_inline = False

    while items:
        head = items[0]
        if head in _SHELL_KEYWORDS:
            items.pop(0)
            continue

        if head == "export":
            items.pop(0)
            while items:
                m = _ENV_ASSIGN_RE.match(items[0])
                if not m:
                    break
                if m.group(1) == CONFIRM_ENV_VAR and m.group(2).strip().lower() in _TRUTHY_CONFIRM_VALUES:
                    confirmed_inline = True
                items.pop(0)
            continue

        m = _ENV_ASSIGN_RE.match(head)
        if m:
            if m.group(1) == CONFIRM_ENV_VAR and m.group(2).strip().lower() in _TRUTHY_CONFIRM_VALUES:
                confirmed_inline = True
            items.pop(0)
            continue

        exe = os.path.basename(head).lower()

        if exe == "sudo":
            items.pop(0)
            while items and items[0].startswith("-"):
                flag = items.pop(0)
                if flag == "--":
                    break
                if flag in _SUDO_VALUE_FLAGS and items:
                    items.pop(0)
            continue

        if exe == "env":
            items.pop(0)
            while items:
                if items[0] == "--":
                    items.pop(0)
                    break
                if items[0] in _ENV_VALUE_FLAGS and len(items) >= 2:
                    items = items[2:]
                    continue
                if items[0].startswith("-"):
                    items.pop(0)
                    continue
                env_m = _ENV_ASSIGN_RE.match(items[0])
                if env_m:
                    if env_m.group(1) == CONFIRM_ENV_VAR and env_m.group(2).strip().lower() in _TRUTHY_CONFIRM_VALUES:
                        confirmed_inline = True
                    items.pop(0)
                    continue
                break
            continue

        if exe in {"nohup", "command", "builtin", "exec", "setsid"}:
            items.pop(0)
            while items and items[0].startswith("-"):
                flag = items.pop(0)
                if flag == "--":
                    break
                if exe == "exec" and flag == "-a" and items:
                    items.pop(0)
            continue

        if exe == "timeout":
            items.pop(0)
            while items and items[0].startswith("-"):
                flag = items.pop(0)
                if flag == "--":
                    break
                if flag in _TIMEOUT_VALUE_FLAGS and items:
                    items.pop(0)
            if items:
                items.pop(0)  # duration
            continue

        if exe == "nice":
            items.pop(0)
            if items and items[0] in {"-n", "--adjustment"} and len(items) >= 2:
                items = items[2:]
            elif items and items[0].startswith("-"):
                items.pop(0)
            continue

        if exe == "xargs":
            items.pop(0)
            while items and items[0].startswith("-"):
                flag = items.pop(0)
                if flag == "--":
                    break
                if flag in _XARGS_VALUE_FLAGS and items:
                    items.pop(0)
            continue

        if exe == "sshpass":
            items.pop(0)
            while items and items[0].startswith("-"):
                flag = items.pop(0)
                if flag in {"-p", "-f", "-d", "-P"} and items:
                    items.pop(0)
            continue

        break

    return items, confirmed_inline


def _has_short_flag(args: list[str], char: str) -> bool:
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("-") and not arg.startswith("--") and char in arg[1:]:
            return True
    return False


def _match_executable(exe: str, args: list[str], matched_text: str) -> DangerousCommandMatch | None:
    low_args = [a.lower() for a in args]

    # 1. Ascend NPU reset: `npu-smi set -t reset ...`
    if exe in {"npu-smi", "npu_smi"}:
        if "--help" in low_args or "-h" in low_args:
            return None
        non_opt = [a for a in low_args if not a.startswith("-")]
        if non_opt and non_opt[0] == "set":
            has_reset = (
                "reset" in low_args
                or any(a in {"-treset", "--type=reset"} for a in low_args)
            )
            if has_reset:
                return DangerousCommandMatch(
                    category="npu_reset",
                    label="NPU reset (npu-smi set -t reset)",
                    matched_command=matched_text,
                )
        return None

    # 2. Host reboot: `reboot`
    if exe == "reboot":
        if any(a in {"--help", "-?", "--version", "-w", "--wtmp-only"} for a in low_args):
            return None
        if "--halt" in low_args or "--poweroff" in low_args or _has_short_flag(args, "p"):
            return DangerousCommandMatch(
                category="shutdown",
                label="system shutdown (reboot --poweroff/--halt)",
                matched_command=matched_text,
            )
        return DangerousCommandMatch(
            category="reboot",
            label="system reboot (reboot)",
            matched_command=matched_text,
        )

    # 3. Host shutdown / reboot: `shutdown`
    if exe == "shutdown":
        if any(a in {"--help", "-?", "--version", "--cancel"} for a in low_args) or _has_short_flag(args, "c") or _has_short_flag(args, "k"):
            return None
        if "--reboot" in low_args or _has_short_flag(args, "r"):
            return DangerousCommandMatch(
                category="reboot",
                label="system reboot (shutdown -r)",
                matched_command=matched_text,
            )
        return DangerousCommandMatch(
            category="shutdown",
            label="system shutdown (shutdown)",
            matched_command=matched_text,
        )

    # 4. Host poweroff / halt: `poweroff`, `halt`
    if exe in {"poweroff", "halt"}:
        if any(a in {"--help", "-?", "--version", "-w", "--wtmp-only"} for a in low_args):
            return None
        if "--reboot" in low_args:
            return DangerousCommandMatch(
                category="reboot",
                label=f"system reboot ({exe} --reboot)",
                matched_command=matched_text,
            )
        return DangerousCommandMatch(
            category="shutdown",
            label=f"system shutdown ({exe})",
            matched_command=matched_text,
        )

    # 5. SysV runlevel: `init 0` / `init 6` / `telinit 0` / `telinit 6`
    if exe in {"init", "telinit"}:
        if any(a in {"--help", "-?", "--version"} for a in low_args):
            return None
        non_opt = [a for a in low_args if not a.startswith("-")]
        if non_opt == ["0"]:
            return DangerousCommandMatch(
                category="shutdown",
                label=f"system shutdown ({exe} 0)",
                matched_command=matched_text,
            )
        if non_opt == ["6"]:
            return DangerousCommandMatch(
                category="reboot",
                label=f"system reboot ({exe} 6)",
                matched_command=matched_text,
            )
        return None

    # 6. systemd host power actions: `systemctl reboot|poweroff|halt|shutdown`
    if exe == "systemctl":
        if any(a in {"--help", "-?", "--version"} for a in low_args):
            return None
        non_opt = [a for a in low_args if not a.startswith("-")]
        if not non_opt:
            return None
        action = non_opt[0]
        target = non_opt[1] if len(non_opt) > 1 else ""
        if action in {"reboot", "kexec", "soft-reboot"} or (
            action in {"isolate", "start"} and target in {"reboot.target", "kexec.target", "soft-reboot.target"}
        ):
            return DangerousCommandMatch(
                category="reboot",
                label=f"system reboot (systemctl {action})",
                matched_command=matched_text,
            )
        if action in {"poweroff", "halt", "shutdown"} or (
            action in {"isolate", "start"} and target in {"poweroff.target", "halt.target", "shutdown.target"}
        ):
            return DangerousCommandMatch(
                category="shutdown",
                label=f"system shutdown (systemctl {action})",
                matched_command=matched_text,
            )
        return None

    # 7. IPMI chassis/power control: `ipmitool ... power off|soft|reset|cycle`
    if exe == "ipmitool":
        if any(a in {"--help", "-?", "-h"} for a in low_args):
            return None
        for idx, token in enumerate(low_args[:-1]):
            if token == "power":
                sub = low_args[idx + 1]
                if sub in {"off", "soft"}:
                    return DangerousCommandMatch(
                        category="shutdown",
                        label=f"system shutdown (ipmitool power {sub})",
                        matched_command=matched_text,
                    )
                if sub in {"reset", "cycle"}:
                    return DangerousCommandMatch(
                        category="reboot",
                        label=f"system reboot (ipmitool power {sub})",
                        matched_command=matched_text,
                    )
        return None

    return None


def inspect_dangerous_command(
    command: str,
    *,
    confirm: bool = False,
    env: Mapping[str, str] | None = None,
    _depth: int = 0,
) -> DangerousCommandMatch | None:
    """Return a `DangerousCommandMatch` when `command` contains an unconfirmed dangerous operation."""
    if not isinstance(command, str) or not command.strip():
        return None
    if is_confirmed(confirm=confirm, env=env):
        return None
    if _depth > MAX_SHELL_NESTING:
        return None

    script_confirmed = False
    for segment in _split_shell_segments(command):
        raw_tokens = _tokenize_segment(segment)
        if not raw_tokens:
            continue
        tokens, seg_confirmed = _unwrap_tokens(raw_tokens)
        if seg_confirmed:
            script_confirmed = True
        if not tokens:
            continue
        if script_confirmed or seg_confirmed:
            continue

        exe = os.path.basename(tokens[0]).lower()
        args = tokens[1:]

        # Recurse into nested shell `-c "..."` invocations
        if exe in _SHELL_BINARIES:
            for idx, arg in enumerate(args):
                if arg == "--":
                    break
                if arg in {"-c", "--command"} or (
                    arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]
                ):
                    if idx + 1 < len(args):
                        nested = inspect_dangerous_command(
                            args[idx + 1],
                            confirm=False,
                            env=env,
                            _depth=_depth + 1,
                        )
                        if nested is not None:
                            return nested
                    break
            continue

        # Recurse into `ssh [opts] host <remote_command...>`
        if exe == "ssh":
            rem = list(args)
            while rem and rem[0].startswith("-"):
                flag = rem.pop(0)
                if flag == "--":
                    break
                if flag in _SSH_VALUE_FLAGS and rem:
                    rem.pop(0)
            if len(rem) >= 2:
                remote_cmd = " ".join(rem[1:])
                nested = inspect_dangerous_command(
                    remote_cmd,
                    confirm=False,
                    env=env,
                    _depth=_depth + 1,
                )
                if nested is not None:
                    return nested
            continue

        match = _match_executable(exe, args, " ".join(tokens))
        if match is not None:
            return match

    return None


def blocked_command_payload(
    *,
    tool: str,
    target: dict[str, Any],
    command: str,
    match: DangerousCommandMatch,
    job_id: str | None = None,
) -> dict[str, Any]:
    summary = (
        f"{tool} blocked: dangerous operation ({match.category}: {match.label}) "
        "requires explicit human confirmation before execution."
    )
    next_info = {
        "suggested_action": "ask_user_confirmation",
        "requires_human_confirmation": True,
        "category": match.category,
        "matched_command": match.matched_command,
        "message": match.reason,
    }
    extra: dict[str, Any] = {
        "error": match.reason,
        "category": match.category,
        "matched_command": match.matched_command,
        "requires_human_confirmation": True,
        "command_preview": command[:500],
        "error_details": {
            "type": "DangerousCommandBlocked",
            "category": "permission",
            "submission_state": "not_sent",
            "retryable": False,
        },
    }
    if job_id is not None:
        extra["job_id"] = job_id
        extra["session_id"] = job_id

    result = make_result(
        tool=tool,
        target=target,
        outcome="blocked",
        status="confirmation_required",
        summary=summary,
        preview={"stderr": match.reason},
        next=next_info,
        extra=extra,
    )
    text = f"{summary}\n{match.reason}\n"
    return {"text": text, "result": result}
