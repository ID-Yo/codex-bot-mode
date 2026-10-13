# Contributing to Codex Bot Mode

Thanks for helping. Small, focused changes are easiest to review.

1. Open an issue first for anything larger than a small fix, so we can agree on the approach. New catalog roles are welcome: describe the role, its typical tasks, what "done" means and how its work is checked.
2. Fork the repository, create a branch and make the change.
3. Run the tests: `python -m unittest discover -s tests` and the privacy check `python -B scripts/public_audit.py .`.
4. Open a pull request and fill in the template.

Rules:

- One topic per pull request; add or update tests for logic changes.
- Behaviour that depends on Codex Desktop (creating chats, sidebar, messages between chats) cannot be unit tested: say how you checked it and with which Codex version.
- No secrets, personal data, local absolute paths or chat IDs in the repository.
- Follow [AGENTS.md](AGENTS.md); it applies to people too.
