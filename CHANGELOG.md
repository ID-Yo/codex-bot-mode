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

## [Office 0.1.6] - 2026-10-25

Release: https://github.com/ID-Yo/codex-bot-mode/releases/tag/office-v0.1.6

Windows x64 preview of the Codex Office desktop controller (signed with self-signed certificate `CN=Codex Office (ID-Yo)`, thumbprint `2C4510778788894A8CAA3C891FE3BCB899A0C0F0`; compare the published SHA-256).

### Added
- Cross-project rooms, a named room queue with explicit release, room discussion with replies returned to the journal, and an outcomes panel with confirmed approvals.
- Owner inbox with typed items, including blockers that have no linked issue yet.
- Guided New project setup, project visibility controls, dashboard counters and fresher read models.
- Speech status, label and contrast fixes, and a shared language layer in the Office UI.
- Compact single-column controller layout.
- Agent Monitor reports reviews that were never started (listed alerts, relay reads and checkpoints are bounded and degrade into findings), delivers Manager messages from the automation prompt, installs the Manager registry closure in its rollout and reports stage signals that could not be delivered.

### Fixed
- Upgrade stops the previous Office gracefully, archives a stale service descriptor and aborts on any unsuccessful stop instead of forcing it.
- Quit closes the controller while setup is still needed.
- Draining and stop handling in `/desktop/shutdown` return 202 if already in progress or 409 busy on timeout, avoiding spurious cancellation errors.
- A draining Office closes its event streams so the service exits during a graceful stop.
- The installer shows a Retry/Cancel dialog (English and Bulgarian) when it cannot stop the previous Office; silent installs exit with code 2 without prompting.
- The service no longer spins the CPU while the controller polls its status: the project registry scan behind the Desktop relay allow-list is built once, kept while the registry and rosters are unchanged, and native thread checks are reused between status polls.
- The Desktop relay is addressed on 127.0.0.1 instead of localhost, which avoided a slow refused IPv6 attempt on every call; relay addresses from older descriptors are still accepted and are validated the same way everywhere.
- Shutdown inspection reads no turn history, falls back when Desktop refuses a large answer, and names every busy or unknown cause; chats that are not loaded are treated as not running.
- Office sends are recorded durably (intent and outcome) so an interrupted launch or escalation stays unresolved until its request is closed, and shutdown inspection reads those records.
- Shutdown inspection opens its databases read-only with a valid URI for Windows extended-length and UNC paths, so a controller that stores such paths no longer reports an unknown shutdown state ("invalid uri authority").
- The Desktop relay restarts its Desktop bridge by itself, gives up cleanly after two minutes without an answer, and `scripts/start_codex_office.ps1 -RelayOnly` replaces a relay that no longer answers (after several failed checks) while Office keeps running; the Monitor names this remedy. The starter reports success on stdout and errors on stderr.
- The registry overview is cached between polls, and relay and overview fingerprints tolerate invalid project values.

### Changed
- The test suite runs against a throw-away config and home (`tests/isolation.py`, for pytest and unittest), so running it never writes to a real Chief of Staff project list.

### Notes
- Executables are signed with self-signed certificate `desktop/Codex-Office-ID-Yo.cer` (`2C4510778788894A8CAA3C891FE3BCB899A0C0F0`). Import the certificate into Trusted Root / Trusted Publisher to trust it.
- Installer `Codex Office_0.1.6_x64-setup.exe`: 17,612,992 bytes, SHA-256 `D2B370F7F1CD59FEC42A01A502A21972BB967545C7988602A8A55D10C93FECBC`.

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
