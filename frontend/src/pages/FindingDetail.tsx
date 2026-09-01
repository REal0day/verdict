import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { SeverityChip } from "@/components/ui/Badge";
import { ArrowLeft, Check, Trash2 } from "lucide-react";

const SEVERITIES = ["critical", "high", "medium", "low", "info", "unknown"] as const;
const STATUSES = ["open", "true_positive", "false_positive", "sbp", "duplicate", "fixed"] as const;

type Finding = {
  id: string; scan_id: string; user_id: string;
  title: string; severity: string; status: string;
  cwe: string; cve: string; affected_component: string;
  description: string; steps_to_reproduce: string; remediation: string;
  proof_of_concept: string; references: string; dev_notes: string;
  assigned_to: string; triaged_by: string;
  triaged_at: string | null; created_at: string; updated_at: string;
};

type ScanLite = { id: string; product: string };

export function FindingDetail() {
  const { scan_id = "", finding_id = "" } = useParams();
  const qc = useQueryClient();
  const nav = useNavigate();

  // The finding lives inside /scans/{scan_id} — pull the scan and find it.
  const scan = useQuery({
    queryKey: ["scan", scan_id],
    queryFn: () => api<any>(`/scans/${scan_id}`),
  });

  const f0: Finding | undefined = (scan.data?.findings_list || []).find(
    (f: Finding) => f.id === finding_id
  );

  const [f, setF] = useState<Partial<Finding>>({});
  useEffect(() => {
    if (f0) setF({ ...f0 });
  }, [f0?.id, f0?.updated_at]); // refresh when underlying record changes

  const set = <K extends keyof Finding>(k: K, v: Finding[K]) =>
    setF((x) => ({ ...x, [k]: v }));

  const save = useMutation({
    mutationFn: () =>
      api(`/scans/${scan_id}/findings/${finding_id}`, {
        method: "PATCH",
        body: {
          title: f.title, severity: f.severity, status: f.status,
          cwe: f.cwe, cve: f.cve, affected_component: f.affected_component,
          description: f.description, steps_to_reproduce: f.steps_to_reproduce,
          remediation: f.remediation, proof_of_concept: f.proof_of_concept,
          references: f.references, dev_notes: f.dev_notes,
          assigned_to: f.assigned_to, triaged_by: f.triaged_by,
        },
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["scan", scan_id] }),
  });

  const del = useMutation({
    mutationFn: () =>
      api(`/scans/${scan_id}/findings/${finding_id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["scan", scan_id] });
      nav(`/scans/${scan_id}`);
    },
  });

  if (scan.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (scan.isError) return <div className="text-sm text-danger">Failed to load scan.</div>;
  if (!f0) return <div className="text-sm text-danger">Finding not found in this scan.</div>;
  const s: ScanLite = { id: scan.data.id, product: scan.data.product };
  const cur = f as Finding;

  return (
    <div className="space-y-5">
      <div>
        <Link to={`/scans/${scan_id}`} className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> Back to scan: {s.product || s.id}
        </Link>
        <div className="mt-2 flex items-baseline gap-3 flex-wrap">
          <SeverityChip value={cur.severity || "unknown"} />
          <h1 className="text-2xl font-semibold">
            {cur.title || <span className="text-fgmuted italic">(untitled)</span>}
          </h1>
        </div>
        <p className="text-xs text-fgmuted mt-1">
          {cur.cwe}{cur.cve ? ` · ${cur.cve}` : ""}{" "}
          · status: <strong className="text-fg">{cur.status}</strong>
          {cur.triaged_by ? <> · triaged by {cur.triaged_by}</> : null}
          {cur.triaged_at ? <> at {new Date(cur.triaged_at).toLocaleString()}</> : null}
        </p>
      </div>

      <Card>
        <CardHeader className="flex items-center justify-between">
          <CardTitle>Summary</CardTitle>
          {save.isSuccess ? <span className="text-xs text-success inline-flex items-center gap-1"><Check size={12}/>saved</span> : null}
        </CardHeader>
        <CardBody className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="md:col-span-3">
            <Label>Title</Label>
            <Input value={cur.title ?? ""} onChange={(e) => set("title", e.target.value)} />
          </div>
          <div>
            <Label>Severity</Label>
            <Select value={cur.severity ?? "unknown"} onChange={(e) => set("severity", e.target.value)}>
              {SEVERITIES.map((sv) => <option key={sv} value={sv}>{sv}</option>)}
            </Select>
          </div>
          <div>
            <Label>Status</Label>
            <Select value={cur.status ?? "open"} onChange={(e) => set("status", e.target.value)}>
              {STATUSES.map((st) => <option key={st} value={st}>{st}</option>)}
            </Select>
          </div>
          <div>
            <Label>CWE</Label>
            <Input value={cur.cwe ?? ""} onChange={(e) => set("cwe", e.target.value)} placeholder="CWE-122" />
          </div>
          <div>
            <Label>CVE</Label>
            <Input value={cur.cve ?? ""} onChange={(e) => set("cve", e.target.value)} placeholder="CVE-2025-…" />
          </div>
          <div>
            <Label>Affected component</Label>
            <Input value={cur.affected_component ?? ""} onChange={(e) => set("affected_component", e.target.value)} />
          </div>
          <div>
            <Label>Assigned to</Label>
            <Input value={cur.assigned_to ?? ""} onChange={(e) => set("assigned_to", e.target.value)} />
          </div>
          <div>
            <Label>Triaged by</Label>
            <Input value={cur.triaged_by ?? ""} onChange={(e) => set("triaged_by", e.target.value)} />
          </div>
        </CardBody>
      </Card>

      <FieldCard title="Description">
        <Textarea rows={5} value={cur.description ?? ""} onChange={(e) => set("description", e.target.value)} />
      </FieldCard>
      <FieldCard title="Steps to reproduce">
        <Textarea rows={6} value={cur.steps_to_reproduce ?? ""} onChange={(e) => set("steps_to_reproduce", e.target.value)} />
      </FieldCard>
      <FieldCard title="Remediation">
        <Textarea rows={4} value={cur.remediation ?? ""} onChange={(e) => set("remediation", e.target.value)} />
      </FieldCard>
      <FieldCard title="Proof of concept">
        <Textarea rows={5} value={cur.proof_of_concept ?? ""} onChange={(e) => set("proof_of_concept", e.target.value)} />
      </FieldCard>
      <FieldCard title="References" hint="One URL or reference per line.">
        <Textarea rows={3} value={cur.references ?? ""} onChange={(e) => set("references", e.target.value)} />

        <Label>Dev notes <span className="text-fgmuted">(from share-link triage)</span></Label>
        <Textarea rows={3} value={cur.dev_notes ?? ""} onChange={(e) => set("dev_notes", e.target.value)} />
      </FieldCard>

      <div className="flex items-center gap-2">
        <Button onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save finding"}
        </Button>
        <Button variant="secondary" onClick={() => {
          if (confirm(`Delete this finding?`)) del.mutate();
        }} disabled={del.isPending}>
          <Trash2 size={14} /> Delete
        </Button>
        {save.isError ? <span className="text-xs text-danger">Save failed.</span> : null}
      </div>
    </div>
  );
}

function FieldCard({ title, hint, children }:
  { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardBody>
        {hint ? <p className="text-xs text-fgmuted mb-2">{hint}</p> : null}
        {children}
      </CardBody>
    </Card>
  );
}
