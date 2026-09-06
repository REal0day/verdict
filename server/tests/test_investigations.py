"""Investigations pipeline — foundation (data model + per-stage routing).

No execution yet: this pins the Case -> Finding (via scan) shape, the
StageRun audit trail, and the per-stage model resolution order
(stage -> case -> project -> team -> server default).
"""
import base64, os, pathlib, secrets, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models
from app.auth import hash_password
from app.ai import scope
from app.config import provider_keys


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.Base.metadata.create_all(bind=engine)
    s = Session()

    team = models.Team(name="core"); s.add(team); s.flush()
    user = models.User(email="a@example.com", password_hash=hash_password("pw"),
                       role=models.Role.user, team_id=team.id)
    s.add(user); s.flush()
    proj = models.Project(name="WidgetX", created_by=user.id)
    s.add(proj); s.flush()

    scan = models.VulnScan(user_id=user.id, project_id=proj.id, product="WidgetX",
                           state=models.ScanState.draft)
    s.add(scan); s.flush()
    case = models.Case(user_id=user.id, project_id=proj.id, scan_id=scan.id,
                       title="RCE in upload handler",
                       status=models.CaseStatus.intake)
    s.add(case); s.commit()

    # Both providers configured so pins are considered valid.
    monkeypatch.setattr(provider_keys, "openai_api_key", "sk-test")
    monkeypatch.setattr(provider_keys, "anthropic_api_key", "sk-ant-test")

    s.team, s.user, s.proj, s.scan, s.case = team, user, proj, scan, case
    yield s
    s.close()


# ---------------- shape ----------------

def test_case_reuses_the_scans_findings(db):
    """A Case's findings ARE its scan's findings — no duplicate table."""
    f1 = models.Finding(scan_id=db.scan.id, user_id=db.user.id, title="A",
                        severity=models.Severity.high)
    f2 = models.Finding(scan_id=db.scan.id, user_id=db.user.id, title="B",
                        severity=models.Severity.low)
    db.add_all([f1, f2]); db.commit()
    case = db.get(models.Case, db.case.id)
    titles = {f.title for f in case.scan.finding_rows}
    assert titles == {"A", "B"}, "case -> scan -> findings should surface both"


def test_one_case_many_findings_one_product(db):
    """The batch-import shape: one Case, N findings, one product."""
    for i in range(5):
        db.add(models.Finding(scan_id=db.scan.id, user_id=db.user.id, title=f"V{i}"))
    db.commit()
    case = db.get(models.Case, db.case.id)
    assert len(case.scan.finding_rows) == 5
    assert case.project_id == db.proj.id


def test_case_source_is_one_to_one_and_cascades(db):
    src = models.CaseSource(case_id=db.case.id, kind=models.CaseSourceKind.local_path,
                            local_path="/code/widgetx")
    db.add(src); db.commit()
    case = db.get(models.Case, db.case.id)
    assert case.source is not None
    assert case.source.local_path == "/code/widgetx"
    # deleting the case takes the source with it
    db.delete(case); db.commit()
    assert db.query(models.CaseSource).count() == 0


def test_stage_runs_are_an_audit_trail(db):
    f = models.Finding(scan_id=db.scan.id, user_id=db.user.id, title="A")
    db.add(f); db.flush()
    for _ in range(3):  # reruns pile up rather than overwrite
        db.add(models.StageRun(case_id=db.case.id, finding_id=f.id,
                               stage=models.StageType.source))
    db.commit()
    runs = db.query(models.StageRun).filter_by(finding_id=f.id).all()
    assert len(runs) == 3
    assert all(r.status == models.StageRunStatus.pending for r in runs)


def test_report_stage_is_case_level_no_finding(db):
    run = models.StageRun(case_id=db.case.id, finding_id=None,
                          stage=models.StageType.report)
    db.add(run); db.commit()
    assert run.finding_id is None
    assert run.stage == models.StageType.report


def test_pending_runs_are_the_queue(db):
    f = models.Finding(scan_id=db.scan.id, user_id=db.user.id, title="A")
    db.add(f); db.flush()
    db.add(models.StageRun(case_id=db.case.id, finding_id=f.id,
                           stage=models.StageType.impact,
                           status=models.StageRunStatus.pending))
    db.add(models.StageRun(case_id=db.case.id, finding_id=f.id,
                           stage=models.StageType.remediation,
                           status=models.StageRunStatus.done))
    db.commit()
    queued = db.query(models.StageRun).filter_by(
        status=models.StageRunStatus.pending).all()
    assert len(queued) == 1 and queued[0].stage == models.StageType.impact


# ---------------- per-stage model routing ----------------

def test_stage_with_no_pins_uses_the_server_default(db):
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.report)
    db.add(run); db.commit()
    assert scope.for_stage(db, run.id).is_default


def test_case_default_applies_when_stage_has_no_pin(db):
    db.case.ai_provider = "openai"; db.case.ai_model = "gpt-4o-mini"; db.commit()
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.report)
    db.add(run); db.commit()
    c = scope.for_stage(db, run.id)
    assert (c.provider, c.model, c.source) == ("openai", "gpt-4o-mini", "case")


def test_stage_pin_beats_case_default(db):
    db.case.ai_provider = "openai"; db.commit()
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.poc,
                          ai_provider="anthropic", ai_model="claude-haiku-4-5")
    db.add(run); db.commit()
    c = scope.for_stage(db, run.id)
    assert (c.provider, c.model, c.source) == ("anthropic", "claude-haiku-4-5", "stage")


def test_stage_falls_through_case_to_project(db):
    db.proj.ai_provider = "openai"; db.commit()   # project pinned, case/stage not
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.source)
    db.add(run); db.commit()
    c = scope.for_stage(db, run.id)
    assert (c.provider, c.source) == ("openai", "project")


def test_stage_falls_through_to_the_owners_team(db):
    db.team.ai_provider = "openai"; db.commit()   # only the team is pinned
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.source)
    db.add(run); db.commit()
    c = scope.for_stage(db, run.id)
    assert (c.provider, c.source) == ("openai", "team")


def test_invalid_stage_pin_degrades_instead_of_failing(db):
    db.case.ai_provider = "openai"; db.commit()
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.poc,
                          ai_provider="not-a-provider")
    db.add(run); db.commit()
    # stage pin is junk -> fall through to the case default, not an error
    c = scope.for_stage(db, run.id)
    assert (c.provider, c.source) == ("openai", "case")


def test_unconfigured_stage_pin_degrades(db, monkeypatch):
    monkeypatch.setattr(provider_keys, "gemini_api_key", None)
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.poc,
                          ai_provider="gemini", ai_model="gemini-1.5-pro")
    db.add(run); db.commit()
    assert scope.for_stage(db, run.id).is_default


def test_stage_pin_alias_is_canonicalised(db):
    run = models.StageRun(case_id=db.case.id, stage=models.StageType.poc,
                          ai_provider="claude")
    db.add(run); db.commit()
    assert scope.for_stage(db, run.id).provider == "anthropic"


def test_for_case_reads_the_case_default(db):
    db.case.ai_provider = "openai"; db.commit()
    assert scope.for_case(db, db.case.id).provider == "openai"
    assert scope.for_case(db, None).is_default
