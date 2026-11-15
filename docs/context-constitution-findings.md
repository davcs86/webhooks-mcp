# webhooks-mcp — Constitution Findings

Defects and drift surfaced by `/context-constitution` (context-forge) on 2026-11-14 from branch
`claude/context-constitution-parity-zggeq3` at commit `80429bf` while deriving the constitution. These are the
things an agent trusting the docs or the surface would get **wrong**. They are for triage, not governance: a
defect is not a rule. This log is maintained, not write-only (**CF-N12**). `/context-constitution refresh`
re-verifies every open row and retires the rows that no longer reproduce to `## Resolved`.

## Documentation that lies (docs claim behavior the code lacks)

| What the docs say | What the code does | Evidence | Suggested action |
|---|---|---|---|
| `CLAUDE.md` Rule 2: "No literal TTL, cap or limit elsewhere" | Literal caps remain outside `constants.py`: the event-name length `{0,127}`, the 12-char mint suffix, the default page size `limit=20` (written twice), `_MAX_SEQ`, `READY_POLL_SECONDS`, and the default ports | `src/webhooks_mcp/store.py#_NAME_RE`, `#mint` ("range(12)"), `src/webhooks_mcp/mcp_app.py#list_events`, `#_list_events`, `#_MAX_SEQ`, `src/webhooks_mcp/main.py#READY_POLL_SECONDS`, `#run` | Move the caps (name length, suffix length, page size) to `constants.py`; scope Rule 2 to exclude contract values (ports) and the SQLite ceiling |
| `CLAUDE.md` invariant: `_total_bytes` "changes only inside `EventStore._run`" | `open` also assigns it, via `to_thread` outside the lock, at startup | `src/webhooks_mcp/store.py#open` ("self._total_bytes = _sum_bytes(db)") | Reword: "outside `open`, changes only inside `_run`" |
| `README.md` example `POST /hooks/minted.demo-3f9a1c` (6-char hex suffix) | Minted names end in 12 chars from `a-z2-7` | `README.md#Ingestion`, `src/webhooks_mcp/store.py#mint`, `#_BASE32` | Use a realistic example name |
| `README.md`: "Tests: `uv run pytest`"; `CLAUDE.md#Commands` omits the lock check | CI runs `uv lock --check` and `uv run pytest -W error` | `README.md` ("Tests: `uv run pytest`"), `.github/workflows/ci.yml` ("uv lock --check") | Align both docs with the CI invocation |

## Latent bugs (looks broken, not merely non-obvious)

| Issue | Impact | Evidence |
|---|---|---|
| No schema migration mechanism: `_SCHEMA` is `CREATE … IF NOT EXISTS` only | The first column change silently never reaches the live `/data/events.db`. The owner wants a migration mechanism before any schema change (WM-02) | `src/webhooks_mcp/store.py#_SCHEMA` |
| `mint` does not retry on a name collision | An `IntegrityError` on the `minted_urls` PK would surface as an unmapped tool error. The probability is negligible (60-bit suffix) | `src/webhooks_mcp/store.py#mint` |

## Dead / orphaned code

_None._ All 18 constants in `constants.py` and all three documented env vars have a consumer.

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

_None._

## Resolved

_None._

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). Open items above are defects to
action, not rules to keep — nothing the scan found is discarded (CF-N8); resolved and dismissed items
stay on record rather than being deleted (CF-N12). Re-run `/context-constitution refresh` to catch
newly-resolved rows and pick up new defects._
