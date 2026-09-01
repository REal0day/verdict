"""Shared prompt-template library for the Workbench.

Any logged-in user can list/use any template and add new ones; the creator
(or an admin) can edit/delete. Bodies may contain {{variables}} (e.g.
{{product}}) that the Workbench picker fills in before running.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..auth import get_current_user
from ..database import get_db
from ..models import Role

log = logging.getLogger("irs.prompts")
router = APIRouter(prefix="/prompts", tags=["prompts"])


def _out(db: Session, p: models.PromptTemplate) -> schemas.PromptTemplateOut:
    o = schemas.PromptTemplateOut.model_validate(p)
    if p.created_by:
        u = db.get(models.User, p.created_by)
        if u:
            o.created_by_email = u.email
    return o


def _can_edit(p: models.PromptTemplate, user: models.User) -> bool:
    return user.role == Role.admin or p.created_by == user.id


@router.get("", response_model=list[schemas.PromptTemplateOut])
def list_prompts(
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    rows = (db.query(models.PromptTemplate)
              .order_by(models.PromptTemplate.category, models.PromptTemplate.title).all())
    return [_out(db, p) for p in rows]


@router.post("", response_model=schemas.PromptTemplateOut, status_code=201)
def create_prompt(
    body: schemas.PromptTemplateIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = models.PromptTemplate(
        title=body.title.strip(), description=body.description.strip(),
        category=body.category.strip(), body=body.body, created_by=viewer.id,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return _out(db, p)


@router.get("/{prompt_id}", response_model=schemas.PromptTemplateOut)
def get_prompt(
    prompt_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.PromptTemplate, prompt_id)
    if not p:
        raise HTTPException(404, "Not found")
    return _out(db, p)


@router.put("/{prompt_id}", response_model=schemas.PromptTemplateOut)
def update_prompt(
    prompt_id: str,
    body: schemas.PromptTemplateIn,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.PromptTemplate, prompt_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the author or an admin can edit this prompt")
    p.title = body.title.strip()
    p.description = body.description.strip()
    p.category = body.category.strip()
    p.body = body.body
    db.commit()
    db.refresh(p)
    return _out(db, p)


@router.delete("/{prompt_id}", status_code=204)
def delete_prompt(
    prompt_id: str,
    db: Session = Depends(get_db),
    viewer: models.User = Depends(get_current_user),
):
    p = db.get(models.PromptTemplate, prompt_id)
    if not p:
        raise HTTPException(404, "Not found")
    if not _can_edit(p, viewer):
        raise HTTPException(403, "Only the author or an admin can delete this prompt")
    db.delete(p)
    db.commit()


# ---------------- starter seed ----------------

_COMPLIANCE_BODY = """You are operating a compliance-audit harness (Python, in this workspace). Read README.md,
OPERATING_PROMPT.md, and controls/SCHEMA.md first. It audits a source tree plus a black-box
target by spawning ONE agent per compliance item, so it is NOT limited by any single context
window. Perform the audit by RUNNING the harness — do not try to read the whole codebase
yourself.

INPUTS
- Source tree: the source I selected is mounted in this workspace. Locate its root
  (the directory containing the application code) and use that path. If unsure, list the
  top-level directories and pick the one with the app source.
- Target — {{product}}, reachable as an external client only (no ssh, no server logs, no DB):
    base_url: https://{{host}}:{{port}}
    username: {{username}}
    password: {{password}}
    # if {{product}} uses a token/API key instead of or in addition to a password,
    # put it under target.extra.api_token in config.yaml
- Compliance manifest: my 15 chapters are in controls/controls.yaml. If that file is not
  present, build it from controls/SCHEMA.md (one item per requirement, leave evidence_type
  blank so the triage phase classifies each one).

STEPS
1. Copy config.example.yaml to config.yaml. Set source_root to the source path you located,
   set the target block to the {{product}} values above, and point `controls:` at the
   manifest. Put the credentials and URL ONLY in config.yaml — do not echo them back to me
   or write them into any other file or log.
2. Run:  python run.py --config config.yaml --self-test   — fix anything it reports
   (missing path, missing ANTHROPIC_API_KEY, missing probe binary).
3. Run the full audit:  python run.py --config config.yaml
   It is resumable and parallel; let it finish. If it dies, re-run the same command —
   completed items are skipped. {{product}} runs HTTPS with a self-signed cert; that is
   fine, the probes already use curl -k / openssl as needed.
4. Open reports/coverage.md and ENFORCE completeness before reporting:
   - Every chapter's "Error" column must be 0. Re-run any errored item with:
       python run.py --config config.yaml --force --chapter "<chapter name>"
     (or raise max_turns in config.yaml if an agent ran out of turns).
   - Review the "Blind spots" list. If an area you'd expect a control to cover was never
     touched, a manifest item is probably missing — add it and re-run.
5. Deliver these artifacts and do NOT summarize them away:
   - reports/summary.md       scoreboard + links
   - reports/coverage.md      code coverage + per-chapter item coverage
   - reports/audit_log.jsonl  every grep / read / probe, tagged by control (what was searched)
   - reports/findings.json    verdicts with evidence
   - reports/<chapter>.md     per-chapter narrative

RULES
- Treat indeterminate and attestation_required as UNRESOLVED (residual risk), never as passes.
- Every dynamic "compliant" must carry a negative control in its evidence; flag any finding
  the verify panel refuted (scoreboard "verifier-refuted" count / findings.json verified:false).
- Report honestly: if any Error column is non-zero or a whole area is a blind spot, state
  that the audit is INCOMPLETE and exactly what is missing."""


def seed_starter_prompts(db: Session):
    """Insert the compliance-audit template once, if the library is empty."""
    if db.query(models.PromptTemplate).first():
        return
    admin = db.query(models.User).filter(models.User.role == Role.admin).first()
    db.add(models.PromptTemplate(
        title="Compliance audit (harness)",
        description="Run the compliance-audit harness against a product's source tree + a "
                    "black-box target. Fill in the product and target connection details.",
        category="compliance",
        body=_COMPLIANCE_BODY,
        created_by=admin.id if admin else None,
    ))
    db.commit()
    log.info("seeded starter prompt template(s)")
