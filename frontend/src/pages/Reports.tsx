import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useMemo, useState, type FormEvent } from "react";
import { api, getToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { downloadFile } from "@/lib/download";
import { PageHeader } from "@/components/Layout";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import { ChatReply } from "@/components/ChatReply";
import { AIErrorNotice, AIUnavailableNotice, useAIStatus, useProviderName } from "@/components/AIStatus";
import { Select } from "@/components/ui/Input";
import {
  FileText, Send, Sparkles, Download, MessagesSquare, X,
  FolderGit2, ShieldAlert, Check, Plus, ChevronRight, ArrowLeft, User, Upload,
} from "lucide-react";

type ReportOut = {
  id: string;
  user_id: string;
  filename: string;
  title: string;
  original_path: string | null;
  source_tool: string;
  sha256: string;
  size_bytes: number;
  summary: string | null;
  created_at: string;
  owner_email: string | null;
  agent_hostname: string | null;
  project_id: string | null;
  effective_project_id: string | null;
  derived_scan_id: string | null;
  derived_scan_product: string | null;
  derived_scan_state: string | null;
};

type Project = {
  id: string; name: string;
  i_am_owner?: boolean; i_am_member?: boolean;
};
type AgentLite = {
  id: string; hostname: string; last_seen: string | null;
};
type ScanLite = {
  id: string; title: string; product: string; findings: number;
  project_id: string | null; source_report_id: string | null;
};

// Browser upload of a single report/document: pick a product, optionally attach
// it to an existing scan (to back a scan that shows summary numbers but has no
// source document — its counts are left untouched).
function UploadReportCard({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [projectId, setProjectId] = useState("");
  const [scanId, setScanId] = useState("");
  const [createScan, setCreateScan] = useState(false);

  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });
  const scans = useQuery({ queryKey: ["scans"], queryFn: () => api<ScanLite[]>("/scans") });
  const productScans = (scans.data || []).filter((s) => !projectId || s.project_id === projectId);

  const upload = useMutation({
    mutationFn: async () => {
      const fd = new FormData();
      fd.append("file", file!, file!.name);
      if (projectId) fd.append("project_id", projectId);
      if (scanId) fd.append("scan_id", scanId);
      if (!scanId && createScan) fd.append("create_scan", "true");
      const tok = getToken();
      const r = await fetch("/reports/upload", {
        method: "POST",
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
        body: fd,
      });
      if (!r.ok) throw new Error((await r.text()) || "Upload failed");
      return r.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reports"] });
      qc.invalidateQueries({ queryKey: ["scans"] });
      onClose();
    },
  });

  return (
    <Card className="mb-4 border-primary/40">
      <CardHeader className="flex items-center justify-between py-2.5">
        <CardTitle className="flex items-center gap-2"><Upload size={14} className="text-primary" /> Upload a report</CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}><X size={12} /> Close</Button>
      </CardHeader>
      <CardBody className="space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <Label htmlFor="rf">File <span className="opacity-60">(any document)</span></Label>
            <input id="rf" type="file"
                   onChange={(e) => setFile(e.target.files?.[0] || null)}
                   className="block w-full text-sm text-fgmuted file:mr-3 file:rounded file:border-0 file:bg-muted file:px-3 file:py-1.5 file:text-fg" />
          </div>
          <div>
            <Label htmlFor="rp">Product</Label>
            <Select id="rp" value={projectId} onChange={(e) => { setProjectId(e.target.value); setScanId(""); }}>
              <option value="">— none —</option>
              {(projects.data || []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          </div>
          <div>
            <Label htmlFor="rs">Link to existing scan <span className="opacity-60">(optional)</span></Label>
            <Select id="rs" value={scanId} onChange={(e) => setScanId(e.target.value)}>
              <option value="">— none —</option>
              {productScans.map((s) => (
                <option key={s.id} value={s.id}>
                  {(s.title || s.product || "(untitled)")} · {s.findings} findings{s.source_report_id ? "" : " · no source"}
                </option>
              ))}
            </Select>
          </div>
          {!scanId ? (
            <label className="text-sm flex items-end gap-2 pb-1.5">
              <input type="checkbox" checked={createScan} onChange={(e) => setCreateScan(e.target.checked)} />
              Create a draft scan from this report (text reports only)
            </label>
          ) : null}
        </div>
        <div className="flex items-center gap-3">
          <Button onClick={() => upload.mutate()} disabled={!file || upload.isPending}>
            {upload.isPending ? "Uploading…" : "Upload"}
          </Button>
          {scanId ? <span className="text-xs text-fgmuted">Attaches to the scan; its counts stay as-is.</span> : null}
          {upload.isError ? <span className="text-xs text-danger">{(upload.error as Error).message}</span> : null}
        </div>
      </CardBody>
    </Card>
  );
}

type ChatTurn = {
  user: string;
  assistant: string;
  generated_report?: { id: string; filename: string };
};
type ChatResponse = {
  session_id: string;
  reply: string;
  generated_report_id: string | null;
};

/**
 * Reports landing: a list of products with per-product report counts and
 * a last-upload timestamp. Click a product to drill into the
 * spreadsheet-style table at /reports/by-product/:project_id.
 */
export function Reports() {
  const nav = useNavigate();
  const { me } = useAuth();
  const [mineOnly, setMineOnly] = useState(false);
  const [showUpload, setShowUpload] = useState(false);
  const q = useQuery({
    queryKey: ["reports"],
    queryFn: () => api<ReportOut[]>("/reports?limit=500"),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api<AgentLite[]>("/agents"),
  });

  const projectName = useMemo(() => {
    const m = new Map<string, string>();
    for (const p of projects.data || []) m.set(p.id, p.name);
    return m;
  }, [projects.data]);

  type Bucket = {
    key: string;            // project_id, or "_none"
    name: string;           // display label
    project_id: string | null;
    count: number;
    last_at: string;        // ISO of newest report in the bucket
  };

  const buckets: Bucket[] = useMemo(() => {
    const m = new Map<string, Bucket>();
    const source = mineOnly && me
      ? (q.data || []).filter((r) => r.user_id === me.id)
      : (q.data || []);
    for (const r of source) {
      const raw = r.effective_project_id ?? null;
      // A project_id that no longer resolves (product was deleted) is treated
      // exactly the same as no project at all.
      const pid = raw && projectName.has(raw) ? raw : null;
      const key = pid ?? "_none";
      const cur = m.get(key);
      if (cur) {
        cur.count += 1;
        if (r.created_at > cur.last_at) cur.last_at = r.created_at;
      } else {
        m.set(key, {
          key,
          name: pid ? projectName.get(pid)! : "No product",
          project_id: pid,
          count: 1,
          last_at: r.created_at,
        });
      }
    }
    const out = Array.from(m.values());
    out.sort((a, b) => {
      if (a.project_id === null) return 1;
      if (b.project_id === null) return -1;
      return a.name.localeCompare(b.name);
    });
    return out;
  }, [q.data, projectName, mineOnly, me]);

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Pick a product to see its reports."
        action={
          <div className="flex items-center gap-2">
            <Button size="sm" onClick={() => setShowUpload((v) => !v)}>
              <Upload size={14} /> Upload report
            </Button>
            {q.data && q.data.length > 0 ? (
              <Button variant={mineOnly ? "primary" : "secondary"} size="sm"
                      onClick={() => setMineOnly((v) => !v)}>
                <User size={14} /> {mineOnly ? "Showing only mine" : "Show only mine"}
              </Button>
            ) : null}
          </div>
        }
      />

      {showUpload ? <UploadReportCard onClose={() => setShowUpload(false)} /> : null}

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : q.isError ? (
        <div className="text-sm text-danger">Failed to load reports.</div>
      ) : !q.data || q.data.length === 0 ? (
        <ReportsEmpty agents={agents.data || []} />
      ) : buckets.length === 0 ? (
        <div className="text-sm text-fgmuted">
          None of these reports are yours — turn off “Show only mine” to see everyone's.
        </div>
      ) : (
        <div className="space-y-2">
          {buckets.map((b) => {
            const to = b.project_id
              ? `/reports/by-product/${b.project_id}`
              : `/reports/by-product/none`;
            return (
              <button
                key={b.key}
                type="button"
                onClick={() => nav(to)}
                className="w-full text-left bg-surface border border-border rounded-md
                           hover:border-primary/40 hover:bg-muted/30 transition-colors
                           px-4 py-3 flex items-center justify-between gap-3"
              >
                <div className="flex items-center gap-3 min-w-0">
                  <FolderGit2 size={16} className="text-fgmuted shrink-0" />
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">
                      {b.project_id ? b.name : <span className="text-fgmuted italic">{b.name}</span>}
                    </div>
                    <div className="text-xs text-fgmuted">
                      {b.count} report{b.count === 1 ? "" : "s"} · most recent {fmt(b.last_at)}
                    </div>
                  </div>
                </div>
                <ChevronRight size={16} className="text-fgmuted shrink-0" />
              </button>
            );
          })}
        </div>
      )}
    </>
  );
}


/**
 * Per-product reports view at /reports/by-product/:project_id.
 *
 * Renders the same spreadsheet-style grouped table that used to live on
 * the Reports landing, plus the BulkAssign and ChatPanel, scoped to
 * exactly one product (or "no product" for orphans when the URL param
 * is the literal "none").
 */
export function ProductReports() {
  const { project_id = "" } = useParams();
  const qc = useQueryClient();
  const { me } = useAuth();
  const [mineOnly, setMineOnly] = useState(false);
  const targetId: string | null = project_id === "none" ? null : project_id;

  const q = useQuery({
    queryKey: ["reports", "by-product", project_id],
    queryFn: () => api<ReportOut[]>("/reports?limit=500"),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const knownPids = useMemo(
    () => (projects.data ? new Set(projects.data.map((p) => p.id)) : null),
    [projects.data],
  );
  const normPid = (pid: string | null | undefined) => {
    if (!pid) return null;
    if (knownPids && !knownPids.has(pid)) return null;
    return pid;
  };

  const rows = useMemo(() => {
    return (q.data || []).filter((r) =>
      normPid(r.effective_project_id) === targetId &&
      (!mineOnly || (me ? r.user_id === me.id : true)));
  }, [q.data, targetId, knownPids, mineOnly, me]);

  const project = useMemo(
    () => (projects.data || []).find((p) => p.id === targetId),
    [projects.data, targetId],
  );

  const [selected, setSelected] = useState<Set<string>>(new Set());
  function toggle(id: string) {
    const n = new Set(selected);
    n.has(id) ? n.delete(id) : n.add(id);
    setSelected(n);
  }

  const title = targetId
    ? (project?.name || "No product")
    : "No product (orphan reports)";

  return (
    <>
      <PageHeader
        title={title}
        subtitle={`${rows.length} report${rows.length === 1 ? "" : "s"}`}
        action={
          <div className="flex items-center gap-3">
            <Button variant={mineOnly ? "primary" : "secondary"} size="sm"
                    onClick={() => setMineOnly((v) => !v)}>
              <User size={14} /> {mineOnly ? "Showing only mine" : "Show only mine"}
            </Button>
            <Link to="/" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
              <ArrowLeft size={12} /> Back to products
            </Link>
          </div>
        }
      />
      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : rows.length === 0 ? (
        <Card><CardBody><p className="text-sm text-fgmuted">No reports in this product yet.</p></CardBody></Card>
      ) : (
        <div className="space-y-4">
          <ProjectGroup
            projectId={targetId}
            projectName={project?.name ?? null}
            rows={rows}
            selected={selected}
            toggle={toggle}
            setSelected={setSelected}
          />

          {selected.size > 0 ? (
            <BulkAssign
              selectedIds={Array.from(selected)}
              projects={projects.data || []}
              clearSelection={() => setSelected(new Set())}
              onAssigned={() => {
                qc.invalidateQueries({ queryKey: ["reports", "by-product", project_id] });
                qc.invalidateQueries({ queryKey: ["reports"] });
              }}
            />
          ) : null}

          <ChatPanel
            selected={selected}
            clearSelection={() => setSelected(new Set())}
            totalReports={rows.length}
            onReportSaved={() => {
              qc.invalidateQueries({ queryKey: ["reports", "by-product", project_id] });
              qc.invalidateQueries({ queryKey: ["reports"] });
            }}
          />
        </div>
      )}
    </>
  );
}

function ReportsEmpty({ agents }: { agents: AgentLite[] }) {
  // Three flavors of "no reports": user has no agent → "install one"; user
  // has an agent that's phoned home → "waiting on first upload"; user has an
  // agent that's never been seen → "run the install script".
  if (agents.length === 0) {
    return (
      <Empty
        icon={<FileText size={28} />}
        title="No reports yet"
        hint={<>Install an agent from the <Link to="/agents" className="text-primary hover:underline">Agents</Link> page, run the one-liner on your Claude machine, and reports will appear here.</>}
      />
    );
  }
  const everSeen = agents.some((a) => !!a.last_seen);
  const newestSeen = agents
    .map((a) => a.last_seen)
    .filter((s): s is string => !!s)
    .sort()
    .pop();
  return (
    <Empty
      icon={<FileText size={28} />}
      title={everSeen ? "Your agent is connected — waiting for its first report" : "Agent installed, but it hasn't phoned home yet"}
      hint={
        <>
          {everSeen ? (
            <>Last heartbeat from <code className="text-fg">{agents[0].hostname}</code>: {newestSeen ? fmt(newestSeen) : "moments ago"}. Reports it produces will land here automatically.</>
          ) : (
            <>Run the install one-liner on your Claude machine — see the <Link to="/agents" className="text-primary hover:underline">Agents</Link> page.</>
          )}
        </>
      }
    />
  );
}

function BulkAssign({
  selectedIds, projects, clearSelection, onAssigned,
}: {
  selectedIds: string[];
  projects: Project[];
  clearSelection: () => void;
  onAssigned: () => void;
}) {
  // Only projects the viewer can actually add to.
  const assignable = projects.filter((p) => p.i_am_owner || p.i_am_member);
  const [projectId, setProjectId] = useState<string>("");
  const [savedAt, setSavedAt] = useState(0);

  const assign = useMutation({
    mutationFn: async () => {
      const pid = projectId === "" ? null : projectId;
      // Fire each PATCH in parallel; PATCH /reports/{id} accepts project_id.
      await Promise.all(
        selectedIds.map((id) =>
          api(`/reports/${id}`, {
            method: "PATCH",
            body: { project_id: pid },
          })
        )
      );
    },
    onSuccess: () => {
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(0), 2400);
      onAssigned();
      clearSelection();
    },
  });
  const justSaved = Date.now() - savedAt < 2400;

  return (
    <Card className="border-primary/40">
      <CardHeader className="flex items-center justify-between py-2.5">
        <CardTitle className="flex items-center gap-2">
          <Plus size={14} className="text-primary" />
          Add {selectedIds.length} report{selectedIds.length === 1 ? "" : "s"} to a product
        </CardTitle>
        {justSaved ? (
          <span className="text-xs text-success inline-flex items-center gap-1">
            <Check size={12} /> Assigned!
          </span>
        ) : null}
      </CardHeader>
      <CardBody>
        <div className="flex items-end gap-2 flex-wrap">
          <div className="flex-1 min-w-[16rem]">
            <Label htmlFor="assign-project">Product</Label>
            <Select
              id="assign-project"
              value={projectId}
              onChange={(e) => setProjectId(e.target.value)}
            >
              <option value="">— remove from product —</option>
              {assignable.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>
          <Button onClick={() => assign.mutate()} disabled={assign.isPending}>
            {assign.isPending ? "Assigning…" : "Assign"}
          </Button>
          <Button variant="ghost" onClick={clearSelection}>
            <X size={12} /> Clear
          </Button>
        </div>
        {assign.isError ? (
          <p className="text-xs text-danger mt-2">Some reports failed to update (probably permissions).</p>
        ) : null}
        {assignable.length === 0 ? (
          <p className="text-xs text-fgmuted mt-2">
            You're not a member of any product yet — <Link to="/products" className="text-primary hover:underline">create one</Link> first.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

function ProjectGroup({
  projectId, projectName, rows, selected, toggle, setSelected,
}: {
  projectId: string | null;
  projectName: string | null;
  rows: ReportOut[];
  selected: Set<string>;
  toggle: (id: string) => void;
  setSelected: (s: Set<string>) => void;
}) {
  const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.id));
  const someChecked = rows.some((r) => selected.has(r.id)) && !allChecked;
  function toggleGroup() {
    const next = new Set(selected);
    if (allChecked) rows.forEach((r) => next.delete(r.id));
    else rows.forEach((r) => next.add(r.id));
    setSelected(next);
  }
  return (
    <Card>
      <CardHeader className="flex items-center justify-between py-2.5">
        <CardTitle className="flex items-center gap-2">
          <FolderGit2 size={14} className="text-fgmuted" />
          {projectId ? (
            <Link to={`/projects/${projectId}`} className="text-fg hover:text-primary">
              {projectName}
            </Link>
          ) : (
            <span className="text-fgmuted">(No product)</span>
          )}
          <span className="text-xs text-fgmuted font-normal">
            · {rows.length} report{rows.length === 1 ? "" : "s"}
          </span>
        </CardTitle>
      </CardHeader>
      <Table>
        <THead>
          <TR>
            <TH className="w-10">
              <input
                type="checkbox"
                checked={allChecked}
                ref={(el) => { if (el) el.indeterminate = someChecked; }}
                onChange={toggleGroup}
                aria-label="Select all in group"
              />
            </TH>
            <TH>Title</TH>
            <TH>Tool</TH>
            <TH>By</TH>
            <TH>From</TH>
            <TH>Linked scan</TH>
            <TH>When</TH>
          </TR>
        </THead>
        <tbody>
          {rows.map((r) => (
            <TR key={r.id} className="hover:bg-muted/40">
              <TD>
                <input
                  type="checkbox"
                  checked={selected.has(r.id)}
                  onChange={() => toggle(r.id)}
                  aria-label={`Select ${r.title || r.filename}`}
                />
              </TD>
              <TD>
                <Link to={`/reports/${r.id}`} className="text-primary hover:underline font-medium">
                  {r.title || <span className="text-fgmuted italic">(untitled)</span>}
                </Link>
                <div className="text-[11px] text-fgmuted font-mono mt-0.5 truncate max-w-[36ch]" title={r.filename}>
                  {r.filename}
                </div>
              </TD>
              <TD><Badge tone="muted">{r.source_tool}</Badge></TD>
              <TD className="text-xs text-fgmuted whitespace-nowrap" title={r.owner_email || ""}>
                {r.owner_email || "—"}
              </TD>
              <TD className="text-xs text-fgmuted whitespace-nowrap" title={r.agent_hostname || ""}>
                {r.agent_hostname ? <code className="text-fg">{r.agent_hostname}</code> : "—"}
              </TD>
              <TD>
                {r.derived_scan_id ? (
                  <Link to={`/scans/${r.derived_scan_id}`}
                        className="inline-flex items-center gap-1.5 text-primary hover:underline text-sm">
                    <ShieldAlert size={12} />
                    {r.derived_scan_product || "(unnamed)"}
                    {r.derived_scan_state ? (
                      <Badge tone={r.derived_scan_state === "draft" ? "warning" : "success"} className="ml-1">
                        {r.derived_scan_state}
                      </Badge>
                    ) : null}
                  </Link>
                ) : <span className="text-fgmuted text-sm">—</span>}
              </TD>
              <TD className="text-fgmuted whitespace-nowrap text-xs">{fmt(r.created_at)}</TD>
            </TR>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function ChatPanel({
  selected, clearSelection, totalReports, onReportSaved,
}: {
  selected: Set<string>;
  clearSelection: () => void;
  totalReports: number;
  onReportSaved: () => void;
}) {
  const [message, setMessage] = useState("");
  const [saveAsReport, setSaveAsReport] = useState(false);
  const [filename, setFilename] = useState("");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const aiStatus = useAIStatus();
  const providerName = useProviderName();
  const aiDown = aiStatus.data ? !aiStatus.data.configured : false;

  const send = useMutation({
    mutationFn: () =>
      api<ChatResponse>("/chat", {
        method: "POST",
        body: {
          session_id: sessionId,
          message,
          report_ids: Array.from(selected),
          save_as_report: saveAsReport,
          save_filename: saveAsReport && filename.trim() ? filename.trim() : null,
        },
      }),
    onSuccess: async (r) => {
      setSessionId(r.session_id);
      const turn: ChatTurn = { user: message, assistant: r.reply };
      if (r.generated_report_id) {
        try {
          const meta = await api<ReportOut>(`/reports/${r.generated_report_id}`);
          turn.generated_report = { id: meta.id, filename: meta.filename };
          onReportSaved();
        } catch { /* swallow */ }
      }
      setTurns((t) => [...t, turn]);
      setMessage("");
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (message.trim()) send.mutate();
  }

  const scopeLine =
    selected.size === 0
      ? `No reports selected — ${providerName} will answer using only your message.`
      : selected.size === totalReports
      ? `All ${selected.size} reports selected — ${providerName} will reason over the full set.`
      : `${selected.size} report${selected.size === 1 ? "" : "s"} selected — ${providerName} will reason over those.`;

  return (
    <Card className="mt-6">
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Sparkles size={14} className="text-primary" /> Ask {providerName} about your reports
        </CardTitle>
        {turns.length ? (
          <Button variant="ghost" size="sm" onClick={() => { setTurns([]); setSessionId(null); }}>
            <X size={12} /> clear
          </Button>
        ) : null}
      </CardHeader>
      <CardBody className="space-y-3">
        <AIUnavailableNotice />
        <p className="text-xs text-fgmuted">{scopeLine}</p>

        <form onSubmit={submit} className="space-y-3">
          <Textarea
            rows={3}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="Examples: &quot;What are the unresolved critical findings?&quot; &nbsp; &quot;Generate a weekly rollup as markdown.&quot; &nbsp; &quot;Export a CSV of TP findings with title, severity, CWE.&quot;"
            disabled={send.isPending}
          />

          <div className="flex flex-col md:flex-row md:items-end gap-2">
            <label className="text-sm flex items-center gap-2">
              <input type="checkbox" checked={saveAsReport}
                     onChange={(e) => setSaveAsReport(e.target.checked)} />
              Save reply as a new report
            </label>
            {saveAsReport ? (
              <div className="flex-1 min-w-0">
                <Label htmlFor="fname">Filename</Label>
                <Input
                  id="fname"
                  value={filename}
                  onChange={(e) => setFilename(e.target.value)}
                  placeholder="weekly-rollup.md (or .csv / .json / .txt)"
                />
              </div>
            ) : null}
            <Button type="submit" disabled={send.isPending || !message.trim() || aiDown}>
              <Send size={14} /> {send.isPending ? `${providerName} is thinking…` : "Send"}
            </Button>
            {selected.size > 0 ? (
              <Button type="button" variant="ghost" size="sm" onClick={clearSelection}>
                Clear selection
              </Button>
            ) : null}
          </div>
        </form>

        <AIErrorNotice error={send.error} />

        {turns.length === 0 ? (
          <div className="text-xs text-fgmuted italic flex items-center gap-2 mt-2">
            <MessagesSquare size={12} /> No messages yet — your conversation will appear below.
          </div>
        ) : (
          <div className="space-y-4 mt-3 max-h-[55vh] overflow-y-auto pr-1">
            {turns.map((t, i) => (
              <Turn key={i} turn={t} />
            ))}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function Turn({ turn }: { turn: ChatTurn }) {
  const providerName = useProviderName("assistant");
  return (
    <div className="space-y-2">
      <div className="bg-muted/40 border border-border rounded-md px-3 py-2 text-sm whitespace-pre-wrap">
        <span className="text-xs text-fgmuted block mb-1">you</span>
        {turn.user}
      </div>
      <div className="bg-surface border border-border rounded-md px-3 py-2 text-sm">
        <span className="text-xs text-fgmuted block mb-1">{providerName}</span>
        <ChatReply
          text={turn.assistant}
          defaultFilename={turn.generated_report?.filename}
        />
        {turn.generated_report ? (
          <div className="mt-3 pt-2 border-t border-border flex items-center justify-between gap-3">
            <span className="text-xs text-fgmuted">
              Saved as{" "}
              <Link to={`/reports/${turn.generated_report.id}`} className="text-primary hover:underline font-medium">
                {turn.generated_report.filename}
              </Link>
            </span>
            <Button variant="secondary" size="sm"
                    onClick={() => downloadFile(
                      `/ui/reports/${turn.generated_report!.id}/download`,
                      turn.generated_report!.filename,
                    )}>
              <Download size={12} /> Download
            </Button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function fmt(iso: string) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
