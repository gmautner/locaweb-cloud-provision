# Architectural Decision Records

This directory contains the Architectural Decision Records (ADRs) for the `locaweb-cloud-deploy` project.

| ADR | Title | Status |
|-----|-------|--------|
| [ADR-001](001-kamal-for-deployment.md) | Use Kamal 2 for Container Deployment (not Kubernetes) | Accepted |
| [ADR-002](002-ghcr-with-github-token.md) | Use ghcr.io with GITHUB_TOKEN for Container Registry | Accepted |
| [ADR-003](003-dynamic-kamal-config-generation.md) | Generate Kamal Config Dynamically at Deploy Time | Accepted |
| [ADR-004](004-cloudmonkey-cli-for-cloudstack.md) | CloudMonkey CLI for CloudStack API Interaction | Accepted |
| [ADR-005](005-idempotent-provisioning.md) | Idempotent Provisioning with Name-Based Lookup | Accepted |
| [ADR-006](006-static-nat-for-public-ip.md) | Static NAT (1:1) for Public IP Assignment | Accepted |
| [ADR-007](007-pgdata-subdirectory.md) | PGDATA Subdirectory for ext4 Volume Compatibility | Accepted |
| [ADR-008](008-nip-io-wildcard-dns.md) | nip.io for Wildcard DNS | Accepted |
| [ADR-009](009-aliased-secrets.md) | Aliased Secrets for Environment Variable Mapping | Superseded by ADR-012 |
| [ADR-010](010-fail-fast-secret-validation.md) | Fail-Fast Secret Validation | Accepted |
| [ADR-011](011-teardown-and-redeploy-recovery.md) | Teardown-and-Redeploy as Recovery Strategy | Accepted |
| [ADR-012](012-standardized-postgres-env-vars.md) | Standardized PostgreSQL Environment Variables | Accepted |
| [ADR-013](013-kamal-prefix-env-vars.md) | KAMAL_ Prefix Convention for Custom Environment Variables | Superseded by ADR-019 |
| [ADR-014](014-e2e-test-orchestration.md) | E2E Test Orchestration via Real Workflow Triggers | Accepted |
| [ADR-015](015-in-place-vm-scaling-and-disk-resize.md) | In-Place VM Scaling and Disk Resize | Accepted |
| [ADR-016](016-custom-domain-ssl.md) | Custom Domain Support with Let's Encrypt SSL | Superseded by ADR-027 |
| [ADR-017](017-cross-zone-disaster-recovery.md) | Disaster Recovery via Snapshots | Accepted |
| [ADR-018](018-fail2ban-ssh-protection.md) | fail2ban for SSH Brute-Force Protection | Accepted |
| [ADR-019](019-dotenv-kamal-secrets-vars.md) | Consolidated Dotenv Format for Custom Container Environment Variables | Accepted |
| [ADR-020](020-reusable-workflow-call.md) | Reusable Workflows via workflow_call with Dual Checkout | Accepted (dual checkout in deploy.yml superseded by ADR-028) |
| [ADR-021](021-environment-name-support.md) | Environment Name Support for Multi-Environment Deployments | Accepted |
| [ADR-022](022-per-environment-secret-and-ssh-key-isolation.md) | Per-Environment Secret and SSH Key Isolation | Accepted |
| [ADR-023](023-supabase-postgres-image.md) | Switch to supabase/postgres with Automated Tag Resolution | Rejected (see ADR-025) |
| [ADR-024](024-plan-based-postgres-tuning.md) | Plan-Based PostgreSQL Parameter Tuning | Accepted |
| [ADR-025](025-supabase-postgres-hardcoded.md) | Switch to supabase/postgres with Hardcoded Version | Accepted |
| [ADR-026](026-deploy-caching.md) | Input-Hash Caching for Faster Consecutive Deploys | Accepted |
| [ADR-027](027-universal-tls.md) | Universal TLS via Let's Encrypt | Accepted |
| [ADR-028](028-infra-app-separation.md) | Separate Infrastructure Provisioning from Application Deployment | Accepted |
