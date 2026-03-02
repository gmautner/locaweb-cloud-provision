# Project Instructions for Claude

## Documentation Sync

This project maintains three design documents that must be kept in sync as the project evolves:

- `docs/PRD.md` -- Product Requirements Document (goals, requirements, deployment scenarios)
- `docs/architecture.md` -- Architecture Design Document (system design, components, network, security)
- `docs/adr/` -- Architectural Decision Records (individual decisions with context and consequences)

When making changes to the codebase that affect architecture, requirements, or design decisions:

1. Update the relevant document(s) to reflect the change.
2. If a new architectural decision is made, create a new ADR in `docs/adr/` and add it to `docs/adr/index.md`.
3. If an existing ADR is superseded, update its status to "Superseded by ADR-NNN".
4. Keep the "TODOs" and "Future Considerations" sections in the PRD up to date with the latest implementation details.

## External Context Sources

When working on this project, use the following references to get context on the key technologies:

### CloudStack

- **API Docs:** https://cloudstack.apache.org/api/
- **CloudStack Docs:** https://docs.cloudstack.apache.org/
- If needed, clone https://github.com/apache/cloudstack-documentation and https://github.com/apache/cloudstack locally to `~/` to inspect source code and internal workings.

### Kamal

- **Docs:** https://kamal-deploy.org/docs
- If needed, clone https://github.com/basecamp/kamal locally to `~/` to inspect source code and internal workings.

### GitHub Actions

- **Workflow Syntax Reference:** https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax

## Skills

When changing skills, always use the skill-creator skill to package the skill before committing.

### skill-creator

The `.claude/skills/skill-creator/` skill was vendored from https://github.com/anthropics/skills/tree/main/skills/skill-creator. Check the source repo for updates before using it for new skill work.

### locaweb-cloud-provision

The `.claude/skills/locaweb-cloud-provision/` skill documents the reusable workflow contract for external repositories deploying to Locaweb Cloud. Keep it in sync when changing workflow inputs, secrets, outputs, or platform behavior.

## Release

After every `git push` to the remote, run the `/release` command to move the `v1` floating tag to the new HEAD.

## Development Process

Use the sample FastAPI app in the root of the repo as the test application.

### Iterating on a feature

1. Implement the change.
2. Run the **deploy app** workflow (`deploy-app.yml`) to deploy. Unless the change specifically requires a different configuration, use **one web VM, one worker VM, and one database VM**.
3. Verify the feature works as intended and nothing else is broken.
4. If something is wrong, run the **teardown** workflow (`teardown.yml`) to clean up, iterate the code, and go back to step 2.

### Testing

Reserve the **infrastructure test** workflow (`test-infrastructure.yml`) for after bigger or more impactful changes, as it takes a long time to complete.

### Smoke Testing

For quick manual verification against live infrastructure:

- **CloudMonkey (cmk):** Available in the terminal for direct CloudStack API queries.
- **SSH key:** `~/.ssh/locaweb-cloud-provision-key` — use this to SSH into deployed VMs (e.g., `ssh -i ~/.ssh/locaweb-cloud-provision-key root@<ip>`).
