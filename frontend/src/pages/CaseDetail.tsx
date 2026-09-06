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
import {
  ArrowLeft, FolderGit2, HardDrive, Upload, FolderTree, Check, AlertCircle,
  Play, Trash2, ListChecks, FileText,
} from "lucide-react";

type ResolvedModel = { provider: string | null; model: string | null; source: string };
type StageRun = {
  id: string; case_id: string; finding_id: string | null; finding_title: string | null;
  stage: string; status: string; ai_provider: string | null; ai_model: string | null;
  resolved: ResolvedModel | null; session_id: string | null; has_output: boolean;
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

const STAGES = ["impact", "source", "poc", "remediation", "report"] as const;
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
          <FindingsCard findings={c.findings} scanId={c.scan_id} />
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

function FindingsCard({ findings, scanId }: { findings: FindingRow[]; scanId: string | null }) {
  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Findings ({findings.length})</CardTitle>
        {scanId ? (
          <Link to={`/scans/${scanId}`} className="text-xs text-primary hover:underline">open scan</Link>
        ) : null}
      </CardHeader>
      <CardBody>
        {findings.length === 0 ? (
          <p className="text-sm text-fgmuted">
            No findings yet. They arrive from an import, an extracted report, or the pipeline.
          </p>
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
  const isReport = stage === "report";

  const queue = useMutation({
    mutationFn: () => api<StageRun>(`/cases/${c.id}/stages`, {
      method: "POST",
      body: { stage, finding_id: isReport ? null : (findingId || null) },
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
          {!isReport ? (
            <div className="min-w-[12rem]">
              <Label htmlFor="fi">Finding</Label>
              <Select id="fi" value={findingId} onChange={(e) => setFindingId(e.target.value)} className="text-sm">
                <option value="">Select a finding…</option>
                {c.findings.map((f) => <option key={f.id} value={f.id}>{f.title || f.id.slice(0, 8)}</option>)}
              </Select>
            </div>
          ) : null}
          <Button type="button" size="sm" onClick={() => queue.mutate()}
                  disabled={queue.isPending || (!isReport && !findingId)}>
            <Play size={13} /> Queue
          </Button>
          <span className="text-[11px] text-fgmuted">Queues the run; the executor drains it (not wired yet).</span>
        </div>
        {queue.isError ? (
          <p className="text-xs text-danger">{(queue.error as any)?.detail || "Couldn't queue that stage."}</p>
        ) : null}

        {runs.length === 0 ? (
          <p className="text-sm text-fgmuted">No stage runs queued.</p>
        ) : (
          <Table>
            <THead><TR><TH>Stage</TH><TH>Target</TH><TH>Status</TH><TH>Model</TH></TR></THead>
            <tbody>
              {runs.map((r) => (
                <TR key={r.id}>
                  <TD className="font-medium">{r.stage}</TD>
                  <TD className="text-xs text-fgmuted">
                    {r.finding_id ? (r.finding_title || r.finding_id.slice(0, 8)) : "case-level"}
                  </TD>
                  <TD><Badge tone={STAGE_STATUS_TONE[r.status] ?? "default"}>{r.status}</Badge></TD>
                  <TD className="text-xs text-fgmuted">
                    {r.resolved ? (
                      <span title={`resolved via ${r.resolved.source}`}>
                        {r.resolved.provider ?? "default"}{r.resolved.model ? ` / ${r.resolved.model}` : ""}
                        <span className="opacity-60"> ({r.resolved.source})</span>
                      </span>
                    ) : "—"}
                  </TD>
                </TR>
              ))}
            </tbody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}
