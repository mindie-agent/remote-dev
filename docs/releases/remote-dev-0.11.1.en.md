# remote-dev 0.11.1

[中文](remote-dev-0.11.1.md)

Fix native Windows processes failing to locate bundled OpenSSH when they inherit a Git Bash PATH. The resolver now accepts `Git/ucrt64/bin/git.exe` and `Git/usr/bin/git.exe` alongside the existing layouts, and selects `usr/bin/ssh.exe` from that installation. This fixes a real npu-top Windows CI failure where Git resolved to `C:\Program Files\Git\ucrt64\bin\git.exe`.

The shared authentication behavior from 0.11.0 remains: remote-dev owns passwords and keys, and coordinator and npu-top reuse it. Automatic key installation, password-only access, and revoked-key recovery do not access an OS credential vault or require a system password. Authentication repair occurs before business dispatch; submitted business work is never replayed. Read-only monitoring can use saved passwords without changing remote keys.

Git discovery contracts cover the additional layouts. Real SSH acceptance records the actual Git and SSH paths and exercises all four authentication paths in PowerShell with both its ordinary PATH and a Git Bash layout PATH; WSL2 exercises native Linux OpenSSH. Exact measured commits and results are recorded in the accompanying product 0.1.2 acceptance artifact. The release workflow produces benchmark reports from the tag's clean source.
