"""Which model should answer *this* request?

A single server-wide provider is too blunt. A team reviewing third-party
dependencies may be happy sending them to a hosted API, while the team working
on their own source is not — and that is the same deployment.

Resolution order, most specific first:

    project.ai_provider/ai_model  ->  team.ai_provider/ai_model  ->  server default

Provider and model resolve together: pinning a provider without a model uses
that provider's own configured model, and pinning a model without a provider
applies it to whichever provider is already active.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .base import PROVIDERS, canonical, is_configured

log = logging.getLogger("irs.ai.scope")


@dataclass(frozen=True)
class ScopedChoice:
    provider: str | None    # None = server default
    model: str | None       # None = that provider's configured model
    source: str             # "project" | "team" | "default" — for logging/UI

    @property
    def is_default(self) -> bool:
        return self.provider is None and self.model is None


DEFAULT = ScopedChoice(None, None, "default")


def _valid(provider: str | None) -> str | None:
    """Ignore a pin that names an unknown or unconfigured provider.

    Falling back to the default beats failing a background summarisation
    because someone pinned a provider and later cleared its key.
    """
    if not provider:
        return None
    name = canonical(provider)
    if name not in PROVIDERS:
        log.warning("ignoring unknown pinned provider %r", provider)
        return None
    if not is_configured(name):
        log.warning("ignoring pinned provider %r — not configured", name)
        return None
    return name


def _pin(provider: str | None, model: str | None, source: str) -> ScopedChoice | None:
    """Turn a raw (provider, model) pin into a ScopedChoice, or None to fall through.

    Rules:
      - a provider is named and usable   -> pin it (with the model if given)
      - a provider is named but unusable -> the whole pin is void; the model
        was chosen *for* that provider and is meaningless on another, so it
        dies with it (fall through to the next scope level)
      - only a model is given            -> apply it to whatever provider is
        already active
      - nothing usable                   -> None
    """
    if provider:
        name = _valid(provider)
        if not name:
            return None
        return ScopedChoice(name, model or None, source)
    if model:
        return ScopedChoice(None, model, source)
    return None


def for_project(db: Session, project_id: str | None) -> ScopedChoice:
    from .. import models

    if project_id:
        proj = db.get(models.Project, project_id)
        if proj:
            c = _pin(proj.ai_provider, proj.ai_model, "project")
            if c:
                return c
    return DEFAULT


def for_user(db: Session, user_id: str | None) -> ScopedChoice:
    from .. import models

    if user_id:
        user = db.get(models.User, user_id)
        if user and user.team_id:
            team = db.get(models.Team, user.team_id)
            if team:
                c = _pin(team.ai_provider, team.ai_model, "team")
                if c:
                    return c
    return DEFAULT


def resolve(
    db: Session, *, project_id: str | None = None, user_id: str | None = None
) -> ScopedChoice:
    """Most specific pin wins: project, then the user's team, then default."""
    choice = for_project(db, project_id)
    if not choice.is_default:
        return choice
    return for_user(db, user_id)


# --------------------------------------------------------------------- cases

def for_case(db: Session, case_id: str | None) -> ScopedChoice:
    from .. import models

    if case_id:
        case = db.get(models.Case, case_id)
        if case:
            c = _pin(case.ai_provider, case.ai_model, "case")
            if c:
                return c
    return DEFAULT


def for_stage(db: Session, stage_run_id: str | None) -> ScopedChoice:
    """Resolve the model for one pipeline stage.

    Order, most specific first: the StageRun's own pin, then its Case's default,
    then the Case's project, then the case owner's team, then the server
    default. Invalid or unconfigured pins fall through, same as elsewhere.
    """
    from .. import models

    if not stage_run_id:
        return DEFAULT
    stage = db.get(models.StageRun, stage_run_id)
    if not stage:
        return DEFAULT

    c = _pin(stage.ai_provider, stage.ai_model, "stage")
    if c:
        return c

    case = db.get(models.Case, stage.case_id)
    if not case:
        return DEFAULT
    c = _pin(case.ai_provider, case.ai_model, "case")
    if c:
        return c
    choice = for_project(db, case.project_id)
    if not choice.is_default:
        return choice
    return for_user(db, case.user_id)
