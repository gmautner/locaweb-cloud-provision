# ADR-001: Use Kamal 2 for Container Deployment (not Kubernetes)

## Status

Accepted

## Context

The `locaweb-cloud-provision` project needs to deploy containerized web applications to CloudStack virtual machines. Several deployment strategies were considered:

- **Kubernetes (k3s/k8s):** Full container orchestration with service discovery, auto-scaling, and a rich ecosystem. However, it introduces significant operational complexity -- a cluster must be provisioned, maintained, and upgraded. For single-app deployments on a small number of VMs, this is overkill.
- **Docker Compose over SSH:** A lightweight approach where a `docker-compose.yml` is copied to the remote host and brought up via SSH. Simple, but lacks zero-downtime deployment, health checking, and structured accessory management.
- **Kamal 2:** A deployment tool from Basecamp that orchestrates Docker containers over SSH. It installs Docker on fresh hosts, manages registry authentication, distributes images, and performs health-checked rolling deployments through its companion `kamal-proxy` reverse proxy.

The target environment is CloudStack VMs with no pre-installed container runtime. The deployment must support zero-downtime updates, optional accessories (such as a PostgreSQL database), and worker processes.

## Decision

Use Kamal 2 as the deployment tool for containerized applications on CloudStack VMs.

Kamal 2 provides:

- **Zero-downtime deployments** via `kamal-proxy`, which health-checks new containers before switching traffic.
- **Docker bootstrapping** on fresh VMs -- no pre-configuration needed beyond SSH access.
- **Registry login and image distribution** to all target hosts.
- **Accessory management** for supporting services like PostgreSQL, with volume mounts and environment variables.
- **SSH-based orchestration** that maps naturally to the VM-per-role model used by CloudStack provisioning.

## Consequences

**Positive:**

- Simpler operational model compared to Kubernetes. No cluster to provision, no control plane to maintain, no etcd backups.
- Fresh VMs require only SSH access. Kamal handles Docker installation and all subsequent container lifecycle management.
- The `kamal-proxy` component provides host-based routing, health checking, and zero-downtime container swaps without external load balancers.
- Accessories (e.g., PostgreSQL) are managed declaratively in the same configuration file as the application.

**Negative:**

- Limited to single-container-per-role deployments. No sidecar pattern or multi-container pods.
- No built-in service mesh, auto-scaling, or self-healing beyond basic Docker restart policies.
- Kamal is a relatively young tool with a smaller community than Kubernetes. Breaking changes between versions are possible.
- Debugging deployment issues requires understanding both Kamal's abstractions and the underlying Docker/SSH layer.
