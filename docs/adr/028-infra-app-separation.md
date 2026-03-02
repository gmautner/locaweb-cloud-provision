# ADR-028: Separate Infrastructure Provisioning from Application Deployment

**Status:** Accepted
**Date:** 2026-03-02
**Supersedes:** None

## Context

The `deploy.yml` workflow bundled two concerns in a single job: CloudStack infrastructure provisioning **and** Kamal application deployment. This coupling had several drawbacks:

1. **Secret leakage into infra layer.** External callers had to pass application secrets (via `SECRET_ENV_VARS`) through the infrastructure workflow, even though the infra layer had no need for them.
2. **Unnecessary permissions.** The workflow required `packages: write` for ghcr.io image pushes, even though the infrastructure provisioning steps only needed `contents: read`.
3. **Limited caller control.** Callers could not customize the Kamal deployment (e.g., different build args, pre-deploy hooks, custom env var mapping) because deployment was embedded inside the reusable workflow.
4. **Dual checkout overhead.** The workflow checked out both the caller's application repository and the infrastructure scripts into the same runner, mixing application and infrastructure concerns.

## Decision

Split `deploy.yml` into an **infra-only** workflow that provisions CloudStack resources and outputs everything the caller needs, and let the caller handle application deployment (Kamal) in its own workflow job.

### Changes to deploy.yml

- **Removed:** Application checkout, Kamal gem installation/caching, SSH key preparation for Kamal, Docker cache exposure, `Deploy with Kamal` step, `Reboot accessories if plan changed` step, `Print Kamal deployment summary` step.
- **Removed:** `secret_env_vars` workflow_dispatch input and `SECRET_ENV_VARS` workflow_call secret.
- **Reduced permissions:** `contents: read, packages: write` to `contents: read`.
- **Renamed concurrency group:** `deploy-*` to `infra-*`.
- **Added outputs:**
  - `infrastructure_changed` — `"true"` on fresh provision (cache miss), `"false"` on cache hit. Caller uses this to decide between `kamal setup` and `kamal deploy`.
  - `scaled_accessories` — JSON array of accessory names whose VMs were rescaled. Caller iterates this to reboot affected accessories.
  - `infra_env` — Multiline `KEY=VALUE` string ready to load into `GITHUB_ENV`. Contains `INFRA_WEB_IP`, `INFRA_<NAME>_IP` per accessory, and `INFRA_WORKER_IP_<N>` per worker.

### New deploy-app.yml

A two-job `workflow_dispatch` workflow for internal development and E2E testing:

- **Job 1 (`infra`):** Calls `deploy.yml` with all infrastructure inputs and secrets.
- **Job 2 (`deploy`):** Loads `infra_env` into the environment, installs Kamal, deploys the application, reboots scaled accessories, and prints the deployment summary.

The `secret_env_vars` input is retained on `deploy-app.yml` for E2E compatibility. Real external callers map their secrets directly as environment variables — they never need `SECRET_ENV_VARS`.

### Concurrency alignment

`teardown.yml` concurrency group renamed from `deploy-*` to `infra-*` to match `deploy.yml`, keeping deploy and teardown mutually exclusive.

## Consequences

### Positive

- **Callers get full deployment control.** External repositories can customize their Kamal deployment (build args, pre-deploy hooks, environment variable mapping) without constraints from the reusable workflow.
- **Infra permissions minimized.** The infrastructure workflow no longer needs `packages: write` since it doesn't push Docker images.
- **SECRET_ENV_VARS eliminated from infra layer.** Application secrets no longer pass through the infrastructure workflow. Callers map their secrets directly in their own deploy job.
- **Cleaner separation of concerns.** Infrastructure provisioning and application deployment are independent jobs that can evolve separately.

### Negative

- **Callers need more boilerplate.** Instead of a single `uses:` call, external callers now need a two-job workflow (infra + deploy). The skill documentation and workflow templates are updated to reflect this.
- **E2E test indirection.** The E2E test now triggers `deploy-app.yml` instead of `deploy.yml`, adding one level of indirection. The test logic itself is unchanged.
