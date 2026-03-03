# Caller Workflow Reference

## Table of Contents

- [Two-Job Pattern](#two-job-pattern)
- [Preview Workflow (Default)](#preview-workflow-default)
- [Additional Environments](#additional-environments)
- [Deploy Input Reference](#deploy-input-reference)
- [Deploy Output Reference](#deploy-output-reference)
- [Complete Example (All Inputs)](#complete-example-all-inputs)
- [Workflow Permissions](#workflow-permissions)

## Two-Job Pattern

External callers use a two-job workflow to deploy applications:

1. **`infra` job**: Calls `provision.yml` to provision CloudStack infrastructure. Outputs IPs, cache status, and environment bindings.
2. **`deploy` job** (needs: infra): Checks out the application, loads infra outputs into the environment, installs Kamal, and deploys.

This separation keeps application secrets (database passwords, API keys) out of the infrastructure workflow. The infra workflow only needs `contents: read`; the caller's deploy job handles `packages: write` for ghcr.io.

## Preview Workflow (Default)

The default preview environment is triggered on push, immediately reflecting changes to the main branch — matching a typical developer workflow. No domain needed, uses nip.io for immediate access. Since `"preview"` is the default `env_name`, secrets use unsuffixed names.

```yaml
# .github/workflows/deploy-preview.yml
name: Deploy Preview
on:
  push:
    branches: [main]
    paths-ignore: [".claude/**"]

permissions:
  contents: read
  packages: write

jobs:
  infra:
    uses: gmautner/locaweb-cloud-provision/.github/workflows/provision.yml@v1
    with:
      env_name: "preview"
      zone: "ZP01"
      accessories: '[{"name": "db", "plan": "medium", "disk_size_gb": 20}]'
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }}
      SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}

  deploy:
    needs: infra
    runs-on: ubuntu-latest
    env:
      KAMAL_REGISTRY_PASSWORD: ${{ secrets.GITHUB_TOKEN }}
      POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD }}
    steps:
      - uses: actions/checkout@v4

      - name: Load infrastructure environment
        run: echo "${{ needs.infra.outputs.infra_env }}" >> "$GITHUB_ENV"

      - name: Set repo identity
        run: |
          echo "REPO_NAME=${{ github.event.repository.name }}" >> "$GITHUB_ENV"
          echo "REPO_FULL=${{ github.repository }}" >> "$GITHUB_ENV"
          echo "REPO_OWNER=${{ github.repository_owner }}" >> "$GITHUB_ENV"

      - name: Configure gem path
        run: |
          echo "GEM_HOME=$HOME/.gems" >> "$GITHUB_ENV"
          echo "$HOME/.gems/bin" >> "$GITHUB_PATH"

      - name: Cache Kamal gem
        id: kamal-cache
        uses: actions/cache@v4
        with:
          path: ~/.gems
          key: kamal-${{ runner.os }}-v1

      - name: Install Kamal
        if: steps.kamal-cache.outputs.cache-hit != 'true'
        run: gem install kamal --no-document

      - name: Prepare SSH key for Kamal
        env:
          SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}
        run: |
          mkdir -p .kamal
          install -m 600 /dev/null .kamal/ssh_key
          printf '%s\n' "$SSH_PRIVATE_KEY" > .kamal/ssh_key

      - name: Expose GitHub Actions runtime for Docker cache
        uses: actions/github-script@v7
        with:
          script: |
            const vars = ['ACTIONS_CACHE_URL', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'ACTIONS_CACHE_SERVICE_V2'];
            for (const v of vars) { const val = process.env[v]; if (val) core.exportVariable(v, val); }

      - name: Deploy with Kamal
        run: |
          if [ "${{ needs.infra.outputs.infrastructure_changed }}" = "true" ]; then
            kamal setup -d preview
          else
            kamal deploy -d preview
          fi

      - name: Reboot scaled accessories
        run: |
          python3 << 'PYEOF'
          import json, subprocess
          scaled = json.loads('${{ needs.infra.outputs.scaled_accessories }}')
          for name in scaled:
              print(f"Accessory '{name}' VM was rescaled, rebooting...")
              subprocess.run(["kamal", "accessory", "reboot", name, "-d", "preview"], check=True)
          PYEOF
```

After this runs successfully, the app is accessible at `https://<web_ip>.nip.io`. The `web_ip` is visible in the workflow run summary.

## Additional Environments

Other environments can be created depending on your processes, changing the triggers and workflow inputs as needed. Each `env_name` creates fully isolated infrastructure.

### Secret naming convention

Since `"preview"` is the default environment, its secrets use **unsuffixed** names:

- `SSH_PRIVATE_KEY`, `POSTGRES_PASSWORD`
- Custom secrets: `API_KEY`, `SMTP_PASSWORD`

For additional environments, suffix secret names that are **scoped to that environment** with the environment name (uppercased):

- `SSH_PRIVATE_KEY_PRODUCTION`, `POSTGRES_PASSWORD_PRODUCTION`
- Custom secrets: `API_KEY_PRODUCTION`, `SMTP_PASSWORD_PRODUCTION`

Secrets **common to all environments** (e.g., `CLOUDSTACK_API_KEY`, `CLOUDSTACK_SECRET_KEY`) don't need suffixes — just pass them in every caller workflow.

The caller workflow maps the suffixed secrets to the reusable workflow's standard secret names (see example below).

### Production workflow example

A recommended additional environment is **"production"**, triggered on version tags (`v*`). A tag signals that the pointed commit is ready for production. Uses a custom domain with automatic HTTPS.

```yaml
# .github/workflows/deploy-production.yml
name: Deploy Production
on:
  push:
    tags: ["v*"]

permissions:
  contents: read
  packages: write

jobs:
  infra:
    uses: gmautner/locaweb-cloud-provision/.github/workflows/provision.yml@v1
    with:
      env_name: "production"
      zone: "ZP01"
      web_plan: "medium"
      accessories: '[{"name": "db", "plan": "medium", "disk_size_gb": 50}]'
      web_disk_size_gb: 50
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }}
      SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY_PRODUCTION }}

  deploy:
    needs: infra
    runs-on: ubuntu-latest
    env:
      KAMAL_REGISTRY_PASSWORD: ${{ secrets.GITHUB_TOKEN }}
      POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD_PRODUCTION }}
      STRIPE_KEY: ${{ secrets.STRIPE_KEY }}
    steps:
      - uses: actions/checkout@v4

      - name: Load infrastructure environment
        run: echo "${{ needs.infra.outputs.infra_env }}" >> "$GITHUB_ENV"

      - name: Set repo identity
        run: |
          echo "REPO_NAME=${{ github.event.repository.name }}" >> "$GITHUB_ENV"
          echo "REPO_FULL=${{ github.repository }}" >> "$GITHUB_ENV"
          echo "REPO_OWNER=${{ github.repository_owner }}" >> "$GITHUB_ENV"

      - name: Configure gem path
        run: |
          echo "GEM_HOME=$HOME/.gems" >> "$GITHUB_ENV"
          echo "$HOME/.gems/bin" >> "$GITHUB_PATH"

      - name: Cache Kamal gem
        id: kamal-cache
        uses: actions/cache@v4
        with:
          path: ~/.gems
          key: kamal-${{ runner.os }}-v1

      - name: Install Kamal
        if: steps.kamal-cache.outputs.cache-hit != 'true'
        run: gem install kamal --no-document

      - name: Prepare SSH key for Kamal
        env:
          SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY_PRODUCTION }}
        run: |
          mkdir -p .kamal
          install -m 600 /dev/null .kamal/ssh_key
          printf '%s\n' "$SSH_PRIVATE_KEY" > .kamal/ssh_key

      - name: Expose GitHub Actions runtime for Docker cache
        uses: actions/github-script@v7
        with:
          script: |
            const vars = ['ACTIONS_CACHE_URL', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'ACTIONS_CACHE_SERVICE_V2'];
            for (const v of vars) { const val = process.env[v]; if (val) core.exportVariable(v, val); }

      - name: Deploy with Kamal
        run: |
          if [ "${{ needs.infra.outputs.infrastructure_changed }}" = "true" ]; then
            kamal setup -d production
          else
            kamal deploy -d production
          fi

      - name: Reboot scaled accessories
        run: |
          python3 << 'PYEOF'
          import json, subprocess
          scaled = json.loads('${{ needs.infra.outputs.scaled_accessories }}')
          for name in scaled:
              print(f"Accessory '{name}' VM was rescaled, rebooting...")
              subprocess.run(["kamal", "accessory", "reboot", name, "-d", "production"], check=True)
          PYEOF
```

To deploy to production: `git tag v1.0.0 && git push --tags`. The workflow checks out the tagged commit, so the Dockerfile and source code match the tag exactly.

## Deploy Input Reference

All inputs to `provision.yml`, their types, defaults, and when to use them:

| Input | Type | Default | When to set |
|-------|------|---------|-------------|
| `env_name` | string | `"preview"` | Name of the environment. Each env_name creates fully isolated infrastructure. Defaults to `"preview"` if omitted. |
| `zone` | string | `"ZP01"` | CloudStack zone. Usually leave as default. Use `ZP02` for geographic redundancy. |
| `web_plan` | string | `"small"` | Choose based on runtime footprint and environment. See [scaling.md](scaling.md) for plan specs. |
| `web_disk_size_gb` | number | `20` | Increase if the app stores files (uploads, media). Consider environment: preview can use smaller, production may need more. Can only grow, never shrink. |
| `workers_replicas` | number | `0` | Number of worker VMs. Set to 0 for no workers. |
| `workers_plan` | string | `"small"` | VM size for workers. Choose based on worker workload intensity. See [scaling.md](scaling.md). |
| `accessories` | string | `"[]"` | JSON array of accessory VMs: `[{"name": "db", "plan": "medium", "disk_size_gb": 20}]`. Each object supports an optional `ports` field (comma-separated string, e.g. `"5432"` or `"80,443"`) to open additional firewall ports; port 22 (SSH) is always included. |
| `automatic_reboot` | boolean | `true` | Enable automatic reboot after unattended security upgrades. Usually leave as default. |
| `automatic_reboot_time_utc` | string | `"05:00"` | When automatic reboots happen. Usually leave as default. |
| `recover` | boolean | `false` | Reserved for future disaster recovery workflows. Do not use. |

### Inputs to leave at defaults

For most deployments, omit these (let defaults apply):
- `automatic_reboot` / `automatic_reboot_time_utc` -- security auto-updates are good defaults
- `recover` -- reserved for future use
- `web_disk_size_gb` -- 20 GB is sufficient for most apps unless heavy file storage

## Deploy Output Reference

Outputs from `provision.yml` that the caller's deploy job consumes:

| Output | Type | Description |
|--------|------|-------------|
| `web_ip` | string | Public IP of the web VM |
| `worker_ips` | JSON array | Public IPs of worker VMs (e.g., `["1.2.3.4","5.6.7.8"]`) |
| `accessory_ips` | JSON object | Public IPs of accessory VMs (e.g., `{"db":"1.2.3.4"}`) |
| `infrastructure_changed` | `"true"/"false"` | `"true"` on fresh provision (cache miss), `"false"` on cache hit. Use to decide `kamal setup` vs `kamal deploy`. |
| `scaled_accessories` | JSON array | Names of accessories whose VMs were rescaled (e.g., `["db"]`). Iterate to reboot via `kamal accessory reboot`. |
| `infra_env` | multiline string | `KEY=VALUE` pairs ready to load into `GITHUB_ENV`. Contains `INFRA_WEB_IP`, `INFRA_<NAME>_IP` per accessory, `INFRA_WORKER_IP_<N>` per worker. |

### Loading infra outputs

In the deploy job, load the infra environment into `GITHUB_ENV`:

```yaml
- run: echo "${{ needs.infra.outputs.infra_env }}" >> "$GITHUB_ENV"
```

This makes `INFRA_WEB_IP`, `INFRA_DB_IP`, etc. available as environment variables for Kamal ERB config files.

## Complete Example (All Inputs)

Full-stack example with web, database, and workers. Every input is shown with required/optional and default value annotations.

```yaml
# .github/workflows/deploy-preview.yml
name: Deploy Preview
on:
  push:
    branches: [main]
    paths-ignore: [".claude/**"]

permissions:
  contents: read
  packages: write

jobs:
  infra:
    uses: gmautner/locaweb-cloud-provision/.github/workflows/provision.yml@v1
    with:
      env_name: "preview"                    # Optional, default: "preview"
      zone: "ZP01"                           # Optional, default: "ZP01" (options: ZP01, ZP02)
      web_plan: "small"                      # Optional, default: "small"
      web_disk_size_gb: 20                   # Optional, default: 20 (grow only, never shrink)
      workers_replicas: 2                    # Optional, default: 0 (0 = no workers)
      workers_plan: "small"                  # Optional, default: "small"
      accessories: |-                        # Optional, default: "[]"
        [{"name": "db", "plan": "medium", "disk_size_gb": 20}]
      automatic_reboot: true                 # Optional, default: true
      automatic_reboot_time_utc: "05:00"     # Optional, default: "05:00"
      recover: false                         # Optional, default: false (reserved for future DR)
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}       # Required
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }} # Required
      SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}             # Required

  deploy:
    needs: infra
    runs-on: ubuntu-latest
    env:
      KAMAL_REGISTRY_PASSWORD: ${{ secrets.GITHUB_TOKEN }}
      POSTGRES_PASSWORD: ${{ secrets.POSTGRES_PASSWORD }}
      API_KEY: ${{ secrets.API_KEY }}
      SMTP_PASSWORD: ${{ secrets.SMTP_PASSWORD }}
    steps:
      - uses: actions/checkout@v4

      - name: Load infrastructure environment
        run: echo "${{ needs.infra.outputs.infra_env }}" >> "$GITHUB_ENV"

      - name: Set repo identity
        run: |
          echo "REPO_NAME=${{ github.event.repository.name }}" >> "$GITHUB_ENV"
          echo "REPO_FULL=${{ github.repository }}" >> "$GITHUB_ENV"
          echo "REPO_OWNER=${{ github.repository_owner }}" >> "$GITHUB_ENV"

      - name: Configure gem path
        run: |
          echo "GEM_HOME=$HOME/.gems" >> "$GITHUB_ENV"
          echo "$HOME/.gems/bin" >> "$GITHUB_PATH"

      - name: Cache Kamal gem
        id: kamal-cache
        uses: actions/cache@v4
        with:
          path: ~/.gems
          key: kamal-${{ runner.os }}-v1

      - name: Install Kamal
        if: steps.kamal-cache.outputs.cache-hit != 'true'
        run: gem install kamal --no-document

      - name: Prepare SSH key for Kamal
        env:
          SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}
        run: |
          mkdir -p .kamal
          install -m 600 /dev/null .kamal/ssh_key
          printf '%s\n' "$SSH_PRIVATE_KEY" > .kamal/ssh_key

      - name: Expose GitHub Actions runtime for Docker cache
        uses: actions/github-script@v7
        with:
          script: |
            const vars = ['ACTIONS_CACHE_URL', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_RUNTIME_URL', 'ACTIONS_RESULTS_URL', 'ACTIONS_CACHE_SERVICE_V2'];
            for (const v of vars) { const val = process.env[v]; if (val) core.exportVariable(v, val); }

      - name: Deploy with Kamal
        run: |
          if [ "${{ needs.infra.outputs.infrastructure_changed }}" = "true" ]; then
            kamal setup -d preview
          else
            kamal deploy -d preview
          fi

      - name: Reboot scaled accessories
        run: |
          python3 << 'PYEOF'
          import json, subprocess
          scaled = json.loads('${{ needs.infra.outputs.scaled_accessories }}')
          for name in scaled:
              print(f"Accessory '{name}' VM was rescaled, rebooting...")
              subprocess.run(["kamal", "accessory", "reboot", name, "-d", "preview"], check=True)
          PYEOF
```

## Workflow Permissions

The caller workflow **must** include:

```yaml
permissions:
  contents: read
  packages: write
```

`packages: write` is required because the caller's deploy job pushes the container image to ghcr.io via Kamal. The infra workflow (`provision.yml`) only needs `contents: read`. The teardown workflow does not need `packages: write`.
