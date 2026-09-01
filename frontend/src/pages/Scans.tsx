import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { PageHeader } from "@/components/Layout";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Empty } from "@/components/ui/Empty";
import { ShieldAlert, User, Pencil, Save } from "lucide-react";

type ScanOut = {
  id: string; user_id: string; product: string; title: string;
  scan_target: string; harness_used: string;
  scan_by: string; tp: number; fp: number; sbp: number; findings: number;
  untriaged: number; highest_severity: string; state: string;
  source_session_id: string | null; project_id: string | null;
  created_at: string;
};

export function Scans() {
  const { me } = useAuth();
  const qc = useQueryClient();
  const [mineOnly, setMineOnly] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const q = useQuery({ queryKey: ["scans"], queryFn: () => api<ScanOut[]>("/scans") });

  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api(`/scans/${id}`, { method: "PATCH", body: { title } }),
    onSuccess: () => { setEditingId(null); qc.invalidateQueries({ queryKey: ["scans"] }); },
  });

  const all = q.data ?? [];
  const rows = mineOnly && me ? all.filter((s) => s.user_id === me.id) : all;

  return (
    <>
      <PageHeader
        title="Vulnerability scans"
        subtitle="Type-A scan summaries with triage counts."
        action={
          all.length > 0 ? (
            <Button variant={mineOnly ? "primary" : "secondary"} size="sm"
                    onClick={() => setMineOnly((v) => !v)}>
              <User size={14} /> {mineOnly ? "Showing only mine" : "Show only mine"}
            </Button>
          ) : null
        }
      />
      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : all.length === 0 ? (
        <Empty icon={<ShieldAlert size={28} />} title="No scans yet"
               hint="Upload a vulnerability report .md and Claude will auto-extract a draft scan." />
      ) : rows.length === 0 ? (
        <Empty icon={<User size={28} />} title="None of these are yours"
               hint="You haven't created any scans yet. Turn off “Show only mine” to see everyone's." />
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Title</TH>
              <TH>Target</TH>
              <TH>Harness</TH>
              <TH>Findings</TH>
              <TH>TP / FP / SBP</TH>
              <TH>Untriaged</TH>
              <TH>State</TH>
              <TH>When</TH>
            </TR>
          </THead>
          <tbody>
            {rows.map((s) => (
              <TR key={s.id} className="hover:bg-muted/40">
                <TD>
                  {editingId === s.id ? (
                    <form className="flex items-center gap-1.5"
                          onSubmit={(e) => { e.preventDefault(); rename.mutate({ id: s.id, title: draft.trim() }); }}>
                      <Input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)}
                             placeholder={s.product || "Scan title"} className="h-8 w-56"
                             onKeyDown={(e) => { if (e.key === "Escape") setEditingId(null); }} />
                      <Button type="submit" size="sm" disabled={rename.isPending}><Save size={12} /></Button>
                      <Button type="button" variant="ghost" size="sm" onClick={() => setEditingId(null)}>Cancel</Button>
                    </form>
                  ) : (
                    <span className="inline-flex items-center gap-1.5 group">
                      <Link to={`/scans/${s.id}`} className="text-primary hover:underline font-medium">
                        {s.title || s.product || "(unset)"}
                      </Link>
                      <button type="button" title="Rename scan"
                              className="text-fgmuted hover:text-fg opacity-0 group-hover:opacity-100 transition-opacity"
                              onClick={() => { setEditingId(s.id); setDraft(s.title || s.product || ""); }}>
                        <Pencil size={13} />
                      </button>
                    </span>
                  )}
                </TD>
                <TD className="text-fgmuted text-xs">{s.scan_target}</TD>
                <TD className="text-fgmuted text-xs">{s.harness_used}</TD>
                <TD className="tabular-nums">{s.findings}</TD>
                <TD className="text-xs tabular-nums">
                  <span className="text-success">{s.tp}</span> /{" "}
                  <span className="text-danger">{s.fp}</span> /{" "}
                  <span className="text-warning">{s.sbp}</span>
                </TD>
                <TD className="tabular-nums">{s.untriaged}</TD>
                <TD><Badge tone={s.state === "draft" ? "warning" : "success"}>{s.state}</Badge></TD>
                <TD className="text-fgmuted whitespace-nowrap text-xs">
                  {new Date(s.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                </TD>
              </TR>
            ))}
          </tbody>
        </Table>
      )}
    </>
  );
}
