# Spec: Engine Provider Integration Surface — Typed Providers + Legacy call_service + Direct-SDK Clients

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Ready for Proofing

---

**Purpose**: Codify the **current, messy, divergent** state of how scenecraft-engine talks to external generative AI and LLM providers. Today the engine runs nine distinct provider integrations across three incompatible patterns (typed `plugin_api.providers`, legacy `call_service` shim, direct-SDK clients). Five of seven remote-cost providers bypass the spend ledger entirely. This spec freezes what exists today so downstream specs, audits, and migrations have a concrete reference to point at — it is **not** an aspirational unification.

**Source**: `--from-draft` reconstructing from audit-2 report §1F, §3 leak #1, and direct code reads. See audit-2 (`agent/reports/audit-2-architectural-deep-dive.md`) and per-file references below.

> **NOTE**: Replicate provider behavior (`plugin_api.providers.replicate`) is already specced on the scenecraft side at `agent/specs/local.replicate-provider.md`. This spec **references** it rather than re-specifying it — Section "Replicate Provider (Typed)" below lists only the integration-surface facts needed to reason about the *set* of providers as a whole.

---

## Scope

### In-Scope

- Provider 1 — **Replicate** (typed; `plugin_api/providers/replicate.py`) — surface reference only
- Provider 2 — **Musicful** (legacy `call_service` shim via `SERVICE_REGISTRY`; consumed by `generate_music` plugin)
- Provider 3 — **Google Imagen** (direct SDK via `GoogleVideoClient.generate_image` → `client.models.generate_images`)
- Provider 4 — **Google Veo** (direct SDK via `GoogleVideoClient` video-generation paths)
- Provider 5 — **Kling** (`KlingClient`; direct urllib; Replicate-hosted but called without the typed Replicate provider)
- Provider 6 — **Runway** (`RunwayVideoClient`; direct urllib against `api.dev.runwayml.com`)
- Provider 7 — **Anthropic** (multiple direct entry points: `ai/provider.py::AnthropicProvider`, `chat.py::_stream_response`, `render/narrative.py`, `render/transition_describer.py`, `api_server.py` inline, `audio_intelligence.py`)
- Provider 8 — **Google GenAI** (direct SDK used in `audio_intelligence.py` for narrative descriptions — the *non-video* Gemini surface; distinct instantiation from Imagen/Veo which share the same SDK)
- Core 9 — **Spend Ledger Writer** (`plugin_api.record_spend`) — the single allowed write path to `server.db::spend_ledger`; all providers either use it or don't
- For each provider: interface shape, auth env var, spend-tracking presence, retry policy, rate-limit backoff, error hierarchy, disconnect-survival behavior
- The three integration patterns (typed / legacy shim / direct-SDK) and which provider sits in which pattern

### Out-of-Scope (Non-Goals)

- **Aspirational unification** — the plan to route Imagen/Veo/Kling/Runway/Anthropic/GenAI through `plugin_api.providers` belongs in a future design doc, not this snapshot
- **Replicate provider internals** — fully specced at `scenecraft:agent/specs/local.replicate-provider.md`; we only reference its public surface here
- **`record_spend` internals** — ledger-writer is specced at `scenecraft:agent/specs/local.spend-ledger.md` (referenced, not re-specced)
- **Plugin-side business logic** — e.g., `generate_music._poll_worker` behavior beyond its observable provider calls (specced in the music-generation plugin spec)
- **Broker mode for `call_service`** — stubbed today (`ServiceConfigError` is raised when the env var is missing); brokered routing through scenecraft.online is a future milestone
- **Prompt content, model selection heuristics, or safety-filter handling logic** — covered by generation-pipeline specs
- **Frontend-visible balance displays** — covered by panel specs
- **Kling upstream billing reconciliation** — Kling predictions run on Replicate infrastructure and are charged to the Replicate account, but no spend-ledger row is written today; see OQ-2
- **Fixing any of the `undefined` behaviors** — every one is flagged as an Open Question with a link from the Behavior Table

---

## Requirements

### Pattern taxonomy

- **R1**: Exactly three integration patterns exist in the engine today:
  1. **Typed provider** — module under `scenecraft.plugin_api.providers`, exporting a named public surface (`run_prediction`, `attach_polling`, `get_balance`, etc.), owning auth + HTTP + polling + backoff + spend-ledger attribution + download. Currently: Replicate only.
  2. **Legacy `call_service` shim** — a string-keyed entry in `SERVICE_REGISTRY = {service_name: (base_url, env_var, auth_header_name)}`, consumed via `plugin_api.call_service(service=..., method=..., path=..., ...)`. The shim handles BYO-mode auth by reading the env var and attaching the configured header. It does NOT poll, does NOT manage spend, does NOT download artifacts; callers do all of those themselves. Currently: Musicful only.
  3. **Direct-SDK / direct-HTTP client** — a freestanding class or module that owns everything inline: reads env var, instantiates the provider SDK (or builds raw HTTP requests), polls, retries, downloads. It bypasses `plugin_api.providers` and `plugin_api.call_service` entirely. Currently: Imagen, Veo, Kling, Runway, Anthropic, GenAI.
- **R2**: Seven of the nine provider units charge real money. Of those seven: **one** (Replicate) writes to `spend_ledger` automatically via its typed provider; **one** (Musicful) writes to `spend_ledger` from plugin code after the poll-worker completes; **five** (Imagen, Veo, Kling, Runway, Anthropic, GenAI) write **zero** rows to `spend_ledger`. Total tracked: 2/7. Untracked: 5/7 (Imagen, Veo, Kling, Runway, Anthropic, GenAI — the audit count; the "5 of 7" phrasing in the audit counts Imagen+Veo as one "Google" entry; this spec counts them separately so the number rendered here is 6 untracked providers out of 8 cost-bearing direct-external units).
- **R3**: The spend-ledger write path is `plugin_api.record_spend()` and only that function. Per invariant R9a, plugins must not write raw SQL to `spend_ledger`. Direct-SDK clients today simply skip the call; they do not bypass by writing raw SQL.

### Replicate Provider (Typed) — reference only

- **R4**: `plugin_api.providers.replicate` exports `run_prediction`, `attach_polling`, `get_balance`, `PredictionResult`, and the exception hierarchy `ReplicateError` → `{ReplicateNotConfigured, ReplicatePredictionFailed, ReplicateDownloadFailed}`.
- **R5**: Auth env var is `REPLICATE_API_TOKEN`. Missing token raises `ReplicateNotConfigured`.
- **R6**: Spend is recorded exactly once per successful prediction, **before** artifact download (see `local.replicate-provider.md` for why). The ledger row id is surfaced on `PredictionResult.spend_ledger_id` and on `ReplicateDownloadFailed.spend_ledger_id`.
- **R7**: Rate-limit backoff on HTTP calls is `(1s, 2s, 4s)` with a hard cap of 3 retries; `_poll_to_completion` has **no** polling timeout and continues indefinitely until a terminal status is returned (by design — Replicate predictions can take hours).
- **R8**: Disconnect-survival is provided via `attach_polling(prediction_id=..., source=..., on_complete=...)` which resumes polling on an already-created prediction without re-submitting.
- **R9**: Full behavior is specced in `scenecraft:agent/specs/local.replicate-provider.md`; this spec treats all R4–R8 behavior as authoritative there.

### Musicful Legacy Shim

- **R10**: `SERVICE_REGISTRY["musicful"] = ("https://api.musicful.ai", "MUSICFUL_API_KEY", "x-api-key")`. No other providers are currently registered in `SERVICE_REGISTRY`.
- **R11**: Auth env var is `MUSICFUL_API_KEY`. Missing env var raises `ServiceConfigError` from `call_service` (NOT a Musicful-specific exception — the error is keyed on the service-registry entry).
- **R12**: `call_service` uses `httpx` when available; falls back to stdlib `urllib.request` in `_call_service_urllib` when httpx is not installed. Both paths raise the same three-typed exception hierarchy: `ServiceError(status, body)` for HTTP ≥400, `ServiceTimeoutError` for transport timeouts, `ServiceConfigError` for unknown service or missing env var.
- **R13**: `call_service` does NOT retry on 429 or any other status — the caller must implement backoff. The `generate_music` plugin's `_poll_worker` implements a caller-side backoff schedule (`RATE_LIMIT_BACKOFF` constant) that consumes one entry per 429 and finalizes the generation as failed when the schedule is exhausted. Exact backoff sequence is plugin-owned, not shim-owned.
- **R14**: `call_service` does NOT poll; the caller polls by issuing repeated `call_service` calls (the `generate_music` plugin polls `GET /v1/music/tasks` every 5 seconds via a daemon thread).
- **R15**: `call_service` does NOT write to `spend_ledger`. The `generate_music` plugin calls `plugin_api.record_spend(plugin_id="generate_music", amount=<count_of_successful_songs>, unit="credit", operation="generate-music.run", ...)` **once** at poll-worker `_finalize` time, only after pool segments are persisted, and only for the count of successful songs. Failed songs are not billed. `record_spend` exceptions are caught and logged; they never fail the finalize.
- **R16**: There is NO polling timeout on the Musicful poll worker — it loops `while pending: time.sleep(POLL_INTERVAL_SECONDS)` until every task reaches a terminal Musicful status or the backoff queue is exhausted on 429s. Stuck tasks (no terminal status, no 429) loop indefinitely. See OQ-5.
- **R17**: Disconnect-survival: the poll worker is a **daemon thread** spawned from `_exec_generate_music_run` and survives WS disconnect. No `attach_polling`-style re-entry path exists; on engine restart, the worker is gone and the generation is stranded in in-flight state in `music_generations` sidecar table with no recovery. (This matches the invariant "Generation jobs survive disconnect" — but only across WS disconnects, not across engine restarts.)

### Google Imagen (Direct-SDK)

- **R18**: Instantiation: `GoogleVideoClient(vertex=False)` reads `GOOGLE_API_KEY`; `GoogleVideoClient(vertex=True)` uses ADC (Application Default Credentials) via `GOOGLE_CLOUD_PROJECT`. Missing `GOOGLE_API_KEY` in AI-Studio mode raises `ValueError`; missing `GOOGLE_CLOUD_PROJECT` in Vertex mode raises `ValueError`.
- **R19**: `generate_image` calls `client.models.generate_images(model=..., prompt=..., config=...)` via `_retry_on_429`.
- **R20**: Rate-limit backoff: `_retry_on_429` uses exponential backoff `(2, 4, 8, 16, 32)` seconds for up to `max_retries=5` attempts. After 5 attempts, it sleeps **60 seconds** and resets the retry counter, looping indefinitely on persistent 429/`RESOURCE_EXHAUSTED` errors. There is no terminal give-up. See OQ-1.
- **R21**: Non-429 exceptions bubble up unmodified from the SDK. No typed provider exception hierarchy exists for Imagen.
- **R22**: Spend tracking: **none**. `record_spend` is never called for Imagen generations.
- **R23**: Disconnect-survival: none. Generation runs synchronously in the calling thread; on the calling thread's cancellation, the SDK call may or may not complete depending on SDK internals. No attach/resume path.
- **R24**: The same `_generate_image_replicate` codepath exists as a fallback that goes through `replicate.run(...)` directly (the stdlib `replicate` SDK), **not** through `plugin_api.providers.replicate` — so it also does not write to `spend_ledger`. This is a leak of the typed-provider boundary.

### Google Veo (Direct-SDK)

- **R25**: Same `GoogleVideoClient` instance as Imagen; same auth mechanism (R18).
- **R26**: Video generation uses `_retry_video_generation(generate_fn, client, output_path, max_retries=8, on_status=...)`. Inside:
  - Per-attempt polling timeout: **10 minutes** (`if elapsed > 600: raise TimeoutError`).
  - Per-attempt poll interval: **10 seconds**.
  - Inter-attempt backoff on retryable errors: `min(2^(attempt+1), 60)` seconds.
  - Non-retryable: `PromptRejectedError` on "safety" / "blocked" / "filtered" substrings in the operation error, or on 6+ consecutive `None` results.
  - Retryable: 429, `RESOURCE_EXHAUSTED`, `"timed out"` (case-insensitive).
- **R27**: `_retry_on_429` (used by image paths) is ALSO in scope for any Veo helper call that wraps it. It has the same "sleep 60s and reset" infinite-retry loop as R20. See OQ-1.
- **R28**: Spend tracking: **none**.
- **R29**: Disconnect-survival: none. Synchronous, in-thread. However, `chat_generation.py` spawns `generate_fn` inside a daemon thread (see generation-pipelines spec), which survives WS disconnect but NOT engine restart.
- **R30**: Runway is used as an explicit fallback for Veo 3.1 (see R35) — the dispatch between Veo-native and Runway-backed Veo happens in the calling code (`chat_generation.py` / `narrative.py`), not inside `GoogleVideoClient`.

### Kling (Direct-HTTP)

- **R31**: `KlingClient(api_token=...)` reads `REPLICATE_API_TOKEN` (yes — Kling models run on Replicate infrastructure via `api.replicate.com/v1/models/kwaivgi/kling-v3-omni-video/predictions`). Uses stdlib `urllib.request`, NOT the `replicate` SDK, and NOT `plugin_api.providers.replicate`.
- **R32**: HTTP polling: `_wait_for_prediction(prediction, poll_interval=5, timeout=600)` polls every 5 seconds with a hard **600-second timeout** that raises `TimeoutError`. On Replicate status `"failed"` or `"canceled"`, raises `RuntimeError(f"Kling prediction failed: {error}")`. No typed exception hierarchy.
- **R33**: Rate-limit backoff: **none**. A 429 at either POST-submission or GET-poll time surfaces as an `HTTPError` from `urllib.request.urlopen`, which the method does not catch (at poll time) or which raises from `_post` at submit time.
- **R34**: Spend tracking: **none from the scenecraft side**. Replicate charges the account that owns `REPLICATE_API_TOKEN`, but no row is written to scenecraft's `spend_ledger`. See OQ-2.
- **R35**: Runway/Veo-fallback and Kling are distinct providers; Kling is used for its own generation paths (`generate_segment`, `generate_from_image`) invoked by generation code.

### Runway (Direct-HTTP)

- **R36**: `RunwayVideoClient(api_key=..., model="veo3.1_fast")` reads `RUNWAY_API_KEY`. Missing key raises `RuntimeError("RUNWAY_API_KEY not set")`.
- **R37**: Uses stdlib `urllib.request` against `api.dev.runwayml.com/v1/image_to_video`. Poll interval: 10 seconds. There is **no polling timeout** — the `while True` loop continues until the task reaches `SUCCEEDED` or `FAILED`. `THROTTLED`, `PENDING`, `RUNNING` all continue polling; unknown statuses log and continue.
- **R38**: Error handling: `HTTPError` during poll is logged and **swallowed** (the poll loop continues). `HTTPError` during submission raises `RuntimeError(f"Runway API error {e.code}: {body}")`. No typed exception hierarchy. No 429-specific handling.
- **R39**: Spend tracking: **none**.
- **R40**: Disconnect-survival: none. Synchronous.

### Anthropic (Direct-SDK, multiple call sites)

- **R41**: Auth env var is `ANTHROPIC_API_KEY`. At least **six** distinct code paths instantiate Anthropic clients directly (per grep against `src/scenecraft/`):
  - `ai/provider.py::AnthropicProvider` — sync `anthropic.Anthropic(api_key=api_key)`; `complete(system, user)` wrapper for `messages.create`; default model `claude-sonnet-4-20250514`, `max_tokens=16384`.
  - `chat.py::_stream_response` — async `anthropic.AsyncAnthropic(api_key=api_key)`; streaming via `client.messages.stream(...)`; model and params set elsewhere in chat.py; returns tool-use events to the chat pipeline.
  - `audio_intelligence.py` (lines ~1063, ~1246) — sync `anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))` for narrative-description passes.
  - `render/narrative.py` (lines ~606, ~715) — sync `Anthropic()` for transition-action generation and slot keyframe generation; explicit `raise RuntimeError("ANTHROPIC_API_KEY required for ...")` when the env var is missing.
  - `render/transition_describer.py` — sync `Anthropic()`.
  - `api_server.py` inline (~line 1659) — sync `Anthropic()` used from a REST handler.
- **R42**: Rate-limit backoff: **none from scenecraft's side**. Whatever retry logic exists is the SDK's internal default.
- **R43**: Error handling: exceptions bubble up from the SDK; no typed wrapper.
- **R44**: Spend tracking: **none**.
- **R45**: Token rotation mid-stream: the key is read once at client instantiation time from the environment; for the WS chat case (`_stream_response`), the client is created at the start of each streaming turn. If `ANTHROPIC_API_KEY` changes in the environment after instantiation, an in-flight stream continues with the old key. See OQ-3.
- **R46**: Disconnect-survival: the async streaming loop in `chat.py` is bound to the WS task. On WS disconnect, the async task is cancelled (see chat-pipeline spec). Partial output is persisted to `chat_messages` with `interrupted: true`. There is no attach/resume path — a reconnecting client cannot re-attach a mid-stream generation.

### Google GenAI (Direct-SDK — narrative descriptions)

- **R47**: Used in `audio_intelligence.py` for Gemini-driven narrative chunk descriptions (`_gemini_describe_chunk`, `_gemini_describe_chunk_structured`). Auth via `GOOGLE_API_KEY`. Separate `genai.Client(api_key=...)` instantiation per chunk (not a shared client).
- **R48**: Model names in use: `gemini-2.5-flash`, `gemini-2.5-pro`. The `google-genai` SDK version is **not pinned in the spec** and is not documented anywhere we found. See OQ-4.
- **R49**: Error handling: broad `except Exception` around the API call in `_gemini_describe_chunk_structured`; on any exception the chunk is logged as skipped and `None` is returned. No retry, no backoff, no rate-limit handling, no typed exceptions.
- **R50**: Missing `GOOGLE_API_KEY` is soft-failed: log "GOOGLE_API_KEY not set — skipping chunk" and return `None`. The `_gemini_describe_chunk` variant does NOT guard this and will instantiate a client with `api_key=None`, deferring the failure to the SDK.
- **R51**: Spend tracking: **none**.
- **R52**: Disconnect-survival: synchronous, in-thread. N/A.
- **R53**: Although Imagen/Veo ALSO use the `google-genai` SDK via `GoogleVideoClient`, the client instances are distinct and do not share config or retry policy with the `audio_intelligence.py` clients.

### Spend Ledger Writer (Core)

- **R54**: `plugin_api.record_spend(*, plugin_id, amount, unit, operation, username='', org='', api_key_id=None, job_ref=None, metadata=None, source='local') -> str` is the single write path to `server.db::spend_ledger`. Returns the ledger row id.
- **R55**: Called outside a scenecraft root (no `SCENECRAFT_ROOT` resolvable) raises `RuntimeError("record_spend called outside a scenecraft root ...")`.
- **R56**: Unit-agnostic: `unit` is a free-form string (`"credit"`, `"prediction"`, `"token"`, `"usd_micro"`, etc.); `amount` is an integer in the smallest atomic unit of `unit`; negative values are refunds.
- **R57**: `plugin_id` trust boundary: the M16 runtime does not verify that the caller's identity matches the claimed `plugin_id` — a plugin could attribute spend to another plugin. A stack-frame / wrapped-handle check is deferred to M17.
- **R58**: Today `record_spend` is called from exactly two places:
  - `plugin_api.providers.replicate._record_spend` (source: "replicate"; unit: "prediction"; amount: 1 per successful prediction; operation: "replicate.run_prediction").
  - `plugins/generate_music/generate_music.py::_finalize` (source: "local"; unit: "credit"; amount: count of successful songs; operation: "generate-music.run").

### Cross-provider behaviors / simultaneity

- **R59**: There is **no per-provider queue, rate-limit bucket, or semaphore** in the engine today. Two plugins calling the same provider simultaneously will each make independent HTTP calls; whatever rate limit the provider enforces is the only coordination. See OQ-6.
- **R60**: All provider calls originate in in-process Python. Cross-process concerns (e.g., the out-of-process MCP server) do not issue provider calls — they delegate back to the engine via HTTP.

---

## Interfaces / Data Shapes

### Typed provider surface (Replicate-shaped template)

```python
# plugin_api.providers.replicate
def run_prediction(
    *,
    model: str,
    input: dict,
    source: str,
    poll_interval: float = 5.0,
) -> PredictionResult: ...
def attach_polling(
    *,
    prediction_id: str,
    source: str,
    on_complete: Callable[[PredictionResult | ReplicateError], None],
    poll_interval: float = 5.0,
) -> None: ...
def get_balance() -> float | None: ...

@dataclass
class PredictionResult:
    prediction_id: str
    status: Literal["succeeded"]
    output_paths: list[Path]
    raw: dict
    spend_ledger_id: str

class ReplicateError(Exception): ...
class ReplicateNotConfigured(ReplicateError): ...
class ReplicatePredictionFailed(ReplicateError): prediction_id: str; error: str
class ReplicateDownloadFailed(ReplicateError): prediction_id: str; spend_ledger_id: str
```

### Legacy `call_service` surface

```python
# plugin_api
SERVICE_REGISTRY: dict[str, tuple[str, str, str]] = {
    "musicful": ("https://api.musicful.ai", "MUSICFUL_API_KEY", "x-api-key"),
}

def call_service(
    *,
    service: str,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    query: dict | None = None,
    timeout_seconds: float = 30.0,
) -> ServiceResponse: ...

class ServiceResponse:
    status: int
    headers: dict
    body: dict | bytes

class ServiceError(Exception): status: int; body
class ServiceConfigError(Exception): ...
class ServiceTimeoutError(Exception): ...
```

### Direct-SDK client surfaces

```python
# scenecraft.render.google_video
class GoogleVideoClient:
    def __init__(self, api_key: str | None = None, vertex: bool = False,
                 project: str | None = None, location: str = "us-central1"): ...
    def stylize_image(self, image_path, style_prompt, output_path, image_model="replicate/nano-banana-2", ...) -> str: ...
    def generate_image(self, prompt, output_path, aspect_ratio="16:9", model="imagen-3.0-generate-002", ...) -> str: ...
    def transform_image(self, image_path, prompt, output_path, ...) -> str: ...
    # Veo video generation entry points (not enumerated here) use _retry_video_generation.

class RunwayVideoClient:
    def __init__(self, api_key: str | None = None, model: str = "veo3.1_fast"): ...
    def generate_video_from_image(self, image_path, prompt, output_path, duration_seconds=8, on_status=None, **kw) -> str: ...
    def generate_video_transition(self, start_frame_path, end_frame_path, prompt, output_path, duration_seconds=8, on_status=None, **kw) -> str: ...

class PromptRejectedError(Exception): ...   # Veo content-safety signal

# scenecraft.render.kling_video
class KlingClient:
    def __init__(self, api_token: str | None = None): ...
    def generate_segment(self, start_frame_path, end_frame_path, prompt, output_path, duration=10, model="kwaivgi/kling-v3-omni-video") -> str: ...
    def generate_from_image(self, image_path, prompt, output_path, duration=10, model="kwaivgi/kling-v3-omni-video") -> str: ...
    # Raises: RuntimeError on failed/canceled; TimeoutError after 600s; urllib.error.HTTPError on non-200 submit.

# scenecraft.ai.provider
class LLMProvider(ABC):
    def complete(self, system: str, user: str) -> str: ...

class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514"): ...
```

### Spend ledger write

```python
# plugin_api
def record_spend(
    *,
    plugin_id: str,
    amount: int,
    unit: str,
    operation: str,
    username: str = "",
    org: str = "",
    api_key_id: str | None = None,
    job_ref: str | None = None,
    metadata: dict | None = None,
    source: str = "local",
) -> str: ...
```

### Provider summary matrix

| # | Provider | Pattern | Auth env var | Retry / backoff | Rate-limit handling | Polling timeout | Spend tracked | Disconnect survival | Typed exceptions |
|---|----------|---------|--------------|------------------|---------------------|-----------------|---------------|---------------------|-------------------|
| 1 | Replicate | Typed | `REPLICATE_API_TOKEN` | 3 × (1s, 2s, 4s) on HTTP; 3× download | 429 → backoff | none (infinite) | **yes**, via provider | `attach_polling` | **yes** (4-class hierarchy) |
| 2 | Musicful | Legacy shim | `MUSICFUL_API_KEY` | none in shim; caller-side schedule in plugin | caller handles 429 | none (R16) | **yes**, via plugin after `_finalize` | WS-disconnect only (daemon thread); not engine-restart | `ServiceError`/`ServiceConfigError`/`ServiceTimeoutError` (generic) |
| 3 | Imagen | Direct-SDK | `GOOGLE_API_KEY` or ADC | exponential 2→32s × 5, then **infinite** 60s cycle | 429/`RESOURCE_EXHAUSTED` → `_retry_on_429` | N/A (sync) | **no** | none (sync) | none |
| 4 | Veo | Direct-SDK | `GOOGLE_API_KEY` or ADC | 8-attempt with 30s/exp backoff; 600s per-attempt poll timeout | 429 retryable; safety/blocked → `PromptRejectedError` | 600s per attempt | **no** | none (sync); calling code may run in daemon thread | `PromptRejectedError` only |
| 5 | Kling | Direct-HTTP | `REPLICATE_API_TOKEN` | none | none (429 surfaces as `HTTPError`) | 600s hard | **no** (charged to Replicate acct; OQ-2) | none (sync) | none (`RuntimeError` / `TimeoutError`) |
| 6 | Runway | Direct-HTTP | `RUNWAY_API_KEY` | none; poll-error swallowed | none | **none** — infinite poll | **no** | none | none (`RuntimeError`) |
| 7 | Anthropic | Direct-SDK (6 call sites) | `ANTHROPIC_API_KEY` | SDK default | SDK default | N/A | **no** | chat.py: WS task cancel persists partial; else none | none |
| 8 | GenAI (Gemini) | Direct-SDK | `GOOGLE_API_KEY` | none | none (broad `except Exception` → skip chunk) | N/A | **no** | none (sync) | none |
| 9 | Spend Ledger | Core (not a provider) | `SCENECRAFT_ROOT` | — | — | — | writes only | — | `RuntimeError` on no-root |

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Plugin calls `plugin_api.providers.replicate.run_prediction` with a valid token and succeeds | Returns `PredictionResult`; writes one `spend_ledger` row before download | `replicate-typed-success-writes-ledger` |
| 2 | Replicate token missing | `ReplicateNotConfigured` raised; no ledger write | `replicate-missing-token-raises-typed` |
| 3 | Plugin calls `call_service("musicful", ...)` with a valid key and gets 200 | Returns `ServiceResponse(status=200, ...)`; no ledger write from shim | `musicful-shim-returns-response-no-ledger` |
| 4 | Musicful env var missing at `call_service` time | `ServiceConfigError` raised from the shim | `musicful-missing-key-raises-config-error` |
| 5 | Musicful plugin completes a generation with 2 successful songs and 1 failed | `record_spend` called once with `amount=2`, `unit="credit"`, `plugin_id="generate_music"`; failed song incurs no spend | `musicful-finalize-writes-ledger-for-successes-only` |
| 6 | `generate_music` receives 429 during poll | Caller-side backoff consumes one entry from `RATE_LIMIT_BACKOFF`; retries | `musicful-429-backoff-in-plugin` |
| 7 | `generate_music` exhausts 429 backoff | Generation finalized as failed with reason `"rate_limit_exceeded"`; no spend | `musicful-429-exhaustion-no-spend` |
| 8 | Imagen call hits 429 persistently | `_retry_on_429` retries 5× with 2→32s backoff, then sleeps 60s and repeats indefinitely | `imagen-429-retry-then-infinite-cycle` |
| 9 | Imagen call succeeds | Returns output_path; no `spend_ledger` row written | `imagen-success-no-ledger` |
| 10 | `GOOGLE_API_KEY` missing for Imagen (AI Studio mode) | `ValueError("GOOGLE_API_KEY environment variable is required...")` | `imagen-missing-key-raises-valueerror` |
| 11 | Veo returns 6 consecutive `None` results | `PromptRejectedError` raised | `veo-repeated-none-raises-prompt-rejected` |
| 12 | Veo operation error contains "safety" | `PromptRejectedError` raised; no retry | `veo-safety-error-raises-prompt-rejected` |
| 13 | Veo per-attempt poll exceeds 600s | `TimeoutError` raised for that attempt; next attempt retries up to 8 total | `veo-per-attempt-poll-timeout` |
| 14 | Veo call succeeds | Video saved to output_path; no `spend_ledger` row | `veo-success-no-ledger` |
| 15 | Kling prediction fails upstream (Replicate status `"failed"`) | `RuntimeError("Kling prediction failed: ...")`; no typed exception | `kling-failed-raises-runtime-error` |
| 16 | Kling prediction still running after 600s | `TimeoutError("Kling prediction timed out after 600s")` | `kling-timeout-600s` |
| 17 | Kling prediction succeeds | Video downloaded; no `spend_ledger` row on scenecraft side (Replicate charges upstream account) | `kling-success-no-scenecraft-ledger` |
| 18 | Runway submit returns non-2xx | `RuntimeError("Runway API error {code}: {body}")` | `runway-submit-error-raises-runtime-error` |
| 19 | Runway poll returns `HTTPError` | Error logged; loop continues polling | `runway-poll-error-swallowed` |
| 20 | Runway task stuck in `PENDING` forever | Infinite polling — no terminal timeout | `runway-infinite-poll` |
| 21 | Runway task succeeds | Video downloaded; no `spend_ledger` row | `runway-success-no-ledger` |
| 22 | Anthropic streaming chat call starts with key A, env var changes to key B mid-stream | Stream continues with key A; no mid-stream rotation | `anthropic-no-mid-stream-rotation` |
| 23 | Anthropic `ANTHROPIC_API_KEY` missing at `_stream_response` | WS sends `{"type": "error", "error": "ANTHROPIC_API_KEY not configured on server"}` then `{"type": "complete"}` | `anthropic-missing-key-sends-ws-error` |
| 24 | Anthropic call succeeds (any call site) | Response returned; no `spend_ledger` row | `anthropic-success-no-ledger` |
| 25 | WS disconnect during Anthropic streaming | Async task cancelled; partial content persisted to `chat_messages` with `interrupted: true`; no resume path | `anthropic-disconnect-persists-partial` |
| 26 | GenAI `generate_content` raises any exception | Caught; chunk skipped; `None` returned | `genai-exception-returns-none` |
| 27 | GenAI call succeeds | Returns parsed dict / text; no `spend_ledger` row | `genai-success-no-ledger` |
| 28 | `GOOGLE_API_KEY` missing for `_gemini_describe_chunk_structured` | Logs "GOOGLE_API_KEY not set — skipping chunk"; returns `None` | `genai-missing-key-soft-fail` |
| 29 | `GOOGLE_API_KEY` missing for `_gemini_describe_chunk` (unstructured variant) | Client instantiated with `api_key=None`; SDK surfaces the failure | `genai-unstructured-missing-key-hard-fail` |
| 30 | `record_spend` called outside scenecraft root | `RuntimeError("record_spend called outside a scenecraft root ...")` | `record-spend-no-root-raises` |
| 31 | `record_spend` called with `plugin_id="foo"` from a plugin named `"bar"` | Ledger row written with `plugin_id="foo"` (trust boundary not enforced in M16) | `record-spend-trust-boundary-not-enforced` |
| 32 | Two plugins call `plugin_api.providers.replicate.run_prediction` simultaneously | Both independently issue HTTP; no per-provider queue or rate-limit bucket | `replicate-simultaneous-no-queue` |
| 33 | Two plugins call `call_service("musicful", ...)` simultaneously | Same — both independent | `musicful-simultaneous-no-queue` |
| 34 | Plugin attempts to write to `spend_ledger` without going through `record_spend` | Not prevented by code; prevented only by R9a convention | `r9a-convention-only` |
| 35 | Veo 429 cycle — spec-time: when should infinite retry give up? | `undefined` | → [OQ-1](#open-questions) |
| 36 | Kling prediction runs on Replicate infra — is Replicate spend reconciled into scenecraft's ledger? | `undefined` | → [OQ-2](#open-questions) |
| 37 | Anthropic key rotates mid-stream — should the in-flight stream be restarted with the new key? | `undefined` | → [OQ-3](#open-questions) |
| 38 | `google-genai` SDK version pin — what version is guaranteed to work? | `undefined` | → [OQ-4](#open-questions) |
| 39 | Musicful poll worker task never reaches terminal status — should there be a wall-clock give-up? | `undefined` | → [OQ-5](#open-questions) |
| 40 | Simultaneous calls to same provider from different plugins — per-provider queue or shared bucket needed? | `undefined` | → [OQ-6](#open-questions) |

---

## Behavior

### Typed-provider call flow (Replicate)

1. Plugin calls `plugin_api.providers.replicate.run_prediction(model=..., input=..., source=<plugin_id>)`.
2. Module reads `REPLICATE_API_TOKEN`; if missing, raises `ReplicateNotConfigured`.
3. POSTs `/v1/predictions` with 429 backoff `(1, 2, 4)` s × 3 attempts.
4. Polls `GET /v1/predictions/:id` every `poll_interval` seconds (default 5s) with no polling timeout until status ∈ {succeeded, failed, canceled}.
5. On `failed`/`canceled`: raises `ReplicatePredictionFailed`; **no ledger write**.
6. On `succeeded`: calls `plugin_api.record_spend(plugin_id=source, amount=1, unit="prediction", operation="replicate.run_prediction", job_ref=prediction_id, source="replicate")` → captures ledger id.
7. Downloads each output URL with 3× backoff. On exhaust: raises `ReplicateDownloadFailed(prediction_id, spend_ledger_id)` (ledger id preserved so caller can surface it).
8. Returns `PredictionResult(spend_ledger_id=..., ...)`.

### Legacy call_service flow (Musicful)

1. Plugin calls `plugin_api.call_service(service="musicful", method=..., path=..., body=..., timeout_seconds=...)`.
2. Shim looks up `SERVICE_REGISTRY["musicful"]` → base URL, env var name, auth header name.
3. Reads `MUSICFUL_API_KEY`; if missing, raises `ServiceConfigError`.
4. Uses `httpx.request(...)` (or stdlib `urllib.request` fallback if httpx is unavailable) with timeout.
5. On `httpx.TimeoutException` → `ServiceTimeoutError`.
6. On 2xx: parses body (JSON if `content-type` matches), returns `ServiceResponse(status, headers, body)`.
7. On ≥400: raises `ServiceError(status=..., body=...)`.
8. **Caller** (`generate_music` plugin) is responsible for: polling loop, 429 backoff schedule, `record_spend` on finalize, artifact download from `audio_url`.

### Direct-SDK flow (Imagen example)

1. Caller instantiates `GoogleVideoClient(vertex=False)` → reads `GOOGLE_API_KEY`.
2. Caller invokes `generate_image(prompt=..., output_path=..., aspect_ratio=..., model=...)`.
3. Internal `_retry_on_429` wraps the SDK call. On 429: sleep `2**(attempt+1)` up to 32s, retry up to 5. On exhaust: sleep 60s, reset counter, **loop forever**.
4. On non-429 SDK exception: re-raise unmodified.
5. On success: save image; return path.
6. **Never** calls `record_spend`.

### Direct-HTTP flow (Kling example)

1. Caller instantiates `KlingClient()` → reads `REPLICATE_API_TOKEN`.
2. `generate_segment` base64-encodes frames into data URIs, POSTs `{REPLICATE_API}/models/kwaivgi/kling-v3-omni-video/predictions`.
3. `_wait_for_prediction` polls `urls.get` every 5s with 600s cap; raises `TimeoutError` on cap; `RuntimeError` on `failed`/`canceled`.
4. On `succeeded`: `urllib.request.urlretrieve(output_url, output_path)`.
5. **Never** calls `record_spend`. Replicate charges the token owner; scenecraft has no visibility.

### Disconnect-survival matrix

| Provider | Survives WS disconnect | Survives engine restart |
|----------|------------------------|--------------------------|
| Replicate (typed) | yes (daemon threads + `attach_polling`) | partial — `attach_polling` works if the caller persists the prediction_id |
| Musicful (plugin daemon) | yes | no (daemon thread gone; no attach/resume path) |
| Imagen, Veo, Kling, Runway, GenAI | depends on caller (Veo/Imagen runs inside `chat_generation.py` daemon thread → survives WS disconnect) | no |
| Anthropic streaming (`_stream_response`) | no — async task cancelled on disconnect; partial persisted with `interrupted: true` | no |

---

## Acceptance Criteria

- [ ] All nine units (1 typed + 1 legacy + 6 direct + 1 core) are enumerated with their auth env var, retry/backoff policy, spend-tracking status, disconnect-survival behavior, and exception hierarchy (or lack thereof).
- [ ] Every provider's test in the Tests section can be implemented against the current codebase without code changes.
- [ ] Each of the six "undefined" rows in the Behavior Table links to a distinct Open Question.
- [ ] The spec does NOT propose unification — all requirements describe observed behavior.
- [ ] Replicate provider is referenced, not re-specced. R4–R9 point at the scenecraft spec.
- [ ] The provider summary matrix contains one row per unit.
- [ ] Every direct-SDK call site for Anthropic (6 enumerated in R41) is cited with a file path (approximate line ok).

---

## Tests

### Base Cases

#### Test: replicate-typed-success-writes-ledger (covers R4, R6, R58)

**Given**: `REPLICATE_API_TOKEN` is set; mock Replicate returns `status="succeeded"` with a single HTTP output URL pointing at a mock CDN; mock CDN returns a 1 KB file.
**When**: Plugin calls `plugin_api.providers.replicate.run_prediction(model="owner/model", input={"prompt": "x"}, source="generate_foley")`.
**Then** (assertions):
- **returns-result**: Return value is a `PredictionResult` with `status="succeeded"`, non-empty `output_paths`, and a non-empty `spend_ledger_id`.
- **ledger-row-written**: Exactly one row is written to `server.db::spend_ledger` with `plugin_id="generate_foley"`, `unit="prediction"`, `amount=1`, `operation="replicate.run_prediction"`, `source="replicate"`, and `job_ref` equal to the prediction id.
- **ledger-before-download**: The ledger row is written before the artifact download is attempted (observable by ordering in a stub that fails the download).

#### Test: replicate-missing-token-raises-typed (covers R5)

**Given**: `REPLICATE_API_TOKEN` is unset.
**When**: Plugin calls `plugin_api.providers.replicate.run_prediction(...)`.
**Then** (assertions):
- **raises**: Raises `ReplicateNotConfigured` (subclass of `ReplicateError`).
- **no-ledger**: No row is written to `spend_ledger`.

#### Test: musicful-shim-returns-response-no-ledger (covers R10, R11, R12, R15)

**Given**: `MUSICFUL_API_KEY` is set; mock Musicful endpoint returns `{"status": 200, "message": "Success", "data": {...}}` with `content-type: application/json`.
**When**: Plugin calls `plugin_api.call_service(service="musicful", method="POST", path="/v1/music/generate", body={})`.
**Then** (assertions):
- **returns-response**: Returns `ServiceResponse` with `status=200` and parsed JSON body.
- **auth-header-set**: The outbound HTTP request carries header `x-api-key: <MUSICFUL_API_KEY value>`.
- **no-ledger**: No `spend_ledger` row is written by the shim.

#### Test: musicful-missing-key-raises-config-error (covers R11)

**Given**: `MUSICFUL_API_KEY` is unset.
**When**: Plugin calls `plugin_api.call_service(service="musicful", ...)`.
**Then** (assertions):
- **raises**: `ServiceConfigError` is raised.
- **message-mentions-env-var**: Error message contains the string `"MUSICFUL_API_KEY"`.

#### Test: musicful-finalize-writes-ledger-for-successes-only (covers R15, R58)

**Given**: A `generate_music` poll worker has 3 task ids; 2 complete with audio URLs, 1 fails with a non-empty `fail_reason`.
**When**: `_finalize` runs.
**Then** (assertions):
- **one-ledger-call**: `plugin_api.record_spend` is called exactly once.
- **amount-equals-successes**: The call's `amount=2`, `unit="credit"`, `plugin_id="generate_music"`, `operation="generate-music.run"`.
- **metadata-carries-task-ids**: `metadata={"task_ids": [all 3 ids]}`.

#### Test: imagen-success-no-ledger (covers R22)

**Given**: `GOOGLE_API_KEY` is set; mock `google-genai` SDK returns an image.
**When**: `GoogleVideoClient(vertex=False).generate_image(prompt="x", output_path=...)` is called.
**Then** (assertions):
- **returns-path**: Returns the output path string.
- **file-written**: The output path exists on disk after the call.
- **no-ledger**: No `spend_ledger` row is written.

#### Test: imagen-missing-key-raises-valueerror (covers R18)

**Given**: `GOOGLE_API_KEY` is unset and `vertex=False`.
**When**: Code calls `GoogleVideoClient()`.
**Then** (assertions):
- **raises**: `ValueError` is raised.
- **message**: Error message contains `"GOOGLE_API_KEY environment variable is required"`.

#### Test: veo-success-no-ledger (covers R28)

**Given**: Mock Veo operation reaches `done=True` with a non-empty `generated_videos[0]`.
**When**: A Veo video generation call completes.
**Then** (assertions):
- **file-written**: Video saved to `output_path`.
- **no-ledger**: No `spend_ledger` row is written.

#### Test: veo-safety-error-raises-prompt-rejected (covers R26)

**Given**: Mock Veo `operation.error` contains the substring `"safety"`.
**When**: `_retry_video_generation` inspects the operation.
**Then** (assertions):
- **raises**: `PromptRejectedError` is raised.
- **no-retry**: Subsequent attempts are NOT made (loop exits immediately).

#### Test: kling-success-no-scenecraft-ledger (covers R34)

**Given**: Mock Replicate returns `status="succeeded"` with an output URL; download succeeds.
**When**: `KlingClient().generate_segment(...)` is called.
**Then** (assertions):
- **file-written**: Output video downloaded.
- **no-scenecraft-ledger**: No row is written to `server.db::spend_ledger` (the upstream Replicate account is charged, which is outside scenecraft's observability).

#### Test: kling-failed-raises-runtime-error (covers R32)

**Given**: Mock Replicate returns `status="failed"` with an error message.
**When**: `_wait_for_prediction` observes the status.
**Then** (assertions):
- **raises**: `RuntimeError` is raised with a message containing `"Kling prediction failed"`.
- **not-typed**: The exception is NOT a subclass of `ReplicateError` (there is no typed Kling hierarchy).

#### Test: runway-submit-error-raises-runtime-error (covers R38)

**Given**: Mock Runway `POST /v1/image_to_video` returns `HTTPError(code=400, body="bad")`.
**When**: `RunwayVideoClient._run_image_to_video` is invoked.
**Then** (assertions):
- **raises**: `RuntimeError` with message containing `"Runway API error 400: bad"`.

#### Test: runway-success-no-ledger (covers R39)

**Given**: Runway task transitions `PENDING → RUNNING → SUCCEEDED`; output URL downloadable.
**When**: `RunwayVideoClient.generate_video_from_image(...)` is called.
**Then** (assertions):
- **file-written**: Output downloaded to `output_path`.
- **no-ledger**: No `spend_ledger` row is written.

#### Test: anthropic-missing-key-sends-ws-error (covers R41, R46)

**Given**: `ANTHROPIC_API_KEY` is unset; a WS client is connected to a chat session.
**When**: `_stream_response` is entered.
**Then** (assertions):
- **ws-error-frame**: A WS frame is sent matching `{"type": "error", "error": "ANTHROPIC_API_KEY not configured on server"}`.
- **ws-complete-frame**: A WS frame is sent matching `{"type": "complete"}`.
- **no-sdk-call**: `anthropic.AsyncAnthropic` is NOT instantiated.
- **no-ledger**: No `spend_ledger` row written.

#### Test: anthropic-success-no-ledger (covers R44)

**Given**: `ANTHROPIC_API_KEY` set; mock Anthropic SDK returns a simple text response.
**When**: Any of the six call sites (R41) invokes the SDK.
**Then** (assertions):
- **returns-text**: A text response is returned.
- **no-ledger**: No `spend_ledger` row written.

#### Test: genai-exception-returns-none (covers R49)

**Given**: `GOOGLE_API_KEY` set; mock `genai.Client.models.generate_content` raises `RuntimeError("boom")`.
**When**: `_gemini_describe_chunk_structured` is called.
**Then** (assertions):
- **returns-none**: Function returns `None`.
- **no-raise**: No exception propagates to caller.
- **no-ledger**: No `spend_ledger` row written.

#### Test: genai-success-no-ledger (covers R51)

**Given**: `GOOGLE_API_KEY` set; mock Gemini returns a JSON string.
**When**: `_gemini_describe_chunk_structured` is called.
**Then** (assertions):
- **returns-dict**: Returns a parsed dict.
- **no-ledger**: No `spend_ledger` row written.

#### Test: record-spend-no-root-raises (covers R55)

**Given**: `SCENECRAFT_ROOT` is unset and no auto-discovery finds a root.
**When**: Any caller invokes `plugin_api.record_spend(...)`.
**Then** (assertions):
- **raises**: `RuntimeError` with message containing `"record_spend called outside a scenecraft root"`.
- **no-row**: No ledger row is written.

### Edge Cases

#### Test: imagen-429-retry-then-infinite-cycle (covers R20, OQ-1)

**Given**: Mock SDK call raises an exception whose `str(e)` contains `"429"` on every call.
**When**: `_retry_on_429` wraps the call.
**Then** (assertions):
- **five-initial-waits**: Observed `time.sleep` calls are `[2, 4, 8, 16, 32]` during the first 5 attempts.
- **then-60s-cycle**: A 60-second sleep follows, after which the 5-attempt pattern restarts.
- **never-raises-giveup**: The wrapper never raises a "giving up" exception; only the outer caller's thread cancellation can stop it.

#### Test: veo-repeated-none-raises-prompt-rejected (covers R26)

**Given**: Mock Veo operation reaches `done=True` but `operation.result is None` on every retry.
**When**: `_retry_video_generation` runs with default `max_retries=8`.
**Then** (assertions):
- **raises-after-attempts**: After `max_retries` attempts, `PromptRejectedError` is raised.
- **message-mentions-none**: Error message contains `"None result"` or `"empty video list"`.

#### Test: veo-per-attempt-poll-timeout (covers R26)

**Given**: Mock Veo operation never reaches `done=True`; each attempt's polling clock crosses 600s.
**When**: `_retry_video_generation` runs.
**Then** (assertions):
- **per-attempt-timeout**: Each attempt raises `TimeoutError("Veo generation polling timed out after 10 minutes")` after 600s.
- **next-attempt-continues**: The outer retry loop continues to the next attempt up to `max_retries=8`.
- **final-raises**: After 8 attempts, the outer `RuntimeError` is raised.

#### Test: kling-timeout-600s (covers R32)

**Given**: Mock Replicate prediction never reaches terminal status.
**When**: `_wait_for_prediction(..., timeout=600)` runs.
**Then** (assertions):
- **raises**: `TimeoutError` with message containing `"Kling prediction timed out after 600s"`.

#### Test: runway-poll-error-swallowed (covers R37, R38)

**Given**: Mock Runway `GET /v1/tasks/{id}` intermittently raises `HTTPError(500)`; eventually returns `SUCCEEDED`.
**When**: `RunwayVideoClient._run_image_to_video` polls.
**Then** (assertions):
- **poll-continues**: Each transient `HTTPError` during poll is logged and the loop continues.
- **eventually-succeeds**: The method returns the output path when the task ultimately succeeds.

#### Test: runway-infinite-poll (covers R37, OQ-1-adjacent)

**Given**: Mock Runway task stays in `PENDING` indefinitely; monkey-patch `time.sleep` to raise after N iterations.
**When**: `RunwayVideoClient._run_image_to_video` polls.
**Then** (assertions):
- **never-terminal-timeout**: No `TimeoutError` is raised by Runway's client; only the test harness interrupts the loop.
- **loop-does-not-exit-on-pending**: `PENDING` → `RUNNING` → unknown statuses all continue polling.

#### Test: anthropic-no-mid-stream-rotation (covers R45, OQ-3)

**Given**: `ANTHROPIC_API_KEY=keyA` when `_stream_response` instantiates `AsyncAnthropic`; then the env var is set to `keyB` while streaming is in flight.
**When**: The stream continues.
**Then** (assertions):
- **uses-initial-key**: In-flight SDK calls continue using `keyA`.
- **next-turn-reads-env**: A *new* turn (next user message) re-reads the env var and uses `keyB`.

#### Test: anthropic-disconnect-persists-partial (covers R46)

**Given**: A WS chat turn is streaming content and the client disconnects before completion.
**When**: The async task is cancelled.
**Then** (assertions):
- **partial-persisted**: The partial assistant message is persisted to `chat_messages` with `interrupted: true`.
- **no-resume-endpoint**: There is no WS or REST endpoint that lets a reconnecting client re-attach the in-flight stream (the next connection starts a new turn).

#### Test: genai-missing-key-soft-fail (covers R50)

**Given**: `GOOGLE_API_KEY` is unset.
**When**: `_gemini_describe_chunk_structured(chunk_path, ...)` is called.
**Then** (assertions):
- **logs**: A log line contains `"GOOGLE_API_KEY not set"` or `"skipping chunk"`.
- **returns-none**: Function returns `None`.
- **no-sdk-import-failure**: Absence of `google-genai` is also soft-failed (separate early-return path); this test specifically exercises the env-var path.

#### Test: genai-unstructured-missing-key-hard-fail (covers R50)

**Given**: `GOOGLE_API_KEY` is unset.
**When**: `_gemini_describe_chunk(chunk_path, start_time, end_time)` is called.
**Then** (assertions):
- **client-instantiated-with-none**: The `genai.Client(api_key=None)` call is attempted.
- **sdk-raises-or-returns-error**: The SDK's own error surfaces (no soft fail in this variant).

#### Test: record-spend-trust-boundary-not-enforced (covers R57, OQ-6-adjacent)

**Given**: A plugin whose registered identity is `plugin_A` calls `plugin_api.record_spend(plugin_id="plugin_B", ...)`.
**When**: The call executes.
**Then** (assertions):
- **row-written-with-claimed-id**: A ledger row is written with `plugin_id="plugin_B"` (the claimed id).
- **no-identity-check**: No exception is raised; no log warning about identity mismatch.
- **documents-m17-gap**: This test explicitly encodes the M16 behavior that M17 is intended to fix.

#### Test: replicate-simultaneous-no-queue (covers R59, OQ-6)

**Given**: Two plugin threads simultaneously call `plugin_api.providers.replicate.run_prediction(...)` against a mock Replicate server that records inbound request timing.
**When**: Both calls run concurrently.
**Then** (assertions):
- **both-submit**: The mock server observes two independent `POST /v1/predictions` calls with overlapping timing.
- **no-queue-wait**: Neither call blocks on the other; total wall time ≈ max, not sum.
- **independent-ledger-rows**: On success, two distinct `spend_ledger` rows are written.

#### Test: musicful-simultaneous-no-queue (covers R59, OQ-6)

**Given**: Two plugin threads simultaneously call `plugin_api.call_service("musicful", ...)`.
**When**: Both calls run concurrently.
**Then** (assertions):
- **both-submit**: The mock Musicful server observes two independent inbound HTTP calls.
- **no-queue-wait**: Neither call blocks on the other.

#### Test: musicful-429-backoff-in-plugin (covers R13, R14)

**Given**: Mock Musicful `/v1/music/tasks` returns 429 on the first poll, then 200 with in-flight status.
**When**: `_poll_worker` runs one cycle.
**Then** (assertions):
- **shim-does-not-retry**: `call_service` returns/raises after one HTTP attempt (no retry inside shim).
- **plugin-backoff-consumed**: The first entry of the plugin's `RATE_LIMIT_BACKOFF` list is consumed; worker sleeps the configured amount and continues.

#### Test: musicful-429-exhaustion-no-spend (covers R13)

**Given**: Mock Musicful returns 429 on every poll.
**When**: `_poll_worker` runs until `RATE_LIMIT_BACKOFF` is empty.
**Then** (assertions):
- **generation-finalized-failed**: `update_music_generation_status(..., "failed", error="rate_limit_exceeded")` is called.
- **no-spend**: `record_spend` is NOT called.
- **no-pool-segments**: No `pool_segments` are created.

#### Test: r9a-convention-only (covers R3, R54)

**Given**: A hypothetical plugin imports `scenecraft.db` directly (violating R9a).
**When**: The plugin calls a raw DB helper that can write to `spend_ledger`.
**Then** (assertions):
- **no-runtime-block**: Today, nothing at runtime prevents this — R9a is a convention, not enforcement.
- **documents-gap**: This test explicitly encodes the gap for future enforcement work.

---

## Non-Goals

- A design for unifying Imagen/Veo/Kling/Runway/Anthropic/GenAI under `plugin_api.providers`.
- Wiring `spend_ledger` writes for the untracked 6 providers. (The data and model choices — unit names, amounts per unit — are undecided.)
- Adding typed exception hierarchies to direct-SDK clients.
- Introducing per-provider queues, rate-limit buckets, or coordination primitives.
- Fixing the Imagen/Veo infinite-retry cycle (OQ-1) or Runway's absence of polling timeout.
- Specifying a schema for spend_ledger rows beyond the `record_spend` signature.
- Specifying Kling upstream-billing reconciliation.
- Pinning the `google-genai` SDK version.
- Adding a mid-stream token-rotation path for Anthropic.
- Adding a resume/attach path for Anthropic streaming.
- Adding a resume/attach path for Musicful poll workers across engine restarts.

---

## Open Questions

### OQ-1 — Veo / Imagen infinite retry cycle: when should it give up?

The `_retry_on_429` helper (used by Imagen calls) and the Veo `_retry_video_generation` wrapper both have a pattern of retrying indefinitely after the initial attempt schedule is exhausted. `_retry_on_429` sleeps 60s and resets its counter; `_retry_video_generation` has a hard 8-attempt cap, but inside each attempt the 600s poll can itself be interrupted by 429 retries up to 60s each. The net effect is calls can block for hours without terminal failure.

- Should there be a wall-clock maximum per call (e.g. 30 minutes)?
- Should exhaustion raise a typed `RateLimitExhausted` exception that callers can surface to the user?
- Should the backoff be capped at a finite total number of cycles (e.g. 3 full 5-attempt cycles = ~5 minutes)?

### OQ-2 — Kling prediction spend attribution

Kling predictions run on Replicate infrastructure and are billed to the Replicate account that owns `REPLICATE_API_TOKEN`. Scenecraft has access to the same token, but today `KlingClient` does NOT write a `spend_ledger` row — partly because Kling is not registered as a "Replicate provider" (`KlingClient` uses raw `urllib`, not `plugin_api.providers.replicate`), partly because the unit and amount are undecided.

- Should Kling go through `plugin_api.providers.replicate.run_prediction` so spend is tracked uniformly? (Requires Kling to be callable via the standard predictions endpoint — it already is, per R31.)
- If not, should `KlingClient` call `plugin_api.record_spend` directly with `source="replicate"` and a Kling-specific operation id?
- Is Replicate's billing granularity (per-prediction) compatible with the `unit="prediction"` convention Replicate provider uses?

### OQ-3 — Anthropic token rotation mid-stream

If `ANTHROPIC_API_KEY` is rotated while a WS chat stream is active, the in-flight `AsyncAnthropic` client continues with the old key. If the old key has been revoked, the stream may fail mid-turn with an auth error from Anthropic.

- Should mid-stream rotation be detected and a new client instantiated with the new key?
- Is it acceptable for the stream to fail cleanly on revocation (partial message persisted, user sees an error frame)?
- Is there a usage signal (token refresh event, env-var watch) that should trigger rotation without waiting for a failure?

### OQ-4 — `google-genai` SDK version pin

The `google-genai` SDK is imported lazily from multiple files (`GoogleVideoClient`, `_gemini_describe_chunk`, `_gemini_describe_chunk_structured`). There is no version pin documented in `pyproject.toml` that we confirmed, and the models referenced (`gemini-2.5-flash`, `gemini-2.5-pro`, `imagen-3.0-generate-002`) depend on specific SDK support.

- What minimum and maximum `google-genai` versions are compatible with the current code?
- Should this be pinned in `pyproject.toml` with a tested-against range?
- Should there be a startup-time compatibility check that fails fast on incompatible SDK versions?

### OQ-5 — Musicful poll worker task never terminal

If a Musicful task never reaches a terminal status (no `audio_url`, no `fail_reason`, no `fail_code > 0`, no 429), the `_poll_worker` loops indefinitely. There is no wall-clock give-up and no orphan-detection pass.

- Should there be a per-generation wall-clock timeout (e.g., 30 minutes)?
- Should there be a periodic health check that cancels stuck generations?
- Should the Musicful status code itself be inspected more carefully to detect "stuck" states?

### OQ-6 — Simultaneous calls to same provider

Today every provider allows unbounded parallelism from any caller in-process. The engine does NOT implement:

- Per-provider request queues
- Per-provider rate-limit buckets (e.g., Anthropic's 20 req/min default)
- Per-API-key throttling
- Cross-plugin coordination

If two plugins call Anthropic simultaneously and the combined rate exceeds the account's rate limit, both get 429s that today's direct-SDK paths handle independently (mostly poorly). Questions:

- Should `plugin_api.providers` grow a shared bucket per provider?
- Should the bucket be keyed per API key, per plugin, or per account?
- Should there be a system-wide "in-flight provider calls" inspector for debugging?
- How should back-pressure surface to plugins — `BackpressureError`, or silent wait-and-retry?

### OQ-7 — `ai/provider.py` vs `chat.py` Anthropic duplication

`ai/provider.py::AnthropicProvider` is a sync abstraction intended to be the single place Anthropic is called, but six other call sites instantiate Anthropic clients directly. Is `AnthropicProvider` dead code, the target future abstraction, or a parallel implementation that never got adopted? This is not strictly a behavior question — it affects decisions in the future-looking unification design doc.

---

## Related Artifacts

- **Audit source**: `agent/reports/audit-2-architectural-deep-dive.md` §1F (Providers + External SDKs), §3 leak #1 (Provider spend un-tracked for 6 of 7 providers).
- **Referenced spec (Replicate internals)**: `../scenecraft/agent/specs/local.replicate-provider.md`.
- **Referenced spec (spend ledger internals)**: `../scenecraft/agent/specs/local.spend-ledger.md` (if present; otherwise the `record_spend` signature in this spec is the authoritative surface).
- **Related engine specs (planned)**: `engine-generation-pipelines`, `engine-chat-pipeline`, `engine-plugin-loading-lifecycle`.
- **Code pointers**:
  - `src/scenecraft/plugin_api/__init__.py` — `call_service`, `SERVICE_REGISTRY`, `record_spend`, exception hierarchy, `providers` re-export
  - `src/scenecraft/plugin_api/providers/__init__.py` — namespace root; re-exports `replicate`
  - `src/scenecraft/plugin_api/providers/replicate.py` — the only typed provider
  - `src/scenecraft/render/google_video.py` — `GoogleVideoClient` (Imagen + Veo), `RunwayVideoClient`, `_retry_on_429`, `_retry_video_generation`, `PromptRejectedError`
  - `src/scenecraft/render/kling_video.py` — `KlingClient`
  - `src/scenecraft/ai/provider.py` — `AnthropicProvider`
  - `src/scenecraft/chat.py` — `_stream_response` (Anthropic AsyncAnthropic, line ~5334)
  - `src/scenecraft/audio_intelligence.py` — Gemini narrative descriptions (`_gemini_describe_chunk`, `_gemini_describe_chunk_structured`); Anthropic call sites (lines ~1063, ~1246)
  - `src/scenecraft/render/narrative.py` — Anthropic `from anthropic import Anthropic` (lines ~606, ~715)
  - `src/scenecraft/render/transition_describer.py` — Anthropic sync client
  - `src/scenecraft/api_server.py` — Anthropic inline call site (line ~1659)
  - `src/scenecraft/plugins/generate_music/client.py` — Musicful via `call_service`
  - `src/scenecraft/plugins/generate_music/generate_music.py` — `_poll_worker` (R16, R17), `_finalize` (R15)
