# Research tools for the Researcher profile

Applies to the `research-analyst` catalog profile (display name Researcher). Native web, browser, GitHub and
document access come first; use an optional connector layer only when native tools do not cover the task.

## Check before promising research

1. Which tools are available now (web search, browser, `gh`, document extraction, provided files)?
2. Does each need a login, API key or cookie? Who owns it, and is it approved for this task?
3. What does it cost, and where can the output be read by the requester?
4. If anything is missing, say so and offer the bounded alternative. Do not work around it.

## AgentReach (optional)

AgentReach is a connector layer that routes requests to platform tools and normalizes the access. It is optional.
It does not make every platform reachable and it is not free access to everything.

Verified locally by the General project on 2026-10-14 (read only evidence, one run):

- Windows, Python 3.13, agent-reach 1.5.0 built from a local wheel. The upstream commit and the license were not verified.
- One YouTube video: captions were read with `yt-dlp` (the documented YouTube route) and a Node runtime. The plain `en` track returned HTTP 429; `en-orig` worked on retry. This one result does not show anything about other platforms.
- `agent-reach transcribe` was not tested. It needs a Groq or OpenAI key and sends audio to that provider; price and data policy were not verified.
- Age restricted or member videos can use a browser cookie option. It was not used and needs separate owner consent.
- Other platforms need logins, cookies or extra tools and were not tested. An earlier Reddit failure report is stale; re-verify before relying on it.

## Bounded checks

- Version: record the installed version and where it came from.
- `doctor`: a point in time snapshot. It does not prove that a target works.
- One harmless read per platform you plan to use, with spaced, bounded retries (for example 3 tries with growing waits).
- Record the result with the date; do not reuse an old result for a new claim.

## Fallbacks

- Platform not reachable: use the public web page, the official API through `gh` or fetch, or ask the owner for an export.
- Needs a login or cookie: stop and ask the Manager; owner approval must name the account and the action.
- No captions or transcript: ask for a provided file; a speech to text provider needs a named key and explicit approval.

## Not authorized by this document

Installing software, extracting browser cookies, using or changing provider keys, changing logins, creating chats,
conducting interviews, or claiming support for platforms that were not tested.
