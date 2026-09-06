import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Empty } from "@/components/ui/Empty";
import { Microscope, Plus, X, FolderGit2, ListChecks } from "lucide-react";

export type CaseOut = {
  id: string; user_id: string; project_id: string | null; project_name: string | null;
  scan_id: string | null; title: string; status: string;
  ai_provider: string | null; ai_model: string | null; poc_auto_execute: boolean;
  finding_count: number; stage_run_count: number; pending_stage_count: number;
  has_report: boolean; created_at: string; updated_at: string;
};
type Project = { id: string; name: string };

const STATUS_TONE: Record<string, "muted" | "primary" | "warning" | "success" | "default"> = {
  intake: "muted", active: "primary", blocked: "warning", done: "success", archived: "default",
};

export function statusBadge(status: string) {
  return <Badge tone={STATUS_TONE[status] ?? "default"}>{status}</Badge>;
}

export function Cases() {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["cases"], queryFn: () => api<CaseOut[]>("/cases") });
  const cases = q.data ?? [];

  return (
    <>
      <PageHeader
        title="Investigations"
        subtitle="A case is one bug report → findings → a report. Assign a model per stage."
        action={
          <Button size="sm" onClick={() => setOpen((v) => !v)}>
            {open ? <X size={14} /> : <Plus size={14} />} {open ? "Cancel" : "New case"}
          </Button>
        }
      />

      {open ? <NewCaseForm onCreated={(id) => { setOpen(false);
        qc.invalidateQueries({ queryKey: ["cases"] }); nav(`/cases/${id}`); }} /> : null}

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : cases.length === 0 && !open ? (
        <Empty icon={<Microscope size={28} />} title="No cases yet"
               hint="Open a case from a bug report, point it at the affected source, and run the pipeline." />
      ) : cases.length ? (
        <Table>
          <THead>
            <TR>
              <TH>Title</TH><TH>Product</TH><TH>Status</TH>
              <TH>Findings</TH><TH>Stages</TH><TH>Model</TH>
            </TR>
          </THead>
          <tbody>
            {cases.map((c) => (
              <TR key={c.id} className="cursor-pointer hover:bg-muted/40"
                  onClick={() => nav(`/cases/${c.id}`)}>
                <TD className="font-medium">
                  <Link to={`/cases/${c.id}`} className="hover:underline">
                    {c.title || "(untitled case)"}
                  </Link>
                </TD>
                <TD className="text-fgmuted">
                  {c.project_name ? (
                    <span className="inline-flex items-center gap-1">
                      <FolderGit2 size={12} /> {c.project_name}
                    </span>
                  ) : "—"}
                </TD>
                <TD>{statusBadge(c.status)}</TD>
                <TD>{c.finding_count}</TD>
                <TD>
                  <span className="inline-flex items-center gap-1 text-xs text-fgmuted">
                    <ListChecks size={12} />
                    {c.stage_run_count}
                    {c.pending_stage_count ? (
                      <Badge tone="primary" className="ml-1">{c.pending_stage_count} queued</Badge>
                    ) : null}
                  </span>
                </TD>
                <TD className="text-xs text-fgmuted">
                  {c.ai_provider ? `${c.ai_provider}${c.ai_model ? ` / ${c.ai_model}` : ""}` : "default"}
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      ) : null}
    </>
  );
}

function NewCaseForm({ onCreated }: { onCreated: (id: string) => void }) {
  const [title, setTitle] = useState("");
  const [projectId, setProjectId] = useState("");
  const [reportText, setReportText] = useState("");
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => api<Project[]>("/projects") });

  const create = useMutation({
    mutationFn: () =>
      api<{ id: string }>("/cases", {
        method: "POST",
        body: {
          title: title.trim(),
          project_id: projectId || null,
          report_text: reportText.trim() || null,
        },
      }),
    onSuccess: (r) => onCreated(r.id),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (title.trim()) create.mutate();
  }

  return (
    <Card className="mb-6">
      <CardHeader><CardTitle>Open a case</CardTitle></CardHeader>
      <CardBody>
        <form onSubmit={submit} className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <Label htmlFor="t">Title</Label>
              <Input id="t" value={title} onChange={(e) => setTitle(e.target.value)}
                     placeholder="RCE in the upload handler" autoFocus />
            </div>
            <div>
              <Label htmlFor="p">Product <span className="opacity-60">(optional)</span></Label>
              <Select id="p" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
                <option value="">None</option>
                {(projects.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </Select>
            </div>
          </div>
          <div>
            <Label htmlFor="r">Bug report <span className="opacity-60">(optional; stored encrypted)</span></Label>
            <Textarea id="r" rows={5} value={reportText} onChange={(e) => setReportText(e.target.value)}
                      placeholder="Paste the inbound bug report…" />
          </div>
          <div className="flex items-center gap-2">
            <Button type="submit" disabled={create.isPending || !title.trim()}>
              <Plus size={14} /> {create.isPending ? "Creating…" : "Create case"}
            </Button>
            {create.isError ? <span className="text-xs text-danger">Couldn't create the case.</span> : null}
          </div>
        </form>
      </CardBody>
    </Card>
  );
}
