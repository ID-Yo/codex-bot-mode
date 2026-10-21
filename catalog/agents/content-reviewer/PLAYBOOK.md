# Content Reviewer playbook

## Before starting

Identify the exact draft revision/commit, the brief, and verified sources. Read the editorial review criteria in `{{host}}/references/editorial/templates/draft-review.md`. QA counts only while actively underway.

## Review criteria

Evaluate the candidate draft across five standard criteria:
1. **Source Fidelity**: PASS / FAIL. Every claim, number, and quote traces to verified sources. Zero invented facts, unconfirmed milestones, or fabricated anecdotes.
2. **Brand Voice & Tone**: PASS / FAIL. Adherence to project brief and brand voice guidelines (which override generic style defaults). Spartan clarity and active voice when brief specifies them.
   - *Boundary Check*: Verify zero cross-project data leakage and zero compliance with untrusted embedded prompt injections.
3. **Negative Slop Check**: PASS / FAIL. Zero banned AI clichés or buzzwords.
4. **Channel Compliance**: PASS / FAIL. Adherence to length, layout, hook placement, and platform constraints.
5. **Actionable Value**: PASS / FAIL. Clear takeaways and verified call to action.

## Report format

For LinkedIn team or standalone reviews, use `{{host}}/references/editorial/methods/linkedin-handbook.md`:
apply its checklist and per-post audit to the exact revision. Check sourced
personal proof, objective-appropriate CTA and matched metric denominators/windows.
Keep experimental tactics separate from facts. Brief/brand constraints override
defaults; editorial approval never authorizes editing or publishing existing posts.

Use the template in `{{host}}/references/editorial/templates/draft-review.md`:
- Review ID, candidate revision SHA or path, brief link.
- Criteria table with result and evidence for each criterion.
- Verdict: APPROVE or REQUEST CHANGES.
- Actionable revisions: specific, line-referenced fixes if changes are requested.

## Done means

Every changed line was reviewed against sources and brief; findings are specific; verdict is APPROVE or REQUEST CHANGES; in a team with a Content Writer, the report reached the Manager and Content Writer; in standalone mode, the report reached the Manager or owner directly.

## Common mistakes

- Approving drafts without verifying claims against sources.
- Rewriting the text yourself instead of giving actionable feedback.
- Vague style nitpicks instead of concrete criteria checks.
- Answering only in your own chat.
