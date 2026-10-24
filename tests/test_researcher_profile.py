import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from team_room import teams

CATALOG = ROOT / 'catalog'
AGENT = CATALOG / 'agents' / 'research-analyst'
NEW = ('research_domain', 'research_question', 'scope', 'inputs', 'outputs', 'source_quality')


def read(path):
    return Path(path).read_text(encoding='utf-8')


class ResearcherProfileTests(unittest.TestCase):
    def test_profile_is_the_upgraded_research_analyst_presented_as_researcher(self):
        _, meta = teams.agent_template(CATALOG, 'research-analyst')
        self.assertEqual((meta['role'], meta['version']), ('Research Analyst', 2))
        self.assertEqual(meta['parameters']['display_name']['default'], 'Researcher')
        for name in NEW:
            parameter = meta['parameters'][name]
            self.assertTrue(parameter.get('question') and ('default' in parameter or parameter.get('required')), name)
        self.assertIn('version: 2', '\n'.join(line for line in read(CATALOG / 'index.yaml').splitlines()
                                              if 'research-analyst' in line or line.strip() == 'version: 2'))

    def test_placeholders_match_declared_parameters(self):
        _, meta = teams.agent_template(CATALOG, 'research-analyst')
        known = set(meta['parameters']) | {'role', 'bot', 'team', 'project', 'host', 'display_name'}
        import re
        for name in ('SOUL.md', 'PROFILE.md', 'PLAYBOOK.md'):
            used = set(re.findall(r'{{(\w+)}}', read(AGENT / name)))
            self.assertLessEqual(used, known, name)
        for name in NEW:
            self.assertIn('{{' + name + '}}', read(AGENT / 'PROFILE.md'))

    def test_research_rules_and_tool_checks_are_present(self):
        text = read(AGENT / 'PROFILE.md').lower() + read(AGENT / 'PLAYBOOK.md').lower()
        for phrase in ('primary sources', 'linked', 'date', 'facts from inference', 'conflicting evidence',
                       'limitations', 'manager handoff', 'authentication', 'cost', 'where the output'):
            self.assertIn(phrase, text, phrase)
        self.assertIn('guarantees neither tool access nor correctness', text)

    def test_agentreach_is_optional_and_claims_are_bounded(self):
        doc = read(ROOT / 'docs' / 'operations' / 'research-tools.md').lower()
        for phrase in ('optional', 'native', 'doctor', 'point in time', 'not tested', 'not verified',
                       'not free access to everything', 'cookie', 'owner approval'):
            self.assertIn(phrase, doc, phrase)
        for forbidden in ('all platforms are supported', 'works on every platform'):
            self.assertNotIn(forbidden, doc)
        self.assertIn('research-tools.md', read(AGENT / 'PROFILE.md'))

    def test_lead_researcher_stays_the_sales_qualifier(self):
        _, meta = teams.agent_template(CATALOG, 'lead-researcher')
        self.assertEqual(meta['version'], 1)
        self.assertIn('prospect', read(CATALOG / 'agents' / 'lead-researcher' / 'PROFILE.md').lower())
        self.assertIn('lead researcher', read(AGENT / 'PROFILE.md').lower())


if __name__ == '__main__':
    unittest.main()

