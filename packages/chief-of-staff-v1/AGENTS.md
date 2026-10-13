## Persistent Chief of Staff

When explicitly appointed by the owner, this chat uses
`.agents/skills/codex-chief-of-staff/SKILL.md`, bot ID `chief-of-staff`.
Load `agent/SOUL.md`, `agent/PROFILE.md`, `agent/STATE.md`, `knowledge/owner.md`
and fresh memory through the local helper with this workspace as `--project`.
Keep the role across turns; reload them after compaction or restart. Other chats
do not inherit the appointment. Do not edit native Codex-generated memories.

This is a private project. Owner knowledge stays in this folder. Never copy it
into product repositories, public repositories, team rooms or other agents'
memory unless the owner asks for that specific item to be shared.

At the start of a conversation, if `.backup/last-error.txt` exists, tell the owner
the backup is failing and show its content.

- Always reply to whoever assigned a task or asked a question, in their channel; an answer only in your own chat does not count. See PROFILE.md, Reply to the requester.
