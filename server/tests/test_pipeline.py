"""Stage execution (JSON mode). Stages chat and return JSON; the source tree is
inlined as a digest. A fake provider whose chat() returns canned JSON drives
each stage; parse failures preserve the model's raw response for review.
"""
import base64, os, pathlib, secrets, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IRS_ENCRYPTION_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
os.environ.setdefault("IRS_SECRET_KEY", "test-secret")
os.environ["IRS_DATABASE_URL"] = "sqlite://"

import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app import database, models, crypto, pipeline
from app.auth import hash_password
from app.ai.errors import AIKeyInvalid


class FakeProvider:
    """chat() returns whatever `reply` is set to (a string). Records prompts."""
    display_name = "Fake"
    model = "fake-1"
    def __init__(self, reply="", context_window=200000):
        self.reply = reply
        self.context_window = context_window
        self.calls = []
    def chat(self, system, messages, max_tokens=None):
        self.calls.append(messages[-1]["content"])
        return self.reply


def _J(obj):  # a model that answers with clean JSON
    return FakeProvider(json.dumps(obj))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    database.engine = engine; database.SessionLocal = TS
    database.Base.metadata.create_all(bind=engine)
    db = TS()
    u = models.User(email="a@example.com", password_hash=hash_password("pw"), role=models.Role.user)
    db.add(u); db.flush()
    scan = models.VulnScan(user_id=u.id, product="P", state=models.ScanState.draft)
    db.add(scan); db.flush()
    case = models.Case(user_id=u.id, scan_id=scan.id, title="RCE",
                       report_enc=crypto.encrypt(b"attacker uploads .php -> RCE"))
    db.add(case); db.flush()
    finding = models.Finding(scan_id=scan.id, user_id=u.id, title="Unrestricted upload",
                             severity=models.Severity.critical, cwe="")
    db.add(finding); db.flush()
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "upload.py").write_text("def handle(f):\n    open('/webroot/'+f.name,'wb').write(f.read())  # no validation\n")
    src = models.CaseSource(case_id=case.id, kind=models.CaseSourceKind.local_path,
                            status=models.SourceStatus.ready, workspace_key=str(tmp_path))
    db.add(src); db.commit()
    db.case, db.finding, db.tmp = case, finding, tmp_path
    yield db
    db.close()


def _run(env, provider, stage=models.StageType.source, finding=True):
    import unittest.mock as m
    run = models.StageRun(case_id=env.case.id,
                          finding_id=env.finding.id if finding else None, stage=stage)
    env.add(run); env.commit()
    with m.patch.object(pipeline, "get_provider", lambda p, mdl: provider):
        pipeline.run_stage(env, run)
    env.refresh(run)
    return run


# ---------------- digest / json helpers ----------------

def test_source_digest_inlines_the_tree(env):
    from pathlib import Path
    d = pipeline._source_digest(Path(str(env.tmp)), budget=100_000)
    assert "api/upload.py" in d and "no validation" in d


def test_extract_json_handles_fences_and_prose():
    assert pipeline._extract_json('```json\n{"a":1}\n```') == {"a": 1}
    assert pipeline._extract_json('Sure! Here you go: {"a": 2} hope that helps') == {"a": 2}
    assert pipeline._extract_json('[1,2,3]') == [1, 2, 3]
    assert pipeline._extract_json('not json at all') is None


# ---------------- discover ----------------

def test_discover_populates_findings(env):
    provider = _J({"findings": [
        {"title": "Unrestricted file upload", "severity": "critical", "cwe": "CWE-434",
         "affected_component": "api/upload.py", "description": "no validation"},
        {"title": "Missing authz", "severity": "high"},
    ]})
    run = _run(env, provider, stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.done
    fs = env.query(models.Finding).filter_by(scan_id=env.case.scan_id).all()
    titles = {f.title for f in fs}
    assert "Unrestricted file upload" in titles and "Missing authz" in titles
    assert "| # | Title |" in crypto.decrypt(run.output_enc).decode()


def test_discover_bare_array_also_works(env):
    provider = FakeProvider(json.dumps([{"title": "Bare finding", "severity": "low"}]))
    run = _run(env, provider, stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.done
    assert env.query(models.Finding).filter_by(title="Bare finding").first() is not None


def test_discover_empty_is_handled(env):
    run = _run(env, _J({"findings": []}), stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.done
    assert "No vulnerabilities identified" in crypto.decrypt(run.output_enc).decode()


# ---------------- source ----------------

def test_source_updates_finding_and_uses_the_digest(env):
    provider = _J({"affected_component": "api/upload.py",
                   "summary": "no content-type check (api/upload.py:2)",
                   "key_locations": ["api/upload.py:2"], "confidence": "high"})
    run = _run(env, provider, stage=models.StageType.source)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert env.finding.affected_component == "api/upload.py"
    # the source digest (real file content) was placed in the prompt
    assert "no validation" in provider.calls[-1]


# ---------------- impact + cvss ----------------

def test_impact_computes_cvss31_and_updates_finding(env):
    provider = _J({
        "cvss31_vector": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
        "cvss40_vector": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
        "cvss40_score": 9.3, "cwe": "CWE-434",
        "cvss31_rationale": [{"metric": "AV", "value": "N", "reason": "network reachable."}],
        "summary": "Unauth RCE."})
    run = _run(env, provider, stage=models.StageType.impact)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert env.finding.cvss31_score == 9.8
    assert env.finding.severity == models.Severity.critical
    assert env.finding.cwe == "CWE-434"
    out = crypto.decrypt(run.output_enc).decode()
    assert "9.8" in out and "computed" in out and "model-estimated" in out


def test_impact_bad_vector_is_graceful(env):
    run = _run(env, _J({"cvss31_vector": "CVSS:3.1/AV:N/AC:L", "cwe": "CWE-434", "summary": "x"}),
               stage=models.StageType.impact)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert env.finding.cvss31_score is None
    assert "could not compute" in crypto.decrypt(run.output_enc).decode()


# ---------------- remediation / poc / report ----------------

def test_remediation_sets_finding(env):
    run = _run(env, _J({"summary": "Validate content-type; store outside webroot (api/upload.py:2).",
                        "references": ["https://owasp.org/upload"]}),
               stage=models.StageType.remediation)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert "content-type" in env.finding.remediation


def test_poc_writes_downloadable_attachment(env):
    run = _run(env, _J({"filename": "poc.py", "language": "python",
                        "poc": "import sys\nTARGET = sys.argv[1]\nprint(TARGET)\n",
                        "usage": "python poc.py https://your-target"}),
               stage=models.StageType.poc)
    assert run.status == models.StageRunStatus.done and run.artifact_id
    att = env.get(models.Attachment, run.artifact_id)
    assert att.filename == "poc.py" and att.finding_id == env.finding.id
    body = crypto.decrypt(att.content_enc).decode()
    assert "sys.argv[1]" in body


def test_report_assembles_and_saves(env):
    run = _run(env, FakeProvider("# Investigation report\n\nExecutive summary."),
               stage=models.StageType.report, finding=False)
    assert run.status == models.StageRunStatus.done
    rpt = env.query(models.Report).filter_by(source_tool=models.SourceTool.generated).first()
    assert rpt and crypto.decrypt(rpt.content_enc).decode().startswith("# Investigation report")


# ---------------- diagnostics: errors keep the model's words ----------------

def test_unparseable_response_preserves_the_raw_reply(env):
    provider = FakeProvider("I looked and found a prototype pollution bug but I won't format it as JSON.")
    run = _run(env, provider, stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.error
    assert "did not return valid JSON" in run.error
    # the model's actual words are stored so the operator can review them
    assert run.output_enc is not None
    saved = crypto.decrypt(run.output_enc).decode()
    assert "prototype pollution bug" in saved


def test_provider_error_is_friendly(env):
    class Boom:
        display_name = "Anthropic"; model = "x"
        def chat(self, *a, **k): raise AIKeyInvalid("Anthropic")
    run = _run(env, Boom(), stage=models.StageType.source)
    assert run.status == models.StageRunStatus.error
    assert "rejected" in run.error.lower() and "ANTHROPIC_API_KEY" not in run.error


def test_unregistered_stage_errors_cleanly(env, monkeypatch):
    monkeypatch.setitem(pipeline._RUNNERS, models.StageType.poc, None)
    run = _run(env, FakeProvider("{}"), stage=models.StageType.poc)
    assert run.status == models.StageRunStatus.error
    assert "not implemented" in run.error


# ---------------- context-window sizing ----------------

def test_source_too_big_errors_clearly_not_a_raw_400(env):
    provider = _J({"findings": []})
    provider.context_window = 100   # tiny — the digest can't fit
    run = _run(env, provider, stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.error
    assert "context window" in run.error and "100" in run.error


def test_output_tokens_are_capped_to_the_context(env):
    captured = {}
    class P(FakeProvider):
        def chat(self, system, messages, max_tokens=None):
            captured["max_tokens"] = max_tokens
            return '{"findings": []}'
    provider = P(context_window=4096)
    _run(env, provider, stage=models.StageType.discover, finding=False)
    # output uses the room left in the context, but the whole request must fit
    assert 0 < captured["max_tokens"] < 4096


def test_endpoint_context_400_reports_the_real_loaded_context(env):
    from app.ai.errors import AIProviderUnavailable
    class P(FakeProvider):
        def chat(self, system, messages, max_tokens=None):
            raise AIProviderUnavailable("Local model", "n_keep: 10341 >= n_ctx: 4096")
    # Verdict is set to 32000 but the endpoint is really at 4096 -> say so + reload.
    run = _run(env, P(context_window=32000), stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.error
    assert "4,096" in run.error and "32,000" in run.error and "RELOAD" in run.error


def test_autopilot_queues_impact_after_discovery(env, monkeypatch):
    # discover finds 2 vulns; autopilot should auto-queue impact for each.
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda rid: submitted.append(rid))
    env.case.autopilot = True
    env.commit()
    provider = _J({"findings": [
        {"title": "A", "severity": "high"}, {"title": "B", "severity": "low"},
    ]})
    run = _run(env, provider, stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.done
    impact_runs = env.query(models.StageRun).filter_by(stage=models.StageType.impact).all()
    assert len(impact_runs) == 3   # the fixture finding + the 2 discovered
    assert len(submitted) == 3   # all queued to the executor


def test_autopilot_off_does_not_queue(env, monkeypatch):
    submitted = []
    monkeypatch.setattr(pipeline, "submit", lambda rid: submitted.append(rid))
    env.case.autopilot = False
    env.commit()
    run = _run(env, _J({"findings": [{"title": "A", "severity": "high"}]}),
               stage=models.StageType.discover, finding=False)
    assert run.status == models.StageRunStatus.done
    assert env.query(models.StageRun).filter_by(stage=models.StageType.impact).count() == 0
