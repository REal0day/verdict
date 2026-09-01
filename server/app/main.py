import logging

from fastapi import FastAPI, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from . import models
from .auth import hash_password, verify_password, create_access_token
from .config import settings
from .database import Base, engine, get_db
from .routers import auth as r_auth, users as r_users, teams as r_teams
from .routers import agents as r_agents, reports as r_reports, chat as r_chat
from .routers import registration as r_registration
from .routers import agent_install as r_agent_install
from .routers import scans as r_scans
from .routers import runs as r_runs
from .routers import projects as r_projects
from .routers import attachments as r_attachments
from .routers import imports as r_imports
from .routers import notifications as r_notifications
from .routers import project_requests as r_project_requests
from .routers import invites as r_invites
from .routers import harnesses as r_harnesses
from .routers import analytics as r_analytics
from .routers import share as r_share
from .routers import remote as r_remote
from .routers import settings as r_settings
from .routers import prompts as r_prompts
from .ai.errors import AIProviderError

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("irs")

app = FastAPI(title="Verdict — AI Report Server")

# API routers
app.include_router(r_auth.router)
app.include_router(r_users.router)
app.include_router(r_teams.router)
app.include_router(r_agents.router)
app.include_router(r_reports.router)
app.include_router(r_chat.router)
app.include_router(r_registration.router)
app.include_router(r_agent_install.router)
app.include_router(r_scans.api)
app.include_router(r_scans.ui)
app.include_router(r_runs.router)
app.include_router(r_runs.api)
app.include_router(r_projects.api)
app.include_router(r_projects.ui)
app.include_router(r_attachments.router)
app.include_router(r_imports.router)
app.include_router(r_notifications.router)
app.include_router(r_project_requests.router)
app.include_router(r_invites.api)
app.include_router(r_harnesses.router)
app.include_router(r_analytics.router)
app.include_router(r_share.api)
app.include_router(r_share.public)
app.include_router(r_remote.api)
app.include_router(r_remote.sess_api)
app.include_router(r_remote.agent_api)
app.include_router(r_settings.router)
app.include_router(r_prompts.router)

# ---- AI provider failures ----
# Providers raise typed AIProviderError subclasses (missing key, rejected key,
# upstream down). Without this handler they surface as an opaque 500 and the
# UI can only say "request failed" — the operator never learns the key is the
# problem. One handler covers every route that touches a provider.
@app.exception_handler(AIProviderError)
async def _ai_provider_error(request: Request, exc: AIProviderError):
    from fastapi.responses import JSONResponse

    # Only admins can fix the key, so only they get told where to do it.
    is_admin = False
    try:
        from .database import SessionLocal
        db = SessionLocal()
        try:
            u = _user_from_cookie(request, db)
            is_admin = bool(u and u.role == models.Role.admin)
        finally:
            db.close()
    except Exception:  # never let the handler itself fail the response
        pass

    log.warning("AI provider error on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail(is_admin),
            "error": type(exc).__name__,
            "provider": exc.provider,
        },
    )


# UI
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)
    _ensure_onboarding_column()
    _bootstrap_admin()
    _load_ai_settings()
    _seed_prompts()


def _seed_prompts():
    from .database import SessionLocal
    from .routers.prompts import seed_starter_prompts
    db = SessionLocal()
    try:
        seed_starter_prompts(db)
    finally:
        db.close()


def _load_ai_settings():
    from .database import SessionLocal
    from .routers.settings import load_ai_settings
    db = SessionLocal()
    try:
        load_ai_settings(db)
    finally:
        db.close()


def _ensure_onboarding_column():
    """create_all doesn't ALTER existing tables. Adding users.onboarded_at
    (and similar later additions) needs explicit DDL bumps until Alembic
    is wired up."""
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded_at "
            "TIMESTAMP WITH TIME ZONE"
        ))
        # Existing users (created before onboarding existed) shouldn't see
        # the wizard — mark them onboarded as a one-time backfill.
        conn.execute(text(
            "UPDATE users SET onboarded_at = created_at "
            "WHERE onboarded_at IS NULL"
        ))
        # Access requests can carry an attached folder import.
        conn.execute(text(
            "ALTER TABLE project_access_requests "
            "ADD COLUMN IF NOT EXISTS import_id VARCHAR(36)"
        ))
        # Runs can reference a Harness (the folder Claude was run inside).
        conn.execute(text(
            "ALTER TABLE runs "
            "ADD COLUMN IF NOT EXISTS harness_id VARCHAR(36)"
        ))
        # FolderImport: pre-pinned product.
        conn.execute(text(
            "ALTER TABLE folder_imports "
            "ADD COLUMN IF NOT EXISTS project_id VARCHAR(36)"
        ))
        # VulnScan: who agreed with Claude's draft + when.
        conn.execute(text(
            "ALTER TABLE vuln_scans "
            "ADD COLUMN IF NOT EXISTS confirmed_by VARCHAR(36)"
        ))
        conn.execute(text(
            "ALTER TABLE vuln_scans "
            "ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMP WITH TIME ZONE"
        ))
        # Backfill confirmed_at for any scans that are already `confirmed`
        # so the UI doesn't flag historic data as un-agreed.
        conn.execute(text(
            "UPDATE vuln_scans SET confirmed_at = updated_at "
            "WHERE state = 'confirmed' AND confirmed_at IS NULL"
        ))
        # Findings: AI verdict + rationale + free-form tag list.
        conn.execute(text(
            "DO $$ BEGIN "
            "  CREATE TYPE aiverdict AS ENUM ('open','true_positive','false_positive'); "
            "EXCEPTION WHEN duplicate_object THEN NULL; "
            "END $$"
        ))
        conn.execute(text(
            "ALTER TABLE findings "
            "ADD COLUMN IF NOT EXISTS ai_verdict aiverdict NOT NULL DEFAULT 'open'"
        ))
        conn.execute(text(
            "ALTER TABLE findings "
            "ADD COLUMN IF NOT EXISTS ai_rationale TEXT NOT NULL DEFAULT ''"
        ))
        conn.execute(text(
            "ALTER TABLE findings "
            "ADD COLUMN IF NOT EXISTS tags JSON NOT NULL DEFAULT '[]'::json"
        ))
        # Findings: dev/PM triage notes set via share links.
        conn.execute(text(
            "ALTER TABLE findings "
            "ADD COLUMN IF NOT EXISTS dev_notes TEXT NOT NULL DEFAULT ''"
        ))
        # ProjectFile: which source-code component (if any) the file belongs to.
        conn.execute(text(
            "ALTER TABLE project_files "
            "ADD COLUMN IF NOT EXISTS component_id VARCHAR(36)"
        ))
        # RemoteSession: per-session Claude model (NULL = agent/CLI default).
        conn.execute(text(
            "ALTER TABLE remote_sessions "
            "ADD COLUMN IF NOT EXISTS model VARCHAR(128)"
        ))
        # VulnScan: optional human-friendly title (falls back to product).
        conn.execute(text(
            "ALTER TABLE vuln_scans "
            "ADD COLUMN IF NOT EXISTS title VARCHAR(255) NOT NULL DEFAULT ''"
        ))

    # ALTER TYPE … ADD VALUE can't run inside a transaction; needs its
    # own AUTOCOMMIT connection. Run it after the main DDL block above.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for v in ("report_uploaded",):
            try:
                conn.exec_driver_sql(
                    f"ALTER TYPE notificationkind ADD VALUE IF NOT EXISTS '{v}'"
                )
            except Exception as e:
                log.warning("ALTER TYPE notificationkind ADD VALUE %r failed: %s", v, e)


def _bootstrap_admin():
    from .database import SessionLocal
    db = SessionLocal()
    try:
        if not db.query(models.User).filter(models.User.role == models.Role.admin).first():
            u = models.User(
                email=settings.bootstrap_admin_email,
                password_hash=hash_password(settings.bootstrap_admin_password),
                role=models.Role.admin,
            )
            db.add(u)
            db.commit()
            log.info("Bootstrapped admin user %s", u.email)
    finally:
        db.close()


# ---- minimal HTML UI (cookie- or Bearer-backed JWT) ----
def _user_from_cookie(request: Request, db: Session):
    """Resolve the current user from EITHER the cookie OR a Bearer header.

    The name is legacy — the SPA can't carry the cookie on plain `<a href>`
    navigation, so it sends `Authorization: Bearer …` from fetch instead.
    Either path is acceptable; both verify the same JWT against the same
    secret."""
    from jose import jwt, JWTError

    # 1. Authorization: Bearer …
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        try:
            payload = jwt.decode(auth[7:].strip(), settings.secret_key, algorithms=["HS256"])
            u = db.get(models.User, payload.get("sub"))
            if u:
                return u
        except JWTError:
            pass

    # 2. cookie
    tok = request.cookies.get("irs_token")
    if not tok:
        return None
    try:
        payload = jwt.decode(tok, settings.secret_key, algorithms=["HS256"])
    except JWTError:
        return None
    return db.get(models.User, payload.get("sub"))


_SPA_DIR = __import__("pathlib").Path(__file__).parent / "static" / "spa"


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/app/", status_code=307)


@app.get("/app", include_in_schema=False)
def app_redirect():
    return RedirectResponse("/app/", status_code=307)


@app.get("/app/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    """Serve the React SPA. Real files (assets, favicon, etc.) come straight
    from the dist dir; everything else falls back to index.html so the SPA
    can handle client-side routes on hard refresh."""
    from fastapi.responses import FileResponse, PlainTextResponse
    if not _SPA_DIR.exists():
        return PlainTextResponse(
            "SPA bundle not present. Did Docker build complete?",
            status_code=503,
        )
    candidate = (_SPA_DIR / full_path).resolve()
    # don't allow escaping out of the dist dir
    try:
        candidate.relative_to(_SPA_DIR.resolve())
    except ValueError:
        candidate = _SPA_DIR / "index.html"
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(_SPA_DIR / "index.html")


@app.post("/ui/login")
def ui_login(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == email).first()
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/?err=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("irs_token", create_access_token(user.id), httponly=True, samesite="lax")
    return resp


@app.get("/ui/logout")
def ui_logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("irs_token")
    return resp


@app.get("/healthz")
def health():
    return {"ok": True}


def _load_viewable_report(request: Request, report_id: str, db: Session):
    """Cookie-auth + RBAC. Returns (user, report). user is None if not logged in."""
    from fastapi import HTTPException
    from .permissions import assert_can_view_report
    user = _user_from_cookie(request, db)
    if not user:
        return None, None
    rpt = db.get(models.Report, report_id)
    if not rpt:
        raise HTTPException(404, "Not found")
    assert_can_view_report(db, user, rpt)
    return user, rpt


@app.get("/ui/reports/{report_id}", response_class=HTMLResponse)
def ui_view_report(
    report_id: str, request: Request, db: Session = Depends(get_db)
):
    """Render a report's markdown as HTML for in-browser viewing."""
    from . import crypto
    from markdown_it import MarkdownIt

    user, rpt = _load_viewable_report(request, report_id, db)
    if user is None:
        return RedirectResponse("/", status_code=303)

    text = crypto.decrypt(rpt.content_enc).decode("utf-8", errors="replace")
    # html=False disables raw HTML pass-through (XSS defence on uploaded content).
    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})
    body_html = md.render(text)
    summary = crypto.decrypt_str(rpt.summary_enc) if rpt.summary_enc else ""

    derived_scan = (
        db.query(models.VulnScan)
        .filter(models.VulnScan.source_report_id == rpt.id)
        .first()
    )

    if user.role == models.Role.admin:
        my_projects = (
            db.query(models.Project).order_by(models.Project.name).all()
        )
    else:
        my_projects = sorted(user.projects, key=lambda p: p.name.lower())

    return templates.TemplateResponse(
        request,
        "report_view.html",
        {
            "user": user,
            "r": rpt,
            "body_html": body_html,
            "summary": summary,
            "derived_scan": derived_scan,
            "my_projects": my_projects,
        },
    )


@app.post("/ui/reports/{report_id}/project")
def ui_set_report_project(
    report_id: str,
    request: Request,
    project_id: str = Form(""),
    db: Session = Depends(get_db),
):
    user, rpt = _load_viewable_report(request, report_id, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    # Only the owner or admin can change project assignment on a report.
    if rpt.user_id != user.id and user.role != models.Role.admin:
        from fastapi import HTTPException
        raise HTTPException(403, "Only the owner (or an admin) can attach this report")
    if project_id == "":
        rpt.project_id = None
        msg = "Detached from project."
    else:
        proj = db.get(models.Project, project_id)
        if not proj:
            from fastapi import HTTPException
            raise HTTPException(400, "Project not found")
        if user.role != models.Role.admin and proj.created_by != user.id \
                and not any(m.id == user.id for m in proj.members):
            from fastapi import HTTPException
            raise HTTPException(403, "You're not a member of that project")
        rpt.project_id = proj.id
        msg = f"Attached to project '{proj.name}'."
    db.commit()
    from urllib.parse import quote
    return RedirectResponse(f"/ui/reports/{rpt.id}?ok={quote(msg)}", status_code=303)


def _mime_for(filename: str) -> str:
    """Pick a sensible response content-type by filename extension."""
    import mimetypes
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    overrides = {
        ".md": "text/markdown; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".tsv": "text/tab-separated-values; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".xml": "application/xml; charset=utf-8",
        ".yaml": "application/yaml; charset=utf-8",
        ".yml": "application/yaml; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
    }
    return overrides.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


@app.get("/ui/reports/{report_id}/download")
def ui_download_report(
    report_id: str, request: Request, db: Session = Depends(get_db)
):
    """Cookie-auth mirror of /reports/{id}/download for browser links."""
    from . import crypto
    from fastapi.responses import Response

    user, rpt = _load_viewable_report(request, report_id, db)
    if user is None:
        return RedirectResponse("/", status_code=303)
    return Response(
        content=crypto.decrypt(rpt.content_enc),
        media_type=_mime_for(rpt.filename),
        headers={"Content-Disposition": f'attachment; filename="{rpt.filename}"'},
    )


@app.post("/ui/chat", response_class=HTMLResponse)
def ui_chat(
    request: Request,
    message: str = Form(...),
    report_ids: str = Form(""),
    save_as_report: str | None = Form(None),
    db: Session = Depends(get_db),
):
    user = _user_from_cookie(request, db)
    if not user:
        return HTMLResponse("<p><em>not logged in</em></p>", status_code=401)
    from .routers.chat import chat as chat_handler
    body = __import__("app.schemas", fromlist=["ChatRequest"]).ChatRequest(
        message=message,
        report_ids=[r.strip() for r in report_ids.split(",") if r.strip()],
        save_as_report=bool(save_as_report),
    )
    try:
        resp = chat_handler(body, db, user)
    except Exception as e:
        return HTMLResponse(f"<article class='secondary'><b>error:</b> {e}</article>")
    extra = (
        f"<br><small>saved as report <code>{resp.generated_report_id}</code></small>"
        if resp.generated_report_id else ""
    )
    return HTMLResponse(
        f"<article><b>you:</b> {message}<br><b>assistant:</b> "
        f"<pre style='white-space:pre-wrap'>{resp.reply}</pre>{extra}</article>"
    )
