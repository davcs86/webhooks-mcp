import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from webhooks_mcp.main import StoreOpenError, run

pytest_plugins = ["mcp_fixtures"]

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
GOOD = {"MCP_AUTH_KEY": "k" * 40, "INGEST_BASE_URLS": '{"default": "https://h.example"}'}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ports() -> tuple[int, int]:
    mcp, ingest = _free_port(), _free_port()
    while ingest == mcp:
        ingest = _free_port()
    return mcp, ingest


@pytest.fixture
async def running(settings, ports):
    ready = asyncio.Event()
    task = asyncio.create_task(
        run(settings, mcp_port=ports[0], ingest_port=ports[1], host="127.0.0.1", ready=ready)
    )
    waiter = asyncio.create_task(ready.wait())
    await asyncio.wait({task, waiter}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task.done():
        task.result()
    assert ready.is_set()
    yield task
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _call(client, port, auth, name, args, id_=2):
    body = {
        "jsonrpc": "2.0",
        "id": id_,
        "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    r = await client.post(
        f"http://127.0.0.1:{port}/mcp", json=body, headers={**MCP_HEADERS, **auth}
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"]


async def test_both_listeners_serve_and_share_the_store(running, ports, auth):
    mcp_port, ingest_port = ports
    async with httpx.AsyncClient() as c:
        for port in ports:
            assert (await c.get(f"http://127.0.0.1:{port}/healthz")).status_code == 200
        r = await c.post(
            f"http://127.0.0.1:{ingest_port}/hooks",
            json={"event_name": "orders.new", "payload": {"n": 1}},
        )
        assert r.status_code == 201
        got = await _call(c, mcp_port, auth, "get_event", {"event_id": r.json()["event_id"]})
        assert got["payload"] == {"n": 1}
        assert (await c.post(f"http://127.0.0.1:{mcp_port}/hooks", json={})).status_code == 404
        assert (await c.post(f"http://127.0.0.1:{ingest_port}/mcp", json={})).status_code == 404
        unauth = await c.post(f"http://127.0.0.1:{mcp_port}/mcp", json={}, headers=MCP_HEADERS)
        assert unauth.status_code == 401
    running.cancel()
    await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 5)
    assert running.done()


async def test_wait_for_event_wakes_across_listeners(running, ports, auth):
    mcp_port, ingest_port = ports
    async with httpx.AsyncClient(timeout=10) as c:
        waiting = asyncio.create_task(
            _call(c, mcp_port, auth, "wait_for_event", {"event_name": "a.b", "timeout_s": 10})
        )
        await asyncio.sleep(0.3)
        start = time.monotonic()
        r = await c.post(
            f"http://127.0.0.1:{ingest_port}/hooks", json={"event_name": "a.b", "payload": {"x": 1}}
        )
        assert r.status_code == 201
        result = await asyncio.wait_for(waiting, 2)
        assert time.monotonic() - start < 2
        assert [e["payload"] for e in result["events"]] == [{"x": 1}]


async def test_shutdown_with_a_wait_in_flight_is_prompt(running, ports, auth):
    async with httpx.AsyncClient(timeout=30) as c:
        waiting = asyncio.create_task(
            _call(c, ports[0], auth, "wait_for_event", {"event_name": "a.b", "timeout_s": 25})
        )
        await asyncio.sleep(0.3)
        running.cancel()
        await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 8)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def test_run_refuses_to_start_when_store_cannot_open(settings, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    bad = type(settings)(settings.mcp_auth_key, settings.ingest_base_urls, blocker / "sub")
    with pytest.raises(StoreOpenError, match="DATA_DIR"):
        await run(bad, mcp_port=_free_port(), ingest_port=_free_port(), host="127.0.0.1")


async def test_run_fails_and_closes_store_when_a_port_is_taken(settings, ports):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        with pytest.raises(RuntimeError, match=str(taken)):
            await asyncio.wait_for(
                run(settings, mcp_port=taken, ingest_port=ports[1], host="127.0.0.1"), 10
            )


async def test_sweep_runs_at_start_and_periodically(settings, ports, monkeypatch):
    calls = []

    async def fake_sweep(self):
        calls.append(1)

    monkeypatch.setattr("webhooks_mcp.store.EventStore.sweep", fake_sweep)
    monkeypatch.setattr("webhooks_mcp.main.SWEEP_INTERVAL_SECONDS", 0.05)
    ready = asyncio.Event()
    task = asyncio.create_task(
        run(settings, mcp_port=ports[0], ingest_port=ports[1], host="127.0.0.1", ready=ready)
    )
    await asyncio.wait_for(ready.wait(), 10)
    await asyncio.sleep(0.4)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 3


def _cli(env, **kw):
    full = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(
        [sys.executable, "-m", "webhooks_mcp.main"],
        env=full,
        capture_output=True,
        text=True,
        timeout=20,
        **kw,
    )


@pytest.mark.parametrize(
    ("env", "var"),
    [({}, "MCP_AUTH_KEY"), ({**GOOD, "INGEST_BASE_URLS": "x"}, "INGEST_BASE_URLS")],
)
def test_cli_exits_2_naming_variable_on_bad_env(env, var):
    p = _cli(env)
    assert p.returncode == 2 and var in p.stderr
    assert len(p.stderr.strip().splitlines()) == 1
    assert GOOD["MCP_AUTH_KEY"] not in p.stderr


def test_cli_exits_2_on_unwritable_data_dir(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    p = _cli({**GOOD, "DATA_DIR": str(blocker / "data")})
    assert p.returncode == 2 and "DATA_DIR" in p.stderr
    assert len(p.stderr.strip().splitlines()) == 1


def test_cli_exits_2_on_missing_data_dir(tmp_path):
    p = _cli({**GOOD, "DATA_DIR": str(tmp_path / "nope")})
    assert p.returncode == 2 and "DATA_DIR" in p.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_cli_exits_2_on_read_only_data_dir(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir(mode=0o500)
    p = _cli({**GOOD, "DATA_DIR": str(ro)})
    assert p.returncode == 2 and "DATA_DIR" in p.stderr


def test_process_logs_json_lines_never_contain_the_key_and_exits_0_on_sigterm(tmp_path):
    mcp, ingest = _free_port(), _free_port()
    env = {
        "PATH": os.environ["PATH"],
        **GOOD,
        "DATA_DIR": str(tmp_path),
    }
    code = (
        "import asyncio,sys;from webhooks_mcp.main import _setup_logging, run;"
        "from webhooks_mcp.config import Settings;_setup_logging();"
        f"asyncio.run(run(Settings.from_env(), {mcp}, {ingest}, '127.0.0.1'))"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{ingest}/healthz").status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.05)
        key = GOOD["MCP_AUTH_KEY"]
        auth = {"Authorization": f"Bearer {key}", "X-Secret-Header": "s3cret-header-value"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        httpx.post(f"http://127.0.0.1:{mcp}/mcp", json=body, headers={**MCP_HEADERS, **auth})
        httpx.post(
            f"http://127.0.0.1:{ingest}/hooks",
            json={"event_name": "a.b", "payload": {"a": 1}},
            headers=auth,
        )
    finally:
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert key not in out and key not in err and "s3cret-header-value" not in out + err
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines and all({"time", "level", "logger", "message"} <= set(x) for x in lines)
