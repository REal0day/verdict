import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useState } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Badge, SeverityChip } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input, Label, Select } from "@/components/ui/Input";
import { statusBadge, type CaseOut } from "./Cases";
import { downloadFile } from "@/lib/download";
import {
  ArrowLeft, FolderGit2, HardDrive, Upload, FolderTree, Check, AlertCircle,
  Play, Trash2, ListChecks, FileText, RotateCw, ChevronDown, ChevronRight, Loader2, Download, Plus, ScanSearch,
} from "lucide-react";

type ResolvedModel = { provider: string | null; model: string | null; source: string };
type StageRun = {
  id: string; case_id: string; finding_id: string | null; finding_title: string | null;
  stage: string; status: string; ai_provider: string | null; ai_model: string | null;
  resolved: ResolvedModel | null; session_id: string | null; has_output: boolean;
  artifact_id: string | null; artifact_name: string | null;
  error: string; created_at: string;
};
type FindingRow = {
  id: string; title: string; severity: string; status: string;
  cwe: string; affected_component: string;
};
type CaseSource = {
  kind: string; local_path: string; remote_url: string; ref: string;
  status: string; workspace_key: string; detail: string;
};
type CaseDetailT = CaseOut & {
  report_text: string | null; source: CaseSource | null;
  findings: FindingRow[]; stage_runs: StageRun[];
};

const STAGES = ["discover", "source", "impact", "remediation", "poc", "report"] as const;
const STAGE_STATUS_TONE: Record<string, "muted" | "primary" | "success" | "danger" | "default"> = {
  pending: "muted", running: "primary", done: "success", error: "danger", skipped: "default",
};

export function CaseDetail() {
  const { case_id } = useParams();
  const qc = useQueryClient();
  const nav = useNavigate();
  const q = useQuery({
    queryKey: ["case", case_id],
    queryFn: () => api<CaseDetailT>(`/cases/${case_id}`),
    enabled: !!case_id,
    // Poll while any stage is still working so the executor's progress shows live.
    refetchInterval: (query) => {
      const d = query.state.data as CaseDetailT | undefined;
      const busy = d?.stage_runs?.some((r) => r.status === "pending" || r.status === "running");
      return busy ? 2000 : false;
    },
  });

  const del = useMutation({
    mutationFn: () => api(`/cases/${case_id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["cases"] }); nav("/cases"); },
  });

  if (q.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (q.isError || !q.data) return <Card className="p-6 text-sm text-danger">Case not found.</Card>;
  const c = q.data;
  const refresh = () => qc.invalidateQueries({ queryKey: ["case", case_id] });

  return (
    <>
      <div className="mb-4">
        <Link to="/cases" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> Investigations
        </Link>
      </div>
      <PageHeader
        title={c.title || "(untitled case)"}
        subtitle={
          <span className="inline-flex items-center gap-2">
            {statusBadge(c.status)}
            {c.project_name ? (
              <Link to={`/products/${c.project_id}`} className="inline-flex items-center gap-1 hover:underline">
                <FolderGit2 size={12} /> {c.project_name}
              </Link>
            ) : null}
            <span className="text-fgmuted">
              model: {c.ai_provider ? `${c.ai_provider}${c.ai_model ? ` / ${c.ai_model}` : ""}` : "default"}
            </span>
          </span>
        }
        action={
          <Button size="sm" variant="ghost" onClick={() => {
            if (confirm("Delete this case? The findings' scan is kept.")) del.mutate();
          }}>
            <Trash2 size={14} /> Delete
          </Button>
        }
      />

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          <FindingsCard findings={c.findings} scanId={c.scan_id} caseId={c.id}
                        sourceReady={c.source?.status === "ready"}
                        caseTitle={c.title} onChange={refresh} />
          <StagesCard c={c} onChange={refresh} />
        </div>
        <div className="space-y-6">
          <SourceCard c={c} onChange={refresh} />
          {c.report_text ? (
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2">
                <FileText size={14} /> Bug report</CardTitle></CardHeader>
              <CardBody>
                <pre className="text-xs whitespace-pre-wrap text-fgmuted max-h-64 overflow-y-auto">
                  {c.report_text}
                </pre>
              </CardBody>
            </Card>
          ) : null}
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ source */

function SourceCard({ c, onChange }: { c: CaseDetailT; onChange: () => void }) {
  const src = c.source;
  const [mode, setMode] = useState<string>(src?.kind && src.kind !== "none" ? src.kind : "local_path");

  return (
    <Card>
      <CardHeader><CardTitle className="flex items-center gap-2">
        <HardDrive size={14} /> Source</CardTitle></CardHeader>
      <CardBody className="space-y-3">
        {src && src.kind !== "none" ? (
          <div className="text-xs space-y-1">
            <div className="flex items-center gap-2">
              <span className="text-fgmuted">current:</span>
              <Badge tone={src.status === "ready" ? "success" : src.status === "error" ? "danger" : "muted"}>
                {src.kind} · {src.status}
              </Badge>
            </div>
            {src.local_path ? <div className="text-fgmuted">path: <code>{src.local_path}</code></div> : null}
            {src.detail ? <div className="text-fgmuted">{src.detail}</div> : null}
          </div>
        ) : (
          <p className="text-xs text-fgmuted">No source attached yet.</p>
        )}

        <div className="flex gap-1 text-xs">
          {[["local_path", "Local disk"], ["upload", "Uploaded files"]].map(([k, label]) => (
            <button key={k} type="button" onClick={() => setMode(k)}
              className={"px-2 py-1 rounded border " +
                (mode === k ? "border-primary bg-primary/5" : "border-border text-fgmuted hover:bg-muted/50")}>
              {label}
            </button>
          ))}
        </div>

        {mode === "local_path" ? <LocalPicker caseId={c.id} onChange={onChange} />
          : <UploadInfo caseId={c.id} projectId={c.project_id} onChange={onChange} />}
      </CardBody>
    </Card>
  );
}

function LocalPicker({ caseId, onChange }: { caseId: string; onChange: () => void }) {
  const [path, setPath] = useState("");
  const browse = useQuery({
    queryKey: ["source-browse", path],
    queryFn: () => api<{ available: boolean; path: string; dirs: string[] }>(
      `/cases/source/browse?path=${encodeURIComponent(path)}`),
    retry: false,
  });

  const attach = useMutation({
    mutationFn: (p: string) => api(`/cases/${caseId}/source`, {
      method: "PUT", body: { kind: "local_path", local_path: p },
    }),
    onSuccess: onChange,
  });

  const data = browse.data;
  if (data && !data.available) {
    return (
      <div className="flex items-start gap-2 text-xs rounded border border-warning/40 bg-warning/10 px-2 py-2">
        <AlertCircle size={13} className="mt-0.5 shrink-0 text-warning" />
        <span>No source root configured. Set <code>SOURCE_ROOT</code> in <code>.env</code> and
          restart, or use uploaded files instead.</span>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="text-[11px] text-fgmuted flex items-center gap-1">
        <FolderTree size={11} /> SOURCE_ROOT{path ? ` / ${path}` : ""}
      </div>
      <div className="flex flex-wrap gap-1">
        {path ? (
          <button type="button" onClick={() => setPath(path.split("/").slice(0, -1).join("/"))}
                  className="px-2 py-1 rounded border border-border text-xs text-fgmuted hover:bg-muted/50">
            ← up
          </button>
        ) : null}
        {(data?.dirs ?? []).map((d) => {
          const child = path ? `${path}/${d}` : d;
          return (
            <span key={d} className="inline-flex rounded border border-border overflow-hidden text-xs">
              <button type="button" onClick={() => setPath(child)}
                      className="px-2 py-1 hover:bg-muted/50">{d}</button>
              <button type="button" title="Use this directory"
                      onClick={() => attach.mutate(child)}
                      className="px-1.5 border-l border-border text-primary hover:bg-primary/10">
                <Check size={12} />
              </button>
            </span>
          );
        })}
        {data && data.dirs.length === 0 ? <span className="text-xs text-fgmuted">(no subdirectories)</span> : null}
      </div>
      <div className="flex items-center gap-1">
        <Input value={path} onChange={(e) => setPath(e.target.value)}
               placeholder="or type a path under SOURCE_ROOT" className="text-xs" />
        <Button type="button" size="sm" onClick={() => attach.mutate(path)} disabled={attach.isPending}>
          Attach
        </Button>
      </div>
      {attach.isError ? (
        <p className="text-xs text-danger">{(attach.error as any)?.detail || "Couldn't attach that path."}</p>
      ) : null}
    </div>
  );
}

function UploadInfo({ caseId, projectId, onChange }: {
  caseId: string; projectId: string | null; onChange: () => void;
}) {
  const use = useMutation({
    mutationFn: () => api(`/cases/${caseId}/source`, { method: "PUT", body: { kind: "upload" } }),
    onSuccess: onChange,
  });
  return (
    <div className="space-y-2 text-xs text-fgmuted">
      <p className="flex items-start gap-2">
        <Upload size={13} className="mt-0.5 shrink-0" />
        Uses the source files uploaded to this case's product.
        {projectId ? (
          <> Manage them on the <Link to={`/products/${projectId}`} className="text-primary hover:underline">product page</Link>.</>
        ) : <> Attach this case to a product first.</>}
      </p>
      <Button type="button" size="sm" variant="secondary" onClick={() => use.mutate()}
              disabled={use.isPending || !projectId}>
        Use uploaded files
      </Button>
    </div>
  );
}

/* ---------------------------------------------------------------- findings */

function FindingsCard({ findings, scanId, caseId, sourceReady, caseTitle, onChange }: {
  findings: FindingRow[]; scanId: string | null; caseId: string;
  sourceReady: boolean; caseTitle: string; onChange: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState("unknown");

  const add = useMutation({
    mutationFn: () => api(`/scans/${scanId}/findings`, {
      method: "POST", body: { title: title.trim() || caseTitle || "Untitled finding", severity },
    }),
    onSuccess: () => { setAdding(false); setTitle(""); setSeverity("unknown"); onChange(); },
  });

  // Discover: scan the source and let the model populate the findings table.
  const discover = useMutation({
    mutationFn: () => api(`/cases/${caseId}/stages`, { method: "POST", body: { stage: "discover" } }),
    onSuccess: onChange,
  });

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Findings ({findings.length})</CardTitle>
        <div className="flex items-center gap-3">
          <Button size="sm" onClick={() => discover.mutate()}
                  disabled={discover.isPending || !sourceReady}
                  title={sourceReady ? "Scan the source and list vulnerabilities" : "Attach a source first"}>
            <ScanSearch size={13} /> {discover.isPending ? "Scanning…" : "Discover vulns"}
          </Button>
          {scanId ? (
            <Button size="sm" variant="secondary" onClick={() => { setAdding((v) => !v); setTitle(caseTitle || ""); }}>
              <Plus size={13} /> Add finding
            </Button>
          ) : null}
          {scanId ? (
            <Link to={`/scans/${scanId}`} className="text-xs text-primary hover:underline">open scan</Link>
          ) : null}
        </div>
      </CardHeader>
      <CardBody>
        {adding ? (
          <div className="flex flex-wrap items-end gap-2 mb-4 border-b border-border pb-4">
            <div className="flex-1 min-w-[14rem]">
              <Label htmlFor="ft">Finding title</Label>
              <Input id="ft" value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder="e.g. Prototype pollution in setKey" autoFocus />
            </div>
            <div>
              <Label htmlFor="fs">Severity</Label>
              <Select id="fs" value={severity} onChange={(e) => setSeverity(e.target.value)}>
                {["unknown","info","low","medium","high","critical"].map((v) =>
                  <option key={v} value={v}>{v}</option>)}
              </Select>
            </div>
            <Button size="sm" onClick={() => add.mutate()} disabled={add.isPending}>
              {add.isPending ? "Adding…" : "Add"}
            </Button>
          </div>
        ) : null}
        {findings.length === 0 ? (
          <div className="text-sm text-fgmuted space-y-1">
            <p>No findings yet.</p>
            <p>
              {sourceReady
                ? <>Click <strong>Discover vulns</strong> to scan the attached source — the model reads the code (and your report) and fills this table. Or <strong>Add finding</strong> to enter one by hand.</>
                : <>Attach a source in the panel on the right, then <strong>Discover vulns</strong> scans it and lists what it finds here.</>}
            </p>
          </div>
        ) : (
          <Table>
            <THead><TR><TH>Title</TH><TH>Severity</TH><TH>CWE</TH><TH>Component</TH></TR></THead>
            <tbody>
              {findings.map((f) => (
                <TR key={f.id}>
                  <TD className="font-medium">
                    <Link to={`/scans/${scanId}/findings/${f.id}`} className="hover:underline">
                      {f.title || "(untitled)"}
                    </Link>
                  </TD>
                  <TD><SeverityChip value={f.severity} /></TD>
                  <TD className="text-xs text-fgmuted">{f.cwe || "—"}</TD>
                  <TD className="text-xs text-fgmuted">{f.affected_component || "—"}</TD>
                </TR>
              ))}
            </tbody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ stages */

function StagesCard({ c, onChange }: { c: CaseDetailT; onChange: () => void }) {
  const [stage, setStage] = useState<string>("report");
  const [findingId, setFindingId] = useState<string>("");
  const isCaseLevel = stage === "report" || stage === "discover";

  const queue = useMutation({
    mutationFn: () => api<StageRun>(`/cases/${c.id}/stages`, {
      method: "POST",
      body: { stage, finding_id: isCaseLevel ? null : (findingId || null) },
    }),
    onSuccess: onChange,
  });

  const runs = c.stage_runs;
  return (
    <Card>
      <CardHeader className="flex items-center gap-2">
        <CardTitle className="flex items-center gap-2"><ListChecks size={14} /> Pipeline stages</CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <div className="flex flex-wrap items-end gap-2 border-b border-border pb-4">
          <div>
            <Label htmlFor="st">Stage</Label>
            <Select id="st" value={stage} onChange={(e) => setStage(e.target.value)} className="text-sm">
              {STAGES.map((s) => <option key={s} value={s}>{s}</option>)}
            </Select>
          </div>
          {!isCaseLevel ? (
            <div className="min-w-[12rem]">
              <Label htmlFor="fi">Finding</Label>
              <Select id="fi" value={findingId} onChange={(e) => setFindingId(e.target.value)} className="text-sm">
                <option value="">Select a finding…</option>
                {c.findings.map((f) => <option key={f.id} value={f.id}>{f.title || f.id.slice(0, 8)}</option>)}
              </Select>
            </div>
          ) : null}
          <Button type="button" size="sm" onClick={() => queue.mutate()}
                  disabled={queue.isPending || (!isCaseLevel && !findingId)}>
            <Play size={13} /> Queue
          </Button>
          <span className="text-[11px] text-fgmuted">runs on the executor; drains over time.</span>
        </div>
        {queue.isError ? (
          <p className="text-xs text-danger">{(queue.error as any)?.detail || "Couldn't queue that stage."}</p>
        ) : null}

        {runs.length === 0 ? (
          <p className="text-sm text-fgmuted">No stage runs queued.</p>
        ) : (
          <div className="divide-y divide-border border border-border rounded-md">
            {runs.map((r) => <StageRow key={r.id} caseId={c.id} run={r} onChange={onChange} />)}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function StageRow({ caseId, run, onChange }: {
  caseId: string; run: StageRun; onChange: () => void;
}) {
  const [openOut, setOpenOut] = useState(false);
  const output = useQuery({
    queryKey: ["stage-output", run.id],
    queryFn: () => api<{ output: string }>(`/cases/${caseId}/stages/${run.id}/output`),
    enabled: openOut && run.has_output,
  });
  const rerun = useMutation({
    mutationFn: () => api(`/cases/${caseId}/stages/${run.id}/run`, { method: "POST" }),
    onSuccess: onChange,
  });
  const running = run.status === "running" || run.status === "pending";

  return (
    <div className="text-sm">
      <div className="flex items-center gap-3 px-3 py-2">
        <button type="button" onClick={() => setOpenOut((v) => !v)}
                className="text-fgmuted hover:text-fg disabled:opacity-30" disabled={!run.has_output && !run.error}>
          {openOut ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <span className="font-medium w-24">{run.stage}</span>
        <span className="text-xs text-fgmuted flex-1 truncate">
          {run.finding_id ? (run.finding_title || run.finding_id.slice(0, 8)) : "case-level"}
        </span>
        <Badge tone={STAGE_STATUS_TONE[run.status] ?? "default"}>
          {running ? <Loader2 size={11} className="inline mr-1 animate-spin" /> : null}{run.status}
        </Badge>
        <span className="text-xs text-fgmuted w-40 truncate" title={run.resolved ? `via ${run.resolved.source}` : ""}>
          {run.resolved ? `${run.resolved.provider ?? "default"}${run.resolved.model ? ` / ${run.resolved.model}` : ""}` : "—"}
        </span>
        {run.artifact_id ? (
          <button type="button" title={`Download ${run.artifact_name || "artifact"}`}
                  onClick={() => downloadFile(`/cases/${caseId}/stages/${run.id}/artifact`, run.artifact_name || undefined)}
                  className="text-fgmuted hover:text-primary">
            <Download size={13} />
          </button>
        ) : null}
        <button type="button" onClick={() => rerun.mutate()} disabled={rerun.isPending || run.status === "running"}
                title="Re-run this stage" className="text-fgmuted hover:text-primary disabled:opacity-30">
          <RotateCw size={13} className={rerun.isPending ? "animate-spin" : ""} />
        </button>
      </div>
      {openOut ? (
        <div className="px-3 pb-3">
          {run.error ? (
            <div className="text-xs text-danger bg-danger/10 border border-danger/30 rounded px-2 py-1.5">
              {run.error}
            </div>
          ) : output.isLoading ? (
            <div className="text-xs text-fgmuted">Loading…</div>
          ) : output.data?.output ? (
            <pre className="text-xs whitespace-pre-wrap bg-muted/40 border border-border rounded p-3 max-h-96 overflow-y-auto">
              {output.data.output}
            </pre>
          ) : (
            <div className="text-xs text-fgmuted">No output yet.</div>
          )}
        </div>
      ) : null}
    </div>
  );
}
