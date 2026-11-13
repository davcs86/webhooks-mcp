# Release — from a merge here to a running edge

Merging to `main` deploys **nothing**. CI builds, smoke-tests and pushes `ghcr.io/davcs86/webhooks-mcp:<sha>`
(and moves the tag `main` to it) and prints `ghcr.io/davcs86/webhooks-mcp@sha256:…` in the `image` job's
summary (`.github/workflows/ci.yml`, job `image`). The edge keeps running the digest pinned in
`davcs86/homelab-edge` until that pin is changed.

A change is live only after a second PR, in `davcs86/homelab-edge`, that pins the new digest:

1. That repo's `bump-webhooks-mcp` workflow opens it: daily, or at once with **Actions → bump-webhooks-mcp → Run
   workflow** there. It keeps one branch (`bump/webhooks-mcp`) and one open PR, updated to the newest digest.
2. Only if it has not: replace the digest of `webhooks_mcp_image` in
   `inventories/prod/group_vars/all/versions.yml` and update its row in `docs/versions.md` (same PR), using
   the digest from the `image` job of the `main` run here.
3. After that PR merges, the owner runs **Actions → deploy** (`tailnet`) there. Stored events survive: the
   data volume is kept.

Every merge to `main` publishes a new digest, so the bump PR also appears after a docs-, test- or CI-only
merge; that one changes nothing on the edge and can wait for the next real change.

GHCR creates the package private on first push. The repo owner must set it to public once, because the edge
pulls it without a credential (contract C7, `docs/contracts.md`).

The base image and uv are pinned by digest/version in `Dockerfile` and `.github/workflows/ci.yml`; bump them
deliberately, in one PR, and run the image check (`docker build` + `scripts/smoke.sh`).
