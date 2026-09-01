import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSanitize from "rehype-sanitize";
import { api } from "@/lib/api";
import { downloadFile } from "@/lib/download";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Select } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { ArrowLeft, Check, Download, FileText, Pencil, Save, Sparkles, Trash2 } from "lucide-react";

type ReportDetail = {
  id: string; user_id: string; filename: string; title: string;
  original_path: string | null;
  source_tool: string; sha256: string; size_bytes: number;
  summary: string | null; created_at: string;
  session_id: string | null;
  project_id: string | null;
  effective_project_id: string | null;
  agent_hostname: string | null;
  owner_email: string | null;
  derived_scan_id: string | null;
  derived_scan_product: string | null;
  content: string;
};
type Project = { id: string; name: string };
type ScanLite = {
  id: string; product: string; state: string;
  project_id: string | null;
};

export function ReportView() {
  const { report_id = "" } = useParams();
  const qc = useQueryClient();

  const report = useQuery({
    queryKey: ["report", report_id],
    queryFn: () => api<ReportDetail>(`/reports/${report_id}`),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const [projectId, setProjectId] = useState<string>("");
  const [title, setTitle] = useState<string>("");
  const [editingTitle, setEditingTitle] = useState(false);
  useEffect(() => {
    if (report.data) {
      setProjectId(report.data.project_id || "");
      setTitle(report.data.title || "");
    }
  }, [report.data]);

  const nav = useNavigate();
  const del = useMutation({
    mutationFn: () => api(`/reports/${report_id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["reports"] });
      nav("/");
    },
  });

  const setProject = useMutation({
    mutationFn: () =>
      api(`/reports/${report_id}`, {
        method: "PATCH",
        body: { project_id: projectId || null },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["report", report_id] }),
  });

  const [titleSavedAt, setTitleSavedAt] = useState(0);
  useEffect(() => {
    if (!titleSavedAt) return;
    const t = setTimeout(() => setTitleSavedAt(0), 2400);
    return () => clearTimeout(t);
  }, [titleSavedAt]);
  const titleJustSaved = Date.now() - titleSavedAt < 2400;

  const saveTitle = useMutation({
    mutationFn: () =>
      api(`/reports/${report_id}`, {
        method: "PATCH",
        body: { title },
      }),
    onSuccess: () => {
      setTitleSavedAt(Date.now());
      setEditingTitle(false);
      qc.invalidateQueries({ queryKey: ["report", report_id] });
      qc.invalidateQueries({ queryKey: ["reports"] });
    },
  });

  // Run the server AI over this report and write its findings onto the linked
  // scan (or a new draft). Used for uploads (e.g. a CSV) that are showing raw.
  const parse = useMutation({
    mutationFn: (replace: boolean) =>
      api<{ scan_id: string; findings: number; created_scan: boolean }>(
        `/reports/${report_id}/extract${replace ? "?replace=true" : ""}`,
        { method: "POST" },
      ),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["scans"] });
      qc.invalidateQueries({ queryKey: ["scan", res.scan_id] });
      qc.invalidateQueries({ queryKey: ["report", report_id] });
      nav(`/scans/${res.scan_id}`);
    },
  });
  async function doParse() {
    try {
      await parse.mutateAsync(false);
    } catch (e) {
      const err = e as { status?: number; detail?: unknown };
      if (err?.status === 409) {
        const msg = typeof err.detail === "string" ? err.detail : "This scan already has findings.";
        if (confirm(msg + "\n\nReplace them with a fresh parse?")) parse.mutate(true);
      }
    }
  }

  if (report.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (report.isError || !report.data) return <div className="text-sm text-danger">Report not found.</div>;
  const r = report.data;
  const canEditTitle = title !== (r.title || "");

  return (
    <div className="space-y-4">
      <div>
        <Link to="/" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> Back to reports
        </Link>
        <div className="mt-2 flex items-baseline justify-between gap-3 flex-wrap">
          {editingTitle ? (
            <form className="flex items-center gap-2 flex-1 min-w-0"
                  onSubmit={(e) => { e.preventDefault(); saveTitle.mutate(); }}>
              <FileText size={18} className="text-fgmuted shrink-0" />
              <Input autoFocus value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder="Report title"
                     className="text-lg font-semibold h-9 max-w-xl"
                     onKeyDown={(e) => { if (e.key === "Escape") { setTitle(r.title || ""); setEditingTitle(false); } }} />
              <Button type="submit" size="sm" disabled={saveTitle.isPending}>
                <Save size={13} /> Save
              </Button>
              <Button type="button" variant="ghost" size="sm"
                      onClick={() => { setTitle(r.title || ""); setEditingTitle(false); }}>Cancel</Button>
            </form>
          ) : (
            <h1 className="text-2xl font-semibold flex items-center gap-2 group">
              <FileText size={18} className="text-fgmuted" />
              {r.title || <span className="text-fgmuted italic">(untitled)</span>}
              <button type="button" title="Rename report" className="text-fgmuted hover:text-primary"
                      onClick={() => { setTitle(r.title || ""); setEditingTitle(true); }}>
                <Pencil size={15} />
              </button>
            </h1>
          )}
          <div className="flex items-center gap-2">
            <Button onClick={doParse} disabled={parse.isPending}>
              <Sparkles size={14} /> {parse.isPending ? "Parsing…" : "Parse into findings"}
            </Button>
            <Button variant="secondary"
                    onClick={() => downloadFile(`/ui/reports/${r.id}/download`, r.filename)}>
              <Download size={14} /> Download raw
            </Button>
            <Button variant="secondary" disabled={del.isPending}
                    onClick={() => {
                      if (confirm(`Delete report "${r.title || r.filename}"? This cannot be undone.`))
                        del.mutate();
                    }}>
              <Trash2 size={14} /> Delete
            </Button>
          </div>
        </div>
        <p className="text-xs text-fgmuted mt-1">
          <Badge tone="muted" className="mr-2">{r.source_tool}</Badge>
          <code className="text-fgmuted">{r.filename}</code>
          {" · "}{(r.size_bytes / 1024).toFixed(1)} KB · uploaded {fmt(r.created_at)}
          {r.owner_email ? <> · by <span className="text-fg">{r.owner_email}</span></> : null}
          {r.agent_hostname ? <> · from <span className="text-fg">{r.agent_hostname}</span></> : null}
        </p>
        {parse.isError && (parse.error as { status?: number })?.status !== 409 ? (
          <p className="text-xs text-danger mt-1">
            {typeof (parse.error as { detail?: unknown })?.detail === "string"
              ? String((parse.error as { detail?: unknown }).detail)
              : "Couldn't parse findings from this document."}
          </p>
        ) : null}
      </div>

      {saveTitle.isError ? (
        <p className="text-xs text-danger">Title save failed.</p>
      ) : null}

      {/* Derived scan banner */}
      {r.derived_scan_id ? (
        <div className="bg-warning/10 border border-warning/30 rounded-md px-4 py-2.5 flex items-center justify-between gap-3">
          <div className="text-sm">
            <strong>Auto-extracted draft:</strong> a VulnScan was generated from this report.
          </div>
          <Link to={`/scans/${r.derived_scan_id}`}>
            <Button variant="secondary" size="sm">Review draft</Button>
          </Link>
        </div>
      ) : null}

      {/* Project picker */}
      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Product</CardTitle>
          {setProject.isSuccess ? (
            <span className="text-xs text-success inline-flex items-center gap-1"><Check size={12}/>saved</span>
          ) : null}
        </CardHeader>
        <CardBody className="flex items-end gap-2">
          <div className="flex-1">
            <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">— none —</option>
              {projects.data?.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>
          <Button onClick={() => setProject.mutate()} disabled={setProject.isPending}>
            {setProject.isPending ? "Saving…" : "Save product"}
          </Button>
        </CardBody>
      </Card>

      {/* Linked scan picker */}
      <LinkedScanCard
        reportId={r.id}
        derivedScanId={r.derived_scan_id}
        derivedScanProduct={r.derived_scan_product}
        scopeProjectId={r.effective_project_id}
      />

      {/* AI summary */}
      {r.summary ? (
        <Card>
          <CardHeader><CardTitle>AI summary</CardTitle></CardHeader>
          <CardBody><p className="text-sm whitespace-pre-wrap">{r.summary}</p></CardBody>
        </Card>
      ) : null}

      {/* Markdown body */}
      <Card>
        <CardBody className="prose-irs">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={[rehypeSanitize]}
          >
            {r.content}
          </ReactMarkdown>
        </CardBody>
      </Card>
    </div>
  );
}

function fmt(iso: string) {
  return new Date(iso).toLocaleString(undefined,
    { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function LinkedScanCard({
  reportId, derivedScanId, derivedScanProduct, scopeProjectId,
}: {
  reportId: string;
  derivedScanId: string | null;
  derivedScanProduct: string | null;
  scopeProjectId: string | null;
}) {
  const qc = useQueryClient();
  // Candidate scans: prefer scans in the same project as the report so
  // we don't drown the dropdown. If there's no project, list all visible
  // scans (server scope already filters by viewer permissions).
  const scans = useQuery({
    queryKey: ["scans-for-picker", scopeProjectId || ""],
    queryFn: () =>
      api<ScanLite[]>(
        "/scans" + (scopeProjectId ? `?project_id=${encodeURIComponent(scopeProjectId)}` : "")
      ),
  });
  // Want the current (effective) value pre-selected so re-saving the same
  // value isn't dirty by default. The derived_scan_id is the effective one.
  const [scanId, setScanId] = useState<string>(derivedScanId || "");
  const [savedAt, setSavedAt] = useState(0);

  const save = useMutation({
    mutationFn: () =>
      api(`/reports/${reportId}`, {
        method: "PATCH",
        body: { scan_id: scanId || null },
      }),
    onSuccess: () => {
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(0), 2400);
      qc.invalidateQueries({ queryKey: ["report", reportId] });
      qc.invalidateQueries({ queryKey: ["reports"] });
      qc.invalidateQueries({ queryKey: ["scan-reports"] });
    },
  });
  const justSaved = Date.now() - savedAt < 2400;
  const isDirty = (scanId || null) !== (derivedScanId || null);

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Linked scan</CardTitle>
        <div className="flex items-center gap-2 min-h-[1.5rem]">
          {justSaved ? (
            <span className="text-xs text-success inline-flex items-center gap-1">
              <Check size={12} /> Saved!
            </span>
          ) : isDirty ? (
            <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardBody>
        <p className="text-xs text-fgmuted mb-2">
          Reports show up under their linked scan on the Scan page. Auto-link
          uses the scan that was extracted from this file or its run; you can
          override here.
        </p>
        <Select value={scanId} onChange={(e) => setScanId(e.target.value)}>
          <option value="">— no scan —</option>
          {scans.data?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.product || "(unnamed)"} · {s.state}
            </option>
          ))}
        </Select>
        {derivedScanId ? (
          <p className="text-[11px] text-fgmuted mt-2">
            Currently linked to{" "}
            <Link to={`/scans/${derivedScanId}`} className="text-primary hover:underline">
              {derivedScanProduct || derivedScanId.slice(0, 8) + "…"}
            </Link>.
          </p>
        ) : null}
        {save.isError ? (
          <p className="text-xs text-danger mt-2">Couldn't attach — you might not be a member of that scan's product.</p>
        ) : null}
      </CardBody>
    </Card>
  );
}
