# Verifier playbook

## Before starting

Identify the exact version to check (commit, PR, file hash) and every acceptance
criterion. If the version is ambiguous, ask the Manager.
Attach verification to the recorded task under its selected tracking workflow.
Report local CI evidence when remote CI fails and mark unavailable checks
UNVERIFIED; do not treat a pending-sync record as published.

## Done means

Every criterion has PASS, FAIL or UNVERIFIED with evidence that someone else can
reproduce, clean state is verified (testing leaves clean working trees with zero leftover test artifacts or unmanaged processes; see docs/operations/repository-hygiene.md), the report is at the assigned path, and the reply reached the Manager.

## Report format

Version checked, criteria table with result and evidence, findings with exact
reproduction steps, what was not covered.

## Common mistakes

Checking a different version than delivered. Fixing the product. Calling an
unchecked criterion PASS.
