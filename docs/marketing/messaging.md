---
{"document":"messaging","status":"draft","owner":"project-manager","last_reviewed":"2026-10-15"}
---

# Messaging

## Audience and communication context
Technical developers, software leads, and repository contributors reading project documentation, release notes, or repository guidelines.

## Core message
Structure persistent agent teams inside Codex Desktop. Collaborate through defined agent roles with per-agent file memory, prescribed review workflows, and local team rooms.

## Supporting claims and proof
| Claim | Supporting evidence / date | Limits or approval needed |
|---|---|---|
| Persistent agent roles inside Codex Desktop | 24 reusable agent templates with distinct roles and playbooks ([catalog/index.yaml](../../catalog/index.yaml)), plus per-agent file memory ([catalog/base/memory/README.md](../../catalog/base/memory/README.md); inspected 2026-10-15). | Requires local Codex Desktop runtime on Windows; other platforms untested. |
| Prescribed dual-gate review workflow | Formal Code Reviewer and Verifier playbooks requiring evidence before completion ([catalog/agents/verifier/PLAYBOOK.md](../../catalog/agents/verifier/PLAYBOOK.md), inspected 2026-10-15). | Workflow prescription; acceptance depends on human or designated reviewer evaluation. |
| Local-first bounded public export | Automated public audit script ([scripts/public_audit.py](../../scripts/public_audit.py)) verifying zero private repository paths in public export output (inspected 2026-10-15). | Public export audit verifies export tree hygiene; does not alter model API provider prompt processing. |
| Task follow-through tracking | Mutable, version-checked request tracking with reminders via validated helper ([scripts/team_room/requests.py](../../scripts/team_room/requests.py), inspected 2026-10-15). | Requires updating request state through the helper; accepted delivery is not completed work. |

## Language and next action
Use precise technical terms, active verbs, and evidence-grounded statements. Avoid marketing jargon, exaggerated autonomy claims, and buzzwords. Next action: Review the [Documentation Index](../00-index.md) to inspect setup commands and available catalog templates.

Related fields: [positioning](positioning.md), [value proposition](value-proposition.md).
