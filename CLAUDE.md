<!-- context-forge:behavioral-contract:start -->
## How to Act

Read this first — it governs *how* you work here; everything below is the *what* you work with.
These four behaviors are the operating defaults; the rest of this file (and the constitution) is
context you load per task.

1. **Don't assume — ask, and surface tradeoffs.** Before acting, always list every assumption you
   are making on your own — scope, interpretation, defaults, a choice between options — each with
   its basis, or say "none". On ambiguity, a missing detail, or a design fork, stop and raise it.
   *Red flag:* "it's obvious" / "too small to ask" — that *is* the assumption; list it.
   *(Enforced by the cross-repo contracts `docs/contracts.md` C1–C9 and **WM-08/WM-09**, where a silent guess
   breaks the edge or the publisher.)*
2. **Write the minimum that solves the stated problem.** Nothing speculative — no abstraction,
   option, or "while I'm here" scaffolding. *Red flag:* "might need it later", "for flexibility".
   *(Enforced by **WM-03**: the 192 MB budget punishes speculative buffering.)*
3. **Touch only what the task requires.** Surgical, auditable diffs; clean up only orphans *you*
   introduced. *Red flag:* "cleaner if I also…" — propose it, don't do it.
   *(Enforced by **WM-01/WM-02/WM-06**: store refactors break locking, schema and cap parity.)*
4. **Define success up front, then loop until verified.** State the pass condition before you
   start; claim done only with the check's output from this session in hand.
   *Red flag:* "should work", "looks right", "probably passes".
   *(Enforced by `uv run pytest -W error`, `uv lock --check` and `scripts/smoke.sh`; **WM-04** keeps the cap tests honest.)*

No exceptions for small or obvious tasks — honoring the letter while dodging the intent is a violation.

> Litmus test for any future line in this file: *does it shape how the agent thinks (a behavior), or
> restate a fact the agent can read from the code?* If it's a fact already in the repo, leave it out.
<!-- context-forge:behavioral-contract:end -->
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
| Codebase invariants (`WM-*`) an edit can silently break beyond the list above — locking, memory budget, cap/test coupling, edge seams — plus the defects/doc-drift log | `docs/context-constitution.md`, `docs/context-constitution-findings.md` |
| History of how the first version was built across the three repos | `docs/superpowers/plans/` (executed; historical) |

## Release

When you finish a change here, say in your report whether the image content
changed (`src/`, `Dockerfile`, `pyproject.toml`, `uv.lock`) and that the edge still needs the bump PR merged and
a deploy (`docs/release.md`); do not open a bump PR by hand.

## Teardown

If a change alters behaviour that `README.md`, `docs/contracts.md` or `docs/release.md` describes, update that
doc in the same PR.
