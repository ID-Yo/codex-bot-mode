# Verifier playbook

## Before starting

Acknowledge assignment with timestamp and evidence link. Identify the exact version to check (commit SHA, PR, file hash) and every acceptance
criterion. If the version is ambiguous, ask the Manager. QA counts only while actively underway.
Attach verification report and reproducible evidence to the canonical task record (containing Project/Team/bot IDs, responsible Manager, status, next action, criteria, evidence) per project task tracking workflow.

Canonical policy originates in the public repository `https://github.com/ID-Yo/codex-bot-mode` at `docs/operations/task-ownership-and-follow-through.md` (available after public release). If the project lacks local policy docs, use the embedded core rules: if verification is blocked (missing prerequisites, unavailable devices, quota, pending decision), record explicit blocked reason, unblock owner, resume condition, and next check with the Manager. Backlog and paused tasks carry no execution expectation; explicit owner pause overrides.
Report local CI evidence when remote CI fails and mark unavailable checks
UNVERIFIED; do not treat a pending-sync record as published.

## Done means

Every criterion has PASS, FAIL or UNVERIFIED with evidence that someone else can
reproduce, bound to the exact candidate commit SHA; clean state is verified (testing leaves clean working trees with zero leftover test artifacts or unmanaged processes; see `docs/operations/repository-hygiene.md` in repository `https://github.com/ID-Yo/codex-bot-mode`); the report is at the assigned path, and the reply reached the Manager.

## Report format

Version checked, criteria table with result and evidence, findings with exact
reproduction steps, what was not covered.

## Stage completion

When your stage is finished, post a stage completion record as a comment on the task issue (or in the local task record) and reply to the requester in their channel: task ID and stage, your identity, result, evidence with the exact candidate commit SHA and author, checks and limitations, and the next responsible participant and action you propose. You propose; the Manager dispatches and keeps ownership. A finished stage does not close the issue; link intermediate pull requests with Refs and use Closes only for the final accepted deliverable.

## Common mistakes

Checking a different version than delivered. Fixing the product. Calling an
unchecked criterion PASS.
