"""Issue-before-execution policy (#368): active contract paths require a GitHub issue before
actionable work and no longer allow a local-only substitute."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = [f'catalog/agents/{r}/PLAYBOOK.md' for r in ('manager', 'developer', 'code-reviewer', 'verifier')]
EXPORTED = ['catalog/base/BASE.md', 'catalog/base/AGENTS.md.tmpl', 'docs/design/team-work-tracking.md',
            'docs/operations/task-ownership-and-follow-through.md', 'templates/task.md', *ROLES]
# These paths are not part of the public export; they are checked only in the development repository.
DEV_ONLY = ['AGENTS.md', 'README.md', 'docs/design/github-work-ledger.md',
            'packages/development-team-v1/work/README.md', 'packages/project-manager-v1/agent/PROFILE.md']
IN_DEV = (ROOT / 'docs/design/github-work-ledger.md').exists()
RULE_FILES = EXPORTED + (DEV_ONLY if IN_DEV else [])
INQUIRY = re.compile(r'short read-only (informational )?inquir', re.I)
FORBIDDEN = [
    'Projects without GitHub can keep the local workflow',
    '## Optional GitHub mode',
    "Authoritative record is in the project's chosen task ledger",
    'authoritative local task record',
    'if tracking is local without remote integration',
    'or local task record embedded',
    '(or in the local task record)',
    '(or local record)',
    'For local use a\ntask ledger',
    'This requires an issue even when ordinary tasks use local tracking',
]


def read(rel):
    return (ROOT / rel).read_text(encoding='utf-8').replace('\r\n', '\n')


class IssueBeforeExecutionPolicy(unittest.TestCase):
    def test_rule_is_reachable_everywhere(self):
        for rel in RULE_FILES:
            self.assertIn('github issue', read(rel).lower(), rel)

    def test_inquiry_exception_in_base_and_every_role(self):
        for rel in [*(['AGENTS.md'] if IN_DEV else []), 'catalog/base/BASE.md', 'catalog/base/AGENTS.md.tmpl', *ROLES]:
            self.assertRegex(read(rel), INQUIRY, rel)

    def test_canonical_policy_covers_reuse_outage_and_privacy(self):
        text = read('docs/operations/task-ownership-and-follow-through.md')
        for needle in ('before** execution starts', '**Reuse first**', 'child issue', 'Normal is implicit',
                       'Priority never grants approval to start', 'pending-sync', 'without creating duplicates',
                       'setup blocker', 'does not infer publication rights', 'Never publish private content',
                       'never substitute for the issue'):
            self.assertIn(needle, text, needle)

    def test_no_local_only_substitute_wording_remains(self):
        for rel in RULE_FILES:
            text = read(rel)
            for phrase in FORBIDDEN:
                self.assertNotIn(phrase, text, f'{rel}: {phrase}')

    def test_label_guidance_stays_within_approved_twelve(self):
        approved = {'state:ready', 'state:active', 'state:review', 'state:blocked', 'state:needs-owner', 'state:backlog',
                    'future', 'type:epic', 'type:story', 'type:task', 'priority:high', 'priority:low', 'state:*', 'type:*'}
        for rel in RULE_FILES:
            text = ' '.join(read(rel).split())
            for sentence in re.split(r'(?<=[.;])\s', text):
                for token in re.findall(r'\b(?:state|type|priority|kind|bot):[\w*-]+', sentence):
                    if token not in approved:
                        self.assertRegex(sentence, r'(?i)\b(never|no kind|not)\b', f'{rel}: affirmative guidance for {token}')

    def test_normal_priority_prohibition_is_retained(self):
        for rel in ('catalog/agents/manager/PLAYBOOK.md', 'docs/operations/task-ownership-and-follow-through.md'):
            text = ' '.join(read(rel).split())
            self.assertRegex(text, r'(never create priority:normal|priority:normal. is never created)', rel)

    def test_type_label_is_conditional_on_a_clear_type(self):
        for rel in RULE_FILES:
            text = ' '.join(read(rel).split())
            for phrase in ('one type label', 'the appropriate type label'):
                self.assertNotIn(phrase, text, f'{rel}: {phrase}')
            for m in re.finditer(r'\ba type(?::\*)? label', text):
                self.assertIn('clear', text[m.end():m.end() + 80], f'{rel}: unconditional type label')
        for rel in ('templates/task.md', 'catalog/base/BASE.md', 'docs/design/team-work-tracking.md',
                    'catalog/agents/manager/PLAYBOOK.md', 'docs/operations/task-ownership-and-follow-through.md'):
            self.assertIn('the type is clear', ' '.join(read(rel).split()), rel)
        self.assertIn('never blocks creating the issue', ' '.join(read('docs/operations/task-ownership-and-follow-through.md').split()))

if __name__ == '__main__':
    unittest.main()
