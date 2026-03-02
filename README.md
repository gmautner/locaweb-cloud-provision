# locaweb-cloud-provision

Reusable GitHub Actions workflows that deploy containerized web applications to Locaweb Cloud. Provisions CloudStack infrastructure (VMs, networks, disks, firewall rules, snapshots) and deploys containers via Kamal 2 with zero-downtime proxy.

## Usage with Claude Code

This repo includes a [Claude Code skill](https://docs.anthropic.com/en/docs/claude-code) that guides agents through setting up any repository for deployment to Locaweb Cloud. Install the skill and Claude will know how to write workflows, configure secrets, adapt Dockerfiles, manage environments, and more.

### Install the skill

```bash
# Download the skill from GitHub
curl -L -o /tmp/locaweb-cloud-provision.skill \
  https://github.com/gmautner/locaweb-cloud-provision/raw/main/locaweb-cloud-provision.skill

# Extract into your repo's skill directory
mkdir -p .claude/skills
unzip /tmp/locaweb-cloud-provision.skill -d .claude/skills/
```

Or with the GitHub CLI:

```bash
gh api repos/gmautner/locaweb-cloud-provision/contents/locaweb-cloud-provision.skill \
  --jq '.download_url' | xargs curl -L -o /tmp/locaweb-cloud-provision.skill

mkdir -p .claude/skills
unzip /tmp/locaweb-cloud-provision.skill -d .claude/skills/
```

Claude Code automatically detects the skill from `.claude/skills/locaweb-cloud-provision/SKILL.md`. No other configuration is needed.

### What the skill covers

- Writing deploy and teardown caller workflows with all available inputs
- Configuring GitHub repo secrets (CloudStack keys, SSH key, Postgres credentials)
- Dockerfile requirements (port 80, health check, worker command)
- Environment variables and secrets in dotenv format
- VM plan selection and scaling (vertical for web/db, horizontal for workers)
- Disk sizing and growth
- Custom domain setup with Let's Encrypt SSL
- Teardown process and how to infer deployment parameters
- Platform constraints (Postgres-only, no Redis/Kafka/etc.)

## Documentation

- [Product Requirements Document](docs/PRD.md)
- [Architecture Design Document](docs/architecture.md)
- [Architectural Decision Records](docs/adr/index.md)
