"""Resident line-delimited JSON-RPC helper runner for selected plugin generations.

[Role]
Executes a selected plugin `mcp_server.py` module's resident `serve(surface)` loop
directly over stdio pipes without triggering `--once` whole-stdin EOF reads or
re-entering `serve_selected`.

[Invariants]
- Runs under `python -I` in the selected generation's interpreter.
- Removes `_resident_helper.py`'s own `remote_dev/mcp` directory from `sys.path`,
  prepends only the target script's directory to `sys.path[0]`, and preserves the
  selected generation's installed `remote_dev` (appending the fallback checkout root
  only when `remote_dev` is not installed in site-packages).

[Timeouts & Locks]
- Request deadlines and process lifecycle are owned by the parent `catalog_refresh`
  worker pool.

[Side-Effect Policy]
- Delegates stdio RPC handling to the loaded `mcp_server` module.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2:
        raise SystemExit("usage: python -I _resident_helper.py <mcp_server.py> <surface>")
    own_dir = str(Path(__file__).resolve().parent)
    sys.path[:] = [entry for entry in sys.path if str(Path(entry).resolve()) != own_dir]
    script_path = Path(args[0]).resolve()
    surface = args[1]
    script_dir = str(script_path.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    if importlib.util.find_spec("remote_dev") is None:
        fallback_root = str(Path(__file__).resolve().parents[2])
        if fallback_root not in sys.path:
            sys.path.append(fallback_root)
    if os.name == "nt":
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
    try:
        import diagnostic_support  # type: ignore[import-not-found]
        if callable(getattr(diagnostic_support, "bind_environment", None)):
            diagnostic_support.bind_environment()
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("mindie_selected_mcp_server", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load MCP helper module: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if surface == "remote" and callable(getattr(module, "bind_remote_profile", None)):
        module.bind_remote_profile()
    serve_fn = getattr(module, "serve", None)
    if not callable(serve_fn):
        raise RuntimeError(f"selected MCP helper has no callable serve(): {script_path}")
    result = serve_fn(surface)
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
