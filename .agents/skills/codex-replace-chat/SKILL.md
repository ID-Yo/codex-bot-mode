---
name: codex-replace-chat
description: Replace a participant's broken or too long Codex chat with a new chat for the same agent, keep its memory and history, and update the roster and the team. Use when the owner asks, or propose it when a chat fails or becomes too long.
---

# Replace a participant's chat

The agent keeps its workspace, profile and memory; only the chat changes. Ask the
owner to confirm before creating the new chat. Commands use
`scripts/roster.py --project <absolute project root>`.

1. **Identify:** the participant's bot ID and current chat from `roster.py links`,
   and the reason (broken, too long, other).
2. **Handoff:** read its `agent/STATE.md` and, when the chat still loads, its last
   turns with `read_thread`. Write a short handoff: open requests with links,
   waiting items, next step. Do not copy private conversation beyond that.
3. **Create:** `create_thread` in the same project with the appointment prompt from
   `codex-team-launch` step 4, followed by "Continue from agent/STATE.md. Handoff:"
   and the handoff text. Title it with the display name.
4. **Roster:** `roster.py set-thread <bot> <new threadId> --reason "<reason>"`. The old
   chat moves to history.
5. **Sidebar:** move the new chat into the team's section. Rename the old chat to
   "<display name> (replaced)" so it is not used by mistake. Archive it only if the
   owner asks.
6. **Notify:** send the new link to the team Manager (and the Project Director if
   one exists). Other agents read links from the roster.
7. **Office:** restart Codex Office so it uses the new chat.

Report the old and new links to the owner and update your STATE.md.
