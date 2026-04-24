# Milestone 17: Voice-Controlled Video Editing — Research Spike

**Goal**: Research and produce a feature-matrix comparison of real-time voice AI APIs suitable for conversational video editing (audio stream in → tool calls mid-conversation → spoken response out). Artifact: a decision-ready comparison table, not code.
**Duration**: 1–2 sessions
**Dependencies**: None (M16's `TOOLS` + `OPERATIONS` registry informs the tool-calling requirement, but this spike is independent)
**Status**: Not Started

---

## Overview

SceneCraft's chat agent (`chat.py`) drives the editor via 32 tool calls today — text in, tool calls out. The next frontier is **voice**: the user talks to the agent while looking at the timeline, and the agent calls `add_keyframe`, `update_volume_curve`, etc. mid-conversation while speaking back.

This milestone is a **research spike**. No code. The output is a feature-matrix comparison table covering the viable API stacks, scored against SceneCraft's requirements, with a recommendation.

---

## Research Directive

Evaluate candidate APIs and architectures for real-time voice-controlled video editing. For each candidate, populate the feature matrix below. Sources: official API docs, pricing pages, published latency benchmarks, developer forums, and hands-on playground testing where available.

### Constraints

- **No OpenAI products.** OpenAI is excluded on principle — do not evaluate GPT, Whisper (OpenAI-hosted), OpenAI Realtime API, or any OpenAI service.
- Open-source self-hosted alternatives to OpenAI products (e.g., local Whisper via `faster-whisper`, `whisper.cpp`) ARE acceptable.
- The voice agent must support **function/tool calling mid-conversation** — this is non-negotiable. If an API can only do STT or TTS without integrated tool dispatch, it must be evaluated as part of a hybrid stack (STT → LLM → TTS), not standalone.

### Candidate Stacks to Evaluate

At minimum, evaluate:

1. **Google Gemini Live** — single-provider voice + tool calling
2. **Deepgram STT → Claude → ElevenLabs TTS** — hybrid, best-reasoning stack
3. **Deepgram STT → Claude → Deepgram TTS** — hybrid, single-STT-provider
4. **Deepgram STT → Gemini → ElevenLabs TTS** — hybrid, alternative LLM
5. **AssemblyAI STT → Claude → ElevenLabs TTS** — hybrid, alternative STT
6. **Local Whisper (faster-whisper) → Claude → Coqui/Piper TTS** — fully self-hosted audio, cloud LLM
7. **Convai** — NPC-oriented conversational AI (evaluate fit despite game-engine focus)
8. **Any other viable candidate discovered during research** — add rows as needed

### Feature Matrix Columns

For each candidate stack, populate:

| Column | Description |
|---|---|
| **Stack** | Name / components (STT → LLM → TTS or single-provider) |
| **Tool calling mid-voice** | Native / via LLM / not supported |
| **Voice-to-first-token latency** | End-to-end: user stops speaking → first audio byte of response (ms) |
| **Voice-to-tool-call latency** | User stops speaking → tool call dispatched (ms) |
| **Streaming interface** | WebSocket / HTTP SSE / gRPC / other |
| **Interruption handling** | Can the user interrupt the agent mid-response? How? |
| **Tool schema format** | Anthropic-style / OpenAPI / function declarations / custom |
| **Max concurrent tool calls** | Can it call multiple tools per turn? |
| **Audio format support** | PCM/WAV/Opus/MP3, sample rates |
| **Speaker diarization** | Can it distinguish multiple speakers? |
| **Language support** | English-only or multilingual |
| **Pricing model** | Per-minute / per-token / per-character / free tier |
| **Estimated cost per 10-min session** | Rough $/session at normal conversation pace |
| **Self-hostable** | Fully / partially (STT or TTS only) / cloud-only |
| **Existing SceneCraft deps overlap** | Does the project already use any component? (e.g., `google-genai`, `anthropic`) |
| **Integration complexity** | Low / Medium / High — how much new infra to wire up |
| **Maturity / stability** | GA / beta / alpha / experimental |
| **Notable limitations** | Dealbreakers, missing features, known issues |

### Evaluation Criteria (weighted)

Score each stack 1–5 on these criteria. Weights reflect SceneCraft's priorities:

| Criterion | Weight | Description |
|---|---|---|
| **Tool calling quality** | 5 | Reliability and latency of mid-conversation tool dispatch |
| **Reasoning quality** | 5 | LLM's ability to understand complex editing intent ("make the chorus hit harder") |
| **End-to-end latency** | 4 | Total voice-to-voice round-trip; conversational feel requires <1s |
| **Cost** | 3 | Sustainable for a single-user creative tool, not an enterprise SaaS |
| **Integration effort** | 3 | How much new code / infra to wire into SceneCraft |
| **Interruption support** | 2 | Can the user cut in and redirect? |
| **Self-host potential** | 2 | Can audio processing run locally for privacy / latency? |
| **Existing dep overlap** | 1 | Bonus for reusing `anthropic`, `google-genai`, etc. |

### Deliverable

A single markdown file: `agent/design/local.voice-agent-research.md` containing:

1. The populated feature matrix table (all candidates × all columns)
2. The scored evaluation table (all candidates × all criteria, with weighted totals)
3. A **recommendation** section (1–2 paragraphs): which stack to prototype first and why
4. An **open questions** section: unknowns that can only be resolved by a code spike
5. A **next steps** section: what the prototyping milestone would look like (scope, not plan)

---

## Success Criteria

- [ ] Feature matrix populated for ≥6 candidate stacks
- [ ] Every matrix cell is filled (or explicitly marked "unknown — requires spike")
- [ ] Evaluation scores computed with weighted totals
- [ ] Recommendation is actionable ("prototype X first because Y")
- [ ] Open questions are concrete enough to drive a spike scope
- [ ] No OpenAI products recommended
- [ ] Artifact committed to `agent/design/local.voice-agent-research.md`

---

## Non-Goals

- Writing code
- Building a prototype
- Choosing a final architecture (that's the next milestone, informed by this research)
- Evaluating video-generation or image-generation APIs
- Evaluating text-only chat improvements

---

## Related Artifacts

- `src/scenecraft/chat.py` — existing text-based chat agent (32 tools); the voice agent wraps this
- `agent/specs/local.openapi-tool-codegen.md` — M16 codegen produces the `TOOLS` list the voice agent would consume
- `src/scenecraft/chat_tools_generated.py` (future, from M16) — generated tool registry
