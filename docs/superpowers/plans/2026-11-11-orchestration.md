# webhooks-mcp — Orchestration Plan

> **For agentic workers:** this is the plan the **orchestrator** runs. It does not write product code;
> it spawns workers, enforces gates, merges, and keeps the status table current. Workers get one task
> each and use superpowers:subagent-driven-development conventions (fresh implementer, fresh reviewer).
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the approved design across three repositories with parallel workers, without any worker
depending on something another has not yet frozen or merged.

**Architecture:** One orchestrator session owns three workstreams — W1 listener (`webhooks-mcp`), W2
edge deployment (`homelab-edge`), W3 publisher (`xstockstrat`). The contracts between them are frozen
in section 2, so the workstreams build in parallel and meet only at three owner-controlled gates.

**Spec:** `docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md` (this repo). Every worker reads it.

**Plans:** W1 tasks are in `2026-11-11-webhooks-mcp-listener.md`. W2 tasks are in section 6 here.
W3 tasks come out of xstockstrat's own SDD pipeline (section 7) — that repo forbids coding a new
capability from an outside plan.

## Global Constraints

- The orchestrator and its workers never merge a pull request and never approve one. The owner merges.
- No push to any branch other than the integration branch of each repo (section 3) without the owner's
  explicit permission.
- No secret in a repo, a log, a commit message, or a worker prompt.
- A worker never changes a frozen contract (section 2). It stops and reports instead.
- No test is skipped, disabled or weakened to get green.
- Each repo's own `CLAUDE.md` binds the workers in it (homelab-edge: Ansible only, pin everything,
  comments ≤ 2 lines; xstockstrat: SDD entry point, DRY guard rail, `uv lock` after `pyproject` edits).

---

## 1. Roles

| Role | What it is | What it may do |
|---|---|---|
| **Orchestrator** | The main session | Spawn workers, run gates, merge task branches into the integration branch locally, push the integration branch, open and update PRs, update the status table. Writes no product code |
| **Implementer** | Sub-agent, one per task, in its own git worktree | Edits only the files its task lists; commits on its task branch; never pushes |
| **Reviewer** | Fresh read-only sub-agent per task | Reads the task, the spec and the diff; returns pass or a list of defects |
| **W3 session** | A separate cloud session on `xstockstrat` | Runs the SDD pipeline end to end in its own context; pushes its integration branch; reports by PR state |

**Why W3 is a sub-session and W1/W2 are sub-agents.** W3 runs a five-stage pipeline with its own
artifacts and several services' worth of context — it would crowd out everything else in one context.
W1 and W2 are small, and their tasks share interfaces the orchestrator must see to merge them.

## 2. Frozen contracts

These are the only things one workstream needs from another. Changing any of them means: stop every
workstream, amend the spec, get the owner's approval, restart from the affected tasks.

| # | Contract | Value | Producer → consumers |
|---|---|---|---|
| C1 | MCP listener | container port `9100`, `POST /mcp`, `GET /healthz`; `Authorization: Bearer <key>`; 401 without it | W1 → W2 |
| C2 | Ingest listener | container port `9101`, `POST /hooks`, `POST /hooks/{name}`, `GET /healthz` | W1 → W2, W3 |
| C3 | Ingest responses | 201 `{event_id, duplicate:false}`, 200 `{…duplicate:true}`, 404, 413, 422 (spec 6.2) | W1 → W3 |
| C4 | Container env | `MCP_AUTH_KEY`, `INGEST_BASE_URLS` (JSON map), `DATA_DIR=/data` | W1 → W2 |
| C5 | Client address | Ingest reads the first address of `X-Forwarded-For` as `source_ip` | W2 → W1 |
| C6 | Body cap | 256 KiB at the listener; Caddy's cap on the ingest host equals it | W1 ↔ W2 |
| C7 | Image | `ghcr.io/davcs86/webhooks-mcp@sha256:<digest>`, public | W1 → W2 |
| C8 | Hostnames | MCP `webhooks-mcp.<domain>`; ingest `webhooks.ts.<domain>` on 443 and 8443 | W2 → W3, owner |
| C9 | Wire format | spec 8.3: JSON envelope plus `Idempotency-Key` header | W3 → W1 |

## 3. Branches and merging

### Integration branches

| Repo | Integration branch | PR target | Merge type (owner) |
|---|---|---|---|
| webhooks-mcp | `claude/webhook-listener-mcp-ltjc1r` | `main` | squash |
| homelab-edge | `claude/webhook-listener-mcp-ltjc1r` | `main` | repo default |
| xstockstrat | see open decision D1 (section 9) | `main-dev` | squash |

`webhooks-mcp` PR #1 carries the spec and these plans. Once the owner merges it, the branch is restarted
from `main` (`git fetch origin main && git checkout -B <branch> origin/main`) and W1's code goes into a
new PR — a merged PR is never reused.

### Task branches (W1, W2)

- Each task runs in a worktree on a **local** branch `task/<W>-<N>` cut from the integration branch
  head at spawn time. Task branches are never pushed.
- Tasks that run in parallel own **disjoint files**; the file ownership is in each task's `Files`
  block. Two tasks that touch the same file are sequenced, not parallelised.

### Merge procedure — per task, run by the orchestrator

- [ ] 1. Implementer reports done → orchestrator runs the task's verification command itself.
- [ ] 2. Spawn a reviewer on the diff. Defects go back to the **same** implementer; repeat from 1.
- [ ] 3. Rebase the task branch onto the current integration head.
- [ ] 4. If the rebase conflicts: do not resolve it in the orchestrator. Send the conflict to the
      task's implementer with both sides' task descriptions; repeat from 1.
- [ ] 5. Re-run the **whole** repo check suite on the rebased branch (not only the task's tests).
- [ ] 6. Fast-forward the integration branch to it. No merge commits, so the history is one commit per
      reviewed task.
- [ ] 7. Push the integration branch; tick the task in the status table; delete the worktree.

A task that fails its gate twice stops its workstream. The orchestrator reports to the owner with the
failing output; other workstreams continue.

### Cross-repo merge order

Enforced by gates G1–G3 in section 5. No repo's PR is opened as "ready" before its inbound gate is met,
except where section 5 says it is safe.

## 4. Dependency graph

```
W1  T1 store ─┬─▶ T2 ingest app ─┬─▶ T4 process + sweep ─▶ T5 image + CI ──▶ [G1 owner merges, digest]
              └─▶ T3 MCP app ────┘                                                 │
                                                                                   ▼
W2  E1 secret + compose ─▶ E2 Caddy + routing test ─▶ E3 Gatus, tls-check, DNS ─▶ E4 pin digest, docs
    (E1–E3 start with W1.T1; only E4 waits for G1)                                 │
                                                                    [G2 owner merges, deploys, verifies]
                                                                                   │
W3  S1 story+design ─▶ S2 spec ─▶ S3 proto ─┬─▶ S4 notify ─┬─▶ S6 agent + skill ─▶ S7 staging e2e ◀┘
    (starts immediately)                    └─▶ S5 ingest ─┘                       [G3 owner merges]
```

| Task | Hard dependency | Why |
|---|---|---|
| W1.T2, W1.T3 | W1.T1 merged | Both consume `EventStore` |
| W1.T4 | T2 and T3 merged | Starts both apps |
| W1.T5 | T4 merged | Builds the runnable image |
| W2.E1–E3 | C1–C6, C8 only | Contracts are frozen, so these do not wait for W1 code |
| W2.E2 | E1 merged | Same compose and Caddy files; sequenced |
| W2.E4 | **G1** | Needs the real digest (C7) |
| W3.S1–S6 | C9 only | xstockstrat never imports the listener |
| W3.S4, S5 | S3 merged | Both use the regenerated proto stubs |
| W3.S7 | **G2** | Needs the listener live on the tailnet |

Maximum useful parallelism: W1.T2 ‖ W1.T3; W2 alongside W1; W3 alongside both.

## 5. Gates

Each gate is an owner action. The orchestrator prepares it, states exactly what to do, and waits.

### G1 — listener released

- [ ] W1 PR is green and reviewed task by task; orchestrator requests the owner's merge.
- [ ] Owner merges into `main`; CI publishes the image.
- [ ] Owner sets the GHCR package visibility to public (once).
- [ ] Orchestrator reads the digest from the `main` CI run and records it in the status table.

### G2 — listener live on the edge

- [ ] Owner creates the DNS record for the new public name (`_acme-challenge` CNAME) — W2.E3 names it.
- [ ] Owner runs `init-secrets` so `webhooks_mcp_auth_key` exists.
- [ ] Owner merges the W2 PR and runs `deploy`, then `verify`.
- [ ] Orchestrator's checks, with output reported: Gatus probes green; `POST /mcp` without a key → 401
      from the internet; `POST /hooks` from a tailnet node → 201; the ingest host aborts from the
      internet on 443 **and** 8443.
- [ ] Owner adds the Claude.ai connector with the bearer header and calls `list_event_names`.

### G3 — publisher released

- [ ] W3 PR into `main-dev` is green. It is safe to merge **before** G2: the allowlist is empty by
      default, so `callback_url` is rejected everywhere until the owner sets it.
- [ ] After G2: owner sets the allowlist on homelab staging to the 8443 ingest origin (config rollout).
- [ ] W3.S7 end-to-end on staging passes (section 7).
- [ ] Promotion `main-dev` → `main` follows the repo's normal `/promote` flow; the owner then sets the
      production allowlist to the 443 ingest origin.

## 6. W2 — edge deployment tasks (`homelab-edge`)

Every task ends with the repo's own checks green: `yamllint -s .`, `ansible-lint`,
`ansible-playbook --syntax-check` on every playbook, plus the scripts each task names.
Worker reads first: `docs/runbooks/onboard-service.md`, `docs/decisions.md`, `docs/versions.md`.

### E1: Service, secret, volume

**Files:** `roles/stack/templates/compose.yml.j2`, `roles/stack/tasks/main.yml`,
`inventories/prod/group_vars/all/main.yml`, `scripts/init_secrets.py`, `scripts/secret_keys.py`,
`secrets/README.md`.

- [ ] Add hostnames to `main.yml`: `webhooks_mcp_hostname: "webhooks-mcp.{{ domain }}"`,
      `webhooks_ingest_hostname: "webhooks.{{ magicdns_base_domain }}"`, ports `9100` / `9101`.
- [ ] Add secret key `webhooks_mcp_auth_key` to the `fill_missing` generator (48 random URL-safe
      characters) and to the key registry. Check: `python3 scripts/init-secrets-test.py` and
      `python3 scripts/secrets-keys-test.py` pass.
- [ ] Add a task writing `webhooks-mcp/webhooks-mcp.env` (mode 0600, `no_log: true`) with
      `MCP_AUTH_KEY` and `INGEST_BASE_URLS` =
      `{"default":"https://<ingest host>","staging":"https://<ingest host>:<staging_edge_port>"}`.
- [ ] Add compose service `webhooks-mcp`: `x-hardening` anchor, `read_only: true`, `mem_limit: 192m`,
      volume `webhooks-data:/data`, ports `127.0.0.1:9100:9100` and `127.0.0.1:9101:9101`, `env_file`,
      image from a `versions.yml` variable holding a **placeholder** digest until E4.

### E2: Caddy routes and routing test (after E1)

**Files:** `roles/stack/templates/Caddyfile.j2`, `scripts/caddy-routing-test.sh`,
`inventories/prod/group_vars/all/main.yml` (rate-limit and body-cap values).

- [ ] **Write the failing assertions first** in `caddy-routing-test.sh`:
  - public `POST /mcp` on the MCP host reaches the MCP mock upstream;
  - public `GET /`, `/hooks`, `/healthz` on the MCP host → 404;
  - ingest host from a non-tailnet client → aborted, on 443 **and** on 8443;
  - ingest host from a tailnet client → ingest mock upstream, on 443 **and** on 8443;
  - ingest body over 256 KiB → 413;
  - MCP host is not served on 8443.
- [ ] Run `scripts/caddy-routing-test.sh edge-caddy:ci`; expect the new assertions to fail.
- [ ] Add the public site (own per-IP rate-limit zone, `flush_interval -1`), the ingest host in the
      `*.ts` site, and the ingest host in the `*.ts:8443` site inside `tailnet_only`.
- [ ] Re-run; expect all assertions to pass, old ones included.

### E3: Monitoring, TLS check, names (parallel with E2 — disjoint files)

**Files:** `roles/stack/templates/gatus-config.yaml.j2`, `roles/stack/tasks/main.yml` (`tls-check`
block only), `roles/tailscale/tasks/main.yml`, `README.md`.

`roles/stack/tasks/main.yml` is also edited by E1, so E3 starts only after E1 is merged.

- [ ] Gatus: public probe "`POST /mcp` → 401" with certificate expiry; tailnet probe ingest
      `/healthz` → 200. Check: `python3 scripts/gatus-config-test.py`.
- [ ] Add the new public name to `tls-check`; add split-horizon records for both names.
- [ ] Document the manual `_acme-challenge` CNAME in `README.md` § Rebuild from scratch.

### E4: Pin and document (after G1)

**Files:** `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md`, `docs/decisions.md`,
`docs/runbooks/onboard-service.md`.

- [ ] Replace the placeholder with the G1 digest; add the rationale line to `docs/versions.md`.
- [ ] One `docs/decisions.md` entry: first custom-code service on the edge; ingestion unauthenticated
      by decision and tailnet-only; the 8443 site is the place that rule can break.
- [ ] Re-read every touched doc against the code (repo teardown rule).

## 7. W3 — publisher (`xstockstrat`), run as a sub-session

The orchestrator creates one session on `davcs86/xstockstrat` with this brief, then checks it through
its PR and session status. It does not send it code.

**Brief (verbatim):**

> Implement the publisher side of `webhooks-mcp`. Input: section 8 of the design at
> `https://github.com/davcs86/webhooks-mcp/blob/main/docs/superpowers/specs/2026-11-11-webhooks-mcp-design.md`,
> which the owner has approved. Follow this repo's SDD entry point: `/sdd-story` →
> `/sdd-review product-spec` → `/sdd-design quick` → `/sdd-spec` → `/sdd-review impl-spec` →
> `/sdd-execute sequential`. The wire format in 8.3 and the RPC name `FanoutWebhook` are fixed; do not
> change them — stop and report if the design pipeline argues for a change. Two points are yours to
> settle in `/sdd-design`: how `trigger_backfill` rejects a disallowed URL up front, and whether
> `FanoutWebhook` needs an `x-internal-caller` allow-list. Do not run the staging end-to-end step
> until told the listener is live.

**Expected step order from `/sdd-spec`** (the orchestrator checks the generated spec against this; a
different order is fine if the dependencies hold):

| Step | Content | Depends on |
|---|---|---|
| S3 | Proto: `callback_url` on `TriggerBackfillRequest`; `FanoutWebhook` RPC on `NotifyService`; `./scripts/buf-gen.sh` | spec |
| S4 | Notify: delivery table migration, `FanoutWebhook` handler, delivery worker, allowlist config key migration, dead-letter alert | S3 |
| S5 | Ingest: job columns migration, hand-off on terminal state, retry sweep, sweep-interval config key | S3 |
| S6 | Agent: `trigger_backfill(callback_url=…)`; `strat-lab` `backtest` skill in the same PR; `docs/runbooks/mcp-tools.md` | S5 |
| S7 | Staging end-to-end | G2 |

**S7 — the acceptance check for the whole project:**

- [ ] Via the listener's MCP: `mint_webhook_url("backfill")`; take `urls.staging`.
- [ ] Via xstockstrat staging MCP: `trigger_backfill(…, callback_url=<that URL>)` → `job_id`.
- [ ] Via the listener's MCP: `wait_for_event(<minted name>)` until an event arrives.
- [ ] Assert: `payload.event_name == "ingest.backfill.completed"`, `payload.payload.data.job_id`
      equals the `job_id`, `payload.payload.environment == "staging"`.
- [ ] Stop notify on staging, trigger another backfill, let it finish, start notify: the event still
      arrives (ingest's sweep), exactly once (idempotency key).

## 8. Orchestrator run order

- [ ] 1. Owner merges `webhooks-mcp` PR #1 (spec + plans). Restart the branch from `main`.
- [ ] 2. Owner answers D1 (section 9). Spawn the W3 session with the section 7 brief.
- [ ] 3. Spawn W1.T1 and W2.E1 together.
- [ ] 4. On W1.T1 merged: spawn W1.T2 and W1.T3 together. On W2.E1 merged: spawn W2.E2 and W2.E3.
- [ ] 5. On W1.T2 and T3 merged: W1.T4, then W1.T5. Open the W1 PR. → **G1**.
- [ ] 6. Open the W2 PR after E3 (marked as waiting for the digest). After G1: W2.E4. → **G2**.
- [ ] 7. Tell the W3 session the listener is live; it runs S7. → **G3**.
- [ ] 8. Final report to the owner: what shipped, the evidence for each gate, anything left open.

After every merge and every gate the orchestrator updates the status table and pushes it, so a fresh
orchestrator context can resume from this file alone.

## 9. Open decisions

**D1 — xstockstrat branching.** The SDD pipeline wants `feature/<slug>` and reserves the feature
number by pushing to the `sdd/feature-cursor` ref. This project's sessions are limited to
`claude/webhook-listener-mcp-ltjc1r`. Options: **(a)** the owner permits `feature/<slug>` and the
cursor push for W3 — SDD-native, recommended; **(b)** W3 keeps all SDD artifacts and code on the
`claude/…` branch, with the feature number still needing the cursor push.

## 10. Status

| Item | State | Evidence |
|---|---|---|
| Spec approved | done | owner, 2026-11-11 |
| PR #1 (spec + plans) merged | pending | |
| D1 answered | pending | |
| W1.T1 store | not started | |
| W1.T2 ingest app | not started | |
| W1.T3 MCP app | not started | |
| W1.T4 process + sweep | not started | |
| W1.T5 image + CI | not started | |
| G1 digest | pending | |
| W2.E1 service + secret | not started | |
| W2.E2 Caddy | not started | |
| W2.E3 monitoring + names | not started | |
| W2.E4 pin + docs | blocked on G1 | |
| G2 live on edge | pending | |
| W3 session | not started | |
| W3.S7 staging e2e | blocked on G2 | |
| G3 publisher released | pending | |
