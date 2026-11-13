# webhooks-mcp — CLAUDE.md

A small Python 3.13 service: one process, two listeners. Webhooks POSTed to the ingest listener (9101)
are stored in SQLite under `DATA_DIR`; AI agents read them over MCP (Streamable HTTP, 9100, bearer key).
Human overview and the full HTTP/MCP surface: `README.md`.

## Commands

- Setup (once; tests need the dev extra): `uv sync --extra dev --frozen`
- Tests: `uv run pytest -W error` (the CI invocation)
- Lint: `uv run ruff check .` and `uv run ruff format --check .`
- Image check (needs Docker): `docker build -t webhooks-mcp:dev .` then `scripts/smoke.sh webhooks-mcp:dev`

## Rules

1. **Ports, paths, env var names, response codes and tool names are a contract** with
   `davcs86/homelab-edge` (deployment) and `davcs86/xstockstrat` (publisher). Never change one here
   alone; the values and the change procedure are in `docs/contracts.md`.
2. **Constants live only in `src/webhooks_mcp/constants.py`.** No literal TTL, cap or limit elsewhere.
3. **Ingestion is unauthenticated by design.** Expose port 9101 only to a private network (the tailnet),
   never to the internet. Only the MCP port may sit behind a public proxy.
4. **Payloads are untrusted third-party data.** Return them only inside a `payload` field and end every
   tool description with `NOTICE` (`src/webhooks_mcp/mcp_app.py#NOTICE`).
5. The bearer key is never logged, echoed in an error, or stored in a fixture.
6. Comments state constraints only, at most 2 lines. How and why belong in the PR or the spec.

## Invariants an edit can silently break

- `events.seq` is `INTEGER PRIMARY KEY AUTOINCREMENT` (`store.py#_SCHEMA`): never reused, so `after_seq`
  cursors stay valid across eviction. Keep `AUTOINCREMENT`.
- `EventStore._total_bytes` mirrors `SUM(payload_bytes) + ROW_OVERHEAD_BYTES × rows`. It changes only inside
  `EventStore._run` (the write lock) and only after commit; any new path that inserts or deletes events must
  keep it in step (`store.py#_insert`, `store.py#sweep`).
- An insert never evicts its own row (`store.py#_evict_oldest` takes `keep_seq`).
- Tests inject the clock (`tests/conftest.py#FakeClock`); never read wall time in `store.py` except through
  `self._clock`.

## Context Guide

Read only the row that matches the task.

| Task | Read |
|---|---|
| Any change to a port, path, env var, response code, tool name or wire format | `docs/contracts.md` |
| Why a behaviour is the way it is (ingestion, tools, storage, retention) | `docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` §6 |
| Releasing, bumping the base image or uv, getting a change onto the edge | `docs/release.md` |
| History of how the first version was built across the three repos | `docs/superpowers/plans/` (executed; historical) |

## Release

Merging to `main` deploys nothing; a change is live only after `davcs86/homelab-edge` pins the new digest and
is deployed (`docs/release.md`). When you finish a change here, say in your report whether the image content
changed (`src/`, `Dockerfile`, `pyproject.toml`, `uv.lock`) and that the edge still needs the bump PR merged and
a deploy; do not open a bump PR by hand.

## Teardown

If a change alters behaviour that `README.md`, `docs/contracts.md` or `docs/release.md` describes, update that
doc in the same PR. Keep this file to rules and pointers: a fact the code or a linked doc already states does
not belong here.
