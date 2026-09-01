import datetime as dt
from pydantic import BaseModel, EmailStr, Field

from .models import (
    AccessRequestStatus, AIVerdict, FindingStatus, ImportStatus,
    NotificationKind, Role, ScanState, Severity, SourceTool,
)


# ---- auth ----
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    role: Role = Role.user
    team_id: str | None = None


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    role: Role | None = None
    team_id: str | None = None   # explicit null clears


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class PasswordReset(BaseModel):
    new_password: str


class UserOut(BaseModel):
    id: str
    email: str
    role: Role
    team_id: str | None
    created_at: dt.datetime
    onboarded_at: dt.datetime | None = None

    class Config:
        from_attributes = True


class TeamCreate(BaseModel):
    name: str


class TeamUpdate(BaseModel):
    name: str | None = None
    # "" clears the pin and reverts to the server default.
    ai_provider: str | None = None
    ai_model: str | None = None


class TeamOut(BaseModel):
    id: str
    name: str
    member_count: int = 0
    ai_provider: str | None = None
    ai_model: str | None = None

    class Config:
        from_attributes = True


# ---- agents ----
class AgentRegister(BaseModel):
    hostname: str


class AgentOut(BaseModel):
    id: str
    hostname: str
    api_key: str | None = None  # only returned once on creation
    last_seen: dt.datetime | None = None
    last_ip: str | None = None
    version: str | None = None
    pending_upgrade: bool = False
    update_available: bool = False
    anthropic_key_last4: str | None = None
    anthropic_key_expires_at: dt.datetime | None = None
    anthropic_key_pushed_at: dt.datetime | None = None
    pending_key_push: bool = False

    class Config:
        from_attributes = True


class AnthropicKeyIn(BaseModel):
    key: str
    expires_at: dt.datetime | None = None


# ---- reports ----
class ReportIngest(BaseModel):
    filename: str
    original_path: str | None = None
    source_tool: SourceTool = SourceTool.other
    sha256: str
    size_bytes: int
    file_mtime: dt.datetime | None = None
    content_b64: str  # raw file bytes, base64 (TLS protects in transit)
    session_id: str | None = None  # Claude Code session, if uploaded by the hook


class ReportOut(BaseModel):
    id: str
    user_id: str
    filename: str
    title: str = ""
    original_path: str | None
    source_tool: SourceTool
    sha256: str
    size_bytes: int
    summary: str | None = None
    created_at: dt.datetime

    # Extras the SPA needs that aren't strictly part of the upload payload.
    session_id: str | None = None
    project_id: str | None = None
    # Effective project = direct project_id OR project_id of the report's Run
    # (set when reports share a session). Used for grouping in the UI.
    effective_project_id: str | None = None
    agent_hostname: str | None = None
    owner_email: str | None = None
    derived_scan_id: str | None = None
    derived_scan_product: str | None = None
    derived_scan_state: str | None = None

    class Config:
        from_attributes = True


class ReportDetail(ReportOut):
    content: str


class ReportUpdate(BaseModel):
    title: str | None = None
    project_id: str | None = None  # explicit null clears
    scan_id: str | None = None     # explicit null clears the manual scan link


# ---- attachments ----
class AttachmentIngest(BaseModel):
    filename: str
    original_path: str | None = None
    session_id: str | None = None
    content_type: str = "application/octet-stream"
    sha256: str
    size_bytes: int
    content_b64: str  # raw bytes, base64 (TLS in transit, AES-GCM at rest)


class AttachmentOut(BaseModel):
    id: str
    user_id: str
    session_id: str | None
    scan_id: str | None
    finding_id: str | None
    filename: str
    original_path: str | None
    content_type: str
    sha256: str
    size_bytes: int
    created_at: dt.datetime

    class Config:
        from_attributes = True


# ---- chat ----
class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    report_ids: list[str] = Field(default_factory=list)
    save_as_report: bool = False
    save_filename: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    generated_report_id: str | None = None


# ---- vuln scans (Type A parent) ----
class VulnScanBase(BaseModel):
    product: str = ""
    title: str = ""
    scan_target: str = ""
    harness_used: str = ""
    scan_by: str = ""
    results_file: str = ""
    spreadsheet_link: str = ""
    triaged_by: str = ""
    findings: int = 0
    fp: int = 0
    sbp: int = 0
    tp: int = 0
    duplicates: int = 0
    untriaged: int = 0
    highest_severity: Severity = Severity.unknown
    notes: str = ""


class VulnScanCreate(VulnScanBase):
    source_report_id: str | None = None
    state: ScanState = ScanState.confirmed


class VulnScanUpdate(BaseModel):
    # Every field optional for PATCH-style edits.
    product: str | None = None
    title: str | None = None
    scan_target: str | None = None
    harness_used: str | None = None
    scan_by: str | None = None
    results_file: str | None = None
    spreadsheet_link: str | None = None
    triaged_by: str | None = None
    findings: int | None = None
    fp: int | None = None
    sbp: int | None = None
    tp: int | None = None
    duplicates: int | None = None
    untriaged: int | None = None
    highest_severity: Severity | None = None
    notes: str | None = None
    state: ScanState | None = None
    project_id: str | None = None


class VulnScanOut(VulnScanBase):
    id: str
    user_id: str
    owner_email: str | None = None     # who created/owns the scan; used as
                                       # the default for `scan_by` in the UI.
    source_report_id: str | None = None
    source_session_id: str | None = None
    project_id: str | None = None
    state: ScanState
    confirmed_by: str | None = None
    confirmed_by_email: str | None = None
    confirmed_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


# ---- run logs (Type B child) ----
class RunLogBase(BaseModel):
    day: str = ""
    date: dt.date | None = None
    run: str = ""
    box: str = ""
    product: str = ""
    harness: str = ""
    prompt: str = ""
    results: str = ""
    poc: str = ""
    comment: str = ""
    complete: bool = False


class RunLogCreate(RunLogBase):
    pass


class RunLogUpdate(BaseModel):
    day: str | None = None
    date: dt.date | None = None
    run: str | None = None
    box: str | None = None
    product: str | None = None
    harness: str | None = None
    prompt: str | None = None
    results: str | None = None
    poc: str | None = None
    comment: str | None = None
    complete: bool | None = None


class RunLogOut(RunLogBase):
    id: str
    scan_id: str
    user_id: str
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class VulnScanDetail(VulnScanOut):
    runs: list[RunLogOut] = []
    # `findings_list` is the list of Finding rows; the inherited `findings`
    # field stays the integer "total raised" count from the scan summary.
    findings_list: list["FindingOut"] = []


# ---- findings ----
class FindingBase(BaseModel):
    title: str = ""
    severity: Severity = Severity.unknown
    status: FindingStatus = FindingStatus.open
    cwe: str = ""
    cve: str = ""
    affected_component: str = ""
    description: str = ""
    steps_to_reproduce: str = ""
    remediation: str = ""
    proof_of_concept: str = ""
    references: str = ""
    assigned_to: str = ""
    triaged_by: str = ""
    dev_notes: str = ""
    ai_verdict: AIVerdict = AIVerdict.open
    ai_rationale: str = ""
    tags: list[str] = []


class FindingCreate(FindingBase):
    pass


class FindingUpdate(BaseModel):
    title: str | None = None
    severity: Severity | None = None
    status: FindingStatus | None = None
    cwe: str | None = None
    cve: str | None = None
    affected_component: str | None = None
    description: str | None = None
    steps_to_reproduce: str | None = None
    remediation: str | None = None
    proof_of_concept: str | None = None
    references: str | None = None
    assigned_to: str | None = None
    triaged_by: str | None = None
    dev_notes: str | None = None
    tags: list[str] | None = None     # full replacement; None = leave unchanged


class FindingOut(FindingBase):
    id: str
    scan_id: str
    user_id: str
    triaged_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class AIVerdictRunOut(BaseModel):
    id: str
    finding_id: str
    ran_by: str | None
    ran_by_email: str = ""
    verdict: AIVerdict
    rationale: str
    model: str
    created_at: dt.datetime

    class Config:
        from_attributes = True


# ---- product-level findings rollup ----
class ProductFindingRow(FindingOut):
    """Extends FindingOut with which scan it lives under, for the
    product-scoped findings view that crosses scans."""
    scan_product: str = ""
    scan_target: str = ""
    # Stable "Scan #N" rank computed server-side so the SPA doesn't need
    # to refetch all scans.
    scan_rank: int = 0


class ProductFindingsSummary(BaseModel):
    """Counts for the product detail page summary card."""
    total: int = 0
    # by dev verdict (the 3 enum values plus "sbp"/"duplicate"/"fixed")
    by_status: dict[str, int] = {}
    # by ai_verdict (open / true_positive / false_positive)
    by_ai_verdict: dict[str, int] = {}
    # by tag (sbp / ss / vuln)
    by_tag: dict[str, int] = {}
    # by severity (critical / high / medium / low / info / unknown)
    by_severity: dict[str, int] = {}
    scan_count: int = 0


VulnScanDetail.model_rebuild()


# ---- projects ----
class ProjectBase(BaseModel):
    name: str
    description: str = ""


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    # "" clears the pin and falls back to the team's choice, then the default.
    ai_provider: str | None = None
    ai_model: str | None = None


class ProjectMerge(BaseModel):
    """Merge this project (source) into another (target).

    Admin-only: re-targets every FK pointing at the source project, unions
    members, and then deletes the source row.
    """
    into_id: str


class UserMini(BaseModel):
    id: str
    email: str

    class Config:
        from_attributes = True


class ProjectOut(ProjectBase):
    id: str
    created_by: str
    created_at: dt.datetime
    updated_at: dt.datetime
    # NULL = inherit (team, then server default).
    ai_provider: str | None = None
    ai_model: str | None = None
    # Viewer-relative — true when the viewer owns/belongs to this project.
    # Populated by the router; default False if omitted.
    i_am_owner: bool = False
    i_am_member: bool = False

    class Config:
        from_attributes = True


class ProjectDetail(ProjectOut):
    members: list[UserMini] = []
    # Viewer-relative flags so the SPA can render the right slice of the page.
    i_am_member: bool = False
    i_am_owner: bool = False
    can_edit: bool = False
    # Shared source-file library — uploads from any member's Workbench
    # session land here and are visible to every member.
    file_count: int = 0
    file_bytes: int = 0


class ProjectFileOut(BaseModel):
    id: str
    relpath: str
    content_type: str
    sha256: str
    size_bytes: int
    uploaded_by_email: str | None = None
    created_at: dt.datetime


class ProjectFilesOut(BaseModel):
    count: int
    total_bytes: int
    files: list[ProjectFileOut]


class PromptTemplateIn(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str = ""
    category: str = ""
    body: str = Field(min_length=1)


class PromptTemplateOut(BaseModel):
    id: str
    title: str
    description: str
    category: str
    body: str
    created_by: str | None = None
    created_by_email: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class ProductComponentOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str
    role: str
    source_name: str
    file_count: int
    total_bytes: int
    ai_rationale: str
    created_at: dt.datetime

    class Config:
        from_attributes = True


# ---- runs ----
class RunOut(BaseModel):
    session_id: str
    user_id: str
    title: str
    product: str
    subcomponent: str
    project_id: str | None
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class RunUpdate(BaseModel):
    title: str | None = None
    product: str | None = None
    subcomponent: str | None = None
    project_id: str | None = None  # explicit null clears; absent leaves unchanged
    harness_id: str | None = None  # explicit null clears; absent leaves unchanged


# ---- harnesses ----
class HarnessFileOut(BaseModel):
    relpath: str
    size_bytes: int
    content_type: str
    sha256: str

    class Config:
        from_attributes = True


class HarnessOut(BaseModel):
    id: str
    user_id: str
    project_id: str | None
    project_name: str | None = None
    name: str
    description: str
    file_count: int
    total_bytes: int
    created_at: dt.datetime
    updated_at: dt.datetime

    class Config:
        from_attributes = True


class HarnessDetail(HarnessOut):
    files: list[HarnessFileOut] = []


class HarnessUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    project_id: str | None = None  # explicit null clears


class HarnessFileEdit(BaseModel):
    relpath: str = Field(min_length=1)
    content: str = ""  # UTF-8 text; create-or-update at relpath


# ---- folder imports ----
class StagedFile(BaseModel):
    relpath: str
    size: int
    mime: str | None = None


class FolderImportOut(BaseModel):
    id: str
    user_id: str
    label: str
    status: ImportStatus
    project_id: str | None = None
    project_name: str | None = None
    file_count: int
    total_bytes: int
    created_at: dt.datetime
    planned_at: dt.datetime | None = None
    applied_at: dt.datetime | None = None
    error_message: str = ""

    class Config:
        from_attributes = True


class FolderImportDetail(FolderImportOut):
    files: list[StagedFile] = []
    plan: dict | None = None
    plan_log: str = ""


class FolderImportConfirm(BaseModel):
    """The (possibly user-edited) plan to apply.

    Shape mirrors what the planner emits — see ai/import_planner.py:PLAN_SCHEMA.
    Kept as a free-form dict so the front-end can carry through fields the
    server doesn't need to validate strictly.
    """
    plan: dict


# ---- notifications ----
class NotificationOut(BaseModel):
    id: str
    user_id: str
    kind: NotificationKind
    title: str
    body: str
    link: str
    data: dict | None = None
    actor_user_id: str | None = None
    read_at: dt.datetime | None = None
    created_at: dt.datetime

    class Config:
        from_attributes = True


class NotificationCount(BaseModel):
    unread: int


# ---- project access requests ----
class ProjectAccessRequestCreate(BaseModel):
    project_id: str
    reason: str = ""
    import_id: str | None = None  # optional folder import to apply on approval


class ProjectAccessRequestDecision(BaseModel):
    reason: str = ""


class ProjectAccessRequestOut(BaseModel):
    id: str
    project_id: str
    project_name: str = ""
    user_id: str
    user_email: str = ""
    status: AccessRequestStatus
    reason: str
    import_id: str | None = None
    # Lightweight preview for the owner's approval UI.
    import_file_count: int = 0
    import_status: ImportStatus | None = None
    decided_by: str | None = None
    decided_at: dt.datetime | None = None
    decision_reason: str = ""
    created_at: dt.datetime

    class Config:
        from_attributes = True


# ---- project invites ----
class ProjectInviteCreate(BaseModel):
    expires_in_days: int | None = 30   # None = never expires
    max_uses: int | None = None        # None = unlimited
    note: str = ""


class ProjectInviteOut(BaseModel):
    id: str
    project_id: str
    token: str
    created_by: str
    expires_at: dt.datetime | None = None
    max_uses: int | None = None
    uses_count: int = 0
    revoked_at: dt.datetime | None = None
    note: str = ""
    created_at: dt.datetime

    class Config:
        from_attributes = True


# ---- scan share links (guest triage) ----
class ShareLinkCreate(BaseModel):
    label: str = ""
    expires_in_days: int | None = 30   # None = never expires
    allow_poc: bool = False


class ShareLinkOut(BaseModel):
    id: str
    scan_id: str
    token_prefix: str
    label: str
    created_by: str
    created_by_email: str | None = None
    allow_poc: bool
    expires_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None
    created_at: dt.datetime
    status: str = "active"  # active|expired|revoked
    # Plaintext token + full URL: returned ONCE on creation, never again.
    token: str | None = None
    url: str | None = None

    class Config:
        from_attributes = True


class ProjectInvitePreview(BaseModel):
    """Unauthenticated view served at /invites/{token}.

    Just enough info to render a 'Join Project X' landing page without
    revealing private project details to the world.
    """
    project_id: str
    project_name: str
    project_description: str = ""
    inviter_email: str = ""
    expires_at: dt.datetime | None = None
    status: str = "active"  # active|expired|used_up|revoked|unknown
