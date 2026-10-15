---
{"document":"ideal-customer-profile","status":"draft","owner":"project-manager","last_reviewed":"2026-10-15"}
---

# Ideal Customer Profile

## Audience and context
Software engineers, technical leads, automation developers, and solo founders building software on Windows workstations who use OpenAI Codex Desktop as their primary coding environment (tested on Windows; other platforms untested). They manage multi-step engineering projects that benefit from distinct specialist roles (such as planning, implementation, code review, verification, and monitoring) within their existing codebase.

## Problem, triggers and current behavior
Proposed workflow hypothesis: Developers working with single-chat AI assistants frequently encounter context pollution, overwritten instructions, and unverified completion claims when tasks span multiple phases. Hypothesized workarounds involve opening multiple chat windows and copying text between them. This approach lacks structured handoffs, does not enforce verification before completion, and risks losing operational context across long turns.

## Fit and exclusions
- **Good fit**: Developers who want persistent agent roles with per-agent, project-scoped file memory in each agent's memory folder (directory organization, not OS/runtime sandboxing; see [catalog memory contract](../../catalog/base/memory/README.md)), a prescribed dual-gate review and verification workflow, and local team rooms operating through native Codex Desktop chats alongside a local loopback Office server.
- **Exclusions**: Does not serve developers seeking cloud-hosted autonomous SaaS bots operating without local human oversight, users on platforms without an installed Codex Desktop runtime, or non-technical users seeking web-only chatbots.

## Decision participants
The individual developer or technical project lead decides to adopt the framework and install specialist teams. The repository owner retains ultimate authority over standing approvals, tool execution permissions, and external communications.

## Evidence and open assumptions
- **Observed Evidence**: Source implementation defines local HTTP loopback server (localhost:24242) and Desktop relay communication (sources: [README.md](../../README.md), [teams.md](../operations/teams.md), [scripts/team_room/web.py](../../scripts/team_room/web.py), [scripts/team_room/desktop_relay.py](../../scripts/team_room/desktop_relay.py); inspected 2026-10-15). Current live workstation runtime is not verified by static review.
- **Open Assumptions**: Customer demand frequency and productivity gains are unverified hypotheses. Scalability across distributed multi-host teams remains an unverified deployment scenario; current architecture is tested on single-host desktop environments on Windows.

Related fields: [alternatives](competitors.md), [value proposition](value-proposition.md).
