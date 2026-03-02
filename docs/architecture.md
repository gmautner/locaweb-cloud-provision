# Architecture Design Document -- locaweb-cloud-deploy

**Status:** Living document
**Last updated:** 2026-02-19

---

## Table of Contents

1. [Overview](#overview)
2. [System Architecture](#system-architecture)
3. [Component Details](#component-details)
4. [Network Architecture](#network-architecture)
5. [Data Flow](#data-flow)
6. [Security Model](#security-model)
7. [Reliability and Resilience](#reliability-and-resilience)
8. [Technology Choices](#technology-choices)
9. [Constraints and Limitations](#constraints-and-limitations)

---

## Overview

`locaweb-cloud-deploy` automates end-to-end deployment of containerized web applications onto Locaweb Cloud, a CloudStack-based IaaS platform. The system uses GitHub Actions as its orchestration layer and Kamal 2 as its container deployment tool.

The deploy and teardown workflows support dual triggers: `workflow_dispatch` for direct use, and `workflow_call` for invocation by external repositories. This makes the system a reusable deployment platform — any application repository can reference the workflows without duplicating infrastructure logic. A dual checkout pattern retrieves the caller's application code alongside the infrastructure scripts from this repository.

### Goals

- **One-click deploys**: A single manual workflow dispatch provisions infrastructure and deploys the application with no intermediate steps.
- **Configurable topology**: Users choose VM sizes, enable or disable worker VMs (with configurable replica count), and optionally add a dedicated PostgreSQL database VM -- all through workflow input parameters.
- **Idempotent provisioning**: The provisioning script can be safely re-run; it detects existing resources by name and skips creation, enabling scale-up, scale-down, and configuration changes without destroying existing infrastructure.
- **Clean teardown**: A separate workflow destroys all resources in deterministic reverse order.
- **Testability**: An end-to-end test suite validates every deployment scenario (complete deploy, web-only, scale up, scale down, teardown) using isolated resources.

### Non-goals

- Multi-region or multi-cloud deployment.
- Horizontal auto-scaling (scaling is manual via `workers_replicas` parameter).
- Horizontal web scaling (the web tier scales vertically; Kamal Proxy with Let's Encrypt operates on a single web VM).

### TODOs

- **IP filtering for SSH.** Restrict SSH firewall rules to GitHub Actions runner IP ranges.
- ~~**PostgreSQL extensions.**~~ Resolved: `supabase/postgres` bundles 60+ extensions (pgvector, pg_cron, pgmq, pg_jsonschema, etc.) out of the box (ADR-025).

---

## System Architecture

The system is composed of five logical layers:

```
+---------------------------------------------------------------+
|                      GitHub Actions                           |
|  (Orchestration: deploy.yml, teardown.yml, test-infrastructure.yml,          |
|   e2e-test.yml)                                               |
+---------------------------------------------------------------+
        |                    |                    |
        v                    v                    v
+----------------+  +------------------+  +------------------+
| CloudMonkey    |  | Kamal 2          |  | Test Suite       |
| (cmk CLI)      |  | (Ruby gem)       |  | (Python)         |
| Provisions     |  | Deploys          |  | Validates        |
| CloudStack     |  | containers       |  | infrastructure   |
| resources      |  | via SSH          |  | end-to-end       |
+----------------+  +------------------+  +------------------+
        |                    |
        v                    v
+---------------------------------------------------------------+
|                Locaweb Cloud (CloudStack)                      |
|  +----------+  +----------+  +----------+                     |
|  | Web VM   |  | Worker   |  | DB VM    |                     |
|  | kamal-   |  | VMs (Nx) |  | postgres |                     |
|  | proxy +  |  | app      |  | :17      |                     |
|  | app      |  | workers  |  |          |                     |
|  +----------+  +----------+  +----------+                     |
|                                                               |
|  Isolated Network  |  Public IPs  |  Data Disks  |  Snapshots |
+---------------------------------------------------------------+
```

### Resource naming convention

All CloudStack resources are named using the pattern `{repo-name}-{unique-id}-{env-name}`, where `unique-id` is `github.repository_id` for production deployments and `github.run_id` for test runs, and `env-name` is the environment name (e.g., `preview`, `staging`, `production`). This ensures complete isolation between different repositories, test runs, and environments.

Examples (with env_name `preview`):
- Network: `my-app-123456789-preview`
- SSH keypair: `my-app-123456789-preview-key`
- Web VM: `my-app-123456789-preview-web`
- Worker VMs: `my-app-123456789-preview-worker-1`, `my-app-123456789-preview-worker-2`, ...
- Database VM: `my-app-123456789-preview-db`
- Blob disk: `my-app-123456789-preview-blob`
- Database disk: `my-app-123456789-preview-dbdata`

---

## Component Details

### 1. GitHub Actions Workflows

Four workflow files orchestrate the system. The deploy and teardown workflows support dual triggers: `workflow_dispatch` (manual/internal) and `workflow_call` (reusable/cross-repo). The test workflows use `workflow_dispatch` only.

#### deploy.yml

A single-job reusable workflow that provisions infrastructure and deploys the application sequentially. The job runs on `ubuntu-latest` and requires `contents: read` and `packages: write` permissions (the latter for ghcr.io image pushes).

**Triggers:** `workflow_dispatch` for direct use and `workflow_call` for invocation by external repositories. Both triggers accept the same inputs, though `workflow_call` uses `type: string` where `workflow_dispatch` uses `type: choice` (unsupported by `workflow_call`). Boolean and number types are shared as-is.

**Dual checkout:** The workflow performs two checkouts: (1) `actions/checkout@v4` retrieves the caller's application code (Dockerfile, source), and (2) a second checkout retrieves infrastructure scripts from `gmautner/locaweb-cloud-deploy` into `_infra/`. All script paths use `_infra/scripts/` prefixes. In internal mode, the first checkout gets this repository itself and the second is redundant but harmless.

**Concurrency:** Shares a per-environment concurrency group (`deploy-${{ github.repository }}-${{ inputs.env_name }}`) with `teardown.yml` to prevent overlapping infrastructure operations on the same environment. Deploying to "staging" does not block "production". `cancel-in-progress` is false so queued runs wait rather than cancel.

**Workflow outputs** (exposed to callers): `web_ip`, `worker_ips`, `db_ip`, `db_internal_ip`.

**Workflow inputs** (all configurable at dispatch time):

| Input | Type | Default | Description |
|---|---|---|---|
| `env_name` | string | `preview` | Environment name for resource isolation (e.g., preview, staging, production) |
| `zone` | choice | `ZP01` | CloudStack availability zone |
| `domain` | string | `""` | Custom domain (optional). TLS is always enabled via Let's Encrypt. |
| `web_plan` | choice | `small` | VM size for the web server |
| `blob_disk_size_gb` | string | `20` | Data disk size for blob storage |
| `workers_enabled` | boolean | `false` | Whether to create worker VMs |
| `workers_replicas` | string | `1` | Number of worker VMs |
| `workers_cmd` | string | `sleep infinity` | Container command for workers |
| `workers_plan` | choice | `small` | VM size for workers |
| `db_enabled` | boolean | `false` | Whether to create a database VM |
| `db_plan` | choice | `medium` | VM size for the database |
| `db_disk_size_gb` | string | `20` | Data disk size for PostgreSQL |
| `recover` | boolean | `false` | Recover data disks from snapshots (disaster recovery) |

**Step sequence:**

1. **Validate secrets** -- If `db_enabled` is true, verifies that the `POSTGRES_PASSWORD` secret exists. Fails fast if missing.
2. **Checkout application repository** -- `actions/checkout@v4` retrieves the caller's code (or this repo in internal mode).
2b. **Checkout infrastructure scripts** -- `actions/checkout@v4` with `repository: gmautner/locaweb-cloud-deploy` and `path: _infra`.
3. **Compute infrastructure cache key** -- Hashes `toJSON(inputs)` via `sha256sum` to produce a deterministic cache key that captures every workflow input.
4. **Cache infrastructure state** -- Uses `actions/cache@v4` to cache `/tmp/provision-output.json` keyed by `infra-{repository}-{env_name}-{hash}`. Bypassed when `recover: true`. On cache hit, steps 5-8 and 11 are skipped (see ADR-026).
5. **Build configuration** *(skipped on cache hit)* -- Inline Python assembles workflow inputs into a JSON config file at `/tmp/config.json`.
6. **Extract SSH public key** -- Derives the public key from the `SSH_PRIVATE_KEY` secret using `ssh-keygen -y`.
7. **Install CloudMonkey** *(skipped on cache hit)* -- Downloads the `cmk` binary, configures it with the CloudStack API endpoint (`painel-cloud.locaweb.com.br`), API key, and secret key. Runs `cmk sync` to populate the local API cache.
8. **Provision infrastructure** *(skipped on cache hit)* -- Runs `scripts/provision_infrastructure.py` with the config JSON and public key. Outputs JSON to `/tmp/provision-output.json`.
9. **Set outputs** -- Parses the provision output JSON (freshly created or restored from cache) and writes `web_ip`, `worker_ips`, `db_ip`, and `db_internal_ip` to `GITHUB_OUTPUT` for downstream consumption.
10. **Upload artifact** -- Saves the provision output JSON as a GitHub artifact with 90-day retention.
10b. **Print summary** -- Generates a Markdown table of provisioned resources in the GitHub Actions step summary.
11. **Configure unattended upgrades** *(skipped on cache hit)* -- SSHes into all VMs and writes apt unattended-upgrades configuration.
12. **Configure gem path + Cache Kamal gem** -- Sets `GEM_HOME=~/.gems` and adds `~/.gems/bin` to `PATH` for all subsequent steps. Uses `actions/cache@v4` to cache the gem directory with key `kamal-{runner.os}-v1`.
13. **Install Kamal** *(skipped on gem cache hit)* -- `gem install kamal` (Kamal 2 from RubyGems).
13b. **Verify Kamal** -- Runs `kamal version` to confirm the binary works.
14. **Prepare SSH key** -- Copies the private key to `.kamal/ssh_key` with mode 600.
15. **Create secrets file and env vars** -- Writes `.kamal/secrets` with `$VAR` references for `KAMAL_REGISTRY_PASSWORD`, and conditionally `POSTGRES_PASSWORD` and `DATABASE_URL` (if `db_enabled`). Parses the `SECRET_ENV_VARS` secret and `ENV_VARS` variable (both in dotenv format) using `python-dotenv`: secrets are added as `$VAR` references in `.kamal/secrets` and their resolved values are written to a sourceable env file for the deploy step; variables are written to a JSON file for the config generation step to merge as clear env vars.
16. **Generate deploy config** -- Inline Python dynamically generates `config/deploy.yml` (the Kamal configuration) from the provision output, incorporating conditional sections for workers and database accessories. When the `domain` input is set, the proxy host is set to the domain; otherwise, nip.io wildcard DNS is used. SSL is always enabled via Let's Encrypt for both cases. Merges any custom variables from `ENV_VARS` as clear env vars and custom secrets from `SECRET_ENV_VARS` as secret env vars.
17. **Deploy with Kamal** -- On first deploy (infrastructure cache miss), runs `kamal setup`, which installs Docker on all hosts, boots accessories (PostgreSQL), and deploys the application. On consecutive deploys (cache hit), runs `kamal deploy`, which skips Docker installation and accessory bootstrapping, only building and deploying the new application image. Both modes handle registry authentication, image build and push, and deployment behind kamal-proxy.
18. **Reboot DB accessory if tuning changed** -- (Only when `db_enabled`) Compares the desired PostgreSQL `cmd` from the generated config against the running container's command via `docker inspect`. If the parameters differ (i.e., `db_plan` changed since the last deploy), runs `kamal accessory reboot db` to recreate the container with updated tuning. Skipped on first deploy (container was just created with correct parameters) and when the plan hasn't changed. This is necessary because `kamal setup` skips existing accessory containers, and Docker's `--restart unless-stopped` policy preserves the original command even across VM reboots.
19. **Print deployment summary** -- Outputs commit SHA, image tag, application URL, and health check URL to the step summary.

#### teardown.yml

A reusable workflow that destroys all CloudStack resources in reverse creation order. Supports both `workflow_dispatch` and `workflow_call` triggers. Uses the same CloudMonkey installation pattern as the deploy workflow. Shares the per-environment `deploy-${{ github.repository }}-${{ inputs.env_name }}` concurrency group with `deploy.yml`.

**Dual checkout:** Unlike deploy.yml, the teardown workflow only needs the infrastructure scripts (no application code), so it performs a single checkout of `gmautner/locaweb-cloud-deploy` into `_infra/`.

**Workflow inputs:**

| Input | Type | Required | Description |
|---|---|---|---|
| `env_name` | string | no (default: `preview`) | Environment name to tear down |
| `zone` | choice (`ZP01`/`ZP02`) | yes | CloudStack zone to tear down |

The `zone` input is passed as `--zone` to the teardown script, so only resources in the specified zone are destroyed. This is critical for cross-zone DR scenarios where the same network name exists in multiple zones.

**Destruction sequence (8 steps):**

1. Delete snapshot policies for all tagged data volumes (zone-scoped).
2. Detach and delete data volumes (blob, dbdata).
3. Disable static NAT on all non-source-NAT public IPs.
4. Delete firewall rules on all public IPs.
5. Release (disassociate) public IPs.
6. Destroy all VMs (with expunge to prevent lingering in the trash).
7. Delete the isolated network (with a 5-second wait for VM expunge).
8. Delete the SSH key pair.

The teardown script treats all `cmk` failures as non-fatal warnings because resources may already be partially deleted.

#### test-infrastructure.yml

An infrastructure-focused integration test workflow that validates CloudStack provisioning across multiple scenarios. Uses `github.run_id` as the unique identifier to isolate test resources from production. This workflow tests resource creation, idempotency, and teardown but does **not** run Kamal or deploy the application.

**Test phases:**

| Phase | Scenario | Description |
|---|---|---|
| 0 | Initial teardown | Ensures a clean slate before testing |
| 1a | Complete deploy | Web + 3 workers + DB; verifies all resources, SSH, mount points |
| 1b | Scale down 3 to 1 | Re-provisions with 1 worker; verifies excess workers removed |
| 1c | Teardown verify | Destroys and verifies all resources absent |
| 2a | Web-only deploy | No workers, no DB; verifies minimal footprint |
| 2b | Teardown verify | Destroys and verifies clean removal |
| 3a | Deploy with features | 1 worker + DB; baseline for scale-up test |
| 3b | Scale up 1 to 3 | Re-provisions with 3 workers; verifies new VMs created |
| 3c | Teardown verify | Final cleanup and verification |

The test workflow generates a fresh ed25519 SSH key pair per run to avoid CloudStack's unique-public-key-per-account constraint. An **emergency teardown** step runs unconditionally (`if: always()`) to ensure test resources are cleaned up even if the test suite crashes. Results are saved to `/tmp/test-results.json` and rendered as a Markdown table in the step summary.

#### e2e-test.yml

An application-focused E2E test workflow that triggers the **real** `deploy.yml` workflow, waits for it to complete, then verifies the deployed application works correctly. Unlike `test-infrastructure.yml` which validates CloudStack resources, this workflow validates the full deployment pipeline including Kamal, container deployment, and application behavior.

**Concurrency:** Uses its own concurrency group (`e2e-test-${{ github.repository }}`), separate from the `deploy-${{ github.repository }}` group shared by `deploy.yml` and `teardown.yml`. This prevents deadlocks: the E2E workflow triggers deploy/teardown via `gh workflow run`, and if they shared a group, the triggered workflow would queue behind the E2E run that's waiting for it.

**Permissions:** `contents: read`, `actions: write` (the latter is needed to trigger workflows via the `gh` CLI).

**Workflow inputs:**

| Input | Type | Default | Description |
|---|---|---|---|
| `zone` | choice | `ZP01` | CloudStack availability zone |
| `scenario` | choice | `all` | Test scenario: `complete`, `web-only`, `scale-up`, `scale-down`, or `all` |

**Step sequence:**

1. **Checkout repository**
2. **Install and configure CloudMonkey** -- for emergency teardown (direct script call).
3. **Prepare SSH key** -- from `SSH_PRIVATE_KEY` secret.
4. **Initial teardown** -- direct script call to clean up any leftover resources.
5. **Run E2E tests** -- executes `scripts/e2e_test.py`, the Python orchestrator.
6. **Emergency teardown** (`if: always()`) -- direct script call for cleanup on failure.
7. **Test summary** -- reads results JSON and renders a Markdown table in `$GITHUB_STEP_SUMMARY`.

**Test scenarios:**

| Scenario | Deploy inputs | Verifications |
|---|---|---|
| `complete` | zone, env_name=preview, workers=1, db=true | /up→200, page content, POST note, file upload, blob mount + writable, DB mount, implicit disk sizes (20GB), worker container env vars |
| `web-only` | zone, env_name=preview (defaults) | /up→200 (no DB), "Database not configured" message, env vars visible, file upload, blob mount, no workers/DB in output |
| `scale-up` | env_name=e2etest, workers 1→3, db=true, explicit disk sizes (30GB blob, 25GB db) | Initial worker verified, then 3 workers all have app container + env vars, explicit disk sizes verified, app healthy after scale |
| `scale-down` | zone, env_name=preview, workers 3→1, db=true | 3 workers initially, then 1 remains with app + env vars, app healthy after scale |
| `all` | Runs all four in sequence | Each scenario triggers its own teardown at the end |

**Workflow triggering pattern:** The E2E script records the latest run ID before triggering `gh workflow run`, then polls until a new run with a higher ID appears. It then uses `gh run watch --exit-status` to wait for completion and `gh run download` to retrieve the provision-output artifact.

### 2. CloudStack Provisioning Script

**File:** `scripts/provision_infrastructure.py`

A Python script that uses the CloudMonkey CLI (`cmk`) to interact with the CloudStack API. It accepts a JSON configuration, an SSH public key, and naming parameters, then creates all required infrastructure.

**Key behaviors:**

- **Idempotency**: Every resource creation is preceded by a lookup. If a resource with the expected name already exists, it is reused. This makes the script safe to re-run and enables incremental changes.
- **Retry with exponential backoff**: All `cmk` calls retry up to 5 times with exponential backoff (2, 4, 8, 16, 32 seconds) to handle transient CloudStack API errors. Final failures raise `RuntimeError`.
- **Worker scale-down**: After deploying the desired number of workers, the script probes for excess workers (worker-N+1, worker-N+2, ...) and destroys them along with their associated public IPs, firewall rules, and static NAT mappings.
- **Static NAT conflict avoidance**: When assigning public IPs, the script first checks for existing static NAT mappings per VM. It reuses existing assignments and only acquires new IPs for VMs that lack one. This prevents CloudStack's "VM already has a static NAT IP" error during scale-up scenarios.
- **Userdata injection**: All VMs receive base64-encoded cloud-init scripts. Web and DB scripts format and mount their data disks. All scripts (web, worker, DB) install and configure fail2ban for SSH brute-force protection.
- **Volume tagging**: Data disks are tagged with `locaweb-cloud-deploy-id={network-name}` to enable the teardown script to find them reliably.
- **Disaster recovery**: When `--recover` is passed, the script creates data volumes from the latest available snapshots (MANUAL or RECURRING, in BackedUp state) in the target zone instead of blank disks. Pre-flight checks verify no conflicting deployment exists and required snapshots are available. Snapshot policies are still created on recovered volumes for ongoing protection.
- **Template discovery**: Automatically selects the most recent Ubuntu 24.x template matching the regex `^Ubuntu.*24.*$`.

**Resource creation order:**

1. Resolve zone, offerings, and template IDs.
1b. (Recovery mode only) Run pre-flight checks: verify no existing deployment in target zone, locate snapshots.
2. Create isolated network.
3. Register SSH key pair.
4. Deploy VMs (web always, workers if enabled, DB if enabled) with userdata.
5. Remove excess workers (scale-down).
6. Assign public IPs with static NAT (1:1 per VM).
7. Create firewall rules (SSH+HTTP+HTTPS for web, SSH only for workers and DB).
8. Create and attach data disks (blob for web, dbdata for DB). In recovery mode, disks are created from snapshots instead of blank.
9. Create daily snapshot policies with cross-zone replication.
10. Retrieve internal (private) IPs for inter-VM communication.

### 3. Teardown Script

**File:** `scripts/teardown_infrastructure.py`

Destroys all resources for a given network name in reverse creation order. Unlike the provision script, all `cmk` failures are non-fatal (logged as warnings) because partial deletion states are expected during teardown.

**Zone-aware operation:**

- **`--zone <name>`**: When provided, resolves the zone name to an ID and passes `zoneid` to the network and volume lookups, so only resources in that zone are found and destroyed. This is the mode used by the `teardown.yml` workflow and the `test_infrastructure.py` test runner.
- **No `--zone`**: When omitted, the script finds **all** networks matching the name across all zones and runs the full 8-step destruction for each one. This is the "tear down everything" mode used by the E2E test workflow for initial and emergency cleanup.

### 4. Test Infrastructure Script

**File:** `scripts/test_infrastructure.py`

A custom test framework with `TestScenario` (context manager for assertion tracking) and `TestRunner` (orchestrates phases). Includes:

- **SSHVerifier**: Polls SSH connectivity with 10-second intervals and 180-second timeout. Verifies remote mount points by running `mountpoint -q` via SSH.
- **InfrastructureVerifier**: Queries CloudStack via `cmk` to verify existence/absence of networks, VMs, volumes, snapshot policies, public IPs, firewall rules, static NAT mappings, and SSH key pairs.

### 4b. E2E Test Script

**File:** `scripts/e2e_test.py`

An application-level test orchestrator that triggers real workflow runs and verifies the deployed application. Uses the same `TestScenario` assertion pattern as `test_infrastructure.py` for consistent results output. Includes:

- **SSHVerifier**: SSH connectivity, mount point verification, disk write tests, Docker container discovery, and container environment variable checks via `docker exec`.
- **HTTPVerifier**: HTTP GET/POST with `Host` header for kamal-proxy routing, health check polling, form submission, and multipart file upload.
- **Workflow helpers**: Triggers `deploy.yml` and `teardown.yml` via `gh workflow run`, polls for new run detection, watches completion with `gh run watch`, and downloads artifacts.
- **Disk size verification**: Uses `blockdev --getsize64` via SSH to verify raw block device sizes match the provisioned disk sizes (both implicit defaults and explicit inputs).

### 5. Kamal 2 Deployment Configuration

The Kamal configuration (`config/deploy.yml`) is generated dynamically by Python code embedded in the deploy workflow. It is never committed to the repository.

**Configuration structure:**

```yaml
service: <repo-name>
image: <owner/repo>
registry:
  server: ghcr.io
  username: <repo-owner>
  password:
    - KAMAL_REGISTRY_PASSWORD          # GITHUB_TOKEN
ssh:
  user: root
  keys:
    - .kamal/ssh_key
servers:
  web:
    hosts:
      - <web-public-ip>
  workers:                              # Only if workers_enabled
    hosts:
      - <worker-1-ip>
      - <worker-N-ip>
    cmd: <workers_cmd>
    proxy: false                        # Workers do not use kamal-proxy
proxy:
  host: <domain> or <web-ip>.nip.io    # Custom domain or wildcard DNS via nip.io
  app_port: 80
  forward_headers: false                # No upstream load balancers (static NAT direct to internet)
  ssl: true                             # Always enabled; Let's Encrypt for both nip.io and custom domains
  healthcheck:
    path: /up
    interval: 3
    timeout: 5
env:
  clear:
    BLOB_STORAGE_PATH: /data/blobs
    POSTGRES_HOST: <db-internal-ip>      # Only if db_enabled
    POSTGRES_DB: postgres
    POSTGRES_USER: postgres
  secret:                               # Only if db_enabled
    - POSTGRES_PASSWORD
    - DATABASE_URL
volumes:
  - /data/blobs:/data/blobs
accessories:                            # Only if db_enabled
  db:
    image: supabase/postgres:17.6.1.091
    host: <db-public-ip>
    port: "5432:5432"
    cmd: postgres -D /etc/postgresql -c shared_buffers=1GB -c effective_cache_size=3GB -c work_mem=10MB -c maintenance_work_mem=256MB -c max_connections=100  # Tuned for db_plan (e.g. medium/4GB)
    env:
      secret:
        - POSTGRES_PASSWORD
    directories:
      - /data/db/pgdata:/var/lib/postgresql/data
builder:
  arch: amd64
readiness_delay: 15
deploy_timeout: 180
drain_timeout: 30
```

**Secrets file** (`.kamal/secrets`):

```
KAMAL_REGISTRY_PASSWORD=$KAMAL_REGISTRY_PASSWORD
POSTGRES_PASSWORD=$POSTGRES_PASSWORD                 # only if db_enabled
DATABASE_URL=$DATABASE_URL                           # only if db_enabled
# Custom secrets from SECRET_ENV_VARS appear here:
# REDIS_URL=$REDIS_URL
```

All entries use `$VAR` references that Kamal resolves from the process environment at deploy time. No cleartext secrets are written to disk.

**Custom environment variables via SECRET_ENV_VARS and ENV_VARS:**

Users can pass additional environment variables to the application container through two consolidated entries in dotenv format:

- **`SECRET_ENV_VARS`** (GitHub Secret) → each key=value pair is added as a `$VAR` reference in `.kamal/secrets` and listed in `env.secret`. The container receives the secret value.
- **`ENV_VARS`** (GitHub Variable) → each key=value pair is added to `env.clear` in the Kamal config. The container receives the variable value.

Both use standard dotenv format (parsed by `python-dotenv`), supporting quoting, comments, and `=` in values.

Example `SECRET_ENV_VARS`:
```
REDIS_URL=redis://localhost:6379
STRIPE_KEY=sk_live_xxx
```
The container receives `REDIS_URL` and `STRIPE_KEY` as secret env vars.

Example `ENV_VARS`:
```
LOG_LEVEL=debug
APP_ENV=production
```
The container receives `LOG_LEVEL` and `APP_ENV` as clear env vars.

**Database environment variable contract** (when `db_enabled` is true):

The platform provides the following environment variables to the application container:

| Variable | Source | Description |
|----------|--------|-------------|
| `POSTGRES_HOST` | Clear (db internal IP) | PostgreSQL server address |
| `POSTGRES_DB` | Clear (hardcoded `postgres`) | Database name |
| `POSTGRES_USER` | Clear (hardcoded `postgres`) | PostgreSQL username |
| `POSTGRES_PASSWORD` | Secret | PostgreSQL password |
| `DATABASE_URL` | Secret | Full connection string (`postgres://postgres:pass@host:5432/postgres`) |

`DATABASE_URL` is composed in `.kamal/secrets` via shell variable interpolation from the individual variables, and passed to the container as a secret since it contains the password.

### 6. Sample Application

**File:** `app.py`

A Flask web application that exercises all platform features:

- **Graceful DB handling**: A `DB_CONFIGURED` flag checks whether `POSTGRES_HOST` is set. When the database is not configured, the app operates in a degraded mode: the health check returns 200, the index page shows "Database not configured", and the notes form is hidden. When the database is configured but unreachable, `/up` returns 503 and the page shows "Database unavailable".
- **PostgreSQL CRUD**: A `notes` table for creating and listing text notes. Configuration via environment variables (`POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`). Only active when `DB_CONFIGURED` is true.
- **Filesystem blob storage**: File uploads saved to the path specified by `BLOB_STORAGE_PATH` (default: `/data/blobs`), with timestamp-prefixed filenames. Works regardless of database configuration.
- **Health check endpoint**: `GET /up` returns 200 OK when the database is not configured (web-only mode) or when `SELECT 1` succeeds. Returns 503 only when the database is configured but unreachable. This endpoint is used by kamal-proxy for health-based routing.
- **Custom environment variables**: Displays `MY_VAR` and `MY_SECRET` values on the index page, demonstrating the `SECRET_ENV_VARS`/`ENV_VARS` injection mechanism.
- **HTTP request headers**: Displays all incoming HTTP request headers on the index page for debugging proxy behavior.

**Dockerfile:** Based on `python:3.12-slim`. The CMD conditionally retries `init_db()` up to 30 times only when `POSTGRES_HOST` is set (to wait for the PostgreSQL accessory to become available), then always starts gunicorn with `exec` for proper signal handling. When no database is configured, gunicorn starts immediately without the init_db retry loop.

### 7. VM Userdata Scripts

**Files:** `scripts/userdata/web_vm.sh`, `scripts/userdata/worker_vm.sh`, `scripts/userdata/db_vm.sh`

Cloud-init userdata scripts executed on first boot. All three scripts install and configure fail2ban for SSH brute-force protection (3 retries, 1-hour ban, aggressive mode). The web and DB scripts additionally format and mount their data disks:

1. Install fail2ban and write `/etc/fail2ban/jail.local` with hardened settings.
2. (Web and DB only) Wait up to 300 seconds for the data disk device (`/dev/vdb`) to appear.
3. (Web and DB only) Format as ext4 if no filesystem exists (preserves data on re-attach).
4. (Web and DB only) Create the mount point and mount the device.
5. (Web and DB only) Add an fstab entry with `nofail` for persistence across reboots.

The web VM mounts at `/data/blobs`, the DB VM mounts at `/data/db`. The worker script handles fail2ban only (workers are stateless with no data disks).

---

## Network Architecture

```
                        Internet
                           |
          +----------------+------------------+
          |                |                  |
          v                v                  v
   [Public IP: web]  [Public IP: wk-1]  [Public IP: db]
   FW: 22,80,443     FW: 22             FW: 22
          |                |                  |
     Static NAT       Static NAT         Static NAT
     (1:1)             (1:1)              (1:1)
          |                |                  |
          v                v                  v
   +-----------+    +-----------+      +-----------+
   | Web VM    |    | Worker VM |      | DB VM     |
   | 10.x.x.a |    | 10.x.x.b |      | 10.x.x.c |
   |           |    |           |      |           |
   | kamal-    |    | app       |      | postgres  |
   | proxy     |    | worker    |      | :5432     |
   | + app     |    | container |      | container |
   | container |    |           |      |           |
   +-----------+    +-----------+      +-----------+
          |                |                  |
          +----------------+------------------+
                           |
               CloudStack Isolated Network
                  (private 10.x.x.0/24)
```

### Key networking characteristics

- **Isolated network**: All VMs share a single CloudStack isolated network with private IPs in the 10.x.x.0/24 range. The network is created with the "Default Guest Network" offering and provides internal routing between all VMs without traversing public networks.
- **Static NAT (1:1)**: Each VM receives a dedicated public IP with 1:1 static NAT. This maps all inbound ports on the public IP to the VM's private IP. Outbound traffic from VMs also uses their respective public IPs.
- **Firewall rules**: CloudStack firewall rules control inbound access. The web VM allows TCP ports 22 (SSH), 80 (HTTP), and 443 (HTTPS). Worker and DB VMs allow only TCP port 22 (SSH). All rules use CIDR `0.0.0.0/0` (unrestricted source).
- **Internal database access**: The web application connects to PostgreSQL using the DB VM's internal IP (`POSTGRES_HOST` environment variable), avoiding public network traversal for database traffic.
- **Wildcard DNS / Custom domain**: When no custom domain is configured, kamal-proxy uses `<web-ip>.nip.io` for Host header routing. The nip.io service resolves any `A.B.C.D.nip.io` address to `A.B.C.D`, providing wildcard DNS without custom domain configuration. When a custom domain is set, kamal-proxy uses the domain as the proxy host. TLS via Let's Encrypt is enabled for both cases -- nip.io subdomains are valid public DNS names that support HTTP-01 challenges.
- **Source NAT**: CloudStack automatically provides a source NAT IP for the isolated network, used for outbound internet access (e.g., pulling Docker images from ghcr.io).

---

## Data Flow

### Deployment data flow

```
1. Workflow triggered (workflow_dispatch or workflow_call from caller repo)
   |
   v
2. GitHub Actions runner starts (ubuntu-latest)
   |
   +-- Checks out caller's application code (Dockerfile, source)
   +-- Checks out infrastructure scripts into _infra/
   +-- Computes infrastructure cache key (SHA-256 of all inputs)
   +-- Checks cache for provision-output.json
   |
   v
3. [CACHE MISS] Provision infrastructure (cmk -> CloudStack API)
   |  (skipped on cache hit -- provision-output.json restored from cache)
   |
   +-- Assembles JSON config from workflow inputs
   +-- Installs and configures CloudMonkey (cmk)
   +-- Creates network, keypair
   +-- Deploys VMs with cloud-init userdata
   +-- Assigns public IPs, static NAT, firewall rules
   +-- Creates and attaches data disks
   +-- Sets up daily snapshot policies
   +-- Configures unattended upgrades on all VMs
   +-- Outputs JSON: {IPs, VM IDs, volume IDs}
   |
   v
4. Generate Kamal config from provision output (cached or fresh)
   |
   v
5a. [FIRST DEPLOY] kamal setup (SSH -> each VM)
   |
   +-- Installs Docker on all hosts
   +-- Logs into ghcr.io registry
   +-- Builds Docker image (amd64)
   +-- Pushes image to ghcr.io/<owner>/<repo>:<sha>
   +-- Boots postgres accessory on DB VM (if enabled)
   +-- Deploys web container behind kamal-proxy
   +-- Deploys worker containers (if enabled)
   |
5b. [CONSECUTIVE DEPLOY] kamal deploy (SSH -> each VM)
   |  (Docker already installed, accessories already running)
   |
   +-- Logs into ghcr.io registry
   +-- Builds Docker image (amd64)
   +-- Pushes image to ghcr.io/<owner>/<repo>:<sha>
   +-- Deploys web container behind kamal-proxy (zero-downtime swap)
   +-- Deploys worker containers (if enabled)
   |
   v
6. Application is live
   |
   +-- kamal-proxy routes HTTP by Host header
   +-- Health checks at /up (SELECT 1)
   +-- Provisioning output saved as GitHub artifact (90 days)
```

### Runtime request flow

```
HTTPS Request -> Public IP -> Static NAT -> Web VM:443
  -> kamal-proxy (Host header routing; TLS termination via Let's Encrypt)
    -> app container (gunicorn, 2 workers)
      -> PostgreSQL (DB VM internal IP:5432)
      -> Blob storage (/data/blobs on mounted data disk)
```

### E2E test data flow

```
1. User triggers e2e-test.yml (workflow_dispatch)
   |
   v
2. Initial teardown (direct script call)
   |
   v
3. For each scenario:
   |
   +-- Trigger deploy.yml via `gh workflow run`
   +-- Poll for new run (ID > previous latest)
   +-- Watch run until completion (`gh run watch`)
   +-- Download provision-output artifact
   |
   v
4. Verify deployed application
   |
   +-- HTTP: GET /up (health check, 200 expected)
   +-- HTTP: GET / (page content, env vars, DB status)
   +-- HTTP: POST /notes (create note, verify in page)
   +-- HTTP: POST /upload (file upload, verify in page)
   +-- SSH: mountpoint -q /data/blobs (web VM)
   +-- SSH: mountpoint -q /data/db (DB VM)
   +-- SSH: blockdev --getsize64 (disk size verification)
   +-- SSH: docker exec printenv (worker env vars)
   |
   v
5. Trigger teardown.yml via `gh workflow run`
   |
   v
6. Save results JSON -> GitHub step summary
```

### Disaster recovery data flow

```
1. User triggers deploy.yml with recover=true, zone=<target-zone>
   |
   v
2. Provision infrastructure
   |
   +-- Resolve zone, offerings, template
   +-- Pre-flight checks:
   |     - No existing network/volumes in target zone
   |     - Snapshots available in target zone (BackedUp state)
   +-- Create network, keypair, VMs (same as normal deploy)
   +-- Create data disks FROM SNAPSHOTS (not blank)
   |     - cmk create volume name=<name> snapshotid=<id>
   +-- Tag and attach volumes to VMs
   +-- Create new snapshot policies on recovered volumes
   |
   v
3. Kamal setup (same as normal deploy)
   |
   v
4. Application is live with recovered data
```

### Teardown data flow

```
workflow_dispatch -> CloudMonkey -> CloudStack API
  1. Delete snapshot policies
  2. Detach + delete data volumes
  3. Disable static NAT
  4. Delete firewall rules
  5. Release public IPs
  6. Destroy VMs (expunge=true)
  7. Delete network (after 5s wait)
  8. Delete SSH key pair
```

---

## Security Model

### Secret management

Secrets are scoped per environment. The default preview environment uses unsuffixed names. Additional environments use the environment name (uppercased) as a suffix. Secrets common to all environments (CloudStack credentials, `GITHUB_TOKEN`) have no suffix.

| Secret | Scope | Purpose |
|---|---|---|
| `CLOUDSTACK_API_KEY` | Global (all environments) | CloudStack API authentication |
| `CLOUDSTACK_SECRET_KEY` | Global (all environments) | CloudStack API authentication |
| `SSH_PRIVATE_KEY` | Per-environment (preview: unsuffixed, others: `_<ENV_NAME>`) | SSH access to VMs in the target environment |
| `POSTGRES_PASSWORD` | Per-environment (preview: unsuffixed, others: `_<ENV_NAME>`) | PostgreSQL superuser password |
| `GITHUB_TOKEN` | Automatic (GitHub) | ghcr.io registry authentication |

Example for a "production" environment: `SSH_PRIVATE_KEY_PRODUCTION`, `POSTGRES_PASSWORD_PRODUCTION`. The caller workflow maps these suffixed secrets to the reusable workflow's standard (unsuffixed) secret names.

### Secret handling practices

- **No secrets in source control**: The `.kamal/secrets` file uses `$VAR` references that Kamal resolves from the process environment at runtime. The file is generated during the workflow run and is never committed.
- **Per-environment SSH key isolation**: Each environment has its own SSH key pair (e.g., `~/.ssh/<repo-name>` for preview, `~/.ssh/<repo-name>-production` for production). The private key is written to a temporary file with mode 600 during the workflow run. The public key is derived at runtime using `ssh-keygen -y` (the public key is never stored as a separate secret). This ensures that compromising one environment's key does not grant access to other environments' VMs.
- **Early validation**: When `db_enabled` is true, the workflow validates that `POSTGRES_PASSWORD` is set before any infrastructure is provisioned.
- **Scoped tokens**: `GITHUB_TOKEN` is automatically provided by GitHub Actions with `packages: write` scope, limited to the current repository.

### Network security

- **Firewall rules**: Only the web VM is exposed on HTTP (80) and HTTPS (443). Workers and the database VM are reachable only via SSH (22) from the internet. Database traffic (5432) stays on the private network.
- **Static NAT**: Each VM has its own dedicated public IP. There is no shared ingress point.
- **SSH root access**: Kamal requires root SSH access for Docker management. Each environment has its own isolated SSH key pair, so keys from one environment cannot be used to access VMs in another.

### Identified security considerations

- Firewall rules use `0.0.0.0/0` source CIDR, meaning SSH is open to the internet on all VMs. IP filtering to GitHub Actions runner ranges is a near-term TODO. fail2ban is installed on all VMs to mitigate brute-force attacks (3 retries, 1-hour ban, aggressive mode).
- The database VM's SSH port is exposed publicly, though PostgreSQL's port (5432) is not.
- TLS via Let's Encrypt is always enabled for all deployments (both nip.io and custom domain). All traffic is encrypted.
- Worker VMs have public IPs with SSH access, even though they may not require external connectivity.

---

## Reliability and Resilience

### Retry mechanisms

| Component | Strategy | Details |
|---|---|---|
| CloudMonkey API calls | Exponential backoff | 5 retries: 2s, 4s, 8s, 16s, 32s (total ~62s max wait) |
| Database initialization | Linear retry | 30 attempts with 2-second sleep (up to 60s) |
| Disk attachment wait | Polling | 5-second intervals, 300-second timeout |
| SSH connectivity (tests) | Polling | 10-second intervals, 180-second timeout |

### Idempotent provisioning

The provisioning script is fully idempotent. Every resource creation follows the pattern:

1. Search for an existing resource by name.
2. If found, log and return its ID.
3. If not found, create it.

This enables safe re-execution after partial failures and supports incremental topology changes (adding workers, enabling the database) without reprovisioning existing resources.

### Data durability

- **Data disks**: Persistent CloudStack volumes attached to VMs. Formatted as ext4 on first use; subsequent attachments preserve existing data.
- **Snapshot policies**: Daily snapshots at 03:00 (America/Sao_Paulo timezone) with 3-snapshot retention. Snapshots are replicated across all available CloudStack zones.
- **fstab persistence**: Data disk mount points are added to `/etc/fstab` with the `nofail` option, ensuring the VM boots even if the disk is temporarily unavailable.

### Deployment reliability

- **readiness_delay**: 15-second delay before health checks begin, allowing the application to initialize.
- **deploy_timeout**: 180-second timeout for deployment operations.
- **drain_timeout**: 30-second timeout for draining in-flight requests during container replacement.
- **Health checks**: kamal-proxy checks `GET /up` every 3 seconds with a 5-second timeout. The endpoint returns 200 when the database is not configured (web-only mode) or when the database is reachable. It returns 503 only when the database is configured but unreachable.

### Test coverage

Two complementary test suites validate the system at different levels:

**Infrastructure tests** (`test-infrastructure.yml` / `test_infrastructure.py`) validate:

- Resource creation and absence for all resource types.
- Scale-up from 1 to 3 workers and scale-down from 3 to 1.
- SSH connectivity to all VM types.
- Data disk mount point verification via remote SSH commands.
- Firewall rule correctness (exact port sets per VM role).
- Static NAT mapping correctness (each IP points to the expected VM).
- Complete teardown verification (no orphaned resources).
- Emergency cleanup on test failure.

**E2E application tests** (`e2e-test.yml` / `e2e_test.py`) validate:

- Full deploy pipeline (provision + Kamal + container start).
- HTTP health check (`/up` returns 200).
- Application page content (notes, file listings, env vars).
- Database operations (POST note, verify in page).
- File uploads (POST multipart, verify in page).
- Graceful degradation (web-only mode shows "Database not configured").
- Disk mount points and write access via SSH.
- Disk size verification (implicit defaults and explicit inputs) via `blockdev --getsize64`.
- Worker container environment variables via `docker exec printenv`.
- Scale-up and scale-down with application verification after each change.
- Teardown via real workflow trigger.

---

## Technology Choices

| Technology | Role | Rationale |
|---|---|---|
| **Locaweb Cloud (CloudStack)** | IaaS platform | Target deployment platform. CloudStack-based API for VM, network, and storage management. |
| **GitHub Actions** | CI/CD orchestration | Native integration with GitHub repositories. Provides secrets management, artifact storage, and `workflow_dispatch` for manual triggers. |
| **CloudMonkey (cmk)** | CloudStack CLI | Official Apache CloudStack CLI. JSON output mode enables scriptable infrastructure management. Single static binary with no runtime dependencies. |
| **Kamal 2** | Container deployment | SSH-based deployment tool. Handles Docker installation, image build/push, zero-downtime deployment, and accessory management without requiring a container orchestrator on the target hosts. |
| **kamal-proxy** | Reverse proxy | Lightweight HTTP proxy included with Kamal. Provides Host header routing, health checks, and zero-downtime container swaps. |
| **ghcr.io** | Container registry | GitHub Container Registry. Authentication via `GITHUB_TOKEN` eliminates the need for separate registry credentials. |
| **Python** | Provisioning scripts | Used for infrastructure provisioning, teardown, test suite, and inline workflow scripts. Chosen for readability and availability on `ubuntu-latest` runners without installation. |
| **Flask + gunicorn** | Sample application | Lightweight Python web framework for the sample app. gunicorn provides a production WSGI server with configurable worker count. |
| **PostgreSQL 17 (supabase/postgres)** | Database | Deployed as a Kamal accessory in a Docker container on the dedicated DB VM using the `supabase/postgres` image with a pinned version tag (ADR-025). Bundles 60+ extensions (pgvector, pg_cron, pgmq, pg_jsonschema, etc.) out of the box. Uses `-D /etc/postgresql` to load Supabase's config. PostgreSQL parameters (`shared_buffers`, `effective_cache_size`, `work_mem`, `maintenance_work_mem`, `max_connections`) are auto-tuned based on the selected `db_plan` size (see ADR-024). The host-level `pgdata` subdirectory handles ext4 `lost+found` compatibility (ADR-007). |
| **nip.io** | Wildcard DNS | Free wildcard DNS service. Resolves `A.B.C.D.nip.io` to `A.B.C.D`, eliminating the need for custom DNS configuration during development and testing. |
| **Ubuntu 24.x** | VM template | Auto-discovered from the CloudStack template catalog. Provides a recent LTS base with cloud-init support. |

---

## Constraints and Limitations

### Platform constraints

- **CloudStack static NAT**: CloudStack enforces a one-to-one mapping between a public IP and a VM for static NAT. A VM cannot have two static NAT IPs. The provisioning script explicitly handles this by checking existing mappings before assigning new ones.
- **Single availability zone**: All resources for a deployment are created in a single CloudStack zone (ZP01 or ZP02). Same-zone disaster recovery is supported via the `recover` input, which creates data volumes from existing snapshots. Cross-zone recovery is not currently supported because Locaweb Cloud does not support the `copySnapshot` API.
- **VM plan names**: The provisioning script resolves service offering names (micro, small, medium, etc.) at runtime. Available offerings depend on the Locaweb Cloud account and zone.
- **Data disk device path**: Userdata scripts hardcode `/dev/vdb` as the data disk device. This assumes the data disk is the first (and only) attached volume.

### Deployment constraints

- **Single web host**: The Kamal configuration supports only one web server host. Horizontal scaling of the web tier is not supported.
- **Root SSH required**: Kamal requires root-level SSH access to manage Docker. Non-root deployment is not supported.
- **No rolling updates for workers**: Worker containers are deployed simultaneously, not in a rolling fashion.
- **TLS depends on Let's Encrypt and nip.io**: SSL via Let's Encrypt is always enabled. Both nip.io subdomains and custom domains support HTTP-01 challenges. Certificate provisioning depends on Let's Encrypt and (for nip.io deployments) nip.io DNS availability.
- **Sequential provisioning and deployment**: Infrastructure provisioning and Kamal deployment run sequentially in a single job. A provisioning failure prevents deployment; a deployment failure leaves infrastructure provisioned.

### Operational constraints

- **Manual scaling**: Worker replica count must be changed by re-running the deploy workflow with a different `workers_replicas` value. There is no auto-scaling.
- **No rollback mechanism**: The system does not provide automated rollback. A failed deployment requires manual intervention or re-running the workflow with a known-good commit.
- **Artifact retention**: Provisioning output artifacts are retained for 90 days. After expiration, the resource mapping is no longer available through GitHub (though resources can still be found by name in CloudStack).
- **Test isolation**: The test suite uses `github.run_id` for isolation, but shares the same CloudStack account. Concurrent test runs could conflict if resource limits are reached.
- **Single database**: Only one PostgreSQL instance is supported. Multiple databases or read replicas are not configurable through the workflow.

### Known limitations of the sample application

- No connection pooling for PostgreSQL (new connection per request).
- No authentication or authorization.
- File uploads are stored with timestamp-prefixed names but no deduplication.
- The `lost+found` directory in `/data/blobs` is explicitly filtered out of the file listing.
- The `psycopg2` import is deferred to `get_db()` to avoid import failures when no database is configured. This means import errors are only caught at runtime.

### E2E test constraints

- E2E tests use `github.repository_id` for resource naming with env names `preview` and `e2etest`. Running E2E tests will tear down any existing deployment using those env names.
- Each scenario takes 8-15 minutes (provisioning + Kamal deploy + verification + teardown). The "all" scenario may take 45-60 minutes.
- The E2E workflow requires `ENV_VARS` (variable containing `MY_VAR=...`) and `SECRET_ENV_VARS` (secret containing `MY_SECRET=...`) to be configured in the repository for environment variable verification.
