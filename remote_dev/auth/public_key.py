"""[Role] Shared POSIX public-key installation primitive for SSH and containers.
[Invariants] Preserve existing keys and line boundaries; add the requested line once.
[Timeouts & Locks] The calling SSH or container process owns the execution deadline.
[Side-Effect Policy] Creates the selected SSH directory and appends authorized_keys.
"""

INSTALL_PUBLIC_KEY_SH = r"""
remote_dev_install_public_key() (
    key=$1
    ssh_dir=${2:-"$HOME/.ssh"}
    umask 077
    mkdir -p "$ssh_dir" &&
    touch "$ssh_dir/authorized_keys" &&
    chmod 700 "$ssh_dir" &&
    chmod 600 "$ssh_dir/authorized_keys" &&
    (grep -qxF -- "$key" "$ssh_dir/authorized_keys" || {
        if [ -s "$ssh_dir/authorized_keys" ]; then
            printf '\n' >> "$ssh_dir/authorized_keys"
        fi
        printf '%s\n' "$key" >> "$ssh_dir/authorized_keys"
    })
)
"""
