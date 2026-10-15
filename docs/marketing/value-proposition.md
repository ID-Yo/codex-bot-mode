---
{"document":"value-proposition","status":"draft","owner":"project-manager","last_reviewed":"2026-10-15"}
---

# Value Proposition

## Audience need and desired outcome
Proposed workflow hypothesis: Developers managing complex features need structured delegation and verifiable quality control without losing execution context or spending hours copying prompts between separate chat windows.

## Offer and mechanism
- **Offer**: Reusable catalog of 24 agent templates (including Manager, Director, and blank template), local team room web interface (Codex Office), and verified handoff protocols.
- **Mechanism**: Local HTTP server on localhost:24242 and Desktop relay on localhost:24243 coordinating with native desktop threads. Version-checked request tracking with reminders ([scripts/team_room/requests.py](../../scripts/team_room/requests.py)) tracks task assignments, and a prescribed dual-gate review workflow (Code Reviewer + Verifier) requires evidence before completion claims.

## Evidence and limitations
- **Observed Evidence**: Source code includes automated test suites covering teams, human materials export preflight, and request tracking ([tests/test_teams.py](../../tests/test_teams.py), [tests/test_human_materials.py](../../tests/test_human_materials.py), [tests/test_requests.py](../../tests/test_requests.py); inspected 2026-10-15). Current live workstation test execution requires local test runner invocation.
- **Limitations**: Tested on Windows workstations with installed Codex Desktop; other operating systems are untested. Single-host workstation focus.

## Cost, effort and objections
- **Cost**: Open-source under the MIT license with no repository license fee. Codex model access and API usage costs depend on the user's account and provider pricing and are not assessed or benchmarked here.
- **Effort**: Launch local Office via [scripts/start_codex_office.ps1](../../scripts/start_codex_office.ps1) or `Codex Office.cmd`; team installation automated via [scripts/team.py](../../scripts/team.py).
- **Objections**: Running multiple agents consumes additional model turns and context; mitigated by role scoping, task boundaries, and configuring appropriate models per task.

Related fields: [audience](ideal-customer-profile.md), [positioning](positioning.md), [messaging](messaging.md).
