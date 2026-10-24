"""Launch teams and appoint agents from the catalog.

Every install needs --confirm, given only after the owner approved the plan.
Agent additions also need --approval once (the owner approved this case) or
--approval standing:<id> covered by team/approvals.yaml.

Examples:
  python -B scripts/team.py catalog
  python -B scripts/team.py questions --team-template development
  python -B scripts/team.py plan-team development --project <project>
  python -B scripts/team.py install-team development --project <project> --tracking-mode github --approval once --confirm
  python -B scripts/team.py add-agent --project <project> --team Development --template blank --answers a.json --actor manager --approval once --confirm
  python -B scripts/team.py dreamdocs-offer --project <project> [--set yes|no|skip] [--owner-change]
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_room import dreamdocs_offer, manifest, teams  # noqa: E402


def answers(path):
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise teams.TeamError('Answers must be a JSON object')
    return data


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--catalog', help='Catalog path or github:owner/repo@ref (default: this repository)')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('catalog', help='List agent and team templates.')
    q = sub.add_parser('questions', help='Show the setup questions and defaults of a template.')
    q.add_argument('--team-template')
    q.add_argument('--template')
    for name in ('plan-team', 'install-team'):
        p = sub.add_parser(name, help='Dry run of a team launch.' if name == 'plan-team' else 'Launch a team.')
        p.add_argument('template')
        p.add_argument('--project', default='.')
        p.add_argument('--answers', help='JSON: {"team": {...}, "agents": {"<role>": {...}}}')
        p.add_argument('--tracking-mode', choices=['github', 'local', 'both'])
        p.add_argument('--tracking-authority', choices=['github', 'local'])
        if name == 'install-team':
            p.add_argument('--approval', required=True)
            p.add_argument('--actor', default='owner')
            p.add_argument('--confirm', action='store_true')
    for name in ('plan-agent', 'add-agent'):
        p = sub.add_parser(name, help='Dry run of adding one agent.' if name == 'plan-agent' else 'Add one agent to a team.')
        p.add_argument('--project', default='.')
        p.add_argument('--team', help='Team name (not used for the director template)')
        p.add_argument('--template', required=True)
        p.add_argument('--answers', help='JSON object with parameter answers')
        if name == 'add-agent':
            p.add_argument('--approval', required=True)
            p.add_argument('--actor', required=True, help='owner, chief-of-staff, director or a manager bot ID')
            p.add_argument('--proposed-by')
            p.add_argument('--confirm', action='store_true')
    d = sub.add_parser('dreamdocs-offer', help='Show or record the owner\'s answer to the optional Open Dreamdocs offer.')
    d.add_argument('--project', default='.')
    d.add_argument('--place', choices=dreamdocs_offer.PLACES, default='team-launch')
    d.add_argument('--set', choices=dreamdocs_offer.CHOICES, help='Record the owner\'s explicit answer.')
    d.add_argument('--owner-change', action='store_true', help='Change an earlier Yes or No; only when the owner asks.')
    d.add_argument('--adopted-tag', help='Stable release tag adopted after the owner approved the install scope.')
    d.add_argument('--adopted-commit', help='Full commit of that tag.')
    a = sub.add_parser('approvals', help='List standing approvals.')
    a.add_argument('--project', default='.')
    args = parser.parse_args(argv)
    try:
        if args.action == 'catalog':
            catalog, label = teams.fetch(args.catalog)
            emit({**teams.index(catalog), 'source': label})
        elif args.action == 'questions':
            catalog, _ = teams.fetch(args.catalog)
            if args.team_template:
                _, spec = teams.team_template(catalog, args.team_template)
                out = {'team': teams.questions(spec.get('parameters')), 'agents': {}}
                for role in spec['roles']:
                    out['agents'][role] = teams.questions(teams.agent_template(catalog, role)[1].get('parameters'))
                emit(out)
            elif args.template:
                emit(teams.questions(teams.agent_template(catalog, args.template)[1].get('parameters')))
            else:
                raise teams.TeamError('Pass --team-template or --template')
        elif args.action in ('plan-team', 'install-team'):
            given = answers(args.answers)
            given.setdefault('team', {})
            for key in ('tracking_mode', 'tracking_authority'):
                if getattr(args, key) is not None:
                    given['team'][key] = getattr(args, key)
            plan = teams.plan_team(args.project, args.template, given.get('agents'), args.catalog, given.get('team'))
            if args.action == 'plan-team':
                emit(teams.summary(plan))
                return 0
            approval = teams.check_approval(args.project, args.approval, args.actor, 'team', plan['team'], args.template)
            if not args.confirm:
                raise teams.TeamError('Review plan-team with the owner, then repeat with --confirm')
            result = teams.install(plan)
            teams.record_use(args.project, approval, args.actor, f'team {plan["team"]}')
            emit({'installed': result, 'section': plan['section'], 'room': plan['room'],
                  'director_proposal': plan['director_proposal']})
        elif args.action in ('plan-agent', 'add-agent'):
            extra = None
            if args.action == 'add-agent':
                extra = {'approved_by': 'owner' if args.approval == 'once' else args.approval,
                         'proposed_by': args.proposed_by or args.actor}
            plan = teams.plan_agent(args.project, args.team, args.template, answers(args.answers), args.catalog, extra)
            if args.action == 'plan-agent':
                emit(teams.summary(plan))
                return 0
            approval = teams.check_approval(args.project, args.approval, args.actor, plan['kind'], args.team, args.template)
            if not args.confirm:
                raise teams.TeamError('Review plan-agent with the owner, then repeat with --confirm')
            result = teams.install(plan)
            teams.record_use(args.project, approval, args.actor, f'{plan["kind"]} {result[0]["bot"]}')
            emit({'installed': result})
        elif args.action == 'dreamdocs-offer':
            if args.adopted_tag or args.adopted_commit:
                if not (args.adopted_tag and args.adopted_commit):
                    raise teams.TeamError('Give both --adopted-tag and --adopted-commit')
                dreamdocs_offer.mark_adopted(args.project, args.adopted_tag, args.adopted_commit)
            elif args.set:
                dreamdocs_offer.record(args.project, args.set, args.place, owner_change=args.owner_change)
            emit({'record': dreamdocs_offer.load(args.project),
                  'ask_now': dreamdocs_offer.should_offer(args.project, args.place),
                  'already_present': dreamdocs_offer.already_present(args.project)})
        elif args.action == 'approvals':
            file = teams.approvals_path(args.project)
            emit(manifest.load(file) if file.exists() else {'approvals': []})
        return 0
    except (teams.TeamError, manifest.ManifestError, json.JSONDecodeError, OSError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
