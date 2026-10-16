# Editorial Reference Library

Reusable content creation methods, frameworks, channel adapters, and workflow templates for editorial teams.

## Principles

1. **Source Grounding**: Every claim, statistic, and quote must trace directly to provided project sources. Missing facts must be omitted or framed as open questions. Never fabricate personal experiences, client results, or numbers.
2. **Spartan Clarity**: Prefer short, impactful sentences, active voice, and clear paragraph separation. Avoid corporate jargon and inflated setup phrasing.
3. **Anti-AI-Slop Guardrail**: Avoid generic filler words (e.g. delve, tapestry, embark, unlock, game-changer, skyrocket, revolutionize, harness, realm, navigating, landscape).
4. **Selective Loading**: Roles read only the specific method file required for an assigned task rather than loading the entire reference directory into conversational context.
5. **Channel Adaptability**: Base narratives are defined independently of platform rules. Channel adapters configure length, formatting, and link rules without compromising factual integrity.

## Method Catalog

| Module | Method Name | Purpose | Suitable For | Required Inputs | Fallback |
|---|---|---|---|---|---|
| **Extraction** | `summarize` | Condense long source to core thesis and key findings | Executive summaries, overview posts | Source text, target length | Outline of confirmed points and open gaps |
| **Extraction** | `extract-wisdom` | Extract high-value insights, rules, and attributed quotes | Research analysis, podcast takeaways | Source transcript or notes | Bulleted key themes without direct quotes |
| **Framing** | `problem-agitate-solve` | Frame challenge, cost of inaction, and solution | Educational posts, landing pages | Verified problem, consequence, solution | Direct tips without agitation |
| **Framing** | `misconception-vs-reality` | Debunk popular myth with verified fact | Thought leadership, LinkedIn, X | Common myth, verified counter-fact | Straightforward educational listicle |
| **Framing** | `lessons-learned` | Synthesize 5 concrete rules from real experience | Retrospectives, case studies | Project retrospective or milestone data | 3 actionable tips |
| **Framing** | `actionable-tips` | Deliver 3-5 immediate tactical recommendations | Quick social tips, carousels | Tactical domain knowledge | Single focused rule |
| **Framing** | `limiting-beliefs` | Overcome internal resistance or false constraints | Mindset / strategy shifts | Audience belief, factual reframing | Misconception vs. reality |
| **Framing** | `transformation-story` | Structured timeline showing verified progress | Case studies, milestone updates | Verified before/after milestones and dates | Standard project update |
| **Framing** | `engaging-questions` | Spark informed audience reflection and discussion | Community engagement, polls | Core topic, industry dilemmas | Discussion prompt in brief |
| **Offers** | `value-offer` | Formulate clear outcome, deliverables, and CTA | Service announcements, proposals | Verified product/service capabilities | Descriptive service outline |
| **Articles** | `article-outline` | Comprehensive H2/H3 article structure | Long-form blog posts, documentation | Topic, target audience, search intent | Section bullet points |
| **Articles** | `essay-longform` | In-depth reasoned argument with evidence | Thought leadership, deep dives | Detailed thesis, verified evidence | Micro essay |
| **Articles** | `micro-essay` | Compact 250-400 word focused insight | LinkedIn articles, newsletters | Single strong idea, source example | Short social post |
| **Video** | `short-video-hooks` | 3-5 second scroll-stopping hooks and concepts | TikTok, Instagram Reels, Shorts | Video topic, key takeaway | Direct problem question |
| **Video** | `short-video-script` | 30-60 second structured video delivery | Short video production | Core insight, demonstration steps | Hook and outline |
| **Video** | `long-video-script` | 5-15 minute structured video with chapters | YouTube videos, webinars | Full topic outline or research pack | Section-by-section roadmap |
| **Video** | `video-timestamps` | Chronological chapters from actual timed input | Video descriptions, show notes | Timed transcript or timecoded outline | Untimed chapter breakdown |

## Channel Adapters (`methods/channels.md`)

- **LinkedIn**: Professional, whitespace-separated paragraphs, strong hook before "see more", 2-3 hashtags maximum.
- **X (Twitter)**: High density, under 280 characters for single post, or numbered threads.
- **Facebook**: Conversational, community-focused, natural storytelling.
- **Instagram**: Visual concept pairing, concise captions, bulleted insights, bio link CTA.

## Templates (`templates/`)

- `brief.md`: Standard input contract from Strategist to Writer.
- `draft-review.md`: Criteria evaluation form for Content Reviewer.
- `calendar.md`: Editorial calendar tracking.
- `performance-note.md`: Qualitative feedback tracking.
