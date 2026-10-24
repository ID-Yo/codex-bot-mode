# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- Chief of Staff playbook limits message bursts to agents: at most 3 messages in a row, mass changes wait at least 20 seconds between messages, and one configuration-level change preferred over many messages.
- Windows x64 Codex Office controller with tray, external Chrome workflow and bundled service; scoped signed component updates, inventory, preview, recovery and separate activation evidence.
- Evidence-aware LinkedIn editorial handbook, drafting/review checklist and post-audit template, discoverable by team and standalone Strategist, Content Writer and Content Reviewer roles.
- One canonical SVG working-structure diagram in the public README and website, with four unified tiers (Human owner, Chief of Staff, Project Director, Teams A/B/C), Project grouping and scoped two-way communication.
- Empty Chief of Staff personal knowledge scaffold, selective owner-confirmed knowledge guidance, manual preservation instructions and complete template-package privacy checks.
- Task Ownership and Follow-through procedure, reusable task record and role guidance for active execution, current-version gates and explicit waiting states.
- Populated documentation drafts with public sources, explicit hypotheses and platform/cost/privacy limits; citation validation covers spaced paths and dotfiles.
- Documentation index, marketing and status drafts, seven MIT-attributed knowledge templates, and a fixed public intake scaffold with export privacy checks.
- Repository Hygiene procedure and role guidance for verified task closeout, configurable daily inspection, protected work preservation and narrowly authorized cleanup.
- Agent Monitor profile and setup guide: metadata-only, task-aware and pause-aware checks, authorized Chief of Staff routing, durable alert checkpoints and configurable model/cadence defaults.
- Ten specialist profiles and nine additional core teams: twelve main teams in total, plus a Manager-and-selected-specialist Starter.
- Synthetic deliverable and boundary exercises for each new specialist, with explicit tool requirements and template/live-verification limits.
- Explicit GitHub, local or combined team work tracking, with one authority for combined tracking and mandatory implementation records.
- Catalog request and contribution form, profile acceptance exercises, MIT provenance guidance and identity-preserving update instructions.

### Fixed
- Chief of Staff updates now identify the project, including multi-project, cross-project and personal operations.
- Installation examples now pass the selected tracking mode or the reviewed answers file.

## [Office 0.1.6] - unreleased

Windows x64 preview of the Codex Office desktop controller (signed with self-signed certificate `CN=Codex Office (ID-Yo)`, thumbprint `2C4510778788894A8CAA3C891FE3BCB899A0C0F0`; compare the published SHA-256).

### Added
- Cross-project rooms, a named room queue with explicit release, room discussion with replies returned to the journal, and an outcomes panel with confirmed approvals.
- Owner inbox with typed items, including blockers that have no linked issue yet.
- Guided New project setup, project visibility controls, dashboard counters and fresher read models.
- Speech status, label and contrast fixes, and a shared language layer in the Office UI.
- Compact single-column controller layout.

### Fixed
- Upgrade stops the previous Office gracefully, archives a stale service descriptor and aborts on any unsuccessful stop instead of forcing it.
- Quit closes the controller while setup is still needed.
- Draining and stop handling in `/desktop/shutdown` return 202 if already in progress or 409 busy on timeout, avoiding spurious cancellation errors.
- A draining Office closes its event streams so the service exits during a graceful stop.
- The installer shows a Retry/Cancel dialog (English and Bulgarian) when it cannot stop the previous Office; silent installs exit with code 2 without prompting.
- The service no longer spins the CPU while the controller polls its status: the project registry scan behind the Desktop relay allow-list is built once, kept while the registry and rosters are unchanged, and native thread checks are reused between status polls.

### Notes
- Executables are signed with self-signed certificate `desktop/Codex-Office-ID-Yo.cer` (`2C4510778788894A8CAA3C891FE3BCB899A0C0F0`). Import the certificate into Trusted Root / Trusted Publisher to trust it.

## [0.1.0] - 2026-10-13

### Added
- Catalog of agent and team templates: Development (Manager, Developer, Code Reviewer, Designer, Verifier), Marketing, Sales, Project Director and a blank template for custom roles.
- Team launcher with setup questions or defaults, dry-run plans, one-time and standing approvals.
- Project roster with shared team data and local chat links; chat replacement keeps history.
- Request ledger with reminders and escalation for unanswered requests.
- Portable, Git-shared agent memory.
- Codex Office: team rooms, Leadership room, personal chats, settings, launching a team.
- Chief of Staff template for a personal agent across projects.
- Codex plugin build and the codex-team-launch and codex-replace-chat skills.
