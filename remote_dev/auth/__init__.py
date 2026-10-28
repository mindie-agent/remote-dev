"""[Role] Public SSH access/profile API shared by every MindIE component.
[Invariants] Passwords never belong to endpoint or job records.
[Timeouts & Locks] Implementations own bounded auth probes and profile locks.
[Side-Effect Policy] Only explicit setup/forget and pre-dispatch repair mutate state.
"""
from .access import (
    AuthPlan, auth_status, connection_identity, ensure_key, forget_access,
    prepare_auth, setup_access,
)
from .store import CredentialStore, ProfileStore
from .policy import read_only
from .public_key import INSTALL_PUBLIC_KEY_SH

__all__ = [
    "AuthPlan", "CredentialStore", "ProfileStore", "INSTALL_PUBLIC_KEY_SH",
    "auth_status",
    "connection_identity", "ensure_key", "forget_access", "prepare_auth", "read_only", "setup_access",
]
