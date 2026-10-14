# Documentation Structure

Codex Bot Mode adopts the Open-Dreamdocs category structure, mapping categories to established canonical folders without replacing root instructions, governance, or existing operational documentation.

| Category | Marker | Canonical Mapping in Codex Bot Mode | Contents |
| --- | --- | --- | --- |
| `overview` | core | `README.md`, `public/README.md` | Purpose, project scope, Office startup, and application map. |
| `governance` | core | `AGENTS.md` | Ownership, permissions, Karpathy engineering principles, and persistent manager rules. |
| `context` | core | `docs/context/` | Handoff records and historical conversation context. |
| `design` | core | `docs/design/` | Communication contracts, roster format, work tracking, and agent profile memory. |
| `operations` | core | `docs/operations/` | Team management, Agent Monitor, repository hygiene, and public release procedures. |
| `verification` | core | `docs/verification/` | Independent verification tests, checklists, and template setups. |
| `marketing` | optional | `docs/marketing/` | 5 independent fields: audience/ICP, competitors/alternatives, positioning, value proposition, messaging. |
| `status` | optional | `docs/status/` | Temporary current state, open blockers, and verification dates. |
| `human-materials` | private scaffold | `human-materials/` | Private ignored folder for human-supplied original files with honest recursive index. |
| `templates` | optional | `templates/` | Reusable markdown templates for decisions, facts, procedures, handoffs, and episodes. |

## Rules for Documentation Adoption

1. **Preserve Originals**: Never overwrite, move, or delete established canonical documentation (`docs/context/`, `docs/design/`, `docs/operations/`, `docs/verification/`, root `README.md`, or `AGENTS.md`).
2. **One Canonical Source per Subject**: Route to existing canonical pages rather than duplicating truth.
3. **Private Human Materials**: Original files remain ignored by `human-materials/.gitignore` by default. Do not force-add raw uploads. Only empty template scaffolding is exported publicly.
4. **Marketing Scaffolding & Population Separation**: Structure and topic routes are established in CBM-115; factual dated source population occurs in CBM-116.
