from __future__ import annotations

import ipaddress
import json
import math
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from webhooks_mcp.constants import MAX_BODY_BYTES, MAX_IDEMPOTENCY_KEY_CHARS, MINTED_PREFIX
from webhooks_mcp.store import EventStore, is_valid_event_name


class _HttpError(Exception):
    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-standard JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise _HttpError(413, "body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _HttpError(413, "body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_json(raw: bytes) -> Any:
    try:
        return json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
    except (ValueError, RecursionError):
        raise _HttpError(422, "body is not valid JSON") from None


def _compact(value: Any) -> str:
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise _HttpError(422, "body contains invalid unicode") from None
    return text


def _idempotency_key(request: Request) -> str | None:
    key = request.headers.get("idempotency-key") or None
    if key is not None and len(key) > MAX_IDEMPOTENCY_KEY_CHARS:
        raise _HttpError(422, "Idempotency-Key is too long")
    return key


def _source_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    # Zone ids ("%...") are accepted by ip_address but are unbounded client text.
    if "%" not in forwarded:
        try:
            return str(ipaddress.ip_address(forwarded))
        except ValueError:
            pass
    return request.client.host if request.client else None


def build_ingest_app(store: EventStore) -> Starlette:
    async def _store_event(request: Request, event_name: str, payload_json: str) -> Response:
        event, duplicate = await store.insert(
            event_name, payload_json, _source_ip(request), _idempotency_key(request)
        )
        return JSONResponse(
            {"event_id": event.event_id, "duplicate": duplicate},
            status_code=200 if duplicate else 201,
        )

    async def typed(request: Request) -> Response:
        try:
            body = _parse_json(await _read_body(request))
            if not isinstance(body, dict):
                raise _HttpError(422, "body must be a JSON object")
            name = body.get("event_name")
            if not isinstance(name, str) or not is_valid_event_name(name):
                raise _HttpError(422, "event_name is missing or invalid")
            if name.startswith(MINTED_PREFIX):
                raise _HttpError(422, f"event_name may not use the {MINTED_PREFIX} prefix")
            if "payload" not in body:
                raise _HttpError(422, "payload is required")
            payload_json = _compact(body["payload"])
            # The parsed tree is many times the body size; drop it before waiting for the writer.
            del body
            return await _store_event(request, name, payload_json)
        except _HttpError as err:
            return JSONResponse({"error": err.reason}, status_code=err.status)

    async def minted(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            if not is_valid_event_name(name) or not await store.is_minted_active(name):
                raise _HttpError(404, "unknown or expired webhook name")
            payload_json = _compact(_parse_json(await _read_body(request)))
            return await _store_event(request, name, payload_json)
        except _HttpError as err:
            return JSONResponse({"error": err.reason}, status_code=err.status)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    return Starlette(
        routes=[
            Route("/hooks", typed, methods=["POST"]),
            Route("/hooks/{name}", minted, methods=["POST"]),
            Route("/healthz", healthz, methods=["GET"]),
        ]
    )
