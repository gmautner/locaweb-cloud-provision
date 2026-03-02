# ADR-020: Reusable Workflows via workflow_call with Dual Checkout

**Status:** Accepted
**Date:** 2026-02-13
**Supersedes:** None

## Context

The deploy and teardown workflows were initially designed for internal use via `workflow_dispatch`. Goal #1 of the PRD requires them to be callable by external repositories, so any application repo can use locaweb-cloud-provision as a turnkey deployment platform without duplicating infrastructure logic.

GitHub Actions supports reusable workflows through the `workflow_call` trigger, which allows a workflow to be invoked by other workflows (in the same or different repositories) with typed inputs and secrets.

## Decision

We add `workflow_call` alongside the existing `workflow_dispatch` trigger in both `provision.yml` and `teardown.yml`, making them dual-trigger workflows that serve both internal and cross-repo use cases.

### Dual checkout pattern

When called from an external repository, the runner's default checkout (`actions/checkout@v4`) retrieves the **caller's** application code (Dockerfile, source code). A second checkout retrieves the **infrastructure scripts** from `gmautner/locaweb-cloud-provision` into a subdirectory called `_infra/`:

```yaml
- name: Checkout application repository
  uses: actions/checkout@v4

- name: Checkout infrastructure scripts
  uses: actions/checkout@v4
  with:
    repository: gmautner/locaweb-cloud-provision
    path: _infra
```

All script references in the workflow use `_infra/scripts/` paths. This works for both invocation modes:
- **Internal** (`workflow_dispatch`): the first checkout gets locaweb-cloud-provision itself (which contains the app and scripts), and the second checkout redundantly places the same scripts under `_infra/`. The `_infra/scripts/` paths resolve correctly.
- **External** (`workflow_call`): the first checkout gets the caller's app code, and the second checkout provides the infrastructure scripts. The `_infra/scripts/` paths resolve to the infra repo's scripts.

The teardown workflow only needs the infrastructure scripts (no application code), so it performs a single checkout of `gmautner/locaweb-cloud-provision` into `_infra/`.

### Input and secret contract

`workflow_call` inputs mirror the `workflow_dispatch` inputs with one adjustment:
- `type: choice` is not supported by `workflow_call`, so zone and plan inputs use `type: string` instead.

Both triggers support `type: boolean` and `type: number`, so these are shared as-is.

Secrets are declared explicitly in the `workflow_call` block. External callers must pass each secret individually — `secrets: inherit` only works within the same GitHub organization.

### ENV_VARS fallback

The `ENV_VARS` input uses a fallback expression to support both invocation modes:

```yaml
ENV_VARS: ${{ inputs.env_vars || vars.ENV_VARS }}
```

- External callers pass `env_vars` as a workflow input.
- Internal runs leave the input empty, falling back to the repository variable `vars.ENV_VARS`.

`SECRET_ENV_VARS` needs no fallback because the `secrets` context is unified — it works the same whether secrets come from the caller or from the repository.

### Workflow outputs

`provision.yml` exposes outputs for the caller to consume:
- `web_ip`, `worker_ips`, `db_ip`, `db_internal_ip`

These are mapped from the deploy job's step outputs.

## Consequences

### Positive

- Any GitHub repository can deploy to Locaweb Cloud by referencing the reusable workflow — no infrastructure code to copy or maintain.
- Internal `workflow_dispatch` usage is unchanged; existing E2E tests continue to work.
- The caller contract is minimal: provide a Dockerfile, configure secrets, and invoke the workflow.

### Negative

- The `uses:` path is verbose (`gmautner/locaweb-cloud-provision/.github/workflows/provision.yml@main`) — GitHub requires reusable workflows to live in `.github/workflows/`.
- The dual checkout adds a redundant clone in internal mode (the infra scripts are checked out twice). This is harmless but slightly wasteful (~2s).
- `workflow_call` does not support `choice` input types, so callers don't get dropdown menus for zone and plan inputs. Values are validated downstream by the scripts.
- Secrets must be passed explicitly by external callers (no `secrets: inherit` across organizations).

### Caller example

```yaml
# In the caller repository: .github/workflows/deploy.yml
name: Deploy
on:
  workflow_dispatch:
permissions:
  contents: read
  packages: write
jobs:
  deploy:
    uses: gmautner/locaweb-cloud-provision/.github/workflows/provision.yml@main
    with:
      zone: "ZP01"
      domain: "myapp.example.com"
      web_plan: "small"
      db_enabled: true
      db_plan: "medium"
      env_vars: |-
        APP_ENV=production
        LOG_LEVEL=info
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }}
      SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}
      POSTGRES_USER: ${{ secrets.POSTGRES_USER }}
      POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD }}
      SECRET_ENV_VARS: |-
        STRIPE_KEY=${{ secrets.STRIPE_KEY }}
```
