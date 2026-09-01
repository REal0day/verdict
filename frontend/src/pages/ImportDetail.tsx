import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import {
  ArrowLeft, Check, Play, Sparkles, FileText, Paperclip, X,
  Folder, AlertTriangle, Send,
} from "lucide-react";

type StagedFile = { relpath: string; size: number; mime: string | null };
type ImportDetail = {
  id: string;
  user_id: string;
  status: "staged" | "planning" | "planned" | "applied" | "cancelled" | "error";
  label: string;
  file_count: number;
  total_bytes: number;
  created_at: string;
  planned_at: string | null;
  applied_at: string | null;
  error_message: string;
  files: StagedFile[];
  plan: Plan | null;
  plan_log: string;
};
type Project = { id: string; name: string; i_am_owner?: boolean; i_am_member?: boolean };
type PlanProject = {
  kind: "existing" | "new" | "none";
  existing_id?: string;
  name?: string;
  description?: string;
  rationale?: string;
};
type PlanScan = {
  local_id: string;
  product?: string;
  scan_target?: string;
  harness_used?: string;
  scan_by?: string;
  notes?: string;
  rationale?: string;
};
type PlanItem = {
  relpath: string;
  kind: "report" | "poc" | "skip";
  local_id?: string;
  title?: string;
  scan_local_id?: string;
  attach_to_local_id?: string;
  rationale?: string;
};
type PlanRun = {
  scan_local_id: string;
  day?: string; date?: string; run?: string; box?: string;
  product?: string; harness?: string; prompt?: string;
  results?: string; poc?: string; comment?: string; complete?: boolean;
};
type Plan = {
  project: PlanProject;
  scans?: PlanScan[];
  runs?: PlanRun[];
  items: PlanItem[];
};

export function ImportDetail() {
  const { imp_id = "" } = useParams();
  const nav = useNavigate();
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["import", imp_id],
    queryFn: () => api<ImportDetail>(`/imports/${imp_id}`),
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const [plan, setPlan] = useState<Plan | null>(null);
  // Lift the server plan into local edit state when it arrives / changes.
  useEffect(() => {
    if (q.data?.plan) setPlan(q.data.plan);
  }, [q.data?.plan]);

  const runPlan = useMutation({
    mutationFn: () => api<ImportDetail>(`/imports/${imp_id}/plan`, { method: "POST" }),
    onSuccess: (d) => {
      qc.setQueryData(["import", imp_id], d);
      if (d.plan) setPlan(d.plan);
    },
  });

  const confirm = useMutation({
    mutationFn: () =>
      api(`/imports/${imp_id}/confirm`, {
        method: "POST",
        body: { plan },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["import", imp_id] });
      qc.invalidateQueries({ queryKey: ["reports"] });
      qc.invalidateQueries({ queryKey: ["scans"] });
      qc.invalidateQueries({ queryKey: ["projects"] });
      // Hop to products (if a new one was created the user wants to see it).
      nav("/products");
    },
  });

  const cancel = useMutation({
    mutationFn: () => api(`/imports/${imp_id}`, { method: "DELETE" }),
    onSuccess: () => nav("/"),
  });

  if (q.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (q.isError || !q.data) return <div className="text-sm text-danger">Import not found.</div>;
  const imp = q.data;

  return (
    <>
      <PageHeader
        title={imp.label || "Folder import"}
        subtitle={
          <>
            {imp.file_count} files · {(imp.total_bytes / 1024).toFixed(1)} KB · status{" "}
            <Badge tone={statusTone(imp.status)}>{imp.status}</Badge>
          </>
        }
        action={
          <Link to="/" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
            <ArrowLeft size={12} /> Back to reports
          </Link>
        }
      />

      {imp.status === "applied" ? (
        <Card>
          <CardBody className="text-sm text-success flex items-center gap-2">
            <Check size={14} /> Import applied — your reports & scans are live.
          </CardBody>
        </Card>
      ) : null}

      {imp.error_message ? (
        <Card className="border-danger/40">
          <CardBody className="text-sm text-danger flex items-start gap-2">
            <AlertTriangle size={14} className="shrink-0 mt-0.5" />
            <span>{imp.error_message}</span>
          </CardBody>
        </Card>
      ) : null}

      {imp.status === "staged" || imp.status === "error" ? (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Sparkles size={14} className="text-primary" /> Ask Claude to plan this folder
            </CardTitle>
          </CardHeader>
          <CardBody className="space-y-3">
            <p className="text-sm text-fgmuted">
              Claude will open the files it considers relevant (reports, READMEs,
              notes), figure out which ones are reports vs. POCs, propose a
              product, and draft scans/runs. You'll be able to review and edit
              before anything is saved.
            </p>
            <Button onClick={() => runPlan.mutate()} disabled={runPlan.isPending}>
              <Play size={14} /> {runPlan.isPending ? "Claude is reading…" : "Generate plan"}
            </Button>
            {runPlan.isError ? (
              <p className="text-xs text-danger">
                {(runPlan.error as ApiError)?.detail
                  ? String((runPlan.error as ApiError).detail)
                  : "Plan failed."}
              </p>
            ) : null}
          </CardBody>
        </Card>
      ) : null}

      {/* File tree (always visible until applied) */}
      {imp.status !== "applied" ? (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Folder size={14} className="text-fgmuted" /> Staged files ({imp.files.length})
            </CardTitle>
          </CardHeader>
          <CardBody className="max-h-64 overflow-y-auto text-xs font-mono space-y-0.5">
            {imp.files.map((f) => (
              <div key={f.relpath} className="flex justify-between gap-2 truncate">
                <span className="truncate">{f.relpath}</span>
                <span className="text-fgmuted">{f.size} B</span>
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}

      {/* Plan editor */}
      {plan && (imp.status === "planned" || imp.status === "error") ? (
        <div className="mt-4 space-y-4">
          <PlanProjectCard plan={plan} setPlan={setPlan} projects={projects.data || []} />
          <RequestAccessAttachCard importId={imp.id} projects={projects.data || []} />
          <PlanScansCard plan={plan} setPlan={setPlan} />
          <PlanItemsCard plan={plan} setPlan={setPlan} />
          {plan.runs && plan.runs.length > 0 ? (
            <Card>
              <CardHeader><CardTitle>Run rows ({plan.runs.length})</CardTitle></CardHeader>
              <CardBody>
                <p className="text-xs text-fgmuted">
                  These will be created under the scan with the matching local_id.
                </p>
              </CardBody>
            </Card>
          ) : null}

          <Card className="border-primary/40">
            <CardBody className="flex items-center justify-between flex-wrap gap-3">
              <p className="text-sm text-fgmuted">
                Review the plan above. <strong>Confirm</strong> applies it: projects,
                scans, reports, and POCs are created and the staging folder is wiped.
              </p>
              <div className="flex items-center gap-2">
                <Button variant="ghost" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                  <X size={14} /> Discard
                </Button>
                <Button onClick={() => confirm.mutate()} disabled={confirm.isPending}>
                  <Check size={14} /> {confirm.isPending ? "Applying…" : "Confirm & import"}
                </Button>
              </div>
            </CardBody>
            {confirm.isError ? (
              <CardBody className="pt-0">
                <p className="text-xs text-danger">
                  {(confirm.error as ApiError)?.detail
                    ? String((confirm.error as ApiError).detail)
                    : "Confirm failed."}
                </p>
              </CardBody>
            ) : null}
          </Card>
        </div>
      ) : null}

      {imp.plan_log && (imp.status === "planned" || imp.status === "applied" || imp.status === "error") ? (
        <Card className="mt-4">
          <CardHeader><CardTitle>Plan log</CardTitle></CardHeader>
          <CardBody>
            <pre className="text-[11px] whitespace-pre-wrap text-fgmuted">{imp.plan_log}</pre>
          </CardBody>
        </Card>
      ) : null}
    </>
  );
}

function statusTone(s: ImportDetail["status"]) {
  if (s === "applied") return "success" as const;
  if (s === "error" || s === "cancelled") return "danger" as const;
  if (s === "planned") return "warning" as const;
  return "muted" as const;
}

function PlanProjectCard({
  plan, setPlan, projects,
}: { plan: Plan; setPlan: (p: Plan) => void; projects: Project[] }) {
  const p = plan.project;
  const assignable = projects.filter((x) => x.i_am_owner || x.i_am_member);
  return (
    <Card>
      <CardHeader><CardTitle>Product</CardTitle></CardHeader>
      <CardBody className="space-y-3">
        {p.rationale ? (
          <p className="text-xs text-fgmuted italic">"{p.rationale}"</p>
        ) : null}
        <div className="flex flex-col md:flex-row md:items-end gap-3">
          <div>
            <Label htmlFor="proj-kind">Mode</Label>
            <Select
              id="proj-kind"
              value={p.kind}
              onChange={(e) =>
                setPlan({ ...plan, project: { ...p, kind: e.target.value as PlanProject["kind"] } })
              }
            >
              <option value="existing">Use existing product</option>
              <option value="new">Create new product</option>
              <option value="none">No product</option>
            </Select>
          </div>
          {p.kind === "existing" ? (
            <div className="flex-1">
              <Label htmlFor="proj-existing">Product</Label>
              <Select
                id="proj-existing"
                value={p.existing_id || ""}
                onChange={(e) =>
                  setPlan({ ...plan, project: { ...p, existing_id: e.target.value } })
                }
              >
                <option value="">— pick one —</option>
                {assignable.map((x) => (
                  <option key={x.id} value={x.id}>{x.name}</option>
                ))}
              </Select>
            </div>
          ) : null}
          {p.kind === "new" ? (
            <>
              <div className="flex-1">
                <Label htmlFor="proj-name">Name</Label>
                <Input
                  id="proj-name"
                  value={p.name || ""}
                  onChange={(e) =>
                    setPlan({ ...plan, project: { ...p, name: e.target.value } })
                  }
                />
              </div>
              <div className="flex-1">
                <Label htmlFor="proj-desc">Description</Label>
                <Input
                  id="proj-desc"
                  value={p.description || ""}
                  onChange={(e) =>
                    setPlan({ ...plan, project: { ...p, description: e.target.value } })
                  }
                />
              </div>
            </>
          ) : null}
        </div>
      </CardBody>
    </Card>
  );
}

function PlanScansCard({
  plan, setPlan,
}: { plan: Plan; setPlan: (p: Plan) => void }) {
  const scans = plan.scans || [];
  if (scans.length === 0) return null;
  function update(i: number, patch: Partial<PlanScan>) {
    const next = scans.map((s, j) => (j === i ? { ...s, ...patch } : s));
    setPlan({ ...plan, scans: next });
  }
  function remove(i: number) {
    // Drop the scan + clear any references to it from items.
    const dropped = scans[i].local_id;
    const next = scans.filter((_, j) => j !== i);
    const items = plan.items.map((it) =>
      it.scan_local_id === dropped ? { ...it, scan_local_id: undefined } : it
    );
    setPlan({ ...plan, scans: next, items });
  }
  return (
    <Card>
      <CardHeader><CardTitle>Scans to create ({scans.length})</CardTitle></CardHeader>
      <CardBody className="space-y-3">
        {scans.map((s, i) => (
          <div key={s.local_id || i} className="border border-border rounded-md p-3 space-y-2">
            <div className="flex items-center justify-between">
              <code className="text-xs text-fgmuted">local_id: {s.local_id}</code>
              <Button variant="ghost" size="sm" onClick={() => remove(i)}>
                <X size={12} /> drop
              </Button>
            </div>
            {s.rationale ? <p className="text-xs text-fgmuted italic">"{s.rationale}"</p> : null}
            <div className="grid grid-cols-2 gap-2">
              <div>
                <Label>Product</Label>
                <Input value={s.product || ""} onChange={(e) => update(i, { product: e.target.value })} />
              </div>
              <div>
                <Label>Target</Label>
                <Input value={s.scan_target || ""} onChange={(e) => update(i, { scan_target: e.target.value })} />
              </div>
              <div>
                <Label>Harness</Label>
                <Input value={s.harness_used || ""} onChange={(e) => update(i, { harness_used: e.target.value })} />
              </div>
              <div>
                <Label>Scan by</Label>
                <Input value={s.scan_by || ""} onChange={(e) => update(i, { scan_by: e.target.value })} />
              </div>
            </div>
            <div>
              <Label>Notes</Label>
              <Textarea rows={2} value={s.notes || ""} onChange={(e) => update(i, { notes: e.target.value })} />
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

function PlanItemsCard({
  plan, setPlan,
}: { plan: Plan; setPlan: (p: Plan) => void }) {
  const items = plan.items || [];
  const scans = plan.scans || [];
  const reportLocalIds = items
    .filter((x) => x.kind === "report" && x.local_id)
    .map((x) => x.local_id as string);

  function update(i: number, patch: Partial<PlanItem>) {
    const next = items.map((it, j) => (j === i ? { ...it, ...patch } : it));
    setPlan({ ...plan, items: next });
  }

  const counts = items.reduce(
    (acc, it) => ({ ...acc, [it.kind]: (acc[it.kind] || 0) + 1 }),
    { report: 0, poc: 0, skip: 0 } as Record<string, number>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          File dispositions —{" "}
          <span className="text-success font-normal">{counts.report} reports</span>{" "}
          / <span className="text-warning font-normal">{counts.poc} POCs</span>{" "}
          / <span className="text-fgmuted font-normal">{counts.skip} skipped</span>
        </CardTitle>
      </CardHeader>
      <Table>
        <THead>
          <TR>
            <TH>File</TH>
            <TH>Kind</TH>
            <TH>Title / attach</TH>
            <TH>Scan</TH>
            <TH className="w-1/4">Why</TH>
          </TR>
        </THead>
        <tbody>
          {items.map((it, i) => (
            <TR key={it.relpath}>
              <TD className="text-xs font-mono break-all max-w-[24ch]">
                <Icon kind={it.kind} /> {it.relpath}
              </TD>
              <TD>
                <Select
                  value={it.kind}
                  onChange={(e) => update(i, { kind: e.target.value as PlanItem["kind"] })}
                >
                  <option value="report">report</option>
                  <option value="poc">poc</option>
                  <option value="skip">skip</option>
                </Select>
              </TD>
              <TD>
                {it.kind === "report" ? (
                  <Input
                    placeholder="Title"
                    value={it.title || ""}
                    onChange={(e) => update(i, { title: e.target.value })}
                  />
                ) : it.kind === "poc" ? (
                  <Select
                    value={it.attach_to_local_id || ""}
                    onChange={(e) => update(i, { attach_to_local_id: e.target.value || undefined })}
                  >
                    <option value="">— directory default —</option>
                    {reportLocalIds.map((id) => (
                      <option key={id} value={id}>{id}</option>
                    ))}
                  </Select>
                ) : (
                  <span className="text-fgmuted text-xs">—</span>
                )}
              </TD>
              <TD>
                {it.kind !== "skip" && scans.length > 0 ? (
                  <Select
                    value={it.scan_local_id || ""}
                    onChange={(e) => update(i, { scan_local_id: e.target.value || undefined })}
                  >
                    <option value="">—</option>
                    {scans.map((s) => (
                      <option key={s.local_id} value={s.local_id}>{s.local_id}</option>
                    ))}
                  </Select>
                ) : (
                  <span className="text-fgmuted text-xs">—</span>
                )}
              </TD>
              <TD className="text-xs text-fgmuted">
                <div className="max-w-[36ch] truncate" title={it.rationale || ""}>
                  {it.rationale || "—"}
                </div>
              </TD>
            </TR>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

function Icon({ kind }: { kind: PlanItem["kind"] }) {
  if (kind === "report") return <FileText size={11} className="inline text-success mr-1" />;
  if (kind === "poc") return <Paperclip size={11} className="inline text-warning mr-1" />;
  return <X size={11} className="inline text-fgmuted mr-1" />;
}

/**
 * Sidecar card that lets the importer ask for membership in a project they
 * don't yet belong to AND attach this folder import to that ask. The
 * project's owner then sees the request + the file preview on their
 * project page; clicking Approve grants membership *and* applies the
 * import in one shot, removing the back-and-forth.
 */
function RequestAccessAttachCard({
  importId, projects,
}: { importId: string; projects: Project[] }) {
  const qc = useQueryClient();
  // Filter: only projects the user isn't already in. If there are none, hide
  // the card so the plan-editor doesn't get cluttered.
  const targets = projects.filter((p) => !(p.i_am_owner || p.i_am_member));
  const [projectId, setProjectId] = useState<string>("");
  const [reason, setReason] = useState<string>("");
  const [sentTo, setSentTo] = useState<string | null>(null);

  const send = useMutation({
    mutationFn: () =>
      api(`/project_requests`, {
        method: "POST",
        body: {
          project_id: projectId,
          reason: reason.trim(),
          import_id: importId,
        },
      }),
    onSuccess: () => {
      setSentTo(projectId);
      setReason("");
      qc.invalidateQueries({ queryKey: ["my-requests"] });
    },
  });

  if (targets.length === 0) return null;

  return (
    <Card className="border-warning/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Send size={14} className="text-warning" /> Don't have access to the right product yet?
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-xs text-fgmuted">
          If the right product belongs to someone else, ask them to add you and bring this import with you in one click.
          Approving on their side will grant your membership <em>and</em> auto-import this folder into the product.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-[1fr_1fr_auto] gap-2 items-end">
          <div>
            <Label htmlFor="ra-proj">Product</Label>
            <Select id="ra-proj" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
              <option value="">— pick a product —</option>
              {targets.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>
          <div>
            <Label htmlFor="ra-reason">Reason (optional)</Label>
            <Input id="ra-reason" value={reason} onChange={(e) => setReason(e.target.value)}
                   placeholder="e.g. 'I ran the Acme Gateway fuzz last week, see attached'" />
          </div>
          <Button onClick={() => projectId && send.mutate()} disabled={!projectId || send.isPending}>
            <Send size={14} /> {send.isPending ? "Sending…" : "Request + attach"}
          </Button>
        </div>
        {sentTo ? (
          <p className="text-xs text-success inline-flex items-center gap-1">
            <Check size={12} /> Request sent. The product owner has a notification with this import attached.
          </p>
        ) : null}
        {send.isError ? (
          <p className="text-xs text-danger">Couldn't send the request — try again.</p>
        ) : null}
      </CardBody>
    </Card>
  );
}
