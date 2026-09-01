# Verdict Roadmap

Tracks known gaps and planned improvements before broader user testing of the agent.

## Pre-test blockers (fix before user testing)

### Web portal

- [x] **Show a "Claude is thinking…" indicator after Send.** *(landed in React; legacy Jinja chat still bare)*
  The React Workbench shows a live status line (`Claude is {phase}…`) with a
  `Loader2` spinner and per-phase messaging while busy
  (`frontend/src/pages/Workbench.tsx:954-957`, thinking block at 881-894).
  The legacy HTMX chat form (`server/app/templates/index.html`) still posts and
  silently waits with no indicator — only matters if that page is still in use;
  otherwise drop it when the Jinja chat is retired.

- [x] **Visual overhaul of the UI.** *(superseded by the React + Tailwind SPA)*
  The bare Pico-CSS portal has been replaced by the Vite + React + TypeScript +
  Tailwind SPA at `/app/*` (dark mode, sidebar nav, shadcn-style components) —
  see the "React + Tailwind frontend" and "Full React migration" items below.
  The original goals (real layout, typography, status colors, mobile width)
  are met by the SPA. Remaining cleanup is per-page polish, not a rewrite.

- [x] **User & team management in the UI.** *(landed)*
  Admin-only React pages under `/app/admin/`:
    - **Users**: list + create form (email / password / role / team) + per-row
      edit (role + team via dropdowns) + inline reset-password popover.
      Last-admin guard: a sole admin can't demote themselves.
    - **Teams**: list with member counts + create + inline rename + delete
      (members keep their data; `team_id` becomes NULL).
  Sidebar groups these under an "Admin" heading shown only when role=admin.

- [x] **Password reset / change-password flow.** *(landed)*
  Self-service `POST /auth/change-password` (current + new). Admin
  `POST /users/{id}/reset-password` (set without knowing old). Profile
  page at `/app/profile` carries the self-service form; admin Users page
  carries per-row reset.

- [x] **Self-service registration (domain-gated, no email verification).** *(landed)*
  Public signup at `/ui/register` accepts email + password. Allowed domains
  are env-configured (`IRS_SIGNUP_ALLOWED_DOMAINS`, default `example.com`).
  On submit: validate format, check the domain allowlist, enforce password
  length, refuse duplicates, create the `User` row (`role=user`,
  `team_id=NULL`), set the auth cookie, redirect to `/`. Off by default
  (`IRS_SIGNUP_ENABLED=0`).

  **Decision (2026-05-20):** dropped email verification entirely after
  testing showed delivering to a real corporate inbox would require
  authenticated outbound SMTP (no fully free + no-account path exists).
  Domain allowlist is the only gate. If a stronger gate is needed later,
  add **admin approval** (pending → approved by admin in UI) or
  **invite-code** signup as a separate roadmap item.

- [x] **"Download your agent" page (Linux installer script).** *(landed)*
  After a user verifies, show a page that lets them download a
  pre-configured installer script (`irs-agent-install.sh`) tailored to
  their account. Requirements:
    - The script is generated on demand and contains the **server URL**
      and a freshly **generated random API key** (e.g. 32-byte
      `secrets.token_urlsafe`) scoped to that user.
    - **The server never stores the plaintext API key.** Only a hash
      (bcrypt or argon2id, same scheme as user passwords) is persisted on
      the `Agent` row, alongside a short key prefix (first 8 chars) so
      we can identify it in logs/UI without being able to reconstruct it.
      Auth middleware hashes incoming keys and compares against the stored
      hash.
    - The key is shown / downloadable **exactly once**; if the user loses
      it they must generate a new one (which invalidates the old hash).
    - The installer script should: detect Python 3, `pip install` the
      agent (from a pinned wheel URL or a vendored tarball), write the
      config file with server URL + API key + default `claude_code`
      collector, install a `systemd --user` unit (or system unit if run
      as root), and start it. Print the journalctl command on success.
    - Linux only for the first cut; add Windows/macOS as follow-ups.
  Schema impact: `Agent` table needs `api_key_hash` (replacing/augmenting
  `api_key`) and `api_key_prefix`; migration must rotate any existing
  agents (force re-register) since plaintext keys can't be recovered.

- [x] **Workbench: run a session inside a Harness (portal → agent materialize).** *(landed)*
  Verified shipped: `RemoteSession.harness_id` + `pending_bundle`
  (`models.py:742-748`); bundle endpoint
  `GET /agent/remote/sessions/{sid}/bundle.tar.gz` packing HarnessFile +
  SessionUpload rows (`routers/remote.py:908-1004`); `Job.bundle_url` +
  `session_id` set in `_claim_pending` (`remote.py:837-869`); agent
  `_materialize` downloads with `X-Agent-Key`, extracts to per-session
  scratch dir, forces cwd, returns `workspace`
  (`agent/irs_agent/remote.py:164-215`); UI harness `<Select>`, header chip,
  and "preparing workspace" phase (`frontend/src/pages/Workbench.tsx`).
  Original spec below for reference.

  Let an analyst pick an existing `Harness` when starting a Workbench
  session; the agent materializes the harness on its own disk and runs
  Claude inside it, so the session inherits the harness's `.claude/`
  settings, `.mcp.json`, prompts, and tools.
    - Schema: `RemoteSession.harness_id` (FK, nullable),
      `RemoteSession.pending_bundle` bool. PATCH-able after creation.
    - New agent-auth endpoint
      `GET /agent/remote/sessions/{sid}/bundle.tar.gz` packs every
      `HarnessFile` row for `session.harness_id` (plus `SessionUpload`
      rows, see next item) into a tar stream. Guard:
      `session.agent_id == agent.id`.
    - `Job` gains `bundle_url` + `session_id`. `_claim_pending` sets
      them when `session.pending_bundle` is true.
    - Agent: on `bundle_url`, download with `X-Agent-Key`, extract into
      a **per-session scratch dir** at
      `platformdirs.user_data_dir("irs-agent")/sessions/<sid>/`, force
      `cwd` to that dir, then run `claude -p` as usual. `JobResult`
      gains `workspace`; server writes it to `session.cwd` and clears
      `pending_bundle` so follow-up turns `--resume` in place.
    - UI: harness `<Select>` in `NewSessionButton` (lists harnesses the
      viewer can see); session header shows a `Harness: {name}` chip
      linking to `/harnesses/{id}`; `pending_bundle` renders a
      "preparing workspace…" phase line on the first turn.
    - Owner-only throughout (`_own_session`), same as prompts.
    - Cleanup: archiving/deleting a session enqueues a
      `{"command":"cleanup","session_id":sid}` so the agent can
      `rm -rf` the scratch dir (best-effort). Follow-up, not a blocker.

- [x] **Workbench: upload files/folders/archives into a session.** *(landed)*
  Verified shipped: `SessionUpload` table with `session_id`/`relpath`/`sha256`/
  `size_bytes`/`content_enc` (`models.py:772-797`); `POST /remote/sessions/
  {sid}/files` multipart accepting loose files + `.zip`/`.tar`/`.tar.gz`
  unpacked via `_iter_archive` with `_safe_rel` traversal guard
  (`routers/remote.py:503-565`); `GET`/`DELETE .../files` list+remove
  (`remote.py:493-609`); drag-drop zone and "Files (N)" disclosure
  (`frontend/src/pages/Workbench.tsx:547-727`). Harness files extract first,
  uploads overlay on relpath collision. Original spec below for reference.

  Drop a zip / tarball / loose files / a folder onto a Workbench
  session; they're stored encrypted on the Verdict server, then pushed to
  the agent's per-session scratch dir alongside (or instead of) the
  harness so Claude can analyse them.
    - New `SessionUpload` table mirroring `HarnessFile`
      (`session_id` FK, `relpath`, `sha256`, `size_bytes`,
      `content_enc`). Caps: 200 MiB total / 2000 files / 25 MiB per
      file (tunable).
    - `POST /remote/sessions/{sid}/files` multipart: accepts loose
      files (with `relpaths[]`, same shape as harness upload) **and**
      `.zip` / `.tar` / `.tar.gz` which the server unpacks into rows
      using `_safe_rel` to reject path traversal. Sets
      `pending_bundle=true`.
    - `GET/DELETE /remote/sessions/{sid}/files` to list / remove staged
      uploads. Bundle tarball endpoint (above) includes these rows.
    - UI: drag-drop zone above the prompt box; "Files (N)" disclosure
      in the session header listing staged relpaths with per-file
      delete; "preparing workspace…" reuse from the harness item.
    - Uploads and harness compose: harness files extract first, uploads
      overlay on top (later upload wins on relpath collision).

- [ ] **CISO / admin chat over *structured* portal data (not raw reports).**
  Today's chat + analytics surfaces hand Claude the raw uploaded `.md`
  bodies. The CISO use-case is different: ask natural-language questions
  against the **triaged state** that lives in the DB after devs (via
  share links) and PSIRT analysts have set TP/FP/SBP, severities,
  assignees, etc. Example prompt:
  > "Show total number of items still needing triage, grouped by product,
  > and break each product down by severity."
  Requirements:
    - Admin-only chat page (reuse the existing chat UI shell) where
      Claude is given **tool access** to query the structured tables
      instead of report blobs. Minimum tool set:
        - `list_products()` → id, name, member count
        - `scan_summary(product_id?)` → per-scan tp/fp/sbp/dup/untriaged
          + highest_severity, post-triage
        - `finding_stats(product_id?, status?, severity?)` → counts of
          `Finding` rows sliced by product × status × severity
        - `list_findings(product_id?, status?, severity?, limit)` →
          title / severity / status / assigned_to / triaged_by for
          drill-down answers
      All tool results must respect the caller's RBAC scope (admin sees
      everything; if we later open this to managers, scope accordingly).
    - Answers reflect **current** `Finding.status` / `Finding.severity`
      as edited in the portal — i.e. read from `findings` / `vuln_scans`,
      never re-parse the source markdown.
    - Output can be returned inline and optionally persisted via the
      existing `save_as_report=true` path so the CISO can download or
      share the generated rollup.
    - Stretch: let the same prompt emit a CSV/table the UI can render
      (reuse the Analytics download path).

- [ ] **Agent auto-collects the harness + prompt and links them to the report.** *(mostly open — only manual linkage exists)*
  Status check: `PATCH /runs/{session_id}` accepts `harness_id` and links a
  harness to a run (`routers/runs.py:389-438`) — but everything *automatic*
  is missing: no `tree_hash` anywhere, no `GET /harnesses/lookup`, no
  `Harness.tree_hash` column, no `UserPromptSubmit` hook, no
  `/runs/{session_id}/prompts` endpoint, no `Run.prompts`/`RunPrompt` storage.
  Remaining work below.

  Today the agent's `PostToolUse` hook only ships `.md` reports and
  `poc/*` attachments. Extend it so a reviewer opening any report can
  see exactly *which harness* and *which prompt* produced it.
    - **Harness capture.** On the first hook event of a session, the
      agent inspects `payload["cwd"]` (Claude's working dir). If it
      looks like a harness (heuristic: contains `CLAUDE.md` /
      `harness.yml` / `.claude/`, or sits under a configured
      `harness_roots` path), compute a **tree hash** — sorted list of
      `(relpath, sha256)` for every tracked file, then sha256 the
      concatenation.
    - **Dedup.** New endpoint `GET /harnesses/lookup?tree_hash=…`
      returns `{id}` if a harness with that hash already exists, else
      404. Agent uploads (existing `POST /harnesses` multipart) only on
      miss. Server stores `Harness.tree_hash` (indexed) — needs a
      column + ALTER.
    - **Linkage.** Schema already has `Run.harness_id`; agent sets it
      via a new `PATCH /runs/{session_id}` (or piggy-back on the first
      `/reports/ingest` body: `{…, harness_id}` and have ingest write
      it through to the Run row). Report → Run (via `session_id`) →
      Harness then resolves in the UI: ReportView shows a "Harness:
      {name}" link, RunDetail shows the harness card.
    - **Prompt capture.** Register a second Claude Code hook on
      **`UserPromptSubmit`** (fires with `{prompt, session_id, cwd}`
      every time the user hits Enter). Agent POSTs to a new
      `/runs/{session_id}/prompts` endpoint; server appends to
      `Run.prompts` (new `Text` column, newline-joined, or a child
      `RunPrompt` table if we want per-turn timestamps). RunDetail and
      ReportView surface it as "Prompt(s) used". Same session_id key
      means it auto-links to the report + harness with no extra
      bookkeeping.
    - **Idempotency.** Tree-hash dedup makes harness upload a no-op on
      re-runs; prompt POST should upsert by `(session_id, sha256(text))`
      so hook retries don't duplicate.

- [ ] **Optional: stronger signup gate.**
  Domain allowlist alone means anyone with an `@example.com` address can
  squat on any local-part. If that becomes a problem, layer one of:
    - **Admin approval:** new signups land in a `pending` state and an admin
      promotes them from the UI.
    - **Invite codes:** admin generates one-time signup tokens, only those
      tokens let someone register.
    - **Email verification (deferred):** would require an authenticated
      outbound SMTP relay (your work account, Gmail app password, Brevo,
      etc.). Not free without some account somewhere.

- [x] **Structured vuln scan tables (Phase 1a).** *(landed)*
  Two typed entities — `VulnScan` (Type A: product, target, harness, scan_by,
  findings, FP/SBP/TP, duplicates, untriaged, highest severity) and `RunLog`
  (Type B: day/date/run/box/product/harness/prompt/results/POC/comment/complete)
  with a parent–child FK. Cookie-auth UI under `/ui/scans` plus a Bearer JSON
  API at `/scans`. Manual entry forms in the browser; auto-extraction from
  uploaded `.md` is Phase 1b.

- [x] **Chunked extraction for large vuln reports.** *(landed)*
  Reports up to ~200 KB are split at markdown `## ` / `### ` boundaries
  (with greedy merge + hard-slice fallback) and each chunk goes through
  Claude in its own call: first chunk fills the scan summary + runs, every
  chunk contributes findings. Findings are merged and deduplicated by
  normalized (title, severity). Single-shot path is preserved for small
  docs. Smoke-tested: a synthetic 120-finding report extracted all 120
  with correct severity-tier counts.

- [x] **One-shot import of pre-existing reports + POCs.** *(landed)*
  New `irs-agent import` subcommand walks configured collector paths +
  a handful of common locations (`~/my-reports`, `~/reports`,
  `~/claude-reports`, `~/.claude/projects`), buckets `.md` files as
  reports and anything under a `poc/` segment as attachments, prints a
  summary, prompts before uploading, and dedupes by sha so re-runs are
  no-ops. Skips noisy directories (`node_modules`, `.git`, `.cache`, etc.).
  install.sh runs it at the end of a fresh install if the shell is
  interactive; otherwise it tells the user the command to run later.

- [x] **POC attachments — agent auto-collects `poc/*` files.** *(landed)*
  New `Attachment` model, encrypted at rest, keyed off `session_id`. The
  Claude hook routes Write/Edit events by path: `.md` → Report (existing
  path), anything inside a `poc/` directory → Attachment via
  `POST /attachments/ingest`. On ingest the server auto-links to a
  `VulnScan` if one already exists for the same session. Scan detail page
  shows a "POC files & attachments" card with download links. Visibility
  flows through the same scope as Reports (own / team / project member /
  admin).

- [x] **Share-link guest triage (no login).** *(landed)*
  Per-scan share links at `/share/{token}` let product devs/PMs without an
  Verdict account mark each finding TP/FP/SBP/duplicate and leave free-text
  `dev_notes`. Token is `secrets.token_urlsafe(32)`; only its sha256 is
  stored (`ShareLink.token_hash` + 8-char `token_prefix`), shown once at
  creation. Links default to **30-day expiry** and **PoC hidden**
  (per-link `allow_poc` toggle). Guest writes are scoped to
  `Finding.status` + `Finding.dev_notes` only; `triaged_by` is stamped
  with the reviewer's self-reported name + `(via share <prefix>…)`. Each
  status change recomputes the scan's tp/fp/sbp/untriaged rollups.
  Authed mgmt (mint/list/revoke) at `POST/GET/DELETE /scans/{id}/share`,
  surfaced as a "Share for triage" card on the SPA scan detail page.

- [x] **Per-finding view (severity / title / CWE / remediation / steps / POC).** *(landed)*
  New `Finding` model under each `VulnScan` with severity, status, CWE, CVE,
  affected_component, description, steps_to_reproduce, remediation,
  proof_of_concept, references, assigned_to, triaged_by/at. Inline findings
  table on the scan detail page; full editable page at
  `/ui/scans/{scan_id}/findings/{finding_id}`. The extractor pulls findings
  out of the source markdown alongside the scan + runs.

- [x] **Auto-extract draft VulnScans from uploaded .md (Phase 1b).** *(landed)*
  On `Report` ingest, a background task sends the markdown to Claude with a
  strict JSON schema, parses the response, and persists a
  `VulnScan(state=draft)` + child `RunLog` rows linked back to the source
  report. The report view shows a "Review draft" banner; users edit /
  Confirm in the scans UI. If the markdown isn't a vuln report the
  extractor returns null and no draft is created.

- [x] **Full React migration (Scan/Finding/Report/Attachment/Agents).** *(landed)*
  Every primary interactive page lives in the SPA now: scan detail with
  inline edit + project picker + findings table + runs log + attachments,
  finding detail with all editable long-text fields, report view with
  markdown rendering (`react-markdown` + sanitize), attachment view with
  inline text/image/PDF preview + 1 MiB cap, and a full agents page with
  one-time-key reveal + copy + download. Backend exposes the additional
  metadata (session_id, project_id, agent hostname, derived_scan_id) on
  Report and `findings_list` on VulnScanDetail. Jinja `/ui/*` routes
  remain available for backward links (register, agent install.sh).

- [x] **React + Tailwind frontend (initial slice).** *(landed)*
  Vite + React + TypeScript + Tailwind SPA at `/app/*`. Dark mode, sidebar
  nav, shadcn-style components. Pages: Login, Reports, Runs (list + detail
  with title/project edit), Scans, Projects (list + detail with member
  management). Detail pages for Scan/Finding/Report and the Agent install
  flow still use the Jinja routes at `/ui/*` (migrating page-by-page).
  Multi-stage Docker build does `npm ci` + `vite build` then serves the
  static output from FastAPI. API routes are unchanged.

- [x] **New permission model — admin/manager/user.** *(landed 2026-05-20)*
  - **Admin**: sees everything; manages users, teams, projects globally.
  - **Manager**: visibility identical to a regular user (own uploads +
    projects they're a member of). Manager is just a label today — happy
    to extend with elevated mutation rights later.
  - **User**: own uploads + project membership.
  - Team membership no longer grants visibility (kept as a label).
  - Project list is visible to **every** logged-in user; project contents
    (members, runs, scans, reports) are visible only to admin + owner +
    members. Mutations (rename/delete/add member) are admin + owner only.
  - SPA detail page renders a "you're not a member" stub for outsiders.
  - New `DELETE /projects/{id}` JSON endpoint for SPA parity.

- [x] **Projects + run titles + project-aware visibility.** *(landed)*
  New `Project` entity with members (any logged-in user can create; creator
  is auto-added). New `Run` table keyed by Claude `session_id`, with editable
  `title` and optional `project_id`. Project membership grants visibility
  on top of the existing user/manager/admin scope — members of a project
  see all runs/reports/scans that belong to it. Reports without a session
  can also be attached directly via `Report.project_id`.

- [x] **Run grouping by Claude session_id.** *(landed)*
  Hook payload's `session_id` flows through agent → server → `Report.session_id`.
  The extractor merges all uploads in a session into a single `VulnScan`
  (via `source_session_id`) so 3 files from one Claude run become one scan
  with combined findings/runs. New **Runs** view (`/ui/runs`) groups
  reports per session; report view shows a "Part of run" banner.

## Nice-to-have (post-blocker)

- [x] Pagination / filtering on the reports table (by tool, owner, date).
      *(landed — `list_reports` filters by user/tool/project/scan + limit/offset,
      `routers/reports.py:275-316`; Reports.tsx surfaces them.)*
- [x] Per-report detail view instead of only a download link.
      *(landed — `ReportView.tsx` at route `/reports/{id}`, backed by
      `GET /reports/{report_id}` returning full body, `routers/reports.py:319`.)*
- [~] Surface agent health (last-seen / last-uploaded timestamp per agent).
      *(partial — `last_seen` + `last_ip` tracked and shown on the Agents page;
      `last_uploaded` is still not tracked.)*
- [ ] Streaming chat responses instead of one-shot.
      *(still one-shot — `provider.chat()` blocks, no `stream=True`.)*
- [ ] Audit log for admin actions (user create, role change, team change).
      *(still none — no audit table/logging on the users/teams mutations.)*

## Known issues / things to fix

- [ ] No indication when Claude API key is missing or invalid — chat just
      fails silently from the user's perspective. *(still open — provider
      raises `RuntimeError` unhandled in `chat.py`; UI shows generic
      "Chat request failed".)*
- [x] ~~Owner column on the reports table shows a truncated user id, not the
      user's email.~~ *(fixed — Reports table now renders `owner_email`,
      `frontend/src/pages/Reports.tsx:31,473-474`.)*
- [ ] No CSRF protection on the HTMX form posts under `/ui/*`. *(still open —
      no CSRF middleware/token anywhere in `server/app`.)*
- [ ] Legacy Jinja login still silently swallows errors (redirects `?err=1`,
      template renders nothing). The React login at `/app` *does* show
      "Incorrect email or password." — so this only bites anyone still on the
      Jinja `/ui` login. Fix or retire the Jinja login.

## Testing plan (after the above)

1. Stand up server via `docker compose up --build`.
2. Through the UI: create a team, create a manager + two users on it,
   change the admin password.
3. Install agent on a second machine, point it at a directory with
   `*.md` files, confirm uploads appear in the portal with summaries.
4. Exercise chat: ask about a specific report id, then "generate a
   weekly rollup" with `save_as_report=true` and confirm a new
   `generated` report shows up.
5. Verify RBAC: user sees only own reports; manager sees the team's;
   admin sees everything.
