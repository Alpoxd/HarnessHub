import sys

import pytest

from harnesshub.errors import HubError
from harnesshub.service import Hub
from harnesshub.usage import read_usage


async def test_codex_usage_uses_native_rpc_and_preserves_absent_windows(tmp_path, monkeypatch):
    hub = Hub(tmp_path / "hub")
    hub.create("codex", "work")
    script = tmp_path / "app_server.py"
    script.write_text("""import sys,json
for line in sys.stdin:
    req=json.loads(line)
    method=req['method']
    if method=='initialized': continue
    data={}
    if method=='account/read': data={'account': {'planType':'plus'}}
    if method=='account/rateLimits/read':
        data={'rateLimits': {'primary': {'usedPercent':37, 'windowDurationMins':300}}}
    print(json.dumps({'id':req['id'], 'result':data}),flush=True)
""")
    monkeypatch.setattr(hub, "command", lambda *a, **kw: [sys.executable, str(script)])
    result = await read_usage(hub, "codex", "work")
    assert result["plan"] == "plus"
    assert len(result["windows"]) == 1
    assert result["windows"][0]["remaining_percent"] == 63


async def test_unsupported_usage_is_not_reported_as_zero(tmp_path):
    with pytest.raises(HubError, match="пока"):
        await read_usage(Hub(tmp_path), "kimi")
