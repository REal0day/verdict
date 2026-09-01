# Verdict — AI Report Server

> Pre-test status: see [ROADMAP.md](./ROADMAP.md) for known gaps (UI polish,
> chat loading indicator, user/team management screens) that should land
> before broad agent testing.


Centralised collection + viewing of AI coding-tool reports (Claude Code, OpenAI,
Gemini, Grok). A lightweight cross-platform **agent** watches directories for
`*.md` files and ships them over TLS to a **FastAPI server** where they are
encrypted at rest, summarised by the AI provider you configure, and browsable
via a web UI with team-scoped RBAC. Users can also chat with the model about
their reports and have it generate new server-side reports.

Server-side AI is provider-pluggable — Anthropic, OpenAI, Gemini and xAI are
implemented today and selected with `IRS_DEFAULT_AI_PROVIDER`. See
[ROADMAP.md](./ROADMAP.md#model-agnostic-ai-support) for the work that remains
to make every surface (credential storage, model choice, the Workbench agent)
fully model-agnostic.

```
┌──────────┐  *.md   ┌──────────┐  TLS+API key  ┌──────────────┐
│ ClaudeCd │ ──────▶ │ irs-agent│ ─────────────▶ │  Verdict server  │──▶ Postgres (AES-GCM at rest)
│ OpenAI…  │         │ (watch)  │                │  FastAPI     │──▶ AI provider (summaries / chat)
└──────────┘         └──────────┘                └──────────────┘
```

## Roles
| role    | sees                                  |
|---------|---------------------------------------|
| user    | own reports                           |
| manager | own + everyone on the same `team_id`  |
| admin   | everything; can create users & teams  |

## Quick start (server)
```bash
make env                    # generates IRS_SECRET_KEY / IRS_ENCRYPTION_KEY / admin pw into .env
                            # (prompts before overwriting; backs up old .env)
# then edit .env to add ANTHROPIC_API_KEY etc.
docker compose up --build
# UI:      http://localhost:8000/
# OpenAPI: http://localhost:8000/docs
# default admin: admin@example.com / admin  (change immediately)
```
Put a TLS-terminating reverse proxy (Caddy/Traefik/nginx) in front for production.

### Secret generation
`./scripts/gen-env.sh` (or `make env`) generates cryptographically-random values
for `IRS_SECRET_KEY`, `IRS_ENCRYPTION_KEY`, and `IRS_BOOTSTRAP_ADMIN_PASSWORD`.
It **warns and asks for confirmation** before touching `.env`, creates a
timestamped backup, and explicitly calls out that rotating
`IRS_ENCRYPTION_KEY` will render existing encrypted reports unreadable.

| command | behaviour |
|---|---|
| `make env` / `./scripts/gen-env.sh` | regenerate all three keys (interactive confirm) |
| `make env-missing` | only fill keys that are empty/placeholder; safe to re-run |
| `./scripts/gen-env.sh -y` | non-interactive (CI) |
| `./scripts/gen-env.sh -f path/.env` | target a different env file |

### Admin bootstrap (via API)
```bash
# get token
TOK=$(curl -s -X POST localhost:8000/auth/token -d 'username=admin@example.com&password=admin' | jq -r .access_token)
# create team
curl -s -X POST localhost:8000/teams -H "Authorization: Bearer $TOK" -H 'content-type: application/json' -d '{"name":"core"}'
# create user on that team
curl -s -X POST localhost:8000/users -H "Authorization: Bearer $TOK" -H 'content-type: application/json' \
  -d '{"email":"alice@example.com","password":"pw","role":"user","team_id":"<TEAM_ID>"}'
```

## Quick start (agent — Linux / Windows)
```bash
pip install ./agent          # or build a wheel / use pyinstaller for a single exe
irs-agent init --server https://irs.example.com \
               --email alice@example.com --password pw \
               --enable claude_code --enable openai \
               --path claude_code=~/work/ai-reports
irs-agent run                # foreground (Ctrl-C to quit)
```
The agent:
* does an initial scan of every configured dir for `*.md`
* then watches (recursively) for create/modify/move and uploads changed files
* dedupes by sha256 so re-runs are idempotent

### Keeping the agent running
`run` is foreground only. To keep it alive after you log out and across crashes
— **without** depending on any particular init system — use the built-in
supervisor instead of wiring up systemd by hand:
```bash
irs-agent start     # detaches from the terminal, restarts the worker on crash
irs-agent status    # running / not running
irs-agent stop
```
`start` survives logout on its own (no systemd, no linger, no root). It does
**not** survive a reboot by itself — for that, register a boot hook once:
```bash
irs-agent install-service        # auto-detects the best mechanism for this host
irs-agent uninstall-service      # undo it
```
`install-service` degrades gracefully so the same command works everywhere:

| Platform | Picks | Falls back to |
|---|---|---|
| Linux (systemd present) | `--user` systemd unit + `loginctl enable-linger` | — |
| Linux (no systemd) | `@reboot` crontab entry → `irs-agent start` | printed manual steps |
| macOS | launchd `LaunchAgent` (`RunAtLoad` + `KeepAlive`) | — |
| Windows | Task Scheduler task (runs at logon) | — |

Override the choice with `--method systemd|cron|launchd|schtasks|manual`, or
`--method systemd --system` for a root-level unit instead of a per-user one.

> Why this exists: a hand-rolled `--user` systemd unit dies on logout unless
> `loginctl enable-linger` is set — a footgun that silently kills the agent.
> `install-service` sets linger for you (and warns if it can't), so the
> Workbench doesn't go dark the next time you close your SSH session.

### Adding a new collector
Drop a file in `agent/irs_agent/collectors/<name>.py`:
```python
from .base import Collector, register, _home
register(Collector(name="mytool", default_paths=[_home("mytool-out")], glob="*.md"))
```
…and a matching enum value in `server/app/models.py:SourceTool` if you want it tagged.

## Encryption model
* **In transit:** TLS between agent ↔ server (terminate at your reverse proxy).
* **At rest:** every report body + summary is AES-256-GCM encrypted with a
  server master key (`IRS_ENCRYPTION_KEY`). DB stores only ciphertext.
* The server can decrypt (option-A model) so managers/admins can read scoped
  reports and Claude can summarise/chat over them.

## Chat / generated reports
`POST /chat` (or the UI panel):
```json
{ "message": "Produce a weekly rollup of these runs",
  "report_ids": ["<id1>", "<id2>"],
  "save_as_report": true,
  "save_filename": "weekly-rollup.md" }
```
The reply is stored as a `source_tool=generated` report under the caller's user.

## Scaling to thousands of users
* Move report blobs to object storage (S3/MinIO) — keep only metadata + key in
  Postgres. The `crypto.encrypt` boundary already isolates this.
* Move summarisation + chat to a Celery/RQ worker pool behind Redis (compose
  stubs included, commented out).
* Run multiple `server` replicas behind a load balancer; they're stateless.
* Add Alembic migrations (dir scaffolded) instead of `create_all`.

## Repo layout
```
server/   FastAPI app, AI providers, Dockerfile
agent/    cross-platform collector (pip-installable, `irs-agent` CLI)
```
