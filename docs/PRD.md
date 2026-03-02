# Product Requirements Document: locaweb-cloud-deploy

**Version:** 1.0
**Date:** 2026-02-13
**Status:** Draft

---

## 1. Overview

`locaweb-cloud-deploy` is a reusable GitHub Actions workflow that automates end-to-end deployment of web applications onto Locaweb Cloud, a CloudStack-based Infrastructure-as-a-Service (IaaS) platform. It provisions all required infrastructure -- virtual machines, networks, disks, public IPs, firewall rules, and snapshot policies -- and then deploys a containerized application using Kamal 2.

The workflow is designed to be invoked by other repositories, providing a turnkey deployment layer that any application repository can adopt. Both `workflow_dispatch` (direct/internal) and `workflow_call` (reusable/cross-repo) triggers are supported, so any repo can reference the deploy and teardown workflows without duplicating infrastructure logic.

The project eliminates the manual, error-prone process of provisioning cloud resources and configuring deployments by hand. A single workflow invocation produces a fully operational environment with zero-downtime deployment capabilities, persistent storage, and optional database and worker infrastructure.

A companion teardown workflow destroys all provisioned resources, and a test workflow validates all deployment scenarios end-to-end.

---

## 2. Goals

1. **Reusable workflow.** The deploy and teardown workflows are reusable GitHub Actions workflows (`workflow_call`) that other repositories can invoke, providing deployment capabilities without requiring knowledge of the underlying infrastructure.

2. **Single-action deployment.** A single workflow invocation should go from zero infrastructure to a running, publicly accessible web application.

3. **Idempotent provisioning.** Re-running the deployment workflow against an existing environment must safely reuse existing resources rather than creating duplicates or failing.

4. **Flexible topology.** The workflow must support multiple deployment scenarios: web-only, web with database, web with workers, and the full stack (web, database, and workers) -- all controlled through workflow inputs.

5. **Zero-downtime deploys.** Application updates must be deployed without downtime using Kamal 2 and its built-in kamal-proxy.

6. **Data durability.** Persistent data (blob storage and database files) must reside on dedicated disks with daily snapshot policies and cross-zone replication.

7. **Clean teardown.** All provisioned resources must be destroyable through a single teardown workflow, leaving no orphaned infrastructure.

8. **Testability.** All deployment scenarios must be covered by an automated end-to-end test suite.

9. **AI-agent enablement.** The automation layer should be consumable by AI agents, enabling them to add deployment capabilities on top of their existing application-building skills.

---

## 3. Target Users

The primary target is **AI agents** that build web applications and need deployment capabilities. The workflow provides an automation layer that agents can invoke to deploy applications without requiring familiarity with GitHub Actions, Docker, or cloud infrastructure concepts.

The secondary target is internal Locaweb AI team engineers who maintain and evolve the deployment platform itself.

Users of the workflow (whether human or agent) should not need to interact directly with the CloudStack API, configure virtual machines, or understand container orchestration. They provide an application that meets the application requirements (see section 4.7) and the workflow handles everything else.

---

## 4. Functional Requirements

### 4.1 Infrastructure Provisioning

| ID | Requirement |
|----|-------------|
| FR-01 | The system shall create an isolated CloudStack network for the deployment. |
| FR-02 | The system shall create or reuse an SSH keypair for VM access. |
| FR-03 | The system shall provision a web VM using the specified compute plan and Ubuntu 24.x template. |
| FR-04 | The system shall optionally provision N worker VMs (configurable replica count) using the specified compute plan. |
| FR-05 | The system shall optionally provision a database VM using the specified compute plan. |
| FR-06 | The system shall acquire public IPs and configure 1:1 static NAT for each VM that requires external access. |
| FR-07 | The system shall create firewall rules allowing inbound traffic on required ports (SSH, HTTP/HTTPS). |
| FR-08 | The system shall attach a dedicated data disk to the web VM for blob storage, with a configurable size (default 20 GB). |
| FR-09 | The system shall attach a dedicated data disk to the database VM (if enabled) for PostgreSQL data, with a configurable size (default 20 GB). |
| FR-10 | The system shall configure daily snapshot policies with cross-zone replication for all data disks. |
| FR-37 | The system shall support disaster recovery by creating data volumes from existing snapshots, enabling recovery of blob and database data after a deployment is lost or torn down. |
| FR-38 | The disaster recovery flow shall perform pre-flight checks: no existing deployment in the target zone, and required snapshots available and in BackedUp state. |
| FR-11 | Workers shall be stateless and shall not receive data disks. |
| FR-12 | All provisioning operations shall be idempotent: re-running the workflow must detect and reuse existing resources. |

### 4.2 Application Deployment

| ID | Requirement |
|----|-------------|
| FR-13 | The system shall install Docker on all freshly provisioned VMs. |
| FR-14 | The system shall build the application Docker image and push it to ghcr.io using the repository's GITHUB_TOKEN. |
| FR-15 | The system shall deploy the web application container via Kamal 2 with zero-downtime proxy (kamal-proxy). |
| FR-16 | The system shall use nip.io wildcard DNS when no custom domain is configured, or the custom domain when provided, as the proxy host for kamal-proxy routing. SSL via Let's Encrypt shall always be enabled for both cases. |
| FR-17 | If database is enabled, the system shall deploy PostgreSQL as a Kamal accessory on the database VM. |
| FR-18 | If workers are enabled, the system shall deploy worker containers on each worker VM with the configured command. |
| FR-19 | Inter-VM communication (e.g., web to database) shall use internal/private IP addresses. |
| FR-20 | The PostgreSQL data directory shall use a subdirectory within the mount point to avoid ext4 `lost+found` conflicts. |

### 4.3 Cloud-Init

| ID | Requirement |
|----|-------------|
| FR-21 | The web VM cloud-init script shall format and mount the blob storage disk at the designated path. |
| FR-22 | The database VM cloud-init script shall format and mount the data disk at the designated path. |
| FR-39 | All VM cloud-init scripts (web, worker, DB) shall install and configure fail2ban to block SSH brute-force attempts (3 retries, 1-hour ban, aggressive mode). |

### 4.4 Teardown

| ID | Requirement |
|----|-------------|
| FR-23 | A separate teardown workflow shall destroy all provisioned resources (VMs, disks, networks, IPs, firewall rules, snapshot policies). |
| FR-24 | The teardown workflow shall be safe to run even if some resources have already been deleted. |

### 4.5 Testing

| ID | Requirement |
|----|-------------|
| FR-25 | An infrastructure test workflow (`test-infrastructure.yml`) shall validate all provisioning scenarios (resource creation, scale-up/down, teardown) using CloudStack API verification and SSH connectivity checks. |
| FR-33 | An E2E test workflow (`e2e-test.yml`) shall trigger the real `deploy-app.yml` workflow, wait for completion, and verify application behavior: HTTP health checks, page content, database operations, file uploads, SSH mount points, disk sizes, and container environment variables. |
| FR-34 | The E2E test workflow shall support selectable scenarios: `complete` (full stack), `web-only`, `scale-up`, `scale-down`, and `all`. |
| FR-35 | The E2E test workflow shall use a separate concurrency group from deploy/teardown to prevent deadlocks when triggering workflows. |

### 4.6 Input Validation

| ID | Requirement |
|----|-------------|
| FR-26 | The deploy workflow shall validate that the required secret (`POSTGRES_PASSWORD`) is present when `db_enabled` is true, and fail fast with a clear error message if it is missing. |

### 4.7 Application Requirements

The deployed application must meet the following contract:

| ID | Requirement |
|----|-------------|
| FR-27 | The application shall listen on port 80. |
| FR-28 | The application shall be built from a single Dockerfile at the repository root. |
| FR-29 | If using workers, the same Dockerfile shall support a configurable CMD entrypoint for the worker process. |
| FR-30 | If connecting to a database, the application shall read connection information from environment variables: `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `DATABASE_URL`. These are provided automatically by the platform. |
| FR-31 | The application shall provide a health check endpoint at `/up` that returns HTTP 200 when healthy. When the database is not configured (`POSTGRES_HOST` absent or empty), `/up` shall return 200 without attempting a database connection. |
| FR-32 | The application shall be designed to scale vertically (larger VM) rather than horizontally (multiple web instances), as kamal-proxy with Let's Encrypt operates on a single web VM. |
| FR-36 | The application shall degrade gracefully when the database is not configured: the index page shall display "Database not configured" instead of crashing, and all non-database features (file uploads, environment variable display) shall continue to work. |

---

## 5. Non-Functional Requirements

| ID | Requirement |
|----|-------------|
| NFR-01 | **Reliability.** CloudStack API calls shall use retry logic with exponential backoff to handle transient failures. |
| NFR-02 | **Idempotency.** All provisioning operations must be safe to re-run without side effects. |
| NFR-03 | **Security.** SSH private keys and database credentials shall be stored exclusively as GitHub Actions secrets and never logged or persisted in plain text. |
| NFR-04 | **Security.** The container registry (ghcr.io) shall be accessed using the automatic GITHUB_TOKEN; no separate registry credentials are required. |
| NFR-05 | **Performance.** The provisioning step should complete within a reasonable time frame, limited primarily by CloudStack API response times. |
| NFR-06 | **Observability.** All provisioning and deployment steps shall produce structured log output in the GitHub Actions workflow, making it straightforward to diagnose failures. |
| NFR-07 | **Separation of concerns.** Infrastructure provisioning (CloudStack-specific) and container deployment (Kamal) are clearly separated into distinct workflow jobs. The infra workflow (`deploy.yml`) outputs everything the caller needs; application deployment (Kamal) is handled by the caller (see ADR-028). This is a technical design decision, not aimed at cloud portability. |

---

## 6. Architecture Overview

```
GitHub Actions (workflow_dispatch)
        |
        v
+-------------------+       +---------------------+
| Provisioning      |       | Deployment          |
| (Python + cmk)    | ----> | (Kamal 2)           |
+-------------------+       +---------------------+
        |                           |
        v                           v
+-----------------------------------------------+
|           Locaweb Cloud (CloudStack)          |
|                                               |
|  +----------+  +----------+  +----------+    |
|  | Web VM   |  | DB VM    |  | Worker   |    |
|  | (always) |  | (opt.)   |  | VMs (opt)|    |
|  |          |  |          |  |          |    |
|  | blob     |  | pg data  |  | stateless|    |
|  | disk     |  | disk     |  |          |    |
|  +----------+  +----------+  +----------+    |
|       |              |                        |
|  Public IP      Public IP      Public IPs     |
|  (static NAT)  (static NAT)   (static NAT)   |
+-----------------------------------------------+
        |
        v
   nip.io DNS --> kamal-proxy (TLS) --> App Container
```

### Component Responsibilities

- **GitHub Actions workflows** orchestrate the entire lifecycle: infrastructure provisioning (`deploy.yml`), application deployment (`deploy-app.yml` for internal use, or the caller's own workflow for external use), testing, and teardown.
- **`scripts/provision_infrastructure.py`** calls the CloudStack API via CloudMonkey (`cmk`) to create and configure all infrastructure resources idempotently.
- **Kamal 2** handles Docker installation on fresh VMs, image building and pushing to ghcr.io, container deployment with zero-downtime proxy, and accessory (PostgreSQL) management.
- **Cloud-init scripts** (`scripts/userdata/`) run on first boot to prepare data disks (format, mount).
- **The sample Flask application** (`app.py`) demonstrates PostgreSQL integration, blob storage, and health checking.

---

## 7. Workflow Inputs

The deploy workflow (`deploy.yml`) accepts the following inputs via `workflow_dispatch` and `workflow_call`. When invoked via `workflow_call`, `choice` inputs become `string` (GitHub Actions limitation). Boolean and number types are shared as-is:

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `env_name` | string | `preview` | Environment name for resource isolation (e.g., preview, staging, production). |
| `zone` | choice | -- | CloudStack zone: `ZP01` or `ZP02`. |
| `domain` | string | (empty) | Custom domain (optional). TLS is always enabled via Let's Encrypt. |
| `web_plan` | choice | -- | VM compute offering for the web server. Options range from `micro` through `4xlarge`. |
| `blob_disk_size_gb` | number | 20 | Size in GB of the persistent disk attached to the web VM for blob/file storage. |
| `workers_enabled` | boolean | false | Whether to provision and deploy worker VMs. |
| `workers_replicas` | number | -- | Number of worker VMs to create (only used when `workers_enabled` is true). |
| `workers_cmd` | string | `sleep infinity` | Command to run inside worker containers. |
| `workers_plan` | choice | -- | VM compute offering for worker VMs. |
| `db_enabled` | boolean | false | Whether to provision a dedicated database VM and deploy PostgreSQL. |
| `db_plan` | choice | -- | VM compute offering for the database VM. |
| `db_disk_size_gb` | number | 20 | Size in GB of the persistent disk attached to the database VM for PostgreSQL data. |
| `recover` | boolean | false | Recover from snapshots (disaster recovery). When true, data disks are created from the latest available snapshots instead of blank. |

---

## 8. Secrets Configuration

When using the workflows internally, the following secrets are configured in this repository. When calling from an external repository, each secret must be passed explicitly in the `secrets:` block of the caller workflow (cross-org `secrets: inherit` is not supported by GitHub Actions).

| Secret | Required | Description |
|--------|----------|-------------|
| `CLOUDSTACK_API_KEY` | Always | API key for authenticating with the CloudStack API. |
| `CLOUDSTACK_SECRET_KEY` | Always | Secret key for authenticating with the CloudStack API. |
| `SSH_PRIVATE_KEY` | Always | SSH private key used for VM access during provisioning and Kamal deployment. |
| `POSTGRES_PASSWORD` | When `db_enabled` | PostgreSQL superuser password. Validated at workflow start; workflow fails fast if missing. |
| `GITHUB_TOKEN` | Automatic | Provided automatically by GitHub Actions. Used for pushing container images to ghcr.io and for triggering workflows from the E2E test. |

Application secrets (e.g., `POSTGRES_PASSWORD`, custom API keys) are no longer passed through the infrastructure workflow. Instead, the caller maps them directly as environment variables in its own deploy job. The `SECRET_ENV_VARS` secret has been removed from `deploy.yml` (see ADR-028). For E2E testing, `deploy-app.yml` retains a `secret_env_vars` workflow_dispatch input for compatibility.

---

## 9. Cross-Repository Usage

External repositories invoke the deploy and teardown workflows via `workflow_call`. The caller repository must:

1. **Contain a Dockerfile** at the repository root (the application to deploy).
2. **Configure secrets** in the caller repository's GitHub settings (CloudStack keys, SSH key, database credentials).
3. **Create wrapper workflows** that reference the reusable workflows.

### Caller deploy workflow example

External repositories use a two-job pattern: the first job calls `deploy.yml` for infrastructure, and the second job handles Kamal deployment using the infra outputs.

```yaml
# .github/workflows/deploy.yml
name: Deploy
on:
  workflow_dispatch:
permissions:
  contents: read
  packages: write
jobs:
  infra:
    uses: gmautner/locaweb-cloud-deploy/.github/workflows/deploy.yml@main
    with:
      env_name: "production"
      zone: "ZP01"
      web_plan: "small"
      accessories: '[{"name": "db", "plan": "medium", "disk_size_gb": 20}]'
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }}
      SSH_PRIVATE_KEY: ${{ secrets.SSH_PRIVATE_KEY }}

  deploy:
    needs: infra
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ needs.infra.outputs.infra_env }}" >> "$GITHUB_ENV"
      # ... Install Kamal, prepare SSH key, deploy with Kamal
      # Use needs.infra.outputs.infrastructure_changed to decide kamal setup vs deploy
      # Use needs.infra.outputs.scaled_accessories to reboot rescaled accessories
```

### Caller teardown workflow example

```yaml
# .github/workflows/teardown.yml
name: Teardown
on:
  workflow_dispatch:
jobs:
  teardown:
    uses: gmautner/locaweb-cloud-deploy/.github/workflows/teardown.yml@main
    with:
      env_name: "production"
      zone: "ZP01"
    secrets:
      CLOUDSTACK_API_KEY: ${{ secrets.CLOUDSTACK_API_KEY }}
      CLOUDSTACK_SECRET_KEY: ${{ secrets.CLOUDSTACK_SECRET_KEY }}
```

---

## 10. Deployment Scenarios

The system supports four deployment topologies, all controlled through workflow inputs:

### 9.1 Web Only

- **Inputs:** `db_enabled=false`, `workers_enabled=false`
- **Resources:** 1 web VM, 1 blob storage disk, 1 public IP, network, firewall rules, snapshot policy.
- **Use case:** Stateless web applications or applications using an external database.

### 9.2 Web + Database

- **Inputs:** `db_enabled=true`, `workers_enabled=false`
- **Resources:** 1 web VM, 1 database VM, 2 data disks (blob + postgres), 2 public IPs, network, firewall rules, snapshot policies.
- **Use case:** Standard web application with a PostgreSQL database. The web application connects to the database over the private network.

### 9.3 Web + Workers

- **Inputs:** `db_enabled=false`, `workers_enabled=true`, `workers_replicas=N`
- **Resources:** 1 web VM, N worker VMs, 1 blob storage disk, 1+N public IPs, network, firewall rules, snapshot policy.
- **Use case:** Applications requiring background processing (e.g., AI inference jobs) without a managed database.

### 9.4 Full Stack (Web + Database + Workers)

- **Inputs:** `db_enabled=true`, `workers_enabled=true`, `workers_replicas=N`
- **Resources:** 1 web VM, 1 database VM, N worker VMs, 2 data disks, 2+N public IPs, network, firewall rules, snapshot policies.
- **Use case:** Complete AI application stack with web frontend, database persistence, and background worker processing.

---

## 11. File Structure

```
locaweb-cloud-deploy/
|-- app.py                              Sample Flask application
|-- Dockerfile                          Container image (python:3.12-slim + gunicorn)
|-- requirements.txt                    Python dependencies
|-- .github/
|   `-- workflows/
|       |-- deploy.yml                  Infrastructure provisioning workflow (infra-only)
|       |-- deploy-app.yml             Internal caller: infra + Kamal deployment
|       |-- teardown.yml                Destroy all resources workflow
|       |-- test-infrastructure.yml     Infrastructure validation tests
|       `-- e2e-test.yml               E2E application test workflow
|-- scripts/
|   |-- provision_infrastructure.py     CloudStack provisioning (idempotent)
|   |-- teardown_infrastructure.py      CloudStack resource cleanup
|   |-- test_infrastructure.py          Infrastructure test suite
|   |-- e2e_test.py                     E2E test orchestrator
|   |-- build_config.py                 Build deployment config from inputs
|   |-- generate_kamal_config.py        Generate Kamal deploy config
|   |-- create_kamal_secrets.py         Create Kamal secrets + dotenv env var processing
|   `-- userdata/
|       |-- web_vm.sh                   Cloud-init: fail2ban + format/mount blob disk
|       |-- worker_vm.sh                Cloud-init: fail2ban
|       `-- db_vm.sh                    Cloud-init: fail2ban + format/mount data disk
`-- docs/
    |-- PRD.md                          This document
    |-- architecture.md                 Architecture design document
    `-- adr/                            Architectural decision records
```

---

## 12. TODOs

Near-term, actionable items:

- **IP filtering for SSH access.** Restrict firewall rules for SSH (port 22) to GitHub Actions runner IP ranges only, rather than `0.0.0.0/0`.

---

## 13. Future Considerations

Longer-term directions under consideration:

- ~~**Custom domain support.**~~ Implemented: the `domain` workflow input enables custom domain routing with automatic TLS via Let's Encrypt.
- **Monitoring and alerting.** Integrate application and infrastructure monitoring (metrics, logs, alerts) into the deployment workflow.
- ~~**Multi-environment support.**~~ Implemented: the `env_name` workflow input enables multiple isolated environments (preview, staging, production) from the same repository, each with distinct resource naming and network isolation.
- ~~**Disaster recovery automation.**~~ Implemented: the `recover` workflow input enables recovery from snapshots (same-zone; cross-zone pending Locaweb Cloud support for `copySnapshot`).
- **Vertical scaling of web and database.** The web and database tiers scale vertically (larger VM plans). Kamal Proxy with Let's Encrypt only works with a single web VM, so horizontal web scaling is not planned. The target workloads are expected to scale well vertically.
