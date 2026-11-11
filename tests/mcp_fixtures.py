import asyncio

import httpx
import pytest

from webhooks_mcp.config import Settings
from webhooks_mcp.mcp_app import build_mcp_app

KEY = "k" * 40


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        KEY,
        {"default": "https://hooks.example", "staging": "https://hooks.example:8443"},
        tmp_path,
    )


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
async def mcp_http(store, settings):
    app = build_mcp_app(store, settings)
    started, stop = asyncio.Event(), asyncio.Event()

    # The session manager's task group must be entered and exited in the same task.
    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run_lifespan())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    waiter.cancel()
    if task.done():
        task.result()  # re-raises the startup failure
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    stop.set()
    await task
