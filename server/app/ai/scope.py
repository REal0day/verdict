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


def for_project(db: Session, project_id: str | None) -> ScopedChoice:
    from .. import models

    if project_id:
        proj = db.get(models.Project, project_id)
        if proj and (proj.ai_provider or proj.ai_model):
            p = _valid(proj.ai_provider)
            if p or proj.ai_model:
                return ScopedChoice(p, proj.ai_model or None, "project")
    return DEFAULT


def for_user(db: Session, user_id: str | None) -> ScopedChoice:
    from .. import models

    if user_id:
        user = db.get(models.User, user_id)
        if user and user.team_id:
            team = db.get(models.Team, user.team_id)
            if team and (team.ai_provider or team.ai_model):
                p = _valid(team.ai_provider)
                if p or team.ai_model:
                    return ScopedChoice(p, team.ai_model or None, "team")
    return DEFAULT


def resolve(
    db: Session, *, project_id: str | None = None, user_id: str | None = None
) -> ScopedChoice:
    """Most specific pin wins: project, then the user's team, then default."""
    choice = for_project(db, project_id)
    if not choice.is_default:
        return choice
    return for_user(db, user_id)
