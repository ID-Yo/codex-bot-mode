---
{"document":"competitors","status":"draft","owner":"project-manager","last_reviewed":"2026-10-15"}
---

# Competitors and Alternatives

## Comparison scope
Multi-agent orchestration frameworks and coding assistants operating in local development environments on developer workstations.

## Relevant alternatives
| Alternative | Why the audience chooses it | Limits for this need | Evidence / checked date |
|---|---|---|---|
| Standalone graph orchestration (LangGraph) | Low-level orchestration runtime for building stateful, durable agent applications with persistence ([LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview), accessed 2026-10-15). | Can be used standalone via API or server; does not structure collaboration inside native Codex Desktop chats or project roster files. | Public primary documentation ([LangGraph overview](https://docs.langchain.com/oss/python/langgraph/overview), accessed 2026-10-15). |
| CLI agent teams (Claude Code) | Integrated CLI terminal assistant supporting experimental agent teams where users coordinate teammates directly in the terminal ([Claude Code agent teams](https://code.claude.com/docs/en/agent-teams), accessed 2026-10-15). | Experimental terminal-centric workflow; does not use native Codex Desktop app chats or local Office web rooms. | Public primary documentation ([Claude Code agent teams](https://code.claude.com/docs/en/agent-teams), accessed 2026-10-15). |
| Editor custom agents (VS Code custom agents) | Configurable custom agents defined via .agent.md configuration files with role-specific prompts and handoffs ([VS Code custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents), accessed 2026-10-15). | Tailored to VS Code agent configuration files; does not provide the Codex Bot Mode local Office / Desktop relay environment. | Public primary documentation ([VS Code custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents), accessed 2026-10-15). |
| Other frameworks (AutoGen, CrewAI, Cursor, GitHub Copilot) | Wide ecosystem awareness and community templates. | Uninspected; specific runtime constraints and multi-agent persistence not compared in this repository. | Uninspected alternative; no inspected evidence. |
| Manual multi-chat copy-pasting in Codex Desktop | Zero setup cost; uses the built-in desktop chat interface directly without extra tooling. | Manual copy-pasting across chats lacks structured request tracking with reminders and relies on informal handoffs (operational hypothesis; see [teams.md](../operations/teams.md) for automated team setup). | Operational baseline hypothesis (inspected 2026-10-15). |

## Implications and unknowns
Codex Bot Mode structures multi-agent collaboration directly inside native Codex Desktop chats rather than inventing a competing execution runtime. Market penetration and commercial pricing benchmarks for local-first desktop agent frameworks remain unknown and outside the scope of this repository.

Related fields: [audience](ideal-customer-profile.md), [positioning](positioning.md).
