import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { api, getToken } from "@/lib/api";
import { downloadFile } from "@/lib/download";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge, SeverityChip } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import { ArrowLeft, ArrowUp, ArrowDown, ArrowUpDown, Check, Copy, FileText, Link2, Paperclip, Pencil, Plus, Save, Trash2, Upload } from "lucide-react";

const SEVERITIES = ["critical", "high", "medium", "low", "info", "unknown"] as const;

// Fields the user can edit from this page. Used to compute "dirty" so the
// Save button only appears when there's actually something to save.
// Counts (findings/tp/fp/sbp/duplicates/untriaged) and highest_severity are
// derived from the Findings rows below — not editable here.
const EDITABLE_KEYS = [
  "product", "scan_target", "harness_used", "scan_by",
  "results_file", "spreadsheet_link", "triaged_by",
  "notes", "project_id",
] as const;

// Severity ranking — lower index = more severe. Used to derive highest.
const SEVERITY_RANK: Record<string, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4, unknown: 5,
};

type Finding = {
  id: string; title: string; severity: string; status: string;
  cwe: string; cve: string; created_at: string; triaged_by: string;
  ai_verdict: "open" | "true_positive" | "false_positive";
  ai_rationale: string;
  tags: string[];
};

type SortKey = "severity" | "title" | "status" | "ai_verdict" | "triaged_by" | "created_at";
// Severity sorts by rank (critical first), not alphabetically; the rest by text/date.
const SORT_ACCESSORS: Record<SortKey, (f: Finding) => number | string> = {
  severity:   (f) => SEVERITY_RANK[(f.severity || "").toLowerCase()] ?? SEVERITY_RANK.unknown,
  title:      (f) => (f.title || "").toLowerCase(),
  status:     (f) => (f.status || "").toLowerCase(),
  ai_verdict: (f) => (f.ai_verdict || "").toLowerCase(),
  triaged_by: (f) => (f.triaged_by || "").toLowerCase(),
  created_at: (f) => f.created_at || "",
};
type Run = {
  id: string; date: string | null; day: string; run: string; box: string;
  product: string; harness: string; prompt: string; results: string;
  poc: string; comment: string; complete: boolean;
};
type Scan = {
  id: string; user_id: string; state: string;
  product: string; title: string; scan_target: string; harness_used: string; scan_by: string;
  results_file: string; spreadsheet_link: string; triaged_by: string;
  findings: number; tp: number; fp: number; sbp: number;
  duplicates: number; untriaged: number;
  highest_severity: string; notes: string;
  source_report_id: string | null;
  source_session_id: string | null;
  project_id: string | null;
  owner_email: string | null;
  confirmed_by: string | null;
  confirmed_by_email: string | null;
  confirmed_at: string | null;
  created_at: string; updated_at: string;
  runs: Run[];
  findings_list?: Finding[]; // alias below
};
type ScanDetail = Scan & { findings: number; findings_list: Finding[] };

type Attachment = {
  id: string; filename: string; content_type: string;
  size_bytes: number; created_at: string;
};

type Project = { id: string; name: string };

export function ScanDetail() {
  const { scan_id = "" } = useParams();
  const qc = useQueryClient();

  // The /scans/{id} response uses `findings` as the *list* and a separate
  // count field — the schema is awkward. We grab both.
  const scan = useQuery({
    queryKey: ["scan", scan_id],
    queryFn: async () => {
      const r = await api<any>(`/scans/${scan_id}`);
      // Server emits `findings` as the int count + `findings_list` as the
      // array of Finding rows (see VulnScanDetail in schemas.py).
      const findings_list: Finding[] = Array.isArray(r.findings_list)
        ? r.findings_list : [];
      return { ...r, findings_list } as ScanDetail;
    },
  });

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const attachments = useQuery({
    queryKey: ["scan-attachments", scan_id],
    queryFn: () => api<Attachment[]>(`/attachments?scan_id=${encodeURIComponent(scan_id)}`),
    enabled: !!scan_id,
  });

  // Editable form state mirrors the scan record; reset whenever scan reloads.
  // If `scan_by` is empty, pre-fill with the scan owner's email so it doesn't
  // start blank for every freshly-extracted scan.
  const [form, setForm] = useState<Partial<Scan>>({});
  useEffect(() => {
    if (scan.data) {
      const next: Partial<Scan> = { ...scan.data };
      if (!next.scan_by && scan.data.owner_email) {
        next.scan_by = scan.data.owner_email;
      }
      setForm(next);
    }
  }, [scan.data]);
  const setF = <K extends keyof Scan>(k: K, v: Scan[K]) =>
    setForm((f) => ({ ...f, [k]: v }));

  const [savedAt, setSavedAt] = useState(0);
  useEffect(() => {
    if (!savedAt) return;
    const t = setTimeout(() => setSavedAt(0), 2400);
    return () => clearTimeout(t);
  }, [savedAt]);

  const save = useMutation({
    mutationFn: () =>
      api(`/scans/${scan_id}`, {
        method: "PATCH",
        body: {
          product: form.product, scan_target: form.scan_target,
          harness_used: form.harness_used, scan_by: form.scan_by,
          results_file: form.results_file, spreadsheet_link: form.spreadsheet_link,
          triaged_by: form.triaged_by,
          notes: form.notes,
          project_id: form.project_id || null,
          // counts + highest_severity are derived from findings; not sent.
        },
      }),
    onSuccess: () => {
      setSavedAt(Date.now());
      qc.invalidateQueries({ queryKey: ["scan", scan_id] });
      qc.invalidateQueries({ queryKey: ["scans"] });
    },
  });
  const justSaved = Date.now() - savedAt < 2400;

  const nav = useNavigate();
  const delScan = useMutation({
    mutationFn: () => api(`/scans/${scan_id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scans"] });
      nav("/scans");
    },
  });

  // Inline rename of the scan's display title.
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const renameScan = useMutation({
    mutationFn: (title: string) =>
      api(`/scans/${scan_id}`, { method: "PATCH", body: { title } }),
    onSuccess: () => {
      setEditingTitle(false);
      qc.invalidateQueries({ queryKey: ["scan", scan_id] });
      qc.invalidateQueries({ queryKey: ["scans"] });
    },
  });

  // Hooks must run in the same order every render — keep the useMemo above
  // any early returns. `findings` is computed defensively in case the query
  // hasn't resolved yet.
  const findings: Finding[] = (scan.data?.findings_list as Finding[]) || [];
  const derived = useMemo(() => {
    const counts = { tp: 0, fp: 0, sbp: 0, duplicates: 0, untriaged: 0, fixed: 0 };
    let highest = "unknown";
    for (const fd of findings) {
      switch (fd.status) {
        case "true_positive":  counts.tp++; break;
        case "false_positive": counts.fp++; break;
        case "sbp":            counts.sbp++; break;
        case "duplicate":      counts.duplicates++; break;
        case "open":           counts.untriaged++; break;
        case "fixed":          counts.fixed++; break;
      }
      const r = SEVERITY_RANK[fd.severity] ?? SEVERITY_RANK.unknown;
      if (r < (SEVERITY_RANK[highest] ?? SEVERITY_RANK.unknown)) highest = fd.severity;
    }
    return { ...counts, findings: findings.length, highest_severity: highest };
  }, [findings]);

  // Click a column header to sort by it; click again to flip direction.
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "severity", dir: "asc",   // default: most severe first
  });
  const sortedFindings = useMemo(() => {
    const acc = SORT_ACCESSORS[sort.key];
    const arr = [...findings];
    arr.sort((a, b) => {
      const va = acc(a), vb = acc(b);
      const cmp = typeof va === "number" && typeof vb === "number"
        ? va - vb
        : String(va).localeCompare(String(vb));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [findings, sort]);
  const toggleSort = (key: SortKey) =>
    setSort((s) =>
      s.key === key
        ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key, dir: key === "created_at" ? "desc" : "asc" }); // dates default newest-first
  const sortTH = (label: string, key: SortKey, className = "") => {
    const active = sort.key === key;
    return (
      <TH className={`${className} cursor-pointer select-none`} onClick={() => toggleSort(key)}>
        <span className="inline-flex items-center gap-1">
          {label}
          {active
            ? (sort.dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />)
            : <ArrowUpDown size={11} className="opacity-30" />}
        </span>
      </TH>
    );
  };

  if (scan.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (scan.isError || !scan.data) return <div className="text-sm text-danger">Scan not found.</div>;

  const s = scan.data;
  const f = form as Scan;

  // Dirty if any editable field differs from the server-side value.
  // scan_by has a UI-level default (owner email) when the DB value is empty —
  // treat that default as the baseline so we don't show "Save" on every load.
  const isDirty = EDITABLE_KEYS.some((k) => {
    const a = (f as any)[k];
    let b = (s as any)[k];
    if (k === "project_id") return (a || null) !== (b || null);
    if (k === "scan_by" && !b && s.owner_email) b = s.owner_email;
    return (a ?? "") !== (b ?? "");
  });

  return (
    <div className="space-y-3">
      <div>
        <Link to="/scans" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> Back to scans
        </Link>
        <div className="mt-1.5 flex items-baseline justify-between gap-3 flex-wrap">
          {editingTitle ? (
            <form className="flex items-center gap-2"
                  onSubmit={(e) => { e.preventDefault(); renameScan.mutate(titleDraft.trim()); }}>
              <Input autoFocus value={titleDraft}
                     onChange={(e) => setTitleDraft(e.target.value)}
                     placeholder={s.product || "Scan title"}
                     className="text-lg font-semibold h-9 w-[28rem] max-w-full" />
              <Button type="submit" size="sm" disabled={renameScan.isPending}>
                <Save size={13} /> Save
              </Button>
              <Button type="button" variant="ghost" size="sm" onClick={() => setEditingTitle(false)}>
                Cancel
              </Button>
            </form>
          ) : (
            <h1 className="text-xl font-semibold inline-flex items-center gap-2">
              {s.title || s.product || <span className="text-fgmuted italic">(untitled scan)</span>}
              <button type="button" title="Rename scan"
                      className="text-fgmuted hover:text-primary"
                      onClick={() => { setTitleDraft(s.title || s.product || ""); setEditingTitle(true); }}>
                <Pencil size={15} />
              </button>
            </h1>
          )}
          <div className="flex items-center gap-2">
            <SeverityChip value={derived.highest_severity} />
            <Badge tone={s.state === "draft" ? "warning" : "success"}>{s.state}</Badge>
            <Button variant="secondary" size="sm" disabled={delScan.isPending}
                    onClick={() => {
                      if (confirm(`Delete this scan and all ${s.findings || 0} findings? This cannot be undone.`))
                        delScan.mutate();
                    }}>
              <Trash2 size={12} /> Delete
            </Button>
          </div>
        </div>
        <p className="text-xs text-fgmuted mt-1">
          {s.scan_target || "(no target)"} · created {fmt(s.created_at)}
          {s.source_report_id ? (
            <>
              {" "}· <Link to={`/reports/${s.source_report_id}`} className="text-primary hover:underline">source report</Link>
            </>
          ) : null}
        </p>
      </div>

      {/* Agree-with-Claude banner: only when this scan was AI-extracted
          (has a source report or session) and is still a draft. */}
      {s.state === "draft" && (s.source_report_id || s.source_session_id) ? (
        <AgreeBanner s={s} />
      ) : null}
      {s.state === "confirmed" && s.confirmed_at ? (
        <div className="text-xs text-fgmuted px-1">
          <Check size={11} className="inline text-success mr-1" />
          Confirmed by {s.confirmed_by_email || "—"} on {fmt(s.confirmed_at)}.
        </div>
      ) : null}

      {/* Share for external triage */}
      <ShareCard scanId={s.id} />

      {/* Compact single card: project + severity + all summary fields */}
      <Card id="scan-summary">
        <CardHeader className="flex items-center justify-between py-2.5">
          <CardTitle>Scan summary</CardTitle>
          <div className="flex items-center gap-2 min-h-[1.5rem]">
            {save.isError ? (
              <span className="text-xs text-danger">Save failed.</span>
            ) : justSaved ? (
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
        <CardBody className="space-y-3 py-3">
          {/* Row 1: product (severity moved out — header chip already shows it) */}
          <div>
            <Label>Product</Label>
            <Select
              value={f.project_id ?? ""}
              onChange={(e) => setF("project_id", (e.target.value || null) as any)}
            >
              <option value="">— none —</option>
              {projects.data?.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>

          {/* Row 2: string fields, 3 across on wide screens */}
          <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
            <div><Label>Title <span className="text-fgmuted">(auto-detected)</span></Label>
              <Input value={f.product ?? ""} onChange={(e) => setF("product", e.target.value)} /></div>
            <div><Label>Scan target</Label>
              <Input value={f.scan_target ?? ""} onChange={(e) => setF("scan_target", e.target.value)} /></div>
            <div><Label>Harness used</Label>
              <Input value={f.harness_used ?? ""} onChange={(e) => setF("harness_used", e.target.value)} /></div>
            <div><Label>Scan by</Label>
              <Input value={f.scan_by ?? ""} onChange={(e) => setF("scan_by", e.target.value)} /></div>
            <div><Label>Triaged by</Label>
              <Input value={f.triaged_by ?? ""} onChange={(e) => setF("triaged_by", e.target.value)} /></div>
            <div><Label>Results file</Label>
              <Input value={f.results_file ?? ""} onChange={(e) => setF("results_file", e.target.value)} /></div>
            <div className="md:col-span-3"><Label>Spreadsheet link</Label>
              <Input value={f.spreadsheet_link ?? ""} onChange={(e) => setF("spreadsheet_link", e.target.value)} /></div>
          </div>

          {/* Row 3: count stats (read-only — derived from findings below) */}
          <div>
            <div className="text-[10px] uppercase tracking-wider text-fgmuted/70 mb-1">
              counts <span className="normal-case">(from findings)</span>
            </div>
            <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
              {([
                ["Findings", derived.findings, "text-fg"],
                ["TP", derived.tp, "text-success"],
                ["FP", derived.fp, "text-danger"],
                ["SBP", derived.sbp, "text-warning"],
                ["Duplicates", derived.duplicates, "text-fgmuted"],
                ["Untriaged", derived.untriaged, "text-fg"],
              ] as const).map(([label, value, color]) => (
                <div key={label} className="bg-muted/40 border border-border rounded-md px-2 py-1.5">
                  <div className="text-[10px] uppercase tracking-wider text-fgmuted">{label}</div>
                  <div className={`text-lg font-semibold tabular-nums ${color}`}>{value}</div>
                </div>
              ))}
            </div>
          </div>

          {/* Row 4: notes */}
          <div>
            <Label>Notes</Label>
            <Textarea rows={2} value={f.notes ?? ""}
                      onChange={(e) => setF("notes", e.target.value)} />
          </div>
        </CardBody>
      </Card>

      {/* Harness (only when the scan came from a Claude session) */}
      {s.source_session_id ? <HarnessCard sessionId={s.source_session_id} /> : null}

      {/* Findings */}
      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Findings ({findings.length})</CardTitle>
          {findings.length > 0 ? <RunAiOnAll scanId={s.id} findings={findings} /> : null}
        </CardHeader>
        {findings.length === 0 ? (
          <CardBody><p className="text-sm text-fgmuted">No findings yet.</p></CardBody>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="w-8"></TH>
                {sortTH("Severity", "severity", "w-20")}
                {sortTH("Title", "title")}
                {sortTH("Dev verdict", "status", "w-36")}
                {sortTH("AI verdict", "ai_verdict", "w-32")}
                <TH className="w-40">Tags</TH>
                {sortTH("Triaged by", "triaged_by", "w-36")}
                {sortTH("When", "created_at", "w-24")}
              </TR>
            </THead>
            <tbody>
              {sortedFindings.map((fd) => (
                <FindingRow key={fd.id} scanId={s.id} fd={fd} />
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {/* Source reports */}
      <SourceReports scanId={s.id} projectId={s.project_id} />

      {/* Attachments */}
      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <Paperclip size={14} /> POC files & attachments ({attachments.data?.length ?? 0})
          </CardTitle>
        </CardHeader>
        <CardBody>
          {!attachments.data || attachments.data.length === 0 ? (
            <p className="text-sm text-fgmuted">
              No attachments yet. The agent ships anything Claude writes under a <code className="font-mono">poc/</code> directory automatically.
            </p>
          ) : (
            <ul className="divide-y divide-border">
              {attachments.data.map((a) => (
                <li key={a.id} className="py-2 flex items-center justify-between gap-3">
                  <Link to={`/attachments/${a.id}`} className="text-primary hover:underline font-medium text-sm">
                    {a.filename}
                  </Link>
                  <div className="text-xs text-fgmuted flex items-center gap-3">
                    <span>{a.content_type}</span>
                    <span>{(a.size_bytes / 1024).toFixed(1)} KB</span>
                    <button type="button"
                            onClick={() => downloadFile(`/attachments/${a.id}/download`, a.filename)}
                            className="text-fgmuted hover:text-fg bg-transparent border-0 p-0 h-auto cursor-pointer text-xs">
                      download
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </CardBody>
      </Card>

      {/* Run log */}
      <Card>
        <CardHeader><CardTitle>Run log ({s.runs.length})</CardTitle></CardHeader>
        {s.runs.length === 0 ? (
          <CardBody><p className="text-sm text-fgmuted">No runs logged yet.</p></CardBody>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH className="w-28">Date</TH><TH className="w-20">Day</TH><TH>Run</TH><TH>Box</TH>
                <TH>Product</TH><TH>Harness</TH><TH>Results</TH><TH className="w-12">✓</TH><TH className="w-8"></TH>
              </TR>
            </THead>
            <tbody>
              {s.runs.map((r) => (
                <TR key={r.id}>
                  <TD className="text-xs">{r.date || ""}</TD>
                  <TD className="text-xs">{r.day}</TD>
                  <TD className="text-xs">{r.run}</TD>
                  <TD className="text-xs">{r.box}</TD>
                  <TD className="text-xs">{r.product}</TD>
                  <TD className="text-xs">{r.harness}</TD>
                  <TD className="text-xs text-fgmuted truncate max-w-[24ch]" title={r.results}>{r.results}</TD>
                  <TD className="text-xs">{r.complete ? "✓" : "—"}</TD>
                  <TD>
                    <DeleteRun scan_id={s.id} run_id={r.id} />
                  </TD>
                </TR>
              ))}
            </tbody>
          </Table>
        )}
        <CardBody>
          <AddRunForm scan_id={s.id} />
        </CardBody>
      </Card>
    </div>
  );
}

type SourceReportRow = {
  id: string; filename: string; title: string;
  source_tool: string;
  owner_email: string | null; created_at: string;
  summary: string | null;
};

function AgreeBanner({ s }: { s: Scan }) {
  const qc = useQueryClient();
  const agree = useMutation({
    mutationFn: () => api(`/scans/${s.id}/agree`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scan", s.id] }),
  });

  function scrollToSummary() {
    const el = document.getElementById("scan-summary");
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
      // Briefly flash the border so the user knows where they landed.
      el.classList.add("ring-2", "ring-primary/60");
      setTimeout(() => el.classList.remove("ring-2", "ring-primary/60"), 1500);
    }
  }

  return (
    <div className="rounded-md border border-primary/40 bg-primary/5 px-4 py-3 space-y-2">
      <div className="flex items-start gap-3">
        <div className="flex-1 min-w-0">
          <div className="text-sm font-medium">
            Claude extracted this scan from a {s.source_report_id ? "report" : "Claude session"}.
            Review and agree, or correct anything that's wrong.
          </div>
          <dl className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1 text-xs">
            <BannerField label="Title"     value={s.product} />
            <BannerField label="Target"    value={s.scan_target} />
            <BannerField label="Harness"   value={s.harness_used} />
            <BannerField label="Scan by"   value={s.scan_by} />
          </dl>
          {s.notes ? (
            <div className="mt-2 text-xs text-fgmuted">
              <span className="uppercase tracking-wider text-[10px] mr-1">Notes:</span>
              <span className="italic">{s.notes}</span>
            </div>
          ) : null}
        </div>
        <div className="flex flex-col gap-2 shrink-0">
          <Button size="sm" onClick={() => agree.mutate()} disabled={agree.isPending}>
            <Check size={14} /> {agree.isPending ? "Saving…" : "Agree"}
          </Button>
          <Button size="sm" variant="secondary" onClick={scrollToSummary}>
            Make corrections
          </Button>
        </div>
      </div>
      {agree.isError ? (
        <p className="text-xs text-danger">Couldn't confirm — try again.</p>
      ) : null}
    </div>
  );
}

function BannerField({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2 min-w-0">
      <dt className="text-fgmuted uppercase tracking-wider text-[10px] shrink-0">{label}</dt>
      <dd className="truncate" title={value || "(unset)"}>
        {value ? <code className="text-fg">{value}</code> : <span className="text-fgmuted italic">(unset)</span>}
      </dd>
    </div>
  );
}

const TAG_VALUES = ["sbp", "ss", "vuln"] as const;
type TagValue = (typeof TAG_VALUES)[number];

type AIVerdictRun = {
  id: string; finding_id: string;
  ran_by: string | null; ran_by_email: string;
  verdict: "open" | "true_positive" | "false_positive";
  rationale: string; model: string; created_at: string;
};

function RunAiOnAll({ scanId, findings }: { scanId: string; findings: Finding[] }) {
  const qc = useQueryClient();
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });

  async function run() {
    if (!confirm(`Run AI verdict on ${findings.length} findings? Each one is one Claude call.`)) return;
    setRunning(true);
    setProgress({ done: 0, total: findings.length });
    // Sequential so we don't slam the API. Plenty fast for typical scan sizes.
    for (let i = 0; i < findings.length; i++) {
      try {
        await api(`/scans/${scanId}/findings/${findings[i].id}/ai_verdict`, { method: "POST" });
      } catch {
        // Continue past failures so a single bad finding doesn't stall the batch.
      }
      setProgress({ done: i + 1, total: findings.length });
    }
    setRunning(false);
    qc.invalidateQueries({ queryKey: ["scan", scanId] });
  }

  return (
    <Button size="sm" variant="secondary" onClick={run} disabled={running}>
      {running ? `AI ${progress.done}/${progress.total}…` : "Run AI on all"}
    </Button>
  );
}

function FindingRow({ scanId, fd }: { scanId: string; fd: Finding }) {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);

  const patch = useMutation({
    mutationFn: (body: Partial<{ status: string; tags: string[] }>) =>
      api<Finding>(`/scans/${scanId}/findings/${fd.id}`, {
        method: "PATCH",
        body,
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scan", scanId] }),
  });
  const runAi = useMutation({
    mutationFn: () =>
      api<Finding>(`/scans/${scanId}/findings/${fd.id}/ai_verdict`, { method: "POST" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scan", scanId] });
      qc.invalidateQueries({ queryKey: ["ai-log", fd.id] });
    },
  });

  function verdictBtn(target: "true_positive" | "false_positive" | "open", label: string) {
    const active = fd.status === target;
    const tone =
      active
        ? target === "true_positive" ? "bg-success text-white"
        : target === "false_positive" ? "bg-danger text-white"
        : "bg-muted text-fg"
        : "bg-transparent text-fgmuted hover:bg-muted hover:text-fg border border-border";
    return (
      <button
        type="button"
        onClick={() => !active && patch.mutate({ status: target })}
        disabled={patch.isPending}
        className={`px-2 py-0.5 rounded text-[11px] font-medium ${tone}`}
        title={`Mark as ${label}`}
      >
        {label}
      </button>
    );
  }

  function toggleTag(t: TagValue) {
    const cur = new Set(fd.tags || []);
    if (cur.has(t)) cur.delete(t);
    else cur.add(t);
    patch.mutate({ tags: Array.from(cur) });
  }

  return (
    <>
      <TR className="hover:bg-muted/40 align-top">
        <TD>
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-fgmuted hover:text-fg"
            title="Show AI log"
          >
            {expanded ? "▾" : "▸"}
          </button>
        </TD>
        <TD><SeverityChip value={fd.severity} /></TD>
        <TD>
          <Link to={`/scans/${scanId}/findings/${fd.id}`}
                className="text-primary hover:underline font-medium">
            {fd.title || <span className="text-fgmuted italic">(untitled)</span>}
          </Link>
          <div className="text-[11px] font-mono text-fgmuted mt-0.5">
            {fd.cwe}{fd.cve ? ` · ${fd.cve}` : ""}
          </div>
        </TD>

        {/* Dev verdict — 3 pill buttons */}
        <TD>
          <div className="flex items-center gap-1 flex-wrap">
            {verdictBtn("true_positive", "TP")}
            {verdictBtn("false_positive", "FP")}
            {verdictBtn("open", "Open")}
          </div>
        </TD>

        {/* AI verdict + Run AI */}
        <TD>
          <div className="flex flex-col gap-1">
            <Badge
              tone={
                fd.ai_verdict === "true_positive" ? "success"
                : fd.ai_verdict === "false_positive" ? "danger"
                : "muted"
              }
            >
              {fd.ai_verdict === "true_positive" ? "AI: TP"
                : fd.ai_verdict === "false_positive" ? "AI: FP"
                : "AI: —"}
            </Badge>
            <button
              type="button"
              onClick={() => runAi.mutate()}
              disabled={runAi.isPending}
              className="text-[11px] text-primary hover:underline text-left"
              title={fd.ai_rationale || "Ask Claude to TP/FP this finding"}
            >
              {runAi.isPending ? "Running…" : (fd.ai_verdict === "open" ? "Run AI" : "Re-run")}
            </button>
          </div>
        </TD>

        {/* Tag chips: SBP / SS / VULN */}
        <TD>
          <div className="flex items-center gap-1 flex-wrap">
            {TAG_VALUES.map((t) => {
              const on = (fd.tags || []).includes(t);
              return (
                <button
                  key={t}
                  type="button"
                  onClick={() => toggleTag(t)}
                  disabled={patch.isPending}
                  className={
                    "px-1.5 py-0.5 rounded text-[10px] font-medium uppercase " +
                    (on
                      ? "bg-warning/20 text-warning border border-warning/40"
                      : "bg-transparent text-fgmuted hover:bg-muted border border-border")
                  }
                  title={`Toggle ${t.toUpperCase()}`}
                >
                  {t}
                </button>
              );
            })}
          </div>
        </TD>

        <TD className="text-xs text-fgmuted">{fd.triaged_by || "—"}</TD>
        <TD className="text-xs text-fgmuted">{shortDate(fd.created_at)}</TD>
      </TR>
      {expanded ? <AILogRow scanId={scanId} findingId={fd.id} /> : null}
    </>
  );
}

function AILogRow({ scanId, findingId }: { scanId: string; findingId: string }) {
  const q = useQuery({
    queryKey: ["ai-log", findingId],
    queryFn: () => api<AIVerdictRun[]>(`/scans/${scanId}/findings/${findingId}/ai_verdict`),
  });
  return (
    <TR>
      <TD colSpan={8} className="bg-muted/20">
        <div className="py-2 px-2">
          {q.isLoading ? (
            <span className="text-xs text-fgmuted">Loading AI log…</span>
          ) : !q.data || q.data.length === 0 ? (
            <span className="text-xs text-fgmuted italic">
              No AI runs yet. Click "Run AI" above to get a verdict.
            </span>
          ) : (
            <div className="space-y-2">
              <div className="text-[11px] uppercase tracking-wider text-fgmuted">
                AI verdict log ({q.data.length} run{q.data.length === 1 ? "" : "s"})
              </div>
              {q.data.map((r) => (
                <div key={r.id} className="border-l-2 border-border pl-3">
                  <div className="flex items-center gap-2 text-xs text-fgmuted">
                    <Badge
                      tone={
                        r.verdict === "true_positive" ? "success"
                        : r.verdict === "false_positive" ? "danger"
                        : "muted"
                      }
                    >
                      {r.verdict === "true_positive" ? "TP"
                        : r.verdict === "false_positive" ? "FP"
                        : "Open"}
                    </Badge>
                    <span>{new Date(r.created_at).toLocaleString()}</span>
                    {r.ran_by_email ? <span>· by {r.ran_by_email}</span> : null}
                    {r.model ? <span>· {r.model}</span> : null}
                  </div>
                  {r.rationale ? (
                    <p className="text-xs text-fg mt-1 whitespace-pre-wrap">{r.rationale}</p>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
      </TD>
    </TR>
  );
}

type HarnessLite = { id: string; name: string; project_id: string | null };
type RunInfo = {
  session_id: string;
  harness_id: string | null;
  harness_name: string | null;
};

function HarnessCard({ sessionId }: { sessionId: string }) {
  const qc = useQueryClient();
  const run = useQuery({
    queryKey: ["run", sessionId],
    queryFn: () => api<RunInfo>(`/runs/${sessionId}`),
    enabled: !!sessionId,
  });
  const harnesses = useQuery({
    queryKey: ["harnesses"],
    queryFn: () => api<HarnessLite[]>("/harnesses"),
  });

  const [picked, setPicked] = useState<string>("");
  useEffect(() => {
    if (run.data) setPicked(run.data.harness_id || "");
  }, [run.data]);

  const save = useMutation({
    mutationFn: () =>
      api(`/runs/${sessionId}`, {
        method: "PATCH",
        body: { harness_id: picked || null },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["run", sessionId] }),
  });

  if (run.isLoading) {
    return (
      <Card><CardBody className="text-xs text-fgmuted">Loading harness…</CardBody></Card>
    );
  }
  const current = run.data;
  if (!current) return null;
  const dirty = (picked || null) !== (current.harness_id || null);

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Harness</CardTitle>
        {dirty ? (
          <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save"}
          </Button>
        ) : save.isSuccess ? (
          <span className="text-xs text-success inline-flex items-center gap-1"><Check size={12}/>saved</span>
        ) : null}
      </CardHeader>
      <CardBody className="space-y-2">
        <p className="text-xs text-fgmuted">
          The folder Claude was run inside for this session. Set one so future scans/reports
          can reference the same prompts and tools.
        </p>
        <Select value={picked} onChange={(e) => setPicked(e.target.value)}>
          <option value="">— none —</option>
          {(harnesses.data || []).map((h) => (
            <option key={h.id} value={h.id}>{h.name}</option>
          ))}
        </Select>
        {current.harness_id ? (
          <p className="text-[11px] text-fgmuted">
            Currently linked to{" "}
            <Link to={`/harnesses/${current.harness_id}`} className="text-primary hover:underline">
              {current.harness_name || current.harness_id.slice(0, 8) + "…"}
            </Link>.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}

function SourceReports({ scanId, projectId }: { scanId: string; projectId: string | null }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["scan-reports", scanId],
    queryFn: () => api<SourceReportRow[]>(`/reports?scan_id=${encodeURIComponent(scanId)}&limit=200`),
    enabled: !!scanId,
  });
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [parseAfter, setParseAfter] = useState(true);
  const [status, setStatus] = useState("");
  const upload = useMutation({
    mutationFn: async () => {
      const tok = getToken();
      const auth = tok ? { Authorization: `Bearer ${tok}` } : undefined;
      setStatus("Uploading…");
      const fd = new FormData();
      fd.append("file", file!, file!.name);
      fd.append("scan_id", scanId);
      if (projectId) fd.append("project_id", projectId);
      const up = await fetch("/reports/upload", { method: "POST", headers: auth, body: fd });
      if (!up.ok) throw new Error((await up.text()) || "Upload failed");
      const rep = await up.json();
      if (parseAfter) {
        setStatus("Parsing into findings…");
        const pr = await fetch(`/reports/${rep.id}/extract`, { method: "POST", headers: auth });
        if (!pr.ok) throw new Error("Uploaded, but parsing failed: " + (await pr.text()));
      }
      return rep;
    },
    onSuccess: () => {
      setOpen(false); setFile(null); setStatus("");
      qc.invalidateQueries({ queryKey: ["scan", scanId] });
      qc.invalidateQueries({ queryKey: ["scans"] });
    },
    onError: () => setStatus(""),
    onSettled: () => qc.invalidateQueries({ queryKey: ["scan-reports", scanId] }),
  });
  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle>Source reports ({q.data?.length ?? 0})</CardTitle>
        <Button size="sm" onClick={() => setOpen((v) => !v)}>
          <Upload size={14} /> Upload report
        </Button>
      </CardHeader>
      {open ? (
        <CardBody className="border-b border-border space-y-2">
          <input type="file" onChange={(e) => setFile(e.target.files?.[0] || null)}
                 className="block w-full text-sm text-fgmuted file:mr-3 file:rounded file:border-0 file:bg-muted file:px-3 file:py-1.5 file:text-fg" />
          <label className="text-sm flex items-center gap-2">
            <input type="checkbox" checked={parseAfter} onChange={(e) => setParseAfter(e.target.checked)} />
            Parse into findings after upload (text reports)
          </label>
          <div className="flex items-center gap-2">
            <Button onClick={() => upload.mutate()} disabled={!file || upload.isPending}>
              {upload.isPending ? (status || "Working…") : "Upload to this scan"}
            </Button>
            {upload.isError ? <span className="text-xs text-danger">{(upload.error as Error).message}</span> : null}
          </div>
        </CardBody>
      ) : null}
      {q.isLoading ? (
        <CardBody><p className="text-sm text-fgmuted">Loading…</p></CardBody>
      ) : !q.data || q.data.length === 0 ? (
        <CardBody>
          <p className="text-sm text-fgmuted">
            No reports linked to this scan yet. Open a report and use the
            "Linked scan" picker to attach it.
          </p>
        </CardBody>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Title</TH>
              <TH>Tool</TH>
              <TH>By</TH>
              <TH>When</TH>
              <TH className="w-1/3">Summary</TH>
            </TR>
          </THead>
          <tbody>
            {q.data.map((r) => (
              <TR key={r.id} className="hover:bg-muted/40">
                <TD>
                  <Link to={`/reports/${r.id}`} className="text-primary hover:underline font-medium">
                    {r.title || <span className="text-fgmuted italic">(untitled)</span>}
                  </Link>
                  <div className="text-[11px] text-fgmuted font-mono truncate max-w-[30ch]" title={r.filename}>
                    {r.filename}
                  </div>
                </TD>
                <TD className="text-xs"><span className="text-fgmuted">{r.source_tool}</span></TD>
                <TD className="text-fgmuted text-xs">{r.owner_email || "—"}</TD>
                <TD className="text-fgmuted text-xs whitespace-nowrap">
                  {new Date(r.created_at).toLocaleString(undefined,{dateStyle:"medium",timeStyle:"short"})}
                </TD>
                <TD className="text-xs text-fgmuted">
                  <div className="max-w-[50ch] truncate" title={r.summary || ""}>
                    {r.summary || "—"}
                  </div>
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

type ShareLink = {
  id: string; scan_id: string; token_prefix: string; label: string;
  created_by_email: string | null; allow_poc: boolean;
  expires_at: string | null; revoked_at: string | null;
  last_used_at: string | null; created_at: string; status: string;
  token?: string | null; url?: string | null;
};

function ShareCard({ scanId }: { scanId: string }) {
  const qc = useQueryClient();
  const links = useQuery({
    queryKey: ["share-links", scanId],
    queryFn: () => api<ShareLink[]>(`/scans/${scanId}/share`),
  });

  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState("30");
  const [allowPoc, setAllowPoc] = useState(false);
  const [fresh, setFresh] = useState<ShareLink | null>(null);
  const [copied, setCopied] = useState(false);
  const urlRef = useRef<HTMLInputElement>(null);

  const create = useMutation({
    mutationFn: () =>
      api<ShareLink>(`/scans/${scanId}/share`, {
        method: "POST",
        body: {
          label,
          expires_in_days: days === "" ? null : Math.max(1, parseInt(days, 10) || 30),
          allow_poc: allowPoc,
        },
      }),
    onSuccess: (link) => {
      setFresh(link);
      setCopied(false);
      setOpen(false);
      setLabel(""); setDays("30"); setAllowPoc(false);
      qc.invalidateQueries({ queryKey: ["share-links", scanId] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api(`/scans/${scanId}/share/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["share-links", scanId] }),
  });

  async function copyUrl() {
    if (!fresh?.url) return;
    // navigator.clipboard requires a secure context (https or localhost).
    // Fall back to selecting the visible input + execCommand for plain http.
    let ok = false;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(fresh.url);
        ok = true;
      } catch { /* fall through */ }
    }
    if (!ok && urlRef.current) {
      urlRef.current.focus();
      urlRef.current.select();
      urlRef.current.setSelectionRange(0, fresh.url.length);
      try { ok = document.execCommand("copy"); } catch { /* ignore */ }
    }
    setCopied(ok);
    // Leave the text selected so the user can Ctrl+C manually if both
    // paths fail (locked-down browsers / kiosk mode).
  }

  const active = (links.data || []).filter((l) => l.status === "active");

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Link2 size={14} /> Share for triage
        </CardTitle>
        {!open ? (
          <Button size="sm" variant="secondary" onClick={() => { setOpen(true); setFresh(null); }}>
            <Plus size={14} /> New link
          </Button>
        ) : null}
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-xs text-fgmuted">
          Generate a link for product devs/PMs (no login required) to mark each
          finding as TP/FP and leave notes. Proof-of-concept and POC attachments
          are hidden from share-link viewers unless you enable it per link.
        </p>

        {fresh?.url ? (
          <div className="rounded-md border border-primary/40 bg-primary/5 px-3 py-2 space-y-2">
            <div className="text-xs font-medium">
              Link created — copy it now. It won't be shown again.
            </div>
            <div className="flex items-center gap-2">
              <Input ref={urlRef} readOnly value={fresh.url}
                     onFocus={(e) => e.currentTarget.select()}
                     className="font-mono text-xs" />
              <Button size="sm" type="button" onClick={copyUrl}>
                {copied ? <Check size={14} /> : <Copy size={14} />}
                {copied ? "Copied" : "Copy"}
              </Button>
            </div>
            <div className="text-[11px] text-fgmuted">
              {fresh.label ? <>"{fresh.label}" · </> : null}
              expires {fresh.expires_at ? new Date(fresh.expires_at).toLocaleDateString() : "never"}
              {fresh.allow_poc ? " · PoC visible" : " · PoC hidden"}
            </div>
          </div>
        ) : null}

        {open ? (
          <form
            onSubmit={(e) => { e.preventDefault(); create.mutate(); }}
            className="grid grid-cols-1 md:grid-cols-3 gap-3 border border-border rounded-md p-3"
          >
            <div className="md:col-span-3">
              <Label>Label <span className="text-fgmuted">(who you're sending it to)</span></Label>
              <Input value={label} onChange={(e) => setLabel(e.target.value)}
                     placeholder="e.g. Acme Gateway dev team" />
            </div>
            <div>
              <Label>Expires in (days)</Label>
              <Input type="number" min={1} value={days}
                     onChange={(e) => setDays(e.target.value)} />
            </div>
            <div className="md:col-span-2 flex items-end">
              <label className="inline-flex items-center gap-2 text-sm">
                <input type="checkbox" checked={allowPoc}
                       onChange={(e) => setAllowPoc(e.target.checked)} />
                Allow viewing proof-of-concept
              </label>
            </div>
            <div className="md:col-span-3 flex gap-2">
              <Button type="submit" disabled={create.isPending}>
                {create.isPending ? "Creating…" : "Create link"}
              </Button>
              <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              {create.isError ? (
                <span className="text-xs text-danger self-center">Failed.</span>
              ) : null}
            </div>
          </form>
        ) : null}

        {links.isLoading ? (
          <p className="text-xs text-fgmuted">Loading…</p>
        ) : (links.data || []).length === 0 ? (
          <p className="text-xs text-fgmuted italic">No share links yet.</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Label</TH><TH className="w-24">Prefix</TH>
                <TH className="w-24">Status</TH><TH className="w-28">Expires</TH>
                <TH className="w-28">Last used</TH><TH>By</TH>
                <TH className="w-20">PoC</TH><TH className="w-8"></TH>
              </TR>
            </THead>
            <tbody>
              {links.data!.map((l) => (
                <TR key={l.id}>
                  <TD className="text-xs">{l.label || <span className="text-fgmuted italic">—</span>}</TD>
                  <TD className="text-xs font-mono">{l.token_prefix}…</TD>
                  <TD className="text-xs">
                    <Badge tone={l.status === "active" ? "success" : "muted"}>{l.status}</Badge>
                  </TD>
                  <TD className="text-xs text-fgmuted">
                    {l.expires_at ? new Date(l.expires_at).toLocaleDateString() : "never"}
                  </TD>
                  <TD className="text-xs text-fgmuted">
                    {l.last_used_at ? new Date(l.last_used_at).toLocaleDateString() : "—"}
                  </TD>
                  <TD className="text-xs text-fgmuted">{l.created_by_email || "—"}</TD>
                  <TD className="text-xs text-fgmuted">{l.allow_poc ? "shown" : "hidden"}</TD>
                  <TD>
                    {l.status === "active" ? (
                      <Button variant="ghost" size="sm" disabled={revoke.isPending}
                              onClick={() => { if (confirm("Revoke this link?")) revoke.mutate(l.id); }}>
                        <Trash2 size={12} />
                      </Button>
                    ) : null}
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

function AddRunForm({ scan_id }: { scan_id: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [r, setR] = useState({
    date: "", day: "", run: "", box: "", product: "", harness: "",
    prompt: "", results: "", poc: "", comment: "", complete: false,
  });
  const m = useMutation({
    mutationFn: () => api(`/scans/${scan_id}/runs`, {
      method: "POST",
      body: { ...r, date: r.date || null },
    }),
    onSuccess: () => {
      setOpen(false);
      setR({ date: "", day: "", run: "", box: "", product: "", harness: "",
             prompt: "", results: "", poc: "", comment: "", complete: false });
      qc.invalidateQueries({ queryKey: ["scan", scan_id] });
    },
  });
  function submit(e: FormEvent) { e.preventDefault(); m.mutate(); }
  if (!open) return (
    <Button variant="secondary" onClick={() => setOpen(true)}>
      <Plus size={14} /> Add a run
    </Button>
  );
  return (
    <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-2">
      <div><Label>Date</Label><Input type="date" value={r.date} onChange={(e) => setR({...r, date: e.target.value})} /></div>
      <div><Label>Day</Label><Input value={r.day} onChange={(e) => setR({...r, day: e.target.value})} placeholder="Day 1, Mon, ..." /></div>
      <div><Label>Run</Label><Input value={r.run} onChange={(e) => setR({...r, run: e.target.value})} placeholder="run-001" /></div>
      <div><Label>Box</Label><Input value={r.box} onChange={(e) => setR({...r, box: e.target.value})} /></div>
      <div><Label>Product</Label><Input value={r.product} onChange={(e) => setR({...r, product: e.target.value})} /></div>
      <div><Label>Harness</Label><Input value={r.harness} onChange={(e) => setR({...r, harness: e.target.value})} /></div>
      <div className="md:col-span-3"><Label>Prompt</Label><Textarea rows={2} value={r.prompt} onChange={(e) => setR({...r, prompt: e.target.value})} /></div>
      <div className="md:col-span-3"><Label>Results</Label><Textarea rows={2} value={r.results} onChange={(e) => setR({...r, results: e.target.value})} /></div>
      <div className="md:col-span-3"><Label>POC</Label><Textarea rows={2} value={r.poc} onChange={(e) => setR({...r, poc: e.target.value})} /></div>
      <div><Label>Comment</Label><Input value={r.comment} onChange={(e) => setR({...r, comment: e.target.value})} /></div>
      <div className="flex items-end">
        <label className="inline-flex items-center gap-2 text-sm">
          <input type="checkbox" checked={r.complete} onChange={(e) => setR({...r, complete: e.target.checked})} />
          Complete
        </label>
      </div>
      <div className="flex items-end gap-2">
        <Button type="submit" disabled={m.isPending}>{m.isPending ? "Adding…" : "Add"}</Button>
        <Button type="button" variant="secondary" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
    </form>
  );
}

function DeleteRun({ scan_id, run_id }: { scan_id: string; run_id: string }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => api(`/scans/${scan_id}/runs/${run_id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scan", scan_id] }),
  });
  return (
    <Button variant="ghost" size="sm" onClick={() => {
      if (confirm("Delete this run row?")) m.mutate();
    }} disabled={m.isPending}>
      <Trash2 size={12} />
    </Button>
  );
}

function numOr0(v: any): number {
  const n = parseInt(String(v ?? 0), 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

function fmt(iso: string) {
  return new Date(iso).toLocaleString(undefined,
    { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function shortDate(iso: string) {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "2-digit" });
}
