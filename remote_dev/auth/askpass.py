"""[Role] Answer SSH password prompts from a shared credential reference.
[Invariants]
- stdout contains only the requested password; unsupported challenges fail.
[Timeouts & Locks]
- One provider read per invocation; the owning SSH operation bounds its lifetime.
[Side-Effect Policy]
- Reads the selected credential provider, never changes credentials or profiles.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys


def main():
    # The launcher uses the owning runtime with -I, including source checkouts.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from remote_dev.auth.store import CredentialStore
    from remote_dev.core.errors import RemoteExecutionError
    prompt = " ".join(sys.argv[1:]).casefold()
    changing_password = any(text in prompt for text in ("new password", "retype", "repeat", "current)"))
    if changing_password or os.environ.get("SSH_ASKPASS_PROMPT") == "confirm" or (prompt and "password" not in prompt):
        sys.stderr.write("SSH requires an interactive challenge; a saved password cannot answer it.\n")
        return 1
    try:
        value = CredentialStore().get(os.environ["REMOTE_DEV_ASKPASS_REF"])
    except RemoteExecutionError as exc:
        sys.stderr.write(f"SSH credential lookup failed: {exc}\n")
        return 1
    except Exception as exc:
        sys.stderr.write(f"SSH credential lookup failed ({type(exc).__name__}).\n")
        return 1
    sys.stdout.buffer.write((value + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
