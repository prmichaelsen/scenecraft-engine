# Spec: Engine CLI + Admin Commands

> **🤖 Agent Directive**: This file is a specification. When implementing against it, treat the Behavior Table and Tests section as the contract; translate each test into the target framework and do not invent behavior for `undefined` rows without resolving the corresponding Open Question.

**Namespace**: local
**Version**: 1.0.0
**Created**: 2026-04-27
**Last Updated**: 2026-04-27
**Status**: Draft (proofing)

---

## Purpose

Define the exact observable behavior of the `scenecraft` command-line interface — the single registered entry point for engine administration, development, and one-shot content pipelines. This spec is the black-box contract a caller or packager can rely on without reading `cli.py`.

## Source

- Draft/audit-driven (no prior clarification).
- Primary sources:
  - `src/scenecraft/cli.py` (main Click group, analysis/render/resolve/narrative commands — 2358 lines, 22 top-level commands + 2 subgroups).
  - `src/scenecraft/vcs/cli.py` (auth/user/org/session/auth-keys commands registered onto the main group).
  - `src/scenecraft/vcs/bootstrap.py` (`init_root`, `create_user`, `create_org` semantics).
  - `pyproject.toml` (`[project.scripts] scenecraft = "scenecraft.cli:main"`).
  - `agent/reports/audit-2-architectural-deep-dive.md` §1H (CLI + Admin Tooling — 7 units, 28 commands).

## Scope

**In scope**:
- Entry-point registration (`scenecraft`) and its discoverability.
- Every subcommand across the six admin groups: `init`, `token`, `org {create,list,members}`, `user {add,list,set-password}`, `session {list,prune}`, `auth keys {issue,list,revoke}`.
- The `--no-auth` flag on `scenecraft server`.
- `--root` resolution precedence and `SCENECRAFT_ROOT` env var behavior.
- First-user-is-admin behavior on `scenecraft init`.
- API-key issue-once-show-once semantics and revocation.
- Session pruning 7-day default.
- The Resolve/narrative/beat-analysis commands to the extent they are user-visible: name, group, args/flags, side effects. Internal pipeline behavior (what analysis/render actually produce) is referenced, not redefined.

**Out of scope**:
- REST API (separate spec).
- Database schema, migrations, and table column contracts (separate spec).
- Pipeline internals for `analyze`, `run`, `render`, `audio-intelligence*`, `effects`, `narrative assemble`, `crossfade` — those have their own implementation specs; this spec only pins down their CLI surface.
- Provider billing, GPU provisioning semantics (tracked elsewhere).
- Frontend/browser behavior triggered by `scenecraft token`.

---

## Requirements

1. **R1 (entry point)**: Installing `scenecraft-engine` MUST expose a single console script named `scenecraft` on `PATH`, bound to `scenecraft.cli:main`.
2. **R2 (alias docs vs reality)**: `beatlab` and `scenecraft-cli` are referenced in legacy docs but MUST NOT be registered by `pyproject.toml`. Invoking either MUST fail with a standard shell "command not found" — the spec does not promise an alias.
3. **R3 (group structure)**: `scenecraft --help` MUST list exactly these top-level commands/groups: `init`, `token`, `org`, `user`, `session`, `auth`, `server`, `analyze`, `presets`, `marker-ui`, `generate`, `run`, `resolve`, `render`, `make-patch`, `candidates`, `select`, `split-sections`, `destroy-gpu`, `audio-transcribe`, `audio-intelligence`, `audio-intelligence-multimodel`, `effects`, `delete`, `narrative`, `crossfade`.
4. **R4 (init)**: `scenecraft init [--org NAME] [--admin USER] [--root DIR]` MUST create `<root>/.scenecraft/` with directory tree, initialize `server.db`, `sessions.db`, org.db, user.db, create the named org (default `"default"`), create the admin user (default: current OS user via `getpass.getuser()`), assign role `admin`, and add the admin as an `admin`-role member of the org. The first user created via `init` is implicitly admin regardless of any later `user add` calls.
5. **R5 (init refuses re-init)**: If `<root>/.scenecraft/` already exists, `init` MUST exit non-zero with a message referencing the existing path and MUST NOT mutate the existing tree.
6. **R6 (root resolution)**: Any command requiring `.scenecraft/` MUST resolve the root in this precedence: (a) explicit `--root`, (b) `SCENECRAFT_ROOT` env var, (c) the `.scenecraft/` under the configured `projects_dir` from `scenecraft.config`, (d) cwd-walk upward looking for `.scenecraft/`. If none resolves, exit non-zero with "not inside a .scenecraft directory" pointing the user at `init`, `--root`, or `SCENECRAFT_ROOT`.
7. **R7 (token)**: `scenecraft token` MUST generate a JWT for the given `--user` (default: current OS user) with `--expiry` hours (default 24), store a single-use login code bound to that JWT (5-minute TTL), and print a URL of the form `{scheme}://{host}/auth/login?code={code}[&redirect_uri=...]`. With `--raw`, it MUST print ONLY the JWT and no URL. `--open` MUST attempt `webbrowser.open(url)` and swallow any exception.
8. **R8 (token host default)**: When `--host` is omitted, the CLI MUST detect the machine's primary LAN/WAN IP (UDP-socket-to-8.8.8.8 trick) and default to `{ip}:8890`; on detection failure it MUST fall back to `localhost:8890`.
9. **R9 (org create)**: `scenecraft org create NAME` MUST insert a row into `orgs`, create `orgs/NAME/` with an initialized org.db, and print `Created org: NAME`.
10. **R10 (org list/members)**: `org list` MUST print every org (name + created_at); `org members NAME` MUST print every member (username + role + joined_at) or a "No members" line if empty. Neither mutates state.
11. **R11 (user add)**: `user add USERNAME [--pubkey FILE] [--role admin|editor|viewer] [--org ORG]` MUST create a user row with role (default `editor`) and `must_change_password = 1`, create `users/USERNAME/` + `sessions/` subdir, initialize user.db, load and attach the pubkey file contents if given, and optionally add the user to `--org` as role `member`.
12. **R12 (user list)**: `user list` MUST print every registered user (username + role + created_at).
13. **R13 (user set-password)**: `user set-password USERNAME` MUST clear `must_change_password` on that user. If the user does not exist, exit non-zero with "user '<name>' not found".
14. **R14 (session list)**: `session list` MUST list every active session showing id, username, org/project, branch, last_active. No prompts, no mutations.
15. **R15 (session prune)**: `session prune [--days N]` MUST remove sessions inactive for more than N days (default 7) and their working-copy files, and print `Pruned N stale session(s).`.
16. **R16 (auth keys issue)**: `scenecraft auth keys issue USERNAME --expires YYYY-MM-DD [--label TEXT]` MUST (a) verify the user exists (exit non-zero otherwise), (b) validate the expiry format (exit non-zero otherwise), (c) generate a 32-byte URL-safe raw key, a 16-byte salt, hash via `hash_api_key`, insert a row with `id = ak_<12-hex>`, (d) print the key id, raw key, expiry, optional label, and a "Store this key securely — it will NOT be shown again" line. The raw key MUST appear in output exactly once and MUST NOT be recoverable from the DB.
17. **R17 (auth keys list)**: `auth keys list USERNAME` MUST list all keys for the user showing id, issued_at, expires_at, status (`active` or `revoked`), and optional label. It MUST NOT print the raw key or its hash.
18. **R18 (auth keys revoke)**: `auth keys revoke KEY_ID` MUST set `revoked_at = now` on the row. If the key is already revoked, exit 0 with `Key '<id>' is already revoked.`. If the key does not exist, exit non-zero with `key '<id>' not found`.
19. **R19 (server)**: `scenecraft server [--port N] [--host H] [--work-dir D] [--no-auth]` MUST start the REST + WebSocket engine with the given bind address (default `0.0.0.0:8890`). `--no-auth` MUST disable JWT verification engine-wide for the lifetime of that process; the flag has no persisted effect.
20. **R20 (server projects-dir bootstrap)**: If no projects directory is configured and `--work-dir` is not passed, `server` MUST interactively prompt (`click.prompt`) for a directory (default `~/.scenecraft/projects`) and persist the choice via `scenecraft.config.set_projects_dir`.
21. **R21 (beat-analysis surface)**: Commands `analyze`, `presets`, `marker-ui`, `generate`, `run`, `render`, `make-patch`, `candidates`, `select`, `split-sections`, `destroy-gpu`, `delete`, `crossfade` MUST each accept the exact flags declared at the code locations listed in the Interfaces section. Defaults, hidden flags, and env-coupled flags MUST match the source.
22. **R22 (narrative group)**: `scenecraft narrative` MUST be a subgroup with exactly one registered command: `assemble PROJECT_DIR [--output FILE]`.
23. **R23 (resolve group)**: `scenecraft resolve` MUST be a subgroup with exactly four registered commands: `status`, `inject`, `render`, `pipeline`.
24. **R24 (audio-intelligence family)**: `audio-transcribe`, `audio-intelligence`, and `audio-intelligence-multimodel` MUST be registered as siblings on the main group, not as a subgroup, matching the code.
25. **R25 (exit codes)**: Every admin command in groups init/token/org/user/session/auth MUST exit 0 on success and non-zero with a message to stderr on any of: missing root, missing user, missing key, malformed expiry, existing .scenecraft on init.
26. **R26 (stability of command surface)**: Adding or removing a top-level command is a breaking change. Renaming a flag is a breaking change. Changing a default value (e.g. session-prune days, server port) is a breaking change. These MUST be reflected in a version bump.
27. **R27 (target, OQ-1) org create duplicate**: `scenecraft org create <existing>` MUST catch the SQLite UNIQUE violation and exit 1 with message "org '<name>' already exists — use `org update` for metadata changes." No traceback; no `--force` flag.
28. **R28 (target, OQ-2) user add duplicate**: `scenecraft user add <existing>` MUST catch SQLite UNIQUE and exit 1 with "user '<name>' already exists." When `--org` is passed, membership MUST be idempotently ensured (adding an existing member to the org is a no-op that exits 0 when the user itself is new; when the user already exists, the failure is on the user-row insert).
29. **R29 (target, OQ-3/OQ-5) admin advisory lock**: mutating admin commands (`org create`, `user add`, `user set-password`, `session prune`, `auth keys issue`, `auth keys revoke`) MUST acquire an advisory `flock` on `<root>/.scenecraft/admin.lock` before mutating. Lock acquisition retries 3 times with 1s backoff; if still held, exits 1 with "another admin operation in progress".
30. **R30 (target, OQ-5) server holds read lock only**: `scenecraft server` MUST NOT hold the admin.lock exclusively. Reading sessions.db during server runtime must not block CLI admin commands; advisory lock is for CLI-to-CLI coordination.
31. **R31 (target, OQ-4) filesystem ACL reliance**: CLI does NOT perform UID/ACL checks. OS-level `PermissionError` from DB-open MUST be caught and wrapped as stderr message `cannot access <path>: <errno-name>` with non-zero exit. No stack trace visible to the user.
32. **R32 (target, OQ-6, deferred commands)**: target command surface includes `scenecraft backup`, `scenecraft restore`, `scenecraft list-projects`, `scenecraft gc`, `scenecraft audit`, `scenecraft export-project`. Each is tracked under its own milestone and spec; none are required to exist for this spec's acceptance. `reset-password` is explicitly a frontend-only command, not part of the CLI surface.

---

## Interfaces / Data Shapes

### Entry point

```toml
# pyproject.toml
[project.scripts]
scenecraft = "scenecraft.cli:main"
```

No other scripts are registered. `beatlab` and `scenecraft-cli` are **not** registered.

### Top-level command inventory (28 commands, counting group leaves)

| Group | Command | Source |
|---|---|---|
| *(root)* | `init` | `vcs/cli.py:init_cmd` |
| *(root)* | `token` | `vcs/cli.py:token_cmd` |
| `org` | `create`, `list`, `members` | `vcs/cli.py` |
| `user` | `add`, `list`, `set-password` | `vcs/cli.py` |
| `session` | `list`, `prune` | `vcs/cli.py` |
| `auth keys` | `issue`, `list`, `revoke` | `vcs/cli.py` |
| *(root)* | `server` | `cli.py:1284` |
| *(root)* | `analyze` | `cli.py:36` |
| *(root)* | `presets` | `cli.py:126` |
| *(root)* | `marker-ui` | `cli.py:138` |
| *(root)* | `generate` | `cli.py:157` |
| *(root)* | `run` | `cli.py:219` |
| `resolve` | `status`, `inject`, `render`, `pipeline` | `cli.py:299–385` |
| *(root)* | `render` | `cli.py:475` |
| *(root)* | `make-patch` | `cli.py:982` |
| *(root)* | `candidates` | `cli.py:1014` |
| *(root)* | `select` | `cli.py:1092` |
| *(root)* | `split-sections` | `cli.py:1173` |
| *(root)* | `destroy-gpu` | `cli.py:1253` |
| *(root)* | `audio-transcribe` | `cli.py:1310` |
| *(root)* | `audio-intelligence` | `cli.py:1370` |
| *(root)* | `audio-intelligence-multimodel` | `cli.py:1472` |
| *(root)* | `effects` | `cli.py:1592` |
| *(root)* | `delete` | `cli.py:2000` |
| `narrative` | `assemble` | `cli.py:2237–2249` |
| *(root)* | `crossfade` | `cli.py:2252` |

Total: 2 root admin + 3 org + 3 user + 2 session + 3 auth-keys + 1 server + 13 beat/render/analysis + 4 resolve + 1 narrative = **32 callable command leaves**. The audit claims "28 commands"; the delta is that the audit counts the six admin groups (init, token, org, user, session, auth-keys) plus `server` plus the Resolve group leaves plus beat/analysis plus narrative/crossfade by responsibility-unit, not by leaf. The leaf count in this spec is authoritative.

### Admin command signatures

```
scenecraft init [--org NAME='default'] [--admin USER=$USER] [--root DIR='.']
scenecraft token [--user USER=$USER] [--expiry H=24] [--host H:P=AUTO:8890]
                 [--scheme http|https=http] [--redirect-uri URL]
                 [--open|--no-open=--no-open] [--raw] [--root DIR]
scenecraft org create NAME [--root DIR]
scenecraft org list [--root DIR]
scenecraft org members NAME [--root DIR]
scenecraft user add USERNAME [--pubkey FILE] [--role admin|editor|viewer=editor]
                  [--org NAME] [--root DIR]
scenecraft user list [--root DIR]
scenecraft user set-password USERNAME [--root DIR]
scenecraft session list [--root DIR]
scenecraft session prune [--days N=7] [--root DIR]
scenecraft auth keys issue USERNAME --expires YYYY-MM-DD [--label TEXT] [--root DIR]
scenecraft auth keys list USERNAME [--root DIR]
scenecraft auth keys revoke KEY_ID [--root DIR]
scenecraft server [--port 8890] [--host 0.0.0.0] [--work-dir DIR] [--no-auth]
```

### Root resolution algorithm

```
_require_root(explicit) →
  1. if explicit:
       if Path(explicit) is a dir named '.scenecraft': use it
       elif Path(explicit)/.scenecraft is a dir: use it
       else: error
  2. elif env SCENECRAFT_ROOT is set and find_root resolves: use it
  3. elif scenecraft.config.get_projects_dir() is set and has .scenecraft/: use it
  4. else cwd-walk upward looking for .scenecraft/
  5. else error "not inside a .scenecraft directory"
```

### API-key issue output (stdout, fixed order)

```
Key ID:    ak_<12 hex chars>
API Key:   <43-char urlsafe token>
Expires:   YYYY-MM-DD
Label:     <text>           # only if --label given
<blank line>
Store this key securely — it will NOT be shown again.
```

### Token (login URL) output

```
{scheme}://{host}/auth/login?code={code}[&redirect_uri={url-encoded}]
<blank line>
Valid for 5 minutes. Open in your browser (use SSH port-forward if {host-ip} is remote).
```

`--raw` suppresses everything but the JWT.

---

## Behavior Table

| # | Scenario | Expected Behavior | Tests |
|---|----------|-------------------|-------|
| 1 | Install wheel; run `scenecraft --help` | Lists all 25 top-level commands/groups; exit 0 | `entrypoint-registered`, `help-lists-all-groups` |
| 2 | Run `beatlab` or `scenecraft-cli` | Shell reports "command not found"; CLI does not register these aliases | `legacy-aliases-not-registered` |
| 3 | `scenecraft init` in empty dir as OS user `alice` | `.scenecraft/` created; `alice` registered with role `admin`; org `default` created; `alice` joined to `default` as `admin` | `init-creates-tree`, `init-makes-first-user-admin` |
| 4 | `scenecraft init --org acme --admin bob` | Org `acme` created; user `bob` created with role `admin`; `bob` added to `acme` as `admin` | `init-custom-org-and-admin` |
| 5 | `scenecraft init` when `.scenecraft/` already exists | Exits non-zero; prints `.scenecraft already exists at <path>`; no mutation | `init-refuses-existing` |
| 6 | `scenecraft token` (defaults) | Prints login URL; URL contains a 5-min single-use code bound to a fresh JWT for current OS user | `token-prints-url`, `token-code-single-use`, `token-default-user-is-os-user` |
| 7 | `scenecraft token --raw` | Prints only the JWT on stdout; no URL, no trailing instruction line | `token-raw-prints-only-jwt` |
| 8 | `scenecraft token --open` succeeds opening browser | URL printed AND `webbrowser.open` called; exit 0 | `token-open-calls-webbrowser` |
| 9 | `scenecraft token --open` when browser not available | URL still printed; webbrowser exception swallowed; exit 0 | `token-open-swallows-errors` |
| 10 | `scenecraft org create acme` | Inserts orgs row; creates `orgs/acme/` + org.db; prints `Created org: acme` | `org-create-happy-path` |
| 11 | `scenecraft org create acme` when `acme` exists | Exits 1 with "org 'acme' already exists — use `org update` ..." | `org-create-duplicate-friendly-error` (covers R27, OQ-1) |
| 12 | `scenecraft org list` | One line per org with name + created_at; "No organizations found." if empty | `org-list-happy-path`, `org-list-empty` |
| 13 | `scenecraft org members acme` on empty org | Prints `No members in org 'acme'.` | `org-members-empty` |
| 14 | `scenecraft user add alice --role editor --org acme` | Row inserted with role=editor, must_change_password=1; user dir + user.db created; alice joined acme as member | `user-add-happy-path`, `user-add-sets-must-change-password` |
| 15 | `scenecraft user add alice` when `alice` already exists | Exits 1 with "user 'alice' already exists." `--org` membership is idempotently ensured when adding a new user | `user-add-duplicate-friendly-error`, `user-add-org-membership-idempotent` (covers R28, OQ-2) |
| 16 | `scenecraft user set-password nobody` | Exits non-zero with `user 'nobody' not found` | `user-set-password-missing-user` |
| 17 | `scenecraft user set-password alice` (exists) | `must_change_password` cleared to 0; prints confirmation | `user-set-password-clears-flag` |
| 18 | `scenecraft session list` with none active | Prints `No active sessions.`; exit 0 | `session-list-empty` |
| 19 | `scenecraft session prune` with 7d default | Removes sessions inactive >7 days and their working-copy files; prints count | `session-prune-default-7-days` |
| 20 | `scenecraft session prune --days 0` | Removes all sessions regardless of activity | `session-prune-zero-days-removes-all` |
| 21 | `scenecraft session prune` while a session is in-use by a running server | Acquires `.scenecraft/admin.lock` (server holds only read locks on sessions.db); prune proceeds against committed rows | `admin-lock-blocks-concurrent-mutation` (covers R29, R30, OQ-3) |
| 22 | `scenecraft auth keys issue alice --expires 2027-01-01` | Raw key printed exactly once with issue-once banner; DB row stores hash+salt, not raw; id format `ak_<12hex>` | `keys-issue-happy-path`, `keys-issue-raw-not-persisted`, `keys-issue-id-format` |
| 23 | `scenecraft auth keys issue nobody --expires 2027-01-01` | `undefined` — currently exits non-zero with "user not found"; spec pins this as the required behavior | `keys-issue-missing-user` |
| 24 | `scenecraft auth keys issue alice --expires 2027/01/01` | Exits non-zero with "must be YYYY-MM-DD format" | `keys-issue-bad-date-format` |
| 25 | `scenecraft auth keys list alice` | Metadata-only listing; no raw key, no hash | `keys-list-metadata-only` |
| 26 | `scenecraft auth keys revoke ak_123` on active key | Sets `revoked_at`; prints `Revoked key: ak_123`; future `list` shows status=revoked | `keys-revoke-happy-path` |
| 27 | `scenecraft auth keys revoke ak_123` on already-revoked key | Exits 0 with `Key 'ak_123' is already revoked.`; idempotent, no DB write | `keys-revoke-idempotent` |
| 28 | `scenecraft auth keys revoke nonexistent` | Exits non-zero with `key 'nonexistent' not found` | `keys-revoke-missing` |
| 29 | `scenecraft server --no-auth` | Starts HTTP+WS with JWT checks disabled for process lifetime; flag not persisted | `server-no-auth-disables-jwt`, `server-no-auth-not-persisted` |
| 30 | `scenecraft server` without configured projects_dir, no `--work-dir` | Prompts with `click.prompt`, default `~/.scenecraft/projects`; persists via `set_projects_dir` | `server-prompts-for-projects-dir` |
| 31 | `SCENECRAFT_ROOT` set to a valid `.scenecraft/` | Any admin command uses that root without needing `--root` | `env-root-resolves` |
| 32 | `--root <bad-path>` explicit | Exits non-zero with `no .scenecraft directory at <path>` | `root-flag-invalid` |
| 33 | CLI run from user whose UID does not own `.scenecraft/` | OS `PermissionError` caught; stderr "cannot access <path>: <errno>"; exit non-zero; no stack trace | `cli-db-permission-error-wrapped` (covers R31, OQ-4) |
| 34 | Two `scenecraft session prune` invocations against same root concurrently | Second retries advisory lock 3× at 1s; if still held, exits 1 with "another admin operation in progress" | `admin-lock-retries-then-surfaces` (covers R29, OQ-5) |
| 35 | Deferred commands `backup`, `restore`, `list-projects`, `gc`, `audit`, `export-project` | Absent from current CLI; tracked under separate milestones | `deferred-commands-absent-for-now` (covers R32, OQ-6 deferred) |
| 36 | `scenecraft resolve status` | Either prints Resolve connection status or reports Resolve not reachable; exit reflects reachability | `resolve-status-reports` |
| 37 | `scenecraft narrative assemble DIR` | Invokes `scenecraft.render.narrative.assemble_final(DIR, output)` with default output `narrative_output.mp4` | `narrative-assemble-delegates` |
| 38 | `scenecraft crossfade VIDEO` with no transitions in project | Exits non-zero with `No transitions found in project` | `crossfade-empty-project-fails` |
| 39 | `scenecraft --version` | Prints the package version via `click.version_option(package_name="davinci-beat-lab")` | `version-option-prints-version`, `version-package-name-is-legacy` |
| 40 | `scenecraft token --redirect-uri https://app.example/x?y=1` | URL has `redirect_uri` param fully URL-encoded | `token-redirect-uri-encoded` |
| 41 | `scenecraft init` creates sessions.db, org.db, user.db alongside server.db | All four DB files exist under `.scenecraft/` after init | `init-creates-all-four-dbs` |

---

## Behavior (step-by-step)

### B1. `scenecraft` invocation

1. `python -m scenecraft` (via console script `scenecraft`) runs `scenecraft.cli:main`, which is the Click group.
2. `main()` import-time calls `register_commands(main)` from `scenecraft.vcs.cli`, adding `init`, `token`, `org`, `user`, `session`, `auth` to the root group.
3. `main()` also declares (via `@main.command`/`@main.group` decorators) all beat/render/analysis commands, the `resolve` group, the `narrative` group, `server`, `delete`, `crossfade`, `effects`, audio-intelligence family, and `destroy-gpu`.
4. Click dispatches the subcommand.

### B2. Root resolution (admin commands)

1. If `--root` is provided: resolve. If the path is or contains a `.scenecraft/`, use it; else error.
2. Else if env `SCENECRAFT_ROOT` is set and resolves: use it.
3. Else if `scenecraft.config.get_projects_dir()` returns a dir that contains `.scenecraft/`: use it.
4. Else walk cwd upward looking for `.scenecraft/`.
5. On failure: stderr `Error: not inside a .scenecraft directory. Run 'scenecraft init' first, pass --root, or set SCENECRAFT_ROOT.`; exit 1.

### B3. `init`

1. `sc = root / ".scenecraft"`. If it exists, raise `FileExistsError` → CLI converts to exit 1.
2. Resolve username (default `getpass.getuser()`).
3. Create directories: `sc/`, `sc/orgs/<org>/`, `sc/users/<user>/sessions/`.
4. Open `server.db`; insert the user with role=`admin`, the org, and the `org_members` link with role=`admin`.
5. Initialize empty `sessions.db`, org.db, user.db.
6. Print three confirmation lines.

### B4. `token`

1. Default host: `_detect_primary_ip():8890` (UDP-dial trick → `localhost` fallback).
2. Resolve root.
3. `generate_token(root, username, expiry_hours)` → JWT. Raises `ValueError` on unknown user → exit 1.
4. If `--raw`: print JWT; return.
5. Else `create_login_code(root, tok)` → 5-minute single-use code.
6. Build URL; append `redirect_uri` (URL-encoded) if given; print URL + blank line + instruction line.
7. If `--open`: `webbrowser.open(url)` inside try/except that swallows all exceptions.

### B5. `auth keys issue`

1. Resolve root; open server.db.
2. Verify user exists; else exit 1.
3. Parse `--expires` as `YYYY-MM-DD`; else exit 1.
4. `raw = secrets.token_urlsafe(32)`; `salt = os.urandom(16)`; `key_hash = hash_api_key(raw, salt)`; `id = "ak_" + uuid4.hex[:12]`.
5. INSERT into api_keys (id, username, key_hash, salt, issued_by=username, issued_at=now, expires_at, label).
6. Print id, raw, expires, optional label, then the issue-once banner.

### B6. `server`

1. Resolve work_dir via `resolve_work_dir(work_dir)`.
2. If unresolved and `--work-dir` missing: `click.prompt` with default `~/.scenecraft/projects`; persist via `set_projects_dir`.
3. `run_server(host, port, work_dir, no_auth)` — blocks.

---

## Acceptance Criteria

- [ ] Fresh wheel install exposes exactly one console script: `scenecraft`.
- [ ] `scenecraft --help` prints all groups/commands listed in R3.
- [ ] Every admin command exits non-zero on a root-resolution failure with the standard message.
- [ ] `scenecraft init` is a one-shot — re-running on an existing `.scenecraft/` is a hard error.
- [ ] First user created via `init` is `admin` role in both `users` and `org_members` tables.
- [ ] API keys issued via `auth keys issue` are shown exactly once and are not recoverable from DB inspection.
- [ ] `session prune` default window is 7 days; docs and `--help` match.
- [ ] `--no-auth` disables JWT checks for one server process; setting is ephemeral.
- [ ] `beatlab` and `scenecraft-cli` are not on `PATH` after install (no entry points registered).
- [ ] All six `undefined` rows are tracked in Open Questions and not silently implemented.

---

## Tests

### Base Cases

The core CLI contract: entry-point registration, admin happy paths, and the issue-once-show-once secret semantics.

#### Test: entrypoint-registered (covers R1)

**Given**: A clean venv with `scenecraft-engine` freshly pip-installed.
**When**: The user runs `which scenecraft` and `scenecraft --help`.
**Then** (assertions):
- **on-path**: `which scenecraft` exits 0 and points at the venv's bin dir.
- **help-exits-zero**: `scenecraft --help` exits 0.
- **help-has-usage**: stdout contains `Usage: scenecraft`.

#### Test: help-lists-all-groups (covers R3)

**Given**: Installed CLI.
**When**: User runs `scenecraft --help`.
**Then** (assertions):
- **lists-admin**: stdout contains each of `init`, `token`, `org`, `user`, `session`, `auth`, `server`.
- **lists-analysis**: stdout contains `analyze`, `run`, `render`, `effects`, `audio-transcribe`, `audio-intelligence`, `audio-intelligence-multimodel`.
- **lists-narrative**: stdout contains `narrative`, `crossfade`.
- **lists-resolve**: stdout contains `resolve`.

#### Test: legacy-aliases-not-registered (covers R2)

**Given**: Installed CLI.
**When**: User runs `beatlab` and `scenecraft-cli`.
**Then** (assertions):
- **beatlab-missing**: shell exits with command-not-found status for `beatlab`.
- **cli-alias-missing**: shell exits with command-not-found status for `scenecraft-cli`.

#### Test: init-creates-tree (covers R4, R41)

**Given**: Empty working directory; OS user is `alice`.
**When**: User runs `scenecraft init` with no flags.
**Then** (assertions):
- **dir-created**: `./.scenecraft/` exists and is a directory.
- **server-db**: `./.scenecraft/server.db` exists.
- **sessions-db**: `./.scenecraft/sessions.db` exists.
- **org-db**: `./.scenecraft/orgs/default/org.db` exists.
- **user-db**: `./.scenecraft/users/alice/user.db` exists.
- **sessions-subdir**: `./.scenecraft/users/alice/sessions/` exists.

#### Test: init-creates-all-four-dbs (covers R4)

**Given**: Empty working directory.
**When**: `scenecraft init`.
**Then** (assertions):
- **four-dbs**: exactly these SQLite DBs are created in the init call: server.db, sessions.db, orgs/<org>/org.db, users/<user>/user.db.

#### Test: init-makes-first-user-admin (covers R4)

**Given**: Empty dir; OS user `alice`.
**When**: `scenecraft init` completes.
**Then** (assertions):
- **users-row**: `users.role = 'admin'` for alice.
- **org-members-row**: `org_members.role = 'admin'` for (default, alice).
- **not-must-change**: `users.must_change_password = 0` for alice (admin bootstrap path does NOT set the flag).

#### Test: init-custom-org-and-admin (covers R4)

**Given**: Empty dir.
**When**: `scenecraft init --org acme --admin bob`.
**Then** (assertions):
- **org-name**: orgs contains row `name='acme'`.
- **admin-user**: users contains row `username='bob', role='admin'`.
- **membership**: org_members contains `(acme, bob, admin)`.

#### Test: init-refuses-existing (covers R5)

**Given**: Directory already contains `.scenecraft/` from a prior init.
**When**: User runs `scenecraft init` again.
**Then** (assertions):
- **nonzero-exit**: exit code is non-zero.
- **stderr-message**: stderr contains `.scenecraft already exists at`.
- **no-mutation**: file mtimes under `.scenecraft/` are unchanged after the failed call.

#### Test: token-prints-url (covers R7, R8)

**Given**: Initialized root; user `alice` exists.
**When**: `scenecraft token --user alice`.
**Then** (assertions):
- **scheme-default**: stdout URL starts with `http://`.
- **path**: URL path is `/auth/login`.
- **code-param**: URL query contains `code=`.
- **ttl-note**: a subsequent stdout line contains `Valid for 5 minutes.`.

#### Test: token-code-single-use (covers R7)

**Given**: `scenecraft token` has been run and produced code C.
**When**: The `/auth/login?code=C` endpoint is consumed once, then again.
**Then** (assertions):
- **first-ok**: first consumption succeeds.
- **second-rejected**: second consumption fails (code invalidated).

#### Test: token-raw-prints-only-jwt (covers R7)

**Given**: Initialized root.
**When**: `scenecraft token --raw --user alice`.
**Then** (assertions):
- **one-line**: stdout is a single line.
- **is-jwt**: the line parses as a JWT (three base64url segments separated by `.`).
- **no-url**: stdout contains no `http://` or `https://`.

#### Test: token-default-user-is-os-user (covers R7)

**Given**: OS user `alice` exists in `.scenecraft`.
**When**: `scenecraft token` with no `--user`.
**Then** (assertions):
- **jwt-sub-alice**: the JWT's `sub` claim equals `alice`.

#### Test: org-create-happy-path (covers R9)

**Given**: Initialized root with no org `acme`.
**When**: `scenecraft org create acme`.
**Then** (assertions):
- **row-inserted**: orgs contains `name='acme'`.
- **dir-created**: `.scenecraft/orgs/acme/org.db` exists.
- **stdout**: stdout contains `Created org: acme`.

#### Test: user-add-happy-path (covers R11)

**Given**: Initialized root.
**When**: `scenecraft user add alice --role editor --org default`.
**Then** (assertions):
- **user-row**: users contains `(alice, editor)`.
- **member-row**: org_members contains `(default, alice, member)`.
- **user-dir**: `.scenecraft/users/alice/sessions/` exists.

#### Test: user-add-sets-must-change-password (covers R11)

**Given**: Initialized root.
**When**: `scenecraft user add alice`.
**Then** (assertions):
- **flag-set**: users.must_change_password = 1 for alice.

#### Test: user-set-password-missing-user (covers R13)

**Given**: Initialized root; user `nobody` does not exist.
**When**: `scenecraft user set-password nobody`.
**Then** (assertions):
- **nonzero-exit**: exit code != 0.
- **stderr**: stderr contains `user 'nobody' not found`.
- **no-write**: no rows in users are modified.

#### Test: user-set-password-clears-flag (covers R13)

**Given**: User `alice` has must_change_password = 1.
**When**: `scenecraft user set-password alice`.
**Then** (assertions):
- **cleared**: must_change_password = 0 for alice.
- **stdout**: stdout contains `Cleared must_change_password for user: alice`.

#### Test: session-list-empty (covers R14)

**Given**: No active sessions.
**When**: `scenecraft session list`.
**Then** (assertions):
- **stdout**: stdout contains `No active sessions.`.
- **exit-zero**: exit 0.

#### Test: session-prune-default-7-days (covers R15)

**Given**: 3 sessions — last_active 8d ago, 5d ago, now.
**When**: `scenecraft session prune` (no flag).
**Then** (assertions):
- **only-old-removed**: the 8d-old session is deleted; the other two remain.
- **stdout-count**: stdout contains `Pruned 1 stale session(s).`.

#### Test: keys-issue-happy-path (covers R16)

**Given**: User `alice` exists.
**When**: `scenecraft auth keys issue alice --expires 2027-01-01 --label ci`.
**Then** (assertions):
- **key-id-format**: stdout line `Key ID:` matches `ak_[0-9a-f]{12}`.
- **raw-length**: stdout line `API Key:` matches `[A-Za-z0-9_-]{43}`.
- **expires-line**: stdout contains `Expires:   2027-01-01`.
- **label-line**: stdout contains `Label:     ci`.
- **banner**: stdout contains `Store this key securely — it will NOT be shown again.`.
- **db-row**: api_keys has one row with that id, key_hash != raw, salt length 32 hex chars.

#### Test: keys-issue-raw-not-persisted (covers R16)

**Given**: A key was just issued producing raw string R.
**When**: The tester greps the DB file and all `.scenecraft/` files for R.
**Then** (assertions):
- **not-found**: R does not appear anywhere on disk under `.scenecraft/`.

#### Test: keys-list-metadata-only (covers R17)

**Given**: Two keys issued for alice, one revoked.
**When**: `scenecraft auth keys list alice`.
**Then** (assertions):
- **both-listed**: stdout has exactly two lines, one per key.
- **status-active**: one line contains `status=active`.
- **status-revoked**: one line contains `status=revoked`.
- **no-raw**: stdout contains no base64url strings of length 43.

#### Test: keys-revoke-happy-path (covers R18)

**Given**: Active key `ak_abc`.
**When**: `scenecraft auth keys revoke ak_abc`.
**Then** (assertions):
- **revoked-at-set**: api_keys.revoked_at is a non-null ISO timestamp.
- **stdout**: stdout contains `Revoked key: ak_abc`.

#### Test: server-no-auth-disables-jwt (covers R19)

**Given**: Engine started with `scenecraft server --no-auth`.
**When**: A request hits a protected endpoint without a JWT.
**Then** (assertions):
- **allowed**: response status is 200 (or the endpoint's success status), not 401.

#### Test: version-option-prints-version (covers R3)

**Given**: Installed CLI.
**When**: `scenecraft --version`.
**Then** (assertions):
- **exits-zero**: exit 0.
- **prints-version**: stdout contains a version string.

### Edge Cases

#### Test: env-root-resolves (covers R6)

**Given**: `SCENECRAFT_ROOT=/tmp/project/.scenecraft`; cwd is elsewhere.
**When**: `scenecraft user list`.
**Then** (assertions):
- **uses-env-root**: user list is drawn from `/tmp/project/.scenecraft/server.db`, not cwd.

#### Test: root-flag-invalid (covers R6)

**Given**: `/no/such/path` does not contain `.scenecraft/`.
**When**: `scenecraft user list --root /no/such/path`.
**Then** (assertions):
- **nonzero-exit**: exit != 0.
- **stderr**: stderr contains `no .scenecraft directory at`.

#### Test: token-redirect-uri-encoded (covers R7)

**Given**: Initialized root.
**When**: `scenecraft token --redirect-uri 'https://app.example/x?y=1&z=2'`.
**Then** (assertions):
- **encoded**: stdout URL contains `redirect_uri=https%3A%2F%2Fapp.example%2Fx%3Fy%3D1%26z%3D2`.

#### Test: token-open-calls-webbrowser (covers R7)

**Given**: `webbrowser.open` is monkeypatched to record invocations.
**When**: `scenecraft token --open`.
**Then** (assertions):
- **called-once**: `webbrowser.open` was called exactly once with the printed URL.

#### Test: token-open-swallows-errors (covers R7)

**Given**: `webbrowser.open` raises RuntimeError.
**When**: `scenecraft token --open`.
**Then** (assertions):
- **exit-zero**: exit 0.
- **url-still-printed**: URL is on stdout.

#### Test: org-list-empty (covers R10)

**Given**: Root where orgs table was truncated after init.
**When**: `scenecraft org list`.
**Then** (assertions):
- **stdout**: stdout contains `No organizations found.`.

#### Test: org-members-empty (covers R10)

**Given**: Org `acme` with no members.
**When**: `scenecraft org members acme`.
**Then** (assertions):
- **stdout**: stdout contains `No members in org 'acme'.`.

#### Test: session-prune-zero-days-removes-all (covers R15)

**Given**: Several sessions, all active within last hour.
**When**: `scenecraft session prune --days 0`.
**Then** (assertions):
- **all-gone**: sessions table is empty.
- **files-gone**: working-copy files under `users/*/sessions/` are removed.

#### Test: keys-issue-missing-user (covers R16)

**Given**: User `nobody` does not exist.
**When**: `scenecraft auth keys issue nobody --expires 2027-01-01`.
**Then** (assertions):
- **nonzero-exit**: exit != 0.
- **stderr**: stderr contains `user 'nobody' not found`.
- **no-insert**: api_keys row count is unchanged.

#### Test: keys-issue-bad-date-format (covers R16)

**Given**: User `alice` exists.
**When**: `scenecraft auth keys issue alice --expires 01/01/2027`.
**Then** (assertions):
- **nonzero-exit**: exit != 0.
- **stderr**: stderr contains `--expires must be YYYY-MM-DD format`.
- **no-insert**: api_keys row count is unchanged.

#### Test: keys-revoke-idempotent (covers R18)

**Given**: Key `ak_abc` is already revoked at T0.
**When**: `scenecraft auth keys revoke ak_abc`.
**Then** (assertions):
- **exit-zero**: exit 0.
- **stdout**: stdout contains `already revoked`.
- **no-update**: `revoked_at` value still equals T0 (no row write).

#### Test: keys-revoke-missing (covers R18)

**Given**: No such key.
**When**: `scenecraft auth keys revoke does-not-exist`.
**Then** (assertions):
- **nonzero-exit**: exit != 0.
- **stderr**: stderr contains `key 'does-not-exist' not found`.

#### Test: server-prompts-for-projects-dir (covers R20)

**Given**: `scenecraft.config.get_projects_dir()` returns None; no `--work-dir`.
**When**: `scenecraft server` is invoked with stdin connected to a controlled input stream.
**Then** (assertions):
- **prompt**: stdout contains `Where should SceneCraft store projects?`.
- **default-hinted**: prompt text includes the default `~/.scenecraft/projects` path.
- **persisted**: after the user accepts, `set_projects_dir` has been called with that path and subsequent calls to `get_projects_dir` return it.

#### Test: server-no-auth-not-persisted (covers R19)

**Given**: `scenecraft server --no-auth` runs and exits.
**When**: `scenecraft server` is run again with no flag.
**Then** (assertions):
- **auth-restored**: JWT checks are back on; unauthenticated request → 401.

#### Test: version-package-name-is-legacy (covers R3)

**Given**: Installed CLI.
**When**: Inspecting `cli.py:main()`.
**Then** (assertions):
- **legacy-package-name**: `click.version_option` is attached with `package_name="davinci-beat-lab"` — a pre-rename artifact. This is documented as a known quirk; the spec pins it until a rename is scheduled.

#### Test: resolve-status-reports (covers R23)

**Given**: CLI installed; Resolve may or may not be reachable.
**When**: `scenecraft resolve status`.
**Then** (assertions):
- **exit-deterministic**: exit 0 if a Resolve connection succeeds, non-zero otherwise.
- **stdout-mentions-resolve**: stdout contains the word `Resolve`.

#### Test: narrative-assemble-delegates (covers R22)

**Given**: A valid project dir `P`.
**When**: `scenecraft narrative assemble P --output out.mp4`.
**Then** (assertions):
- **delegates**: `scenecraft.render.narrative.assemble_final` is called exactly once with `(P, 'out.mp4')`.

#### Test: org-create-duplicate-friendly-error (covers R27, OQ-1)

**Given**: org `acme` already exists.
**When**: `scenecraft org create acme`.
**Then**:
- **nonzero-exit**: exit != 0.
- **stderr-message**: stderr contains `org 'acme' already exists`.
- **no-traceback**: stderr does NOT contain `IntegrityError` or `Traceback`.
- **no-mutation**: orgs row count unchanged.

#### Test: user-add-duplicate-friendly-error (covers R28, OQ-2)

**Given**: user `alice` already exists.
**When**: `scenecraft user add alice`.
**Then**:
- **nonzero-exit**: exit != 0.
- **stderr-message**: stderr contains `user 'alice' already exists`.
- **no-traceback**: no `Traceback` on stderr.

#### Test: user-add-org-membership-idempotent (covers R28, OQ-2)

**Given**: user `alice` is new; org `acme` exists with no members.
**When**: `scenecraft user add alice --org acme` is run, then run again (same command).
**Then**:
- **first-succeeds**: first invocation exits 0; alice is a member of acme.
- **second-friendly-dupe**: second invocation exits non-zero on user-row dupe (per R28) — not an org-membership error.

#### Test: admin-lock-blocks-concurrent-mutation (covers R29, R30, OQ-3)

**Given**: a `scenecraft server` process is running on the same `work_dir`.
**When**: `scenecraft session prune` is invoked.
**Then**:
- **prune-succeeds**: prune acquires `.scenecraft/admin.lock` and completes (server does not hold admin.lock exclusively).
- **committed-rows-pruned**: stale rows deleted.

Separately: when a concurrent `scenecraft org create` is in progress and holds admin.lock:
- **second-mutation-waits**: second admin command retries 3×1s.
- **eventually-acquires-or-fails**: if the first releases within 3s, second proceeds; else second exits 1.

#### Test: admin-lock-retries-then-surfaces (covers R29, OQ-5)

**Given**: `.scenecraft/admin.lock` is held by another process (simulated via a helper that holds the flock for >5s).
**When**: `scenecraft session prune` is invoked.
**Then**:
- **retries-three-times**: the CLI attempts `flock` three times with ~1s backoff.
- **exits-with-friendly-error**: after retries, exit 1 with stderr containing `another admin operation in progress`.

#### Test: cli-db-permission-error-wrapped (covers R31, OQ-4)

**Given**: `.scenecraft/server.db` is owned by another user with mode 0600; running CLI user cannot read.
**When**: `scenecraft user list`.
**Then**:
- **nonzero-exit**: exit != 0.
- **stderr-friendly**: stderr contains `cannot access` and a path and an errno-name (e.g. `EACCES`).
- **no-traceback**: no Python traceback on stderr.

#### Test: deferred-commands-absent-for-now (covers R32, OQ-6 deferred)

**Given**: current CLI surface.
**When**: `scenecraft backup`, `scenecraft restore`, `scenecraft list-projects`, `scenecraft gc`, `scenecraft audit`, `scenecraft export-project`, and `scenecraft reset-password` are invoked.
**Then**:
- **not-found**: each of the six operator commands exits non-zero with Click "No such command" (their milestone specs will define them).
- **reset-password-stays-frontend-only**: `scenecraft reset-password` exits non-zero; there is no CLI surface for it now or in the target.

#### Test: crossfade-empty-project-fails (covers R21)

**Given**: Video file whose project YAML has no `transitions`.
**When**: `scenecraft crossfade VIDEO`.
**Then** (assertions):
- **click-exception**: exits non-zero.
- **stderr**: stderr contains `No transitions found in project`.

---

## Non-Goals

- Specifying the Python API of `init_root`, `create_user`, etc. (those are internal; this spec only pins the CLI.)
- Specifying the REST handlers behind `server` or `/auth/login`.
- Specifying the DB schema (column types, constraints, indices).
- Replacing commands absent today (see OQ-6).
- Normalizing the issued-once key banner to make raw keys re-viewable.
- Enforcing ACL or filesystem-ownership checks inside the CLI — we trust filesystem ACLs (see OQ-4).

---

## Transitional Behavior

Per INV-8, target Requirements (R27–R32) encode the target-ideal state. Current code divergences:
- `org create`/`user add` duplicates raise uncaught SQLite UNIQUE violations with Python tracebacks (R27/R28 target: catch + friendly exit 1).
- No advisory lock on admin mutations (R29 target: `flock` on `.scenecraft/admin.lock`).
- No retry-with-backoff on SQLite busy during concurrent CLI invocations (R30 target: 3 attempts × 1s).
- No friendly wrapping of permission errors on DB open (R31 target).
- Missing operator-facing commands (`backup`, `restore`, `list-projects`, `gc`, `audit`, `export-project`) deferred to separate milestones.

## Open Questions

### Resolved

**OQ-1 (resolved)**: Should `scenecraft org create <existing>` fail, update metadata, or silently no-op? **Decision**: catch SQLite UNIQUE, exit 1 with "org already exists — use `org update` for metadata changes." No `--force`. **Tests**: `org-create-duplicate-friendly-error`.

**OQ-2 (resolved)**: `user add alice` when `alice` exists. **Decision**: same pattern as OQ-1. Org membership is idempotent via implicit `--ensure-org-membership` semantic (add-user-to-org is a no-op if already a member). **Tests**: `user-add-duplicate-friendly-error`, `user-add-org-membership-idempotent`.

**OQ-3 (resolved)**: `session prune` during active server. **Decision**: advisory `flock` on `.scenecraft/admin.lock`. Mutating CLI commands (prune, keys issue/revoke, user add, org create) acquire the lock; fail fast with "another admin operation in progress" if held. Server holds read lock only on sessions.db (no blocking). **Tests**: `admin-lock-blocks-concurrent-mutation`.

**OQ-4 (resolved)**: Cross-user CLI invocation — no UID/ACL check. **Decision**: formalize as R31: "CLI relies on OS filesystem ACLs; no UID/ACL check; DB-open permission errors wrapped with friendly 'cannot access <path>: <errno>' message." **Tests**: `cli-db-permission-error-wrapped`.

**OQ-5 (resolved)**: Concurrent CLI invocations → SQLite busy. **Decision**: closed via OQ-3 advisory lock. CLI retries lock-acquire with 1s backoff (3 attempts) before surfacing. **Tests**: `admin-lock-retries-then-surfaces`.

**OQ-6 (deferred)**: Missing operator commands. **Decision**: target command surface includes `scenecraft backup`, `restore`, `list-projects`, `gc`, `audit`, `export-project`. Implementation paced per separate milestones. `reset-password` belongs in the frontend (user-facing), explicitly NOT a CLI command. **Deferred**: each new command has its own spec/milestone; not blocking the FastAPI refactor or current CLI surface.

---

## Related Artifacts

- `agent/reports/audit-2-architectural-deep-dive.md` §1H (source audit)
- `src/scenecraft/cli.py`
- `src/scenecraft/vcs/cli.py`
- `src/scenecraft/vcs/bootstrap.py`
- `pyproject.toml`

---

**Namespace**: local
**Spec**: engine-cli-admin-commands
**Version**: 1.0.0
**Status**: Draft (not committed)
