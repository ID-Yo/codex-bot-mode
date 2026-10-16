# Channel Adapters

Platform-specific formatting, tone, and character constraints.

## Precedence & Configuration Rules
- **Project Precedence**: The project-specific brief, brand voice guidelines, and verified project facts take strict precedence over generic channel conventions.
- **Configurable Settings**: Channel defaults (e.g. hashtag counts, emojis, paragraph layout) are optional starting conventions that are overridden by explicit project brief constraints.

---

## Adapter: LinkedIn (linkedin)
- **Purpose**: Professional network distribution focusing on business, engineering, and career insights.
- **Tone**: Professional, insightful, direct, respectful.
- **Formatting**: Short 1-2 line paragraphs with whitespace. Strong opening hook line before the "see more" cutoff. Bullet points for lists.
- **Hashtags**: Maximum 2 to 3 relevant industry tags at the bottom (omit if project guidelines disallow hashtags).
- **CTA**: Conversational question encouraging practitioner discussion.
- **Fallback**: Standard professional update without bulleted formatting.

---

## Adapter: X / Twitter (twitter-x)
- **Purpose**: Fast-paced, concise public commentary and threaded analysis.
- **Tone**: Concise, punchy, assertive, direct.
- **Single Post**: Under 280 characters. High information density.
- **Thread**: Numbered tweets (1/N to N/N). Hook tweet -> body points -> summary with verified link/CTA.
- **Fallback**: Single punchy summary post.

---

## Adapter: Facebook (facebook)
- **Purpose**: Community-oriented discussion, group updates, and long-form storytelling.
- **Tone**: Conversational, accessible, community-focused.
- **Formatting**: Natural paragraph flow, relatable framing, engaging questions.
- **Fallback**: Direct text announcement.

---

## Adapter: Instagram (instagram)
- **Purpose**: Visual-first engagement paired with informative captions.
- **Tone**: Visual, engaging, clear.
- **Formatting**: Caption paired with image/carousel concept. Bulleted insights. Clear CTA to save, share, or check bio link.
- **Fallback**: Plain caption summarizing visual content.
