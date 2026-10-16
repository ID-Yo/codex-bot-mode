# Video Descriptions and Timestamps

Framework for generating video descriptions, metadata, and chapter breakdowns.

## Method: Video Timestamps and Chapter Outlines

- **Purpose**: Structure video descriptions with chronological chapters and resource links.
- **Input Types**:
  1. **Timed Input (Timed Transcript or Audio/Video Log)**: Contains explicit timestamps or timecodes.
  2. **Untimed Input (Topic Outline, Script, or Notes)**: Contains topics and sections without timestamps.
- **Strict Grounding Rules**:
  1. **Zero Invented Timings**: Never invent timestamps, start times, or durations to satisfy platform conventions.
  2. **Untimed Fallback**: When provided input lacks timestamps, generate an untimed chapter proposal (ordered section titles and summaries without timecodes).
  3. **No Automatic 00:00 Assumption**: If the source audio/video transcript begins at a later time (e.g. `01:15`), record the actual observed timestamp; do not fabricate a `00:00` chapter.
  4. **Chronological & Span Validation**: Timestamps must be strictly chronological, non-overlapping, and fall entirely within the verified source duration.
  5. **Insufficient Timings Fallback**: If only partial or ambiguous timings are provided, label confirmed timestamps and mark unverified sections as untimed.
- **Output Shape**:
  1. Summary: 1 to 2 paragraphs summarizing video value.
  2. Chapters: List of chapters (with verified timestamps if timed input, or untimed list if untimed input).
  3. Reference Links: Bulleted list of verified tools, documents, or topics mentioned.
