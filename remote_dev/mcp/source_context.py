"""Source and interpreter discovery metadata for the shared MCP catalog.

[Role] Keep source-selection guidance and probe properties together.
[Invariants] User-selected new sources are supported; metadata is not runtime
or NPU validation. Explicit interpreters never silently fall back.
[Timeouts & Locks] Pure constants; no waits or locks.
[Side-Effect Policy] None.
"""

SOURCE_CHANGE_GUIDANCE = (
    " For source changes or rebuilds, discover the user's selected source "
    "checkout and a compatible interpreter; root/cwd and the local client "
    "directory do not identify installed sources. Use a new source directory "
    "or editable rebind when requested; otherwise retain existing sources "
    "and binding. A directory change does not imply deleting old sources or "
    "creating a new Python environment. Preserve affected baseline artifacts "
    "before overwriting native outputs; reuse them for Python-only edits and "
    "validate actual loading in a fresh process."
    " Discovery and build helpers are optional: if they do not fit, use "
    "explicit paths, existing CLIs or task-local scripts through remote.bash "
    "or authorized native SSH. Preserve confirmed results and job references; "
    "observe uncertain writes before retrying and retain device/port ownership."
)

NATIVE_CONTINUATION_GUIDANCE = (
    " If a helper is unavailable or unsuitable, continue within the user's "
    "authorized scope using native file/shell/SSH tools or task-local scripts. "
    "Report the helper failure separately, reuse confirmed facts and jobs, "
    "and verify the result. Reconcile uncertain external writes and preserve "
    "device/port ownership; changing tools does not grant new authority."
)

PROBE_PROPERTIES = {
    "modules": {"type": "array", "items": {"type": "string"}, "maxItems": 32, "description": "Explicit module imports to check; default empty. Imports may initialize a workload; prefer distributions for source discovery."},
    "distributions": {"type": "array", "items": {"type": "string"}, "maxItems": 32, "description": "Installed distribution names, e.g. vllm and vllm-ascend. Reads versions, installation roots and direct_url.json editable source paths without importing frameworks. Missing metadata is reported, never guessed."},
    "python_executable": {"type": "string", "description": "Explicit absolute remote business interpreter, e.g. /venv/bin/python. Omitted without discovery uses python3 after the runtime profile. Explicit choices never fall back. Metadata inspection does not prove actual module loading."},
    "python_candidates": {"type": "array", "maxItems": 16, "items": {"type": "string"}, "description": "Additional absolute remote interpreters. Discovery also checks active runtime/venv/Conda bindings, PATH and conventional image paths, bounded to 16 candidates. Different venv prefixes remain distinct."},
    "required_distributions": {"type": "array", "maxItems": 32, "items": {"type": "string"}, "description": "Enable discovery using required package metadata, e.g. torch and torch-npu. Select an eligible active binding or sole environment; several eligible environments are ambiguous. Metadata does not prove ABI compatibility or device readiness."},
    "diagnose_connection": {"type": "boolean", "default": False, "description": "Compare SSH connections with a fixed read-only probe; never replays a business command."},
}
