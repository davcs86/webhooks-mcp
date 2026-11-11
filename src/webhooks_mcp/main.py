import asyncio
import contextlib
import json
import logging
import signal
import sqlite3
import sys
from datetime import UTC, datetime

import uvicorn

from webhooks_mcp.config import ConfigError, Settings
from webhooks_mcp.constants import (
    GRACEFUL_SHUTDOWN_SECONDS,
    INGEST_MAX_CONCURRENT_REQUESTS,
    KEEP_ALIVE_SECONDS,
    MAX_CONCURRENT_REQUESTS,
    SWEEP_INTERVAL_SECONDS,
)
from webhooks_mcp.ingest_app import build_ingest_app
from webhooks_mcp.mcp_app import build_mcp_app
from webhooks_mcp.store import EventStore

READY_POLL_SECONDS = 0.02

log = logging.getLogger("webhooks_mcp")


class StoreOpenError(Exception):
    pass


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


class _Server(uvicorn.Server):
    # run() owns SIGTERM/SIGINT; two servers must not each install their own handlers.
    @contextlib.contextmanager
    def capture_signals(self):
        yield


def _server(app, host: str, port: int, limit: int) -> _Server:
    return _Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            lifespan="on",
            log_config=None,
            access_log=False,
            proxy_headers=False,
            server_header=False,
            timeout_keep_alive=KEEP_ALIVE_SECONDS,
            limit_concurrency=limit,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )


async def _serve(server: _Server, port: int) -> None:
    try:
        await server.serve()
    except SystemExit:
        raise RuntimeError(f"listener on port {port} failed to start") from None


async def _sweep_forever(store: EventStore) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await store.sweep()
        except Exception:
            log.exception("retention sweep failed")


async def _wait_started(servers: list[_Server], tasks: list[asyncio.Task]) -> bool:
    while not all(s.started for s in servers):
        if any(t.done() for t in tasks):
            return False
        await asyncio.sleep(READY_POLL_SECONDS)
    return True


async def run(
    settings: Settings,
    mcp_port: int = 9100,
    ingest_port: int = 9101,
    host: str = "0.0.0.0",
    ready: asyncio.Event | None = None,
) -> None:
    store = EventStore(settings.data_dir / "events.db")
    try:
        await store.open()
    except (OSError, sqlite3.Error) as exc:
        raise StoreOpenError(f"DATA_DIR {settings.data_dir} is not usable: {exc}") from None

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    installed: list[signal.Signals] = []
    servers: list[_Server] = []
    tasks: list[asyncio.Task] = []
    sweeper: asyncio.Task | None = None
    try:
        await store.sweep()
        servers = [
            _server(build_mcp_app(store, settings), host, mcp_port, MAX_CONCURRENT_REQUESTS),
            _server(build_ingest_app(store), host, ingest_port, INGEST_MAX_CONCURRENT_REQUESTS),
        ]
        tasks = [
            asyncio.create_task(_serve(servers[0], mcp_port)),
            asyncio.create_task(_serve(servers[1], ingest_port)),
        ]
        sweeper = asyncio.create_task(_sweep_forever(store))
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
                installed.append(sig)
        if await _wait_started(servers, tasks):
            log.info("listening: mcp=%s ingest=%s", mcp_port, ingest_port)
            if ready is not None:
                ready.set()
            stopper = asyncio.create_task(stop.wait())
            await asyncio.wait({stopper, *tasks}, return_when=asyncio.FIRST_COMPLETED)
            stopper.cancel()
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        for server in servers:
            server.should_exit = True
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if sweeper is not None:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
        await store.close()
    for result in results:
        if isinstance(result, Exception):
            raise result


def cli() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    _setup_logging()
    try:
        asyncio.run(run(settings))
    except StoreOpenError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
