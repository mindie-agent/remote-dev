"""Official SDK, native descriptor writes, and caller-specific correlation."""
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

SERVER_SOURCE = r'''
import os, subprocess, sys
from remote_dev.result import make_result
from remote_dev.observability import observed_tool
def fake(name, arguments, **kwargs):
    os.write(1, b'native stdout is diagnostic only\n')
    print('python stdout is diagnostic only', flush=True)
    echo = [sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))']
    assert subprocess.run(echo, capture_output=True, timeout=3).stdout.strip() == b'0'
    return {'text': 'ok', 'result': make_result(tool=name, target={}, outcome='success', status='ok', summary='ok')}
if OWNER == 'coordinator':
    import mindie_coordinator.task_server as server
    server.vaws_call = fake
else:
    import remote_dev.mcp.server as server
    server.call_tool = observed_tool(lambda name, arguments: name)(fake)
raise SystemExit(server.main())
'''


@pytest.mark.parametrize("owner", ["remote", "coordinator"])
def test_official_sdk_stdio_and_trace_isolation(tmp_path, owner):
    pytest.importorskip("mcp")
    if owner == "coordinator":
        pytest.importorskip("mindie_coordinator")
    environment = dict(os.environ, MINDIE_DIAGNOSTICS_ROOT=str(tmp_path / "logs"), MINDIE_LOG_LEVEL="DEBUG")
    # Run the SDK acceptance in its own process so even teardown bugs have a
    # hard deadline. No SSH, daemon, identity discovery or device execution.
    script = textwrap.dedent('''
        import os, sys, json, anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult
        owner = sys.argv[1]
        server = open(sys.argv[3], encoding='utf-8').read()
        async def check():
            parameters = StdioServerParameters(command=sys.executable, args=['-c', server], env=dict(os.environ))
            with open(sys.argv[2], 'w', encoding='utf-8') as errors:
                async with stdio_client(parameters, errlog=errors) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        name = 'vaws_session' if owner == 'coordinator' else 'remote.read'
                        summaries = []
                        for identity in ['a', 'b']:
                            context = {'trace_id': identity * 32, 'operation_id': identity * 32}
                            params = CallToolRequestParams(name=name, arguments={}, _meta={'mindie_diagnostics': context})
                            result = await session.send_request(CallToolRequest(method='tools/call', params=params), CallToolResult)
                            summary = result.model_dump(by_alias=True)['structuredContent']['diagnostics']
                            assert summary['trace_id'] == context['trace_id'], summary
                            assert summary['parent_operation_id'] == context['operation_id'], summary
                            assert summary['finished_at'] and summary['status'] == 'success'
                            summaries.append(summary['operation_id'])
                        assert summaries[0] != summaries[1]
        anyio.run(check)
    ''')
    server_path = tmp_path / "server.py"
    server_path.write_text("OWNER = " + repr(owner) + "\n" + SERVER_SOURCE, encoding="utf-8")
    stderr = tmp_path / "server-stderr.log"
    child = subprocess.run([sys.executable, "-c", script, owner, str(stderr), str(server_path)], env=environment,
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=25)
    assert child.returncode == 0, child.stderr.decode("utf-8", "replace") + (stderr.read_text(encoding="utf-8") if stderr.exists() else "")
    diagnostic_output = stderr.read_text(encoding="utf-8")
    assert diagnostic_output.count("native stdout is diagnostic only") == 2
    assert diagnostic_output.count("python stdout is diagnostic only") == 2
