import enum
import uuid
import datetime as dt

from sqlalchemy import (
    String, ForeignKey, DateTime, LargeBinary, Integer, BigInteger, Text, Enum,
    UniqueConstraint, Boolean, Date, JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Role(str, enum.Enum):
    user = "user"
    manager = "manager"
    admin = "admin"


class SourceTool(str, enum.Enum):
    claude_code = "claude_code"
    openai = "openai"
    gemini = "gemini"
    grok = "grok"
    generated = "generated"  # produced server-side via chat
    other = "other"


class Project(Base):
    """A grouping of runs/reports that members of the project can see."""
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Optional per-project model pin. NULL = inherit the team's choice, then
    # the server default. Lets one product be analysed by a local model while
    # another uses a hosted one.
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    members: Mapped[list["User"]] = relationship(
        secondary="project_members", back_populates="projects"
    )


class ProjectMember(Base):
    """Join table for Project ⇄ User membership."""
    __tablename__ = "project_members"
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    # Team-level model pin; a project's own choice wins over this.
    ai_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    members: Mapped[list["User"]] = relationship(back_populates="team")


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.admin, nullable=False)
    team_id: Mapped[str | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    # Onboarding wizard: NULL = user hasn't finished it yet. Self-service signup
    # users land on /welcome until they hit "finish"; users created by an
    # admin start as already onboarded (set when the User row is created).
    onboarded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    team: Mapped["Team | None"] = relationship(back_populates="members")
    agents: Mapped[list["Agent"]] = relationship(back_populates="user")
    reports: Mapped[list["Report"]] = relationship(back_populates="user")
    projects: Mapped[list["Project"]] = relationship(
        secondary="project_members", back_populates="members"
    )


class Agent(Base):
    """An installed collector on a user's machine."""
    __tablename__ = "agents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_seen: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pending_upgrade: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Anthropic credentials are pushed from the portal so analysts can rotate
    # their (typically week-lived) keys without SSHing to the box. Expiry is
    # user-supplied — Anthropic keys are opaque and there's no API to read it.
    anthropic_key_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    anthropic_key_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    anthropic_key_pushed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_key_push: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User"] = relationship(back_populates="agents")


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("user_id", "sha256", name="uq_report_user_sha"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    source_tool: Mapped[SourceTool] = mapped_column(Enum(SourceTool), default=SourceTool.other)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    # Human-friendly label set by the user. Falls back to filename in the UI
    # when empty (Claude tends to write generic filenames like findings.md).
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    original_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    summary_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    file_mtime: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Claude Code session id (from PostToolUse hook payload). Lets us group
    # multiple .md files from one Claude session as a single logical "run".
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Direct project association — used for reports that have no session_id
    # (watcher uploads, manual files). Grouped reports inherit project from
    # their Run, but a direct value on the report wins if set.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Manual scan attachment. Auto-link via VulnScan.source_report_id /
    # source_session_id still works (and takes precedence in the UI if both
    # exist), but this lets a user attach a stray report to any scan.
    scan_id: Mapped[str | None] = mapped_column(
        ForeignKey("vuln_scans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User"] = relationship(back_populates="reports")
    agent: Mapped["Agent | None"] = relationship()
    project: Mapped["Project | None"] = relationship()


class ScanState(str, enum.Enum):
    draft = "draft"           # auto-extracted, awaiting human review
    confirmed = "confirmed"   # human reviewed/edited


class Severity(str, enum.Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"
    unknown = "unknown"


class VulnScan(Base):
    """Structured vulnerability-scan summary (Type A).

    May be derived from an uploaded .md Report (source_report_id set) or
    entered manually (source_report_id NULL). All integer count fields default
    to 0 so partially-filled drafts render cleanly.
    """
    __tablename__ = "vuln_scans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    source_report_id: Mapped[str | None] = mapped_column(
        ForeignKey("reports.id"), nullable=True, index=True
    )
    state: Mapped[ScanState] = mapped_column(
        Enum(ScanState), default=ScanState.confirmed, nullable=False
    )
    # When set, subsequent reports from the same Claude session merge into
    # this scan instead of creating new ones.
    source_session_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    # Direct project attachment. The scan inherits visibility from its run's
    # project too, but this lets manually-created scans (no run) be assigned.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Type A fields
    product: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    # Optional human-friendly scan name; the UI falls back to `product` when empty.
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    scan_target: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    harness_used: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    scan_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    results_file: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    spreadsheet_link: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    triaged_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    findings: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    fp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # false positive
    sbp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # true positive
    duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    untriaged: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    highest_severity: Mapped[Severity] = mapped_column(
        Enum(Severity), default=Severity.unknown, nullable=False
    )
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # Audit fields: who Agreed with Claude's draft and when. Stamped any
    # time `state` flips from draft -> confirmed (via PATCH or /agree).
    # NULL means nobody has explicitly confirmed yet.
    confirmed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    # Two FKs into `users` (user_id + confirmed_by) — disambiguate.
    user: Mapped["User"] = relationship(foreign_keys=[user_id])
    project: Mapped["Project | None"] = relationship()
    runs: Mapped[list["RunLog"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan", order_by="RunLog.date.desc()"
    )
    # NB: column `findings` above is an int count; the relationship to Finding
    # rows is exposed as `finding_rows` to avoid the name clash.
    finding_rows: Mapped[list["Finding"]] = relationship(
        back_populates="scan", cascade="all, delete-orphan",
        order_by="Finding.created_at.desc()",
    )


class Run(Base):
    """A Claude Code session worth of files. Created lazily when the first
    Report with this session_id arrives, or by hand from /ui/runs.
    """
    __tablename__ = "runs"
    # The Claude session_id is our natural primary key — never collides,
    # already on every Report row.
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The harness (folder of prompts/configs) Claude was running inside when
    # this session was produced. Nullable: lots of runs predate the harness
    # feature, and reusing a harness is opt-in.
    harness_id: Mapped[str | None] = mapped_column(
        ForeignKey("harnesses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Auto-filled from any derived VulnScan (scan.product / scan.scan_target).
    # User can edit from the Run detail page to approve/correct.
    product: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    subcomponent: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped["User"] = relationship()
    project: Mapped["Project | None"] = relationship()
    harness: Mapped["Harness | None"] = relationship()


class Harness(Base):
    """A reusable folder of prompts/config/tools that Claude is run inside.

    Owned by a user, optionally scoped to a Product, and referenced from
    Run.harness_id so multiple sessions can reuse the same harness.
    Files are stored per-row (HarnessFile) so the UI can browse them
    individually. Encrypted at rest with the same AES-GCM key as reports.
    """
    __tablename__ = "harnesses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    user: Mapped["User"] = relationship()
    project: Mapped["Project | None"] = relationship()
    files: Mapped[list["HarnessFile"]] = relationship(
        back_populates="harness", cascade="all, delete-orphan",
        order_by="HarnessFile.relpath",
    )


class HarnessFile(Base):
    """One file inside a Harness. Encrypted at rest."""
    __tablename__ = "harness_files"
    __table_args__ = (
        UniqueConstraint("harness_id", "relpath", name="uq_harness_file_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    harness_id: Mapped[str] = mapped_column(
        ForeignKey("harnesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream", nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    harness: Mapped["Harness"] = relationship(back_populates="files")


class FindingStatus(str, enum.Enum):
    open = "open"                       # awaiting triage (== untriaged)
    true_positive = "true_positive"
    false_positive = "false_positive"
    sbp = "sbp"
    duplicate = "duplicate"
    fixed = "fixed"


class AIVerdict(str, enum.Enum):
    """Separate from the human dev verdict. Set by the AI assessment
    endpoint; never auto-overwrites a human's status."""
    open = "open"
    true_positive = "true_positive"
    false_positive = "false_positive"


class Finding(Base):
    """A single vulnerability discovered within a VulnScan."""
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("vuln_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    severity: Mapped[Severity] = mapped_column(
        Enum(Severity), default=Severity.unknown, nullable=False
    )
    status: Mapped[FindingStatus] = mapped_column(
        Enum(FindingStatus), default=FindingStatus.open, nullable=False
    )
    cwe: Mapped[str] = mapped_column(String(64), default="", nullable=False)  # e.g. "CWE-122"
    cve: Mapped[str] = mapped_column(String(64), default="", nullable=False)  # e.g. "CVE-2025-1234"
    affected_component: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    steps_to_reproduce: Mapped[str] = mapped_column(Text, default="", nullable=False)
    remediation: Mapped[str] = mapped_column(Text, default="", nullable=False)
    proof_of_concept: Mapped[str] = mapped_column(Text, default="", nullable=False)
    references: Mapped[str] = mapped_column(Text, default="", nullable=False)  # newline-separated URLs
    assigned_to: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    triaged_by: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    triaged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Free-text notes from the product dev/PM who triaged this via a share
    # link. Kept separate from `description`/`remediation` (which are the
    # security team's content) so guest input never overwrites our analysis.
    dev_notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    # AI-derived verdict, set by the /ai_verdict endpoint. Lives alongside
    # the dev verdict (`status`) — they're allowed to disagree, and the UI
    # surfaces both so reviewers can spot the cases that need attention.
    ai_verdict: Mapped[AIVerdict] = mapped_column(
        Enum(AIVerdict, name="aiverdict"), default=AIVerdict.open, nullable=False,
    )
    ai_rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Free-form tag list (SBP / SS / VULN, lowercased). Stored as JSON so
    # we can add new tag values without an enum migration.
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scan: Mapped["VulnScan"] = relationship(back_populates="finding_rows")


class AIVerdictRun(Base):
    """One log entry per /ai_verdict call against a finding.

    Persists every run instead of overwriting Finding.ai_verdict so
    reviewers can see how the verdict has moved over time (and which
    model + user triggered it). The latest run's verdict + rationale
    are mirrored back onto the Finding row for fast UI rendering.
    """
    __tablename__ = "ai_verdict_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    finding_id: Mapped[str] = mapped_column(
        ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ran_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    verdict: Mapped[AIVerdict] = mapped_column(
        Enum(AIVerdict, name="aiverdict"), nullable=False
    )
    rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class RunLog(Base):
    """Per-run log row (Type B). Child of VulnScan."""
    __tablename__ = "run_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("vuln_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)

    # Type B fields
    day: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    run: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    box: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    product: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    harness: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    results: Mapped[str] = mapped_column(Text, default="", nullable=False)
    poc: Mapped[str] = mapped_column(Text, default="", nullable=False)
    comment: Mapped[str] = mapped_column(Text, default="", nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    scan: Mapped["VulnScan"] = relationship(back_populates="runs")


class Attachment(Base):
    """Binary artefact (POC file, crash input, screenshot, etc.) collected
    by the agent alongside a Claude session's markdown reports.

    Grouped by session_id the same way Reports are. Optionally scoped to a
    specific VulnScan and/or Finding once the user (or a future heuristic)
    associates it. Encrypted at rest."""
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agents.id"), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    scan_id: Mapped[str | None] = mapped_column(
        ForeignKey("vuln_scans.id", ondelete="SET NULL"), nullable=True, index=True
    )
    finding_id: Mapped[str | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL"), nullable=True, index=True
    )

    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    original_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream", nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    user: Mapped["User"] = relationship()
    agent: Mapped["Agent | None"] = relationship()


class ImportStatus(str, enum.Enum):
    staged = "staged"           # files uploaded, awaiting plan
    planning = "planning"       # Claude is browsing the dir
    planned = "planned"         # plan ready, awaiting user confirm
    applied = "applied"         # imported into projects/scans/reports
    cancelled = "cancelled"     # user discarded
    error = "error"             # planning or apply failed


class FolderImport(Base):
    """Staging area for "upload a whole folder, let Claude organize it" flows.

    Files land on disk under ``settings.imports_staging_dir/<id>/`` and a
    JSON plan is generated by Claude (via tool use) before the user
    confirms; on confirm we create the project / scans / runs / reports /
    attachments and clean up the staging dir.
    """
    __tablename__ = "folder_imports"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus), default=ImportStatus.staged, nullable=False
    )
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    staging_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    # Pre-pinned product. When set, the planner's project decision is
    # overridden to {kind: "existing", existing_id: project_id}, so a
    # user uploading from a product's page lands straight on a plan
    # targeting that product.
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    plan_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan_log: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error_message: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    planned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), default="Chat")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", order_by="ChatMessage.created_at"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


class NotificationKind(str, enum.Enum):
    access_request = "access_request"           # request landed in your inbox
    access_approved = "access_approved"         # your request was approved
    access_denied = "access_denied"             # your request was denied
    project_member_added = "project_member_added"
    report_uploaded = "report_uploaded"         # agent just uploaded a report


class Notification(Base):
    """User-facing notification. Recipient-scoped (one row per recipient
    even if an event fans out to multiple users)."""
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[NotificationKind] = mapped_column(
        Enum(NotificationKind), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    link: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    # Loose JSON bag for kind-specific fields (project_id, request_id, etc.)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    read_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    cancelled = "cancelled"


class ProjectAccessRequest(Base):
    """A user asking to join a project. Approvable by the project creator
    or any admin. Approving auto-adds the user as a member."""
    __tablename__ = "project_access_requests"
    __table_args__ = (
        # One pending request per (project, user). Approved/denied rows
        # stay around as history; only one can be `pending` at a time
        # which we enforce in the handler.
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[AccessRequestStatus] = mapped_column(
        Enum(AccessRequestStatus), default=AccessRequestStatus.pending, nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # Optional folder import the requester wants to bring in along with their
    # membership. Approval auto-applies the import to *this* project, so the
    # owner doesn't have to chase the user for a follow-up assignment.
    import_id: Mapped[str | None] = mapped_column(
        ForeignKey("folder_imports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decision_reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    project: Mapped["Project"] = relationship()
    user: Mapped["User"] = relationship(foreign_keys=[user_id])


class ShareLink(Base):
    """Unauthenticated triage link for a single VulnScan.

    Lets product devs/PMs (no Verdict account) open ``/share/{token}`` and set
    per-finding TP/FP status + dev_notes. The token is a 32-byte urlsafe
    secret; only its sha256 is stored, alongside an 8-char prefix for
    display/logging. Plaintext is shown exactly once at creation.
    """
    __tablename__ = "share_links"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scan_id: Mapped[str] = mapped_column(
        ForeignKey("vuln_scans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    token_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    label: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Default-deny: PoC text + attachments are exploit material and stay
    # hidden from guest viewers unless the creator opts in per link.
    allow_poc: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )

    scan: Mapped["VulnScan"] = relationship()
    creator: Mapped["User"] = relationship()


class ProjectInvite(Base):
    """Shareable link that auto-adds the redeemer to a project.

    Token is the URL-safe public identifier. Owners distribute the URL
    /join/<token>; first-time visitors who sign up via that link, and any
    logged-in user who follows it, get added as project members.
    """
    __tablename__ = "project_invites"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    uses_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )

    project: Mapped["Project"] = relationship()


class RemotePromptStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    error = "error"


class RemoteSessionStatus(str, enum.Enum):
    idle = "idle"
    running = "running"
    archived = "archived"


class RemoteSession(Base):
    """A persistent Claude conversation on the owner's machine. Each turn
    is a RemotePrompt; follow-ups pass `--resume claude_session_id`."""
    __tablename__ = "remote_sessions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Which agent CLI ran this session ("claude", "generic", ...). NULL for
    # sessions created before the CLI became pluggable.
    cli: Mapped[str | None] = mapped_column(String(32), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    harness_id: Mapped[str | None] = mapped_column(
        ForeignKey("harnesses.id", ondelete="SET NULL"), nullable=True
    )
    # Set whenever harness is (re)attached or files are uploaded; the next
    # claimed prompt for this session carries a bundle_url so the agent
    # materializes the workspace before running claude.
    pending_bundle: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The id Claude prints in its `system` init event — needed for --resume.
    claude_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Claude model for every turn in this session (passed as `claude --model`).
    # NULL = fall back to the agent's configured default / CLI default.
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[RemoteSessionStatus] = mapped_column(
        Enum(RemoteSessionStatus), default=RemoteSessionStatus.idle, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_activity_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )

    agent: Mapped["Agent"] = relationship()
    project: Mapped["Project | None"] = relationship()
    harness: Mapped["Harness | None"] = relationship()
    turns: Mapped[list["RemotePrompt"]] = relationship(
        back_populates="session", order_by="RemotePrompt.created_at",
        cascade="all, delete-orphan",
    )
    uploads: Mapped[list["SessionUpload"]] = relationship(
        back_populates="session", order_by="SessionUpload.relpath",
        cascade="all, delete-orphan",
    )


class SessionUpload(Base):
    """A file the user dropped onto a Workbench session. Pushed to the
    agent's per-session scratch dir as part of the bundle tarball,
    overlaying harness files on relpath collision. Encrypted at rest."""
    __tablename__ = "session_uploads"
    __table_args__ = (
        UniqueConstraint("session_id", "relpath", name="uq_session_upload_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("remote_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream", nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Disk-backed (chunked-GCM) blob under settings.data_dir. Legacy rows
    # written before the disk store keep their bytes in content_enc.
    storage_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    content_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["RemoteSession"] = relationship(back_populates="uploads")


class ProjectFile(Base):
    """Source/firmware uploaded to a Product. Visible to every product
    member and bundled into any RemoteSession linked to that product, so
    anyone on the team can spin up a Claude session against the same code
    regardless of who originally pushed it."""
    __tablename__ = "project_files"
    __table_args__ = (
        UniqueConstraint("project_id", "relpath", name="uq_project_file_path"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relpath: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(128), default="application/octet-stream", nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(160), nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # When the file came in as part of a source-code component upload, which
    # component it belongs to (so the product page can group + delete by it).
    component_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class RemotePrompt(Base):
    """One turn inside a RemoteSession (or a legacy one-shot if session_id
    is null). `events_enc` holds the live stream-json transcript as NDJSON;
    `output_enc` holds the final assistant text."""
    __tablename__ = "remote_prompts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(
        ForeignKey("remote_sessions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    status: Mapped[RemotePromptStatus] = mapped_column(
        Enum(RemotePromptStatus), default=RemotePromptStatus.pending, nullable=False, index=True
    )
    prompt_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    output_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    events_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    agent: Mapped["Agent"] = relationship()
    session: Mapped["RemoteSession | None"] = relationship(back_populates="turns")


class AppSetting(Base):
    """Key/value store for runtime-configurable server settings (e.g. the
    Anthropic API key entered via the admin portal). Values are encrypted at
    rest with the same AES-256-GCM key as report bodies."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ProductComponent(Base):
    """A source-code component of a Product, identified by AI from an uploaded
    archive. Its files live as ProjectFile rows (component_id back-reference)
    so they bundle into Workbench/scan sessions like any other product source."""
    __tablename__ = "product_components"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    role: Mapped[str] = mapped_column(Text, default="", nullable=False)
    source_name: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    ai_rationale: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PromptTemplate(Base):
    """A reusable prompt template for the Workbench. Shared library: every
    logged-in user can see and use any template; the creator (or an admin) can
    edit/delete. Bodies may contain {{variables}} (e.g. {{product}}) that the
    Workbench picker fills in before running."""
    __tablename__ = "prompt_templates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    category: Mapped[str] = mapped_column(String(64), default="", nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
