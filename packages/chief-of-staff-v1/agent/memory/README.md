# Memory contract

Learned memory lives in `agent/memory/` of this private project. The clean
template contains only this contract. The helper creates `summary.md` and
`episodes/*.json` after validated saves; `pending/` holds outcome files awaiting
validation.

Lessons need a stable key, a relative source in this project and an exact quote.
Owner facts belong in `knowledge/` first; lessons point to them. Changed or deleted
sources suppress stale lessons. Reusing a key replaces active recall and keeps
older episodes. One helper writer at a time (`writer.lock`). Native Codex-generated
memory is separate.
