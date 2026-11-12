"""Real stdio fixture for catalog switches with an in-flight request."""
import json
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
if sys.argv[2] == "facade":
    from remote_dev.mcp.catalog_refresh import serve_selected

    def selection():
        generation = json.loads((root / "current.json").read_text())["generation"]
        return [sys.executable, __file__, str(root), generation], {}

    serve_selected(selection)
else:
    generation = sys.argv[2]
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "catalog-fixture", "version": generation}}
        elif method == "tools/list":
            result = {"tools": [{"name": "read", "description": generation,
                                "inputSchema": {"type": "object", "properties": {}}}]}
        else:
            with (root / "calls.jsonl").open("a") as log:
                log.write(json.dumps({"id": message["id"], "generation": generation}) + "\n")
            (root / "started").touch()
            deadline = time.monotonic() + 5
            while not (root / "release").exists():
                if time.monotonic() > deadline:
                    raise TimeoutError("test never released its request")
                time.sleep(.02)
            result = {"content": [{"type": "text", "text": generation}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
