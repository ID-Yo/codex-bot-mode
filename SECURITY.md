# Security Policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub private vulnerability reporting (the **Security** tab, then **Report a vulnerability**). We aim to reply within 7 days.

## What CBM can access

CBM runs locally with your user rights and inside your Codex Desktop. Its scripts read and write files in the projects you choose (agent workspaces, `team/`, `.bot-mode/`) and in the plugin folder. Agents act through Codex chats with the tools and permissions you have given Codex; CBM templates instruct them to ask before appointments, external messages, publishing, spending and deletion, but instructions are not a sandbox. Codex Office listens on localhost only and requires a per-session token. Agent memory is committed with your project: anyone with repository access can read it, so it must never contain secrets.
