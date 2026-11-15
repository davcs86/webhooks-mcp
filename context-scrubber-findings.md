# Context Scrub — Findings

Low-signal context surfaced by `/context-scrubber` (mode `apply`) on 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `9f849b5`, the repo state this audit reflects. Each row is a
line in an **auto-loaded** context file (`CLAUDE.md`, with `AGENTS.md` a symlink to it, and
`docs/context-constitution*.md`) or an opted-in doc (`README.md`, `docs/contracts.md`) that an agent would find for
free, that no longer resolves, that is duplicated, or that the code now contradicts. Every row cites both the
context line and the evidence it fails.

**How the rows were produced.** One read-only `context-auditor` pass classified the five files. The orchestrator
re-opened the evidence for every row it carries: the constant count in `constants.py`, `store.py#close`, the
`Release` sentence and the `contracts.md` provenance paragraph. Sizes are measured with `wc`.

## Summary

| Category | Failing lines | Lines | Characters | Tokens (only if measured) |
|---|---|---|---|---|
| Stale citations | 0 | 0 | 0 | — |
| Restated (agent reads for free) | 2 | 5 | 410 | — |
| Cross-file duplication | 2 | 3 | 340 | — |
| Contradicted by code | 5 | — | — | — |
| Should be just-in-time | 0 (1 keep-but-verify) | — | — | — |
| Brittle / over-specified | 0 | — | — | — |
| Bloat / low-value prose | 1 | 2 | 230 | — |
| **Removable total** (excludes keep-but-verify + contradicted) | 5 | 10 | 980 | ≈ 245 (`chars ÷ 4`) |
| Keep-but-verify (unconfirmed) | 6 | — | — | — |

## Stale citations

_None._ Every cited anchor resolves.

## Restated facts (agent reads for free) — fails CF-N4

| Context line | What restates it (free to read) | Why it fails | Suggested action |
|---|---|---|---|
| `CLAUDE.md#Release`: "Merging to `main` deploys nothing; a change is live only after `davcs86/homelab-edge` pins the new digest and is deployed" | `docs/release.md` (top section), `README.md#Release` | Release facts are a linked doc; only the reporting directive is unique here | trim to the directive plus the `docs/release.md` pointer |
| `CLAUDE.md` intro: "one process, two listeners … ingest listener (9101) … MCP (Streamable HTTP, 9100, bearer key)" | `docs/contracts.md` C1/C2/C4, `README.md`, `Dockerfile` ("EXPOSE 9100 9101") | Facts already in the contract table | **kept** (owner call): a three-line orientation is cheap and points to README |

## Cross-file duplication — CF-N3

| Context line | Duplicate location(s) | Which copy to keep | Suggested action |
|---|---|---|---|
| `CLAUDE.md#Teardown`: "Keep this file to rules and pointers: a fact the code or a linked doc already states does not belong here." | the litmus quote inside the protected behavioral-contract block | the protected block | remove |
| `docs/context-constitution.md#Gotchas & scars`: "Byte budget charges a per-row overhead … Every path that touches the total charges `payload_bytes + ROW_OVERHEAD_BYTES`." | `CLAUDE.md#Invariants an edit can silently break` ("mirrors `SUM(payload_bytes) + ROW_OVERHEAD_BYTES × rows`") | `CLAUDE.md` | trim the gotcha to the scar (commit `696d86d`, ~1000× disk blow-up) |
| `CLAUDE.md#Rules` 3 and 4, `CLAUDE.md#Invariants` (seq, keep_seq) | `README.md#Security model`, `README.md#Retention`, `docs/contracts.md` closing paragraph | `CLAUDE.md` | **no action**: the README and contracts copies are the human and contract surfaces |

## Contradicted by code

Defects (CF-N9). They are logged in `docs/context-constitution-findings.md` and are never `apply`-deleted.

| Context line | What the code does | Evidence | Suggested action |
|---|---|---|---|
| `CLAUDE.md#Rules` 2: "No literal TTL, cap or limit elsewhere" | Literal caps outside `constants.py` | `src/webhooks_mcp/store.py#_NAME_RE`, `#mint`, `src/webhooks_mcp/mcp_app.py#_list_events`, `#_MAX_SEQ`, `src/webhooks_mcp/main.py#READY_POLL_SECONDS` | already in constitution findings |
| `CLAUDE.md#Invariants`: `_total_bytes` "changes only inside `EventStore._run`" | `open()` assigns it outside the lock; the same drift is in the `__init__` comment | `src/webhooks_mcp/store.py#open` | already in constitution findings |
| `README.md#Ingestion`: `minted.demo-3f9a1c` | 12-char `a-z2-7` suffix | `src/webhooks_mcp/store.py#mint` | already in constitution findings |
| `README.md#Run it`: "Tests: `uv run pytest`" | CI: `uv lock --check` plus `pytest -W error` | `.github/workflows/ci.yml` | already in constitution findings |
| `docs/context-constitution-findings.md#Dead / orphaned code`: "All 18 constants" | 19 constants, all consumed | `src/webhooks_mcp/constants.py` | **new**: fix the count at the step-3 refresh |

## Should be just-in-time (pre-loaded → pointer)

_None confirmed._ See keep-but-verify for `CLAUDE.md#Invariants`.

## Brittle / over-specified (anti-altitude)

_None._

## Bloat / low-value prose

| Context line(s) | Why it is filler | Suggested action |
|---|---|---|
| `docs/contracts.md` closing paragraph: "Extracted verbatim from `docs/superpowers/plans/2026-11-11-orchestration.md` §2 (workstreams W1/W2/W3 renamed to their repos) …" | Provenance that drives no agent action; "verbatim" contradicts "renamed" | trim the provenance clause, keep the change-procedure sentence (owner gate) |

## Context budget (file-level)

Soft budget ~2,000 characters per auto-loaded file.

| File | Measured lines | Measured characters | Over soft budget? |
|---|---|---|---|
| `docs/context-constitution.md` | 83 | 10,761 | yes (it is read on demand via the Context Guide row; not auto-loaded) |
| `README.md` | 117 | 6,081 | yes (opt-in human doc) |
| `CLAUDE.md` | 90 | 5,737 | yes (~2,000 of that is the protected contract) |
| `docs/context-constitution-findings.md` | 53 | 4,177 | yes (on demand) |
| `docs/contracts.md` | 25 | 2,232 | marginal |

## Silent skills (weak trigger surface)

_None._ Scanned: 0 · excluded as symlinked-out: 0.

## Keep-but-verify (unconfirmed — CF-1)

- `CLAUDE.md#Commands`: mirrors `ci.yml` almost verbatim. Kept because an agent shouldn't have to open CI to run
  tests. If kept, add `uv lock --check` (already a findings row). Status: **unverified**.
- `CLAUDE.md#Invariants an edit can silently break`: store-only detail loaded on every task. It could become `WM-*`
  rows, but most tasks in a six-file service touch `store.py`. Status: **unverified (owner preference)**.
- `docs/context-constitution.md` WM-01: `store.py#open` (before `_db` is published) and `#close` (takes
  `_write_lock` directly) touch the DB outside `_run`. These look like lifecycle exceptions. Name them at the step-3
  refresh. Status: **unverified**.
- `docs/context-constitution.md` WM-04: "(×8 overall)" counts tests, not patches (there are 10 monkeypatches across
  8 tests). Clarify at the step-3 refresh. Status: **unverified**.
- `README.md#Ingestion` 422 row is incomplete: non-object body, invalid unicode and an over-long Idempotency-Key also
  return 422 (`src/webhooks_mcp/ingest_app.py#typed`, `#_idempotency_key`). Status: **unverified (owner decides
  completeness)**.

## Protected blocks (reported, never trimmed)

| Block | Location | Marker |
|---|---|---|
| behavioral contract | `CLAUDE.md` (by marker) | `context-forge:behavioral-contract` |
| constitution pointer | `CLAUDE.md#Context Guide` row (host index style, no sentinel; same as xstockstrat root) | — |

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). These are low-signal lines to trim, not
rules to keep — nothing grounded is dropped (**CF-N8**). Re-run `/context-scrubber` to re-audit._
