"""Source-investigation stage execution (first executable stage, read-only).

Drives pipeline.run_stage with a scripted fake provider so the real tool
handlers (list_dir / read_file / confinement) run against a temp source tree.
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

from app import database, models, crypto, pipeline
from app.auth import hash_password
from app.ai.tools import AssistantTurn, ToolCall
from app.ai.errors import AIKeyInvalid


class FakeConvo:
    """Replays a script of turns; records which tool calls it was fed results for."""
    def __init__(self, turns):
        self._turns = list(turns)
        self.seen_results = []

    def _next(self):
        return self._turns.pop(0) if self._turns else AssistantTurn(text="done", stop_reason="end_turn")

    def send_user(self, content):
        self.user_content = content
        return self._next()

    def send_tool_results(self, results):
        self.seen_results.extend(results)
        return self._next()


class FakeProvider:
    model = "fake-1"
    def __init__(self, turns):
        self._turns = turns
        self.convo = None
    def start_tools(self, system, tools, max_tokens=None):
        self.convo = FakeConvo(self._turns)
        return self.convo
    def chat(self, system, messages, max_tokens=None):
        self.chat_context = messages[-1]["content"]
        return getattr(self, "chat_reply", "# Report\n\nAssembled write-up.")


def _tc(name, **args):
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


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
                             severity=models.Severity.critical, cwe="CWE-434")
    db.add(finding); db.flush()

    # a real source tree the tools read
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "upload.py").write_text(
        "def handle(f):\n    open('/webroot/'+f.name,'wb').write(f.read())  # no validation\n")
    src = models.CaseSource(case_id=case.id, kind=models.CaseSourceKind.local_path,
                            local_path="widget", status=models.SourceStatus.ready,
                            workspace_key=str(tmp_path))
    db.add(src); db.commit()

    db.case, db.finding, db.tmp = case, finding, tmp_path
    yield db
    db.close()


def _run(db, stage=models.StageType.source, finding=True):
    run = models.StageRun(case_id=db.case.id,
                          finding_id=db.finding.id if finding else None, stage=stage)
    db.add(run); db.commit()
    pipeline.run_stage(db, run)
    db.refresh(run)
    return run


def test_source_stage_reads_files_and_produces_analysis(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("list_dir", path="")], stop_reason="tool_use"),
        AssistantTurn(tool_calls=[_tc("read_file", path="api/upload.py")], stop_reason="tool_use"),
        AssistantTurn(tool_calls=[_tc("submit_analysis",
            affected_component="api/upload.py",
            summary="No content-type check; writes attacker file to webroot.",
            key_locations=["api/upload.py:2"], confidence="high")], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)

    run = _run(env)
    assert run.status == models.StageRunStatus.done
    out = crypto.decrypt(run.output_enc).decode()
    assert "api/upload.py" in out and "webroot" in out
    # the real read_file tool actually ran against the temp tree
    read_back = "".join(r.content for r in provider.convo.seen_results)
    assert "no validation" in read_back
    # non-destructive finding update
    env.refresh(env.finding)
    assert env.finding.affected_component == "api/upload.py"


def test_confinement_error_is_returned_to_the_model_not_crashed(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("read_file", path="../../etc/passwd")], stop_reason="tool_use"),
        AssistantTurn(tool_calls=[_tc("submit_analysis", summary="could not read outside tree")],
                      stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env)
    assert run.status == models.StageRunStatus.done
    fed = "".join(r.content for r in provider.convo.seen_results)
    assert "escapes the source tree" in fed


def test_model_finishing_without_submit_keeps_prose(env, monkeypatch):
    provider = FakeProvider([AssistantTurn(text="Here is my analysis of the bug.", stop_reason="end_turn")])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env)
    assert run.status == models.StageRunStatus.done
    assert "analysis of the bug" in crypto.decrypt(run.output_enc).decode()


def test_provider_error_is_a_friendly_stage_error(env, monkeypatch):
    class Boom:
        model = "x"
        def start_tools(self, *a, **k): raise AIKeyInvalid("Anthropic")
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: Boom())
    run = _run(env)
    assert run.status == models.StageRunStatus.error
    assert "rejected" in run.error.lower()
    assert "ANTHROPIC_API_KEY" not in run.error  # non-admin message


def test_unregistered_stage_errors_cleanly(env, monkeypatch):
    # Every stage is implemented now; verify the defensive path still holds if
    # a runner is ever missing from the dispatch table.
    monkeypatch.setitem(pipeline._RUNNERS, models.StageType.poc, None)
    run = _run(env, stage=models.StageType.poc)
    assert run.status == models.StageRunStatus.error
    assert "not implemented" in run.error


def test_source_stage_needs_a_ready_source(env, monkeypatch):
    env.case.source.status = models.SourceStatus.pending
    env.case.source.workspace_key = "/nonexistent"
    env.commit()
    provider = FakeProvider([AssistantTurn(text="x")])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env)
    assert run.status == models.StageRunStatus.error


def test_lifecycle_timestamps_are_set(env, monkeypatch):
    provider = FakeProvider([AssistantTurn(text="quick", stop_reason="end_turn")])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env)
    assert run.started_at is not None and run.completed_at is not None


def test_impact_stage_computes_cvss31_and_updates_finding(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("submit_impact",
            cvss31_vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
            cvss40_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N",
            cvss40_score=9.3, cwe="CWE-434",
            cvss31_rationale=[{"metric": "AV", "value": "N", "reason": "reachable over the network."}],
            summary="Unauthenticated RCE via arbitrary upload.")], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.impact)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert env.finding.cvss31_score == 9.8           # computed server-side
    assert env.finding.severity == models.Severity.critical
    assert env.finding.cwe == "CWE-434"
    assert env.finding.cvss40_vector.startswith("CVSS:4.0/")
    out = crypto.decrypt(run.output_enc).decode()
    assert "9.8" in out and "computed" in out and "model-estimated" in out


def test_impact_rejects_a_bad_cvss31_vector_gracefully(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("submit_impact",
            cvss31_vector="CVSS:3.1/AV:N/AC:L", cwe="CWE-434",
            summary="incomplete vector")], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.impact)
    assert run.status == models.StageRunStatus.done  # stage still completes
    env.refresh(env.finding)
    assert env.finding.cvss31_score is None          # nothing bogus persisted
    assert "could not compute" in crypto.decrypt(run.output_enc).decode()


def test_remediation_stage_sets_finding_remediation(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("submit_remediation",
            summary="Validate the content-type and store outside the webroot (api/upload.py:2).",
            references=["https://owasp.org/upload"])], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.remediation)
    assert run.status == models.StageRunStatus.done
    env.refresh(env.finding)
    assert "content-type" in env.finding.remediation
    assert "owasp.org" in crypto.decrypt(run.output_enc).decode()


def test_report_stage_assembles_and_saves_a_report(env, monkeypatch):
    provider = FakeProvider([])
    provider.chat_reply = "# Investigation report\n\nExecutive summary: one critical RCE."
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.report, finding=False)  # case-level
    assert run.status == models.StageRunStatus.done
    assert "Executive summary" in crypto.decrypt(run.output_enc).decode()
    # a downloadable generated Report was created
    rpt = env.query(models.Report).filter_by(source_tool=models.SourceTool.generated).first()
    assert rpt is not None and crypto.decrypt(rpt.content_enc).decode().startswith("# Investigation report")


def test_poc_stage_writes_a_downloadable_attachment(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("submit_poc",
            filename="poc_upload_rce.py", language="python",
            poc="import sys, requests\nTARGET = sys.argv[1]  # e.g. https://host\n"
                "requests.post(TARGET + '/api/upload', files={'f': ('x.php', b'<?php ?>')})\n",
            usage="python poc_upload_rce.py https://your-target")], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.poc)
    assert run.status == models.StageRunStatus.done

    # an Attachment was created, linked to the run + finding, target NOT hardcoded
    assert run.artifact_id
    att = env.get(models.Attachment, run.artifact_id)
    assert att and att.filename == "poc_upload_rce.py"
    assert att.finding_id == env.finding.id and att.scan_id == env.case.scan_id
    body = crypto.decrypt(att.content_enc).decode()
    assert "sys.argv[1]" in body and "https://host" not in body.split("#")[0]
    # the stage output shows the file + usage
    out = crypto.decrypt(run.output_enc).decode()
    assert "poc_upload_rce.py" in out and "your-target" in out


def test_poc_stage_needs_actual_poc_content(env, monkeypatch):
    provider = FakeProvider([
        AssistantTurn(tool_calls=[_tc("submit_poc", filename="x.py", poc="")], stop_reason="tool_use"),
    ])
    monkeypatch.setattr(pipeline, "get_provider", lambda p, m: provider)
    run = _run(env, stage=models.StageType.poc)
    assert run.status == models.StageRunStatus.error
    assert run.artifact_id is None
