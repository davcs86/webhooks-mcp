# webhooks-mcp — Constitution Findings

Defects and drift surfaced by `/context-constitution` (context-forge) on 2026-11-14; last refreshed 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `1a7b63c` while deriving the constitution. These are the
things an agent trusting the docs or the surface would get **wrong**. They are for triage, not governance: a
defect is not a rule. This log is maintained, not write-only (**CF-N12**). `/context-constitution refresh`
re-verifies every open row and retires the rows that no longer reproduce to `## Resolved`.

## Documentation that lies (docs claim behavior the code lacks)

_None open._ The four rows re-verified on 2026-11-15 are under `## Resolved`.

## Latent bugs (looks broken, not merely non-obvious)

| Issue | Impact | Evidence |
|---|---|---|
| No schema migration mechanism: `_SCHEMA` is `CREATE … IF NOT EXISTS` only | The first column change silently never reaches the live `/data/events.db`. The owner wants a migration mechanism before any schema change (WM-02). Re-verified open on 2026-11-15; deliberately not built yet (owner decision) | `src/webhooks_mcp/store.py#_SCHEMA` |
| `_LABEL_RE` precompiles `MAX_LABEL_CHARS` at import (WM-04 violation; found 2026-11-15) | Monkeypatching `webhooks_mcp.mcp_app.MAX_LABEL_CHARS` has no effect, so a cap test would silently exercise the real 32. No test patches it today | `src/webhooks_mcp/mcp_app.py#_LABEL_RE` |

## Dead / orphaned code

_None._ All 19 constants in `constants.py` and all three documented env vars have a consumer.

## Open questions (unresolved *why* — needs a maintainer)

- `src/webhooks_mcp/store.py#wait` holds `asyncio.Condition` across `await self.list(...)`. Concurrent waiters
  serialize their DB reads, and `insert`'s `notify_all` waits for each one to park. Question: is this deliberate
  (it closes the missed-wakeup window), and must a refactor keep `list()` inside the condition? Status: **open**
  (owner did not know, 2026-11-14).
- `src/webhooks_mcp/store.py#sweep` runs `PRAGMA incremental_vacuum` under the lock, so reads wait behind the hourly
  sweep. Question: is a sweep stall on a 1 GiB DB acceptable against the 25 s `wait_for_event` budget? Status: **open**.
- `src/webhooks_mcp/main.py#_Server` overrides uvicorn's `capture_signals` with a no-op, and `mcp` is `>=2.3,<3`.
  Question: must a uvicorn or `mcp` upgrade be smoke-tested specifically for SIGTERM handling and the
  `session_manager` priming trick in `build_mcp_app`? Status: **open**.

## Dismissed (won't fix)

| Item | Decision and rationale | Date |
|---|---|---|
| `mint` does not retry on a `minted_urls` PK collision (`IntegrityError`) | Won't fix. A collision needs two live mints with the same label to draw the same 12-char suffix from a 32-symbol alphabet (32^12 = 2^60 space, sampled from `secrets`). A retry loop, a new constant and a test cost more than the risk is worth. Revisit if `MINTED_SUFFIX_CHARS` is ever shortened | 2026-11-15 |

## Resolved

| Was | Fix | Resolved | How confirmed |
|---|---|---|---|
| `CLAUDE.md` Rule 2 vs literal caps outside `constants.py` (`{0,127}`, `range(12)`, `limit=20` ×2, `READY_POLL_SECONDS`, `_MAX_SEQ`, default ports) | Added `MAX_EVENT_NAME_CHARS`, `MINTED_SUFFIX_CHARS`, `DEFAULT_PAGE_SIZE` and `READY_POLL_SECONDS` to `constants.py`. `is_valid_event_name` checks the length at call time instead of baking it into `_NAME_RE`. Rule 2 now names the exceptions: listener ports (contract) and `_MAX_SEQ` (SQLite ceiling) | 2026-11-15 | New `tests/test_store.py#test_name_and_suffix_caps_are_read_at_call_time` (WM-04) and a 128-char boundary assert. The `list_events` MCP input schema is byte-identical before and after (`limit` default 20). `uv run pytest -W error`: 156 passed, 2 skipped |
| `CLAUDE.md` invariant: `_total_bytes` "changes only inside `_run`" | Reworded to "`open` sets it at startup; after that only inside `_run`". Same fix in the `store.py#__init__` comment | 2026-11-15 | Re-read `store.py#open` (`self._total_bytes = _sum_bytes(db)` in `_open`) against the new wording |
| `README.md` example `minted.demo-3f9a1c` | Now `minted.demo-k7q2xw4mzr3p` (12 chars, `a-z2-7`) | 2026-11-15 | Matches `tests/test_store.py#test_mint_name_shape_and_activity` (`[a-z2-7]{12}`) |
| `README.md` "Tests: `uv run pytest`"; `CLAUDE.md#Commands` missing the lock check | Both docs now give `uv lock --check` plus `uv run pytest -W error` | 2026-11-15 | Matches `.github/workflows/ci.yml` (steps `uv lock --check`, `uv run pytest -W error`) |

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). Open items above are defects to
action, not rules to keep — nothing the scan found is discarded (CF-N8); resolved and dismissed items
stay on record rather than being deleted (CF-N12). Re-run `/context-constitution refresh` to catch
newly-resolved rows and pick up new defects._
