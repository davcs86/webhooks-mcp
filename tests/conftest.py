from datetime import UTC, datetime, timedelta

import httpx
import pytest

from webhooks_mcp.ingest_app import build_ingest_app
from webhooks_mcp.store import EventStore


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def store(tmp_path, clock):
    s = EventStore(tmp_path / "events.db", clock)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
async def ingest(store):
    app = build_ingest_app(store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ingest"
    ) as client:
        yield client
