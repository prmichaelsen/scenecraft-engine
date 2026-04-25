# Hosted-Mode Architecture

**Concept**: Migration plan and architecture for transitioning scenecraft from "self-hosted only" to a multi-tenant hosted offering at scenecraft.online, with a defined interim self-hosted-with-auth phase first.
**Created**: 2026-04-25
**Status**: Proposal

---

## Overview

scenecraft is open source. Anyone competent and willing to run the engine themselves can self-host it. The "platform" — scenecraft.online — exists to offer hosted instances to users who don't want to deal with provisioning, ops, or upstream API key management. The platform's value is convenience, not source-code control.

This document defines two phases:

- **Phase 0 (interim, self-hosted with auth)** — what we run today extended with a real auth gate, while the only deployment is the author's own. Closes the immediate `--no-auth` exposure.
- **Phase 1 (hosted mode)** — what changes when scenecraft.online accepts its first paying customer and provisions a multi-tenant `*.scenecraft.online` instance.

The design is deliberately minimal at Phase 0 and only adds the layers that hosted mode actually requires at Phase 1. Items not required until customer #1 are explicitly deferred.

---

## Problem Statement

The engine currently runs with `--no-auth` and the API server is internet-reachable via Cloudflare Tunnel at `https://api.<instance>.scenecraft.online`. Anyone who knows the URL can read and modify any project. That is acceptable for a single-developer dev box but unacceptable the moment a non-author user touches the instance, and definitely unacceptable when the platform charges money for a hosted instance.

In addition, several pieces of the auth/billing model have been left informally decided and would block hosted-mode launch if not pinned down:

- Two distinct user classes exist (platform user = admin of an instance; instance user = end-user on someone's instance) and the current code conflates them.
- Cost attribution: per-instance-user generation cost tracking is half-implemented; the actual billing flow only needs admin-level attribution.
- Provider API keys live in plaintext on each instance (`engine.env`), readable by the admin. For platform-issued keys, that's a key-leakage problem.
- CORS is hardcoded-permissive (origin echoed back, credentials enabled). Works today only because there are no real credentials to leak.
- Source-of-truth for instance users (server.db) exists in concept but has no schema, no password storage, no JWT issuance.

---

## Solution

### User classes

| Class | Lives in | Identifies | Authorizes against |
|---|---|---|---|
| **Platform user** | scenecraft.online's central DB | Person who signed up, has a credit card, owns one or more instances | scenecraft.online platform DB |
| **Instance user** (incl. admin) | The instance's `server.db` | Person logging into a specific `*.scenecraft.online` instance | That instance's `server.db` |

The instance admin is *both* a platform user (for billing/lifecycle) and the highest-privilege instance user (for engine access). They are the only person who appears in both stores. Instance users below admin exist only in `server.db` and are managed by the admin.

Trust boundary: admin has root on the instance and can modify engine code. The platform makes no attempt to defend any data from the admin within their own instance. The platform defends external attackers, defends platform users from other platform users (instance isolation), and provides admins with tooling to defend instance users from other instance users.

### Reconciliation with the platform side (no identity federation)

The platform (scenecraft.online) uses Firebase Auth for its own login. **The engine never sees Firebase tokens, never calls Firebase, and has never heard of Firebase.** The two credential domains are deliberately unfederated:

| Where you log in | Identity store | Token format |
|---|---|---|
| `scenecraft.online` (platform UI: signup, billing, dashboard) | Firebase Auth (platform DB) | Firebase ID token → `__session` cookie |
| `<tenant>.scenecraft.online` (the product UI) | engine's `server.db.users` (argon2id) | HS256 JWT signed with `SCENECRAFT_JWT_SECRET` |

The admin happens to be the same person in both stores, but they have **two separate credentials** — they log into scenecraft.online with their Firebase account, and they log into their instance with the argon2id username/password chosen at signup. Browsers never carry one across origins to the other. Engine never validates Firebase tokens. Platform never issues instance JWTs.

The only coupling between platform and engine is a **per-instance bearer token** issued at provision time (D1 column `instances.bearer_token`, written into `engine.env` as `SCENECRAFT_PLATFORM_BEARER_TOKEN`). It is a machine-to-machine credential, used only by the engine's outbound calls back to scenecraft.online (heartbeat, usage events, BYOP state). It never crosses a browser, never authenticates a user, and never has anything to do with Firebase.

If you find yourself writing code in this repo that involves Firebase, JWK fetching, ID-token forwarding, or membership lookups against scenecraft.online, **stop** — you are reinventing the discarded federation model. The runtime composition is canonically specified in `../scenecraft.online/agent/design/local.per-instance-architecture.md`; consult that doc when uncertain.

### Cost attribution model

All upstream generation costs (Anthropic, Google, Runway, Replicate, Musicful, Vast.ai) are charged to the **admin only**. Per-instance-user data is recorded in `server.db` for auditing and to feed admin-built quota/budget tooling — it is not billing data.

Platform-side billing aggregates by admin → presents a single bill. The admin handles internal allocation and quota enforcement against their instance users themselves (using the audit data the engine emits).

### API key custody

Two modes, controlled by `SCENECRAFT_PROXY_URL` env:

- **Proxy mode** (`SCENECRAFT_PROXY_URL=https://api.scenecraft.online`) — engine routes all provider calls through scenecraft.online. Platform owns the upstream keys, meters every call, applies quota, bills admin. Admin sees only a per-instance proxy token (revocable). Default for hosted instances.
- **Direct mode** (`SCENECRAFT_PROXY_URL=` empty) — engine reads `ANTHROPIC_API_KEY` etc. directly and calls providers. Platform sees nothing. Default for self-hosted.

Engine code is single-path: it always calls "the configured upstream." The choice between proxy and direct is an env-var concern, not a code concern.

### Auth

Phase 0 (interim, today–first hosted customer):

- Engine drops `--no-auth` as the default. New flag is `--no-auth` for local dev only, never set in the systemd unit running on a hosted box.
- `server.db` gains a `users` table.
- Argon2id password hashes (or bcrypt as a tolerable second choice).
- Engine signs JWTs with `SCENECRAFT_JWT_SECRET` from `engine.env`. Claims: `sub`, `is_admin`, `iat`, `exp` (~24h, configurable).
- Frontend sends `Authorization: Bearer <jwt>`.
- Admin username + password are always **inputs to provisioning**, never derived. Hosted: collected at signup, passed in via `engine.env` at provision time. Self-hosted: admin writes them into `engine.env` themselves before first boot. First-run flow: if users table is empty and `SCENECRAFT_BOOTSTRAP_ADMIN_USERNAME` + `SCENECRAFT_BOOTSTRAP_ADMIN_PASSWORD` are both set, engine inserts that admin and ignores those vars on subsequent boots.
- For the **currently running instance** (which predates this design), there is no provisioning step to feed in credentials — admin is backfilled via a CLI subcommand: `python -m scenecraft users create --username admin --password '<pwd>' --admin`. Same code path the bootstrap flow calls into.

Phase 1 (hosted mode) adds:

- A second JWT issuer: scenecraft.online platform JWT, used for platform API calls (instance lifecycle, billing). Independent from instance JWT.
- Provisioning step calls back into the new instance's API to bootstrap the admin user (using the platform-generated token) so the admin can log in immediately at `https://<instance>.scenecraft.online`.
- Instance JWT secret is generated per-instance at provision time and stored in the instance's `engine.env` — platform never holds it.

### CORS

Replace the current "echo any Origin + Allow-Credentials" pattern with an env-driven allowlist:

```python
allowed = set(os.environ.get("SCENECRAFT_CORS_ORIGINS", "").split(",")) - {""}
```

Set at provision time (or by the admin's own `engine.env` for self-hosted) to `https://<instance>.scenecraft.online` plus any local-dev origins the admin needs. No origin = no Access-Control-Allow-Origin header → browser blocks. This is a hard prerequisite for Phase 0.

### Quota enforcement

The engine continues recording per-instance-user generation costs in `server.db`. It does **not** enforce billing. It exposes:

- `/api/usage` (admin-only) — returns aggregated usage per user over a date range.
- `/api/usage/quota` (admin-only) — admin can set monthly token / call budgets per user; engine enforces (rejects calls when over).

Platform billing reads the admin-aggregated total only. Per-user breakdown stays inside the instance.

---

## Implementation

### server.db users table (Phase 0)

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,                     -- uuid
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,             -- argon2id
    is_admin BOOLEAN NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,             -- unix ms
    last_login_at INTEGER,
    disabled_at INTEGER                      -- soft-delete
);

CREATE INDEX idx_users_username ON users(username);
```

JWT claims:

```json
{
  "sub": "<user_id>",
  "username": "<username>",
  "is_admin": true,
  "iat": 1700000000,
  "exp": 1700086400,
  "iss": "scenecraft-engine",
  "aud": "scenecraft-instance"
}
```

### Engine boot flow change (Phase 0)

```
on startup:
  if SCENECRAFT_JWT_SECRET unset and not --no-auth:
    fatal("auth enabled but no SCENECRAFT_JWT_SECRET — refusing to start")
  if users table is empty:
    if SCENECRAFT_BOOTSTRAP_ADMIN_USERNAME and SCENECRAFT_BOOTSTRAP_ADMIN_PASSWORD set:
      insert admin user with argon2id hash
      log "bootstrapped admin <username>" to stderr
    else:
      log warning "no users in DB and no BOOTSTRAP_ADMIN_* env — instance unusable"
      log "create one with: python -m scenecraft users create --admin --username <u> --password <p>"
      continue boot (engine refuses all requests until first user exists)
  else:
    standard boot
```

CLI backfill (for the existing self-hosted instance + as the implementation backing the bootstrap path):

```
python -m scenecraft users create --username <u> --password <p> --admin
python -m scenecraft users list
python -m scenecraft users set-password --username <u>
python -m scenecraft users disable --username <u>
```

### Generation call abstraction (Phase 1 prerequisite)

Engine factors all provider calls behind a single `UpstreamClient` interface. Today it dispatches to `AnthropicDirectClient` / `GoogleDirectClient` / etc. When `SCENECRAFT_PROXY_URL` is set, the factory returns `ProxyClient(base_url=proxy_url, token=proxy_token)` instead. From the engine's perspective, nothing else changes — same method signatures, same response shapes.

The proxy itself (when built) has these endpoints:

```
POST /v1/anthropic/messages       → forwards to api.anthropic.com/v1/messages
POST /v1/google/models/.../generate → forwards to Google Generative API
POST /v1/replicate/predictions    → forwards to api.replicate.com
... etc
```

Each forwards with platform-owned upstream keys, streams responses back unchanged (SSE pass-through), and logs `(instance_id, provider, model, input_tokens, output_tokens, latency)` for billing.

### Provisioning flow (Phase 1)

When a platform user signs up at scenecraft.online, the signup form collects: chosen subdomain, admin username, admin password. The provisioning script runs:

1. Platform allocates a Cloudflare Tunnel + DNS records (already automated via REST).
2. Platform spins up engine container (or k8s pod) with provisioned `engine.env`:
   - `SCENECRAFT_INSTANCE_ID=<uuid>`
   - `SCENECRAFT_INSTANCE_HOSTNAME=<tenant>.scenecraft.online`
   - `SCENECRAFT_CORS_ORIGINS=https://<tenant>.scenecraft.online`
   - `SCENECRAFT_JWT_SECRET=<random>`
   - `SCENECRAFT_PROXY_URL=https://api.scenecraft.online`
   - `SCENECRAFT_PROXY_TOKEN=<provisioned>`
   - `SCENECRAFT_BOOTSTRAP_ADMIN_USERNAME=<from signup>`
   - `SCENECRAFT_BOOTSTRAP_ADMIN_PASSWORD=<from signup>`
   - No raw provider keys.
3. Engine first-boot bootstrap path inserts the admin user from those env vars; from then on those vars are ignored.
4. Platform emails admin with login URL.

The platform never holds the admin password after passing it through to the engine. Provisioning script should erase the password from its memory immediately after the env file is written.

Self-hosted (current instance, future self-hosters): admin sets `SCENECRAFT_BOOTSTRAP_ADMIN_USERNAME` + `SCENECRAFT_BOOTSTRAP_ADMIN_PASSWORD` in their own `engine.env` before first boot, OR runs the CLI backfill (`python -m scenecraft users create --admin ...`) after the engine is already running with an empty users table.

---

## Benefits

- **Single-path engine code.** Same binary runs self-hosted and hosted; differences are env-var-driven. No "hosted mode" code paths to maintain alongside "self-hosted mode."
- **Honest threat model.** Admin can read engine code → platform stops pretending to defend anything from admin within their own instance, focuses defenses where they matter (external + cross-instance + admin-vs-user-within-instance).
- **Clean billing flow.** Single bill per admin, single proxy log as source of truth, no per-instance-user reconciliation across providers.
- **Open source posture preserved.** Self-hosters never need scenecraft.online for anything — no platform call-home, no license keys, no proxy dependency. The platform is genuinely optional.
- **Scalable key rotation.** Provider key rotation happens once at the proxy, not 50 times across hosted instances.

---

## Trade-offs

- **Proxy is a SPOF for hosted instances.** If `api.scenecraft.online` is down, no hosted instance can generate. Mitigation: proxy is small, stateless, deployable to multiple regions; SLO needs to match Cloudflare Tunnel's.
- **Streaming proxying is non-trivial.** SSE pass-through requires care (no buffering, correct chunked transfer, timeout handling). One-time engineering cost; standard libraries (FastAPI `StreamingResponse`, hono streaming, Cloudflare Workers) handle it.
- **Bandwidth costs at the proxy.** Replicate stem-splitter and Runway video can move large payloads. Mitigation: presigned URL pattern — proxy returns a short-lived URL the instance uploads directly to provider, bypassing the proxy for the byte-heavy step. Or carve those calls out of proxy mode entirely.
- **No JWT cross-issuance — bootstrap is by env-var injection.** The earlier draft floated a "platform-signed token used once to bootstrap" pattern; that was discarded. Provisioning writes `SCENECRAFT_BOOTSTRAP_ADMIN_USERNAME` / `_PASSWORD` directly into `engine.env`, engine consumes them on first boot, then ignores them. Engine issues its own JWTs against its own users table only — the platform JWT (Firebase) is never accepted by the engine.
- **Per-user quota enforcement is admin's problem, not platform's.** If an admin's user runs up the bill, the platform charges the admin, not the user. Admin needs UI to set/monitor quotas. Initial version can be CLI-only; full UI is post-launch.

---

## Dependencies

- `argon2-cffi` (or `bcrypt`) — password hashing.
- `pyjwt` — JWT issuance/validation.
- Existing `server.db` migration framework — for the `users` table.
- Cloudflare Tunnel + REST API automation — already in place (this conversation).
- Frontend changes: login route, JWT storage in `localStorage`, `Authorization: Bearer` header on all API/WS calls, refresh-on-401 flow.

Phase 1 additionally:

- Proxy service (FastAPI / Hono / Cloudflare Workers — TBD when needed).
- Platform DB (Postgres / SQLite / Firestore — TBD; not relevant until customer #1).
- Stripe (or equivalent) for billing.

---

## Testing Strategy

Phase 0:

- Unit tests for password hashing round-trip and JWT sign/verify.
- Integration tests for the bootstrap-on-empty-users-table flow.
- Auth gate test: every existing API route returns 401 without a valid JWT (parametrized over routes).
- CORS allowlist test: requests with disallowed Origin get no `Access-Control-Allow-Origin` header.

Phase 1:

- Proxy contract tests: mock provider API, confirm proxy forwards correctly per provider.
- Streaming smoke test: 30-second SSE response from Anthropic flows end-to-end without buffering.
- Provisioning end-to-end test: spin up an instance, bootstrap admin via platform token, log in, generate, verify billing record.

---

## Migration Path

### Phase 0 (now → before sharing this instance with anyone):

1. Add `users` table to server.db migrations.
2. Add password hashing + JWT modules. New deps in `pyproject.toml`.
3. Add `python -m scenecraft users` CLI subcommand (create / list / set-password / disable).
4. Implement bootstrap-on-empty-users-table flow consuming `SCENECRAFT_BOOTSTRAP_ADMIN_*` env vars (calling into the same code as the CLI).
5. Add `/api/auth/login` endpoint.
6. Replace `--no-auth` default with auth-on; keep `--no-auth` flag for local dev.
7. Frontend: add login route, JWT storage, attach `Authorization: Bearer` everywhere.
8. Replace permissive CORS with `SCENECRAFT_CORS_ORIGINS` env-driven allowlist.
9. Update systemd unit + `engine.env` to set `SCENECRAFT_JWT_SECRET` and `SCENECRAFT_CORS_ORIGINS`.
10. **Backfill admin user on this instance** via `python -m scenecraft users create --admin --username <u> --password <p>` (one-time manual step, since this instance predates the provisioning flow).
11. Re-enable auth on production: `systemctl restart scenecraft-engine` without `--no-auth`.

### Phase 1 (when first hosted customer signs up):

1. Stand up `api.scenecraft.online` proxy service with at least Anthropic + Google + Replicate forwarding.
2. Build provisioning automation that creates Cloudflare Tunnel + DNS + container + `engine.env` + admin bootstrap call.
3. Add `SCENECRAFT_PROXY_URL` / `SCENECRAFT_PROXY_TOKEN` handling to the engine's upstream client factory.
4. Add platform DB + Stripe.
5. Add per-user quota tooling (CLI initially) for admins.

Self-hosters need only step 1 (set passwords for their users). Steps 2-9 of Phase 0 are normal upgrade-path concerns. Phase 1 doesn't touch self-hosters at all.

---

## Key Design Decisions

### User Model

| Decision | Choice | Rationale |
|---|---|---|
| Two user classes | Platform user (scenecraft.online) + instance user (server.db) | Mirrors workspace/tenant pattern; admin spans both, instance users never see platform |
| Source of truth for instance auth | server.db | Already exists conceptually; admin owns it; survives self-hosted scenario unchanged |
| Admin can modify engine code | Yes, by design | scenecraft is open source; admin has root on their box; platform doesn't pretend otherwise |

### Cost Attribution

| Decision | Choice | Rationale |
|---|---|---|
| Granularity for billing | Admin (instance) only | One bill per admin = simpler reconciliation; admin handles internal allocation |
| Per-user generation data in server.db | Audit log, not billing | Admin uses for own quota tooling; not consumed by platform billing |
| Quota enforcement layer | Engine (admin-configured) | Hard cap on a runaway user must happen at the engine; platform bills regardless |

### API Key Custody

| Decision | Choice | Rationale |
|---|---|---|
| Default for hosted instances | Proxy through scenecraft.online | Enables metering, key hiding, single rotation point, anomaly detection |
| Default for self-hosted | Direct (admin provides own keys) | Open source = no call-home dependency |
| Code path | Single, env-var-driven | Engine doesn't know if it's hosted; just calls "the configured upstream" |

### Auth Implementation

| Decision | Choice | Rationale |
|---|---|---|
| Password hash | argon2id (preferred), bcrypt (acceptable) | Modern, memory-hard, sane defaults; never roll our own |
| Session model | Stateless JWT (HS256) | Simple, single-issuer; revocation via short TTL + future allowlist if needed |
| Bootstrap admin | Provisioning input only (env vars from signup form) + CLI fallback | Username + password are always known up front during signup; no need for stderr-logged random password. CLI exists for backfill on instances that predate this flow (e.g., the current dev instance). |
| `--no-auth` flag | Kept for local dev only | Useful escape hatch; never set in systemd unit |

### CORS

| Decision | Choice | Rationale |
|---|---|---|
| Allowlist mechanism | `SCENECRAFT_CORS_ORIGINS` env var | Set at provision (or `engine.env` for self-hosted); admin can override |
| Behavior on unknown origin | No `Access-Control-Allow-Origin` header → browser blocks | Explicit deny; safer than `*` with credentials |

---

## Future Considerations

- **Refresh tokens** — current JWT has fixed ~24h TTL. Refresh-token flow can come later if session length becomes painful.
- **OAuth/SSO at platform level** — admin login could federate via Google/GitHub. Doesn't affect instance auth (which stays local to server.db).
- **Per-instance-user OAuth** — admins might want to delegate identity to their org's IdP. Out of scope for Phase 1.
- **Instance migration** — if an admin wants to move from hosted to self-hosted, what's the export path? Ideally a `scenecraft export-instance` command that ships server.db plus project DBs as a tarball.
- **Multi-region proxy** — when proxy traffic outgrows a single region; Cloudflare Workers makes this nearly free.
- **Webhooks for billing events** — stripe webhooks → platform DB → adjustments to admin's quota. Not needed pre-launch.
- **Per-provider failover** — proxy could fall back to alternative providers on outage (e.g., Anthropic down → reroute to OpenAI). Speculative; revisit when scale justifies.

---

**Status**: Proposal — Phase 0 ready to implement, Phase 1 deferred until first hosted customer.
**Recommendation**: Implement Phase 0 (interim auth + CORS allowlist) before sharing this instance with anyone else. Defer Phase 1 work until a paying customer triggers the need.
**Related Documents**:
- `agent/design/local.beatlab-server.md` — existing engine architecture
- `../scenecraft.online/agent/design/local.per-instance-architecture.md` — canonical cross-repo runtime topology + per-repo responsibilities (private). Authoritative for the platform/engine boundary; this doc is engine-internal.
