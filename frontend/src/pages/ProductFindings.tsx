import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { useMemo, useState } from "react";
import { api, getToken } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label, Select } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge, SeverityChip } from "@/components/ui/Badge";
import { ArrowLeft, ArrowUp, ArrowDown, ArrowUpDown, Download, Search } from "lucide-react";

type Finding = {
  id: string;
  scan_id: string;
  title: string;
  severity: string;
  status: string;
  cwe: string;
  cve: string;
  ai_verdict: "open" | "true_positive" | "false_positive";
  ai_rationale: string;
  tags: string[];
  triaged_by: string;
  triaged_at: string | null;
  created_at: string;
  scan_product: string;
  scan_target: string;
  scan_rank: number;
};

// Severity sorts by rank (critical first), not alphabetically.
const SEVERITY_RANK: Record<string, number> = {
  critical: 0, high: 1, medium: 2, low: 3, info: 4, unknown: 5,
};
type SortKey = "severity" | "title" | "scan_rank" | "status" | "ai_verdict" | "triaged_by" | "created_at";
const SORT_ACCESSORS: Record<SortKey, (f: Finding) => number | string> = {
  severity:   (f) => SEVERITY_RANK[(f.severity || "").toLowerCase()] ?? SEVERITY_RANK.unknown,
  title:      (f) => (f.title || "").toLowerCase(),
  scan_rank:  (f) => f.scan_rank ?? 0,
  status:     (f) => (f.status || "").toLowerCase(),
  ai_verdict: (f) => (f.ai_verdict || "").toLowerCase(),
  triaged_by: (f) => (f.triaged_by || "").toLowerCase(),
  created_at: (f) => f.created_at || "",
};

const VERDICT_LABEL: Record<string, string> = {
  open: "Open",
  true_positive: "TP",
  false_positive: "FP",
  sbp: "SBP",
  duplicate: "Dup",
  fixed: "Fixed",
};

const TAG_VALUES = ["sbp", "ss", "vuln"] as const;
type TagValue = (typeof TAG_VALUES)[number];

function Row({ f, productId }: { f: Finding; productId: string }) {
  const qc = useQueryClient();
  const patch = useMutation({
    mutationFn: (body: Partial<{ status: string; tags: string[] }>) =>
      api<Finding>(`/scans/${f.scan_id}/findings/${f.id}`, {
        method: "PATCH",
        body,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["product-findings", productId] });
      qc.invalidateQueries({ queryKey: ["product-findings-summary", productId] });
      // Keep per-scan view consistent if the user navigates over.
      qc.invalidateQueries({ queryKey: ["scan", f.scan_id] });
    },
  });

  function verdictBtn(target: "true_positive" | "false_positive" | "open", label: string) {
    const active = f.status === target;
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
    const cur = new Set(f.tags || []);
    if (cur.has(t)) cur.delete(t);
    else cur.add(t);
    patch.mutate({ tags: Array.from(cur) });
  }

  return (
    <TR className="hover:bg-muted/40 align-top">
      <TD><SeverityChip value={f.severity} /></TD>
      <TD>
        <Link to={`/scans/${f.scan_id}/findings/${f.id}`} className="text-primary hover:underline font-medium">
          {f.title || <span className="text-fgmuted italic">(untitled)</span>}
        </Link>
        <div className="text-[11px] font-mono text-fgmuted mt-0.5">
          {f.cwe}{f.cve ? ` · ${f.cve}` : ""}
        </div>
      </TD>
      <TD className="text-xs">
        <Link to={`/scans/${f.scan_id}`} className="text-primary hover:underline">
          Scan #{f.scan_rank}
        </Link>
        {f.scan_product ? (
          <div className="text-[11px] text-fgmuted truncate max-w-[12ch]" title={f.scan_product}>
            {f.scan_product}
          </div>
        ) : null}
      </TD>
      <TD>
        <div className="flex items-center gap-1 flex-wrap">
          {verdictBtn("true_positive", "TP")}
          {verdictBtn("false_positive", "FP")}
          {verdictBtn("open", "Open")}
        </div>
      </TD>
      <TD>
        <Badge tone={f.ai_verdict === "true_positive" ? "success" :
                      f.ai_verdict === "false_positive" ? "danger" :
                      "muted"}>
          {f.ai_verdict === "true_positive" ? "TP" :
           f.ai_verdict === "false_positive" ? "FP" : "—"}
        </Badge>
        {f.ai_rationale ? (
          <div className="text-[10px] text-fgmuted mt-0.5 max-w-[16ch] truncate"
               title={f.ai_rationale}>
            {f.ai_rationale}
          </div>
        ) : null}
      </TD>
      <TD>
        <div className="flex items-center gap-1 flex-wrap">
          {TAG_VALUES.map((t) => {
            const on = (f.tags || []).includes(t);
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
      <TD className="text-xs text-fgmuted">{f.triaged_by || "—"}</TD>
      <TD className="text-xs text-fgmuted whitespace-nowrap">
        {new Date(f.created_at).toLocaleDateString()}
      </TD>
    </TR>
  );
}

export function ProductFindings() {
  const { project_id = "" } = useParams();
  const q = useQuery({
    queryKey: ["product-findings", project_id],
    queryFn: () => api<Finding[]>(`/projects/${project_id}/findings`),
  });

  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" }>({
    key: "severity", dir: "asc",   // default: most severe first
  });

  const rows = useMemo(() => {
    let r = q.data || [];
    if (statusFilter) r = r.filter((f) => f.status === statusFilter);
    if (tagFilter) r = r.filter((f) => (f.tags || []).includes(tagFilter));
    if (query.trim()) {
      const needle = query.trim().toLowerCase();
      r = r.filter((f) =>
        (f.title || "").toLowerCase().includes(needle) ||
        (f.cwe || "").toLowerCase().includes(needle) ||
        (f.cve || "").toLowerCase().includes(needle) ||
        (f.triaged_by || "").toLowerCase().includes(needle)
      );
    }
    const acc = SORT_ACCESSORS[sort.key];
    r = [...r].sort((a, b) => {
      const va = acc(a), vb = acc(b);
      const cmp = typeof va === "number" && typeof vb === "number"
        ? va - vb
        : String(va).localeCompare(String(vb));
      return sort.dir === "asc" ? cmp : -cmp;
    });
    return r;
  }, [q.data, query, statusFilter, tagFilter, sort]);

  const toggleSort = (key: SortKey) =>
    setSort((s) => s.key === key
      ? { key, dir: s.dir === "asc" ? "desc" : "asc" }
      : { key, dir: key === "created_at" ? "desc" : "asc" }); // dates newest-first
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

  async function exportCsv() {
    const tok = getToken();
    const r = await fetch(`/projects/${project_id}/findings/export?format=csv`, {
      headers: tok ? { Authorization: `Bearer ${tok}` } : {},
    });
    if (!r.ok) return;
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="([^"]+)"/);
    const name = m ? m[1] : "findings.csv";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <>
      <PageHeader
        title="All findings"
        subtitle={`Findings across every scan in this product${rows ? ` · ${rows.length} shown` : ""}`}
        action={
          <div className="flex items-center gap-2">
            <Link to={`/products/${project_id}`} className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
              <ArrowLeft size={12} /> Back to product
            </Link>
            <Button size="sm" variant="secondary" onClick={exportCsv}>
              <Download size={14} /> Export CSV
            </Button>
          </div>
        }
      />

      <Card className="mb-4">
        <CardBody className="grid grid-cols-1 md:grid-cols-[1fr_10rem_10rem] gap-2 items-end">
          <div>
            <Label htmlFor="q">Search</Label>
            <div className="relative">
              <Input id="q" value={query} onChange={(e) => setQuery(e.target.value)}
                     placeholder="title, CWE, CVE, triager…" className="pl-7" />
              <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-fgmuted" />
            </div>
          </div>
          <div>
            <Label htmlFor="st">Dev verdict</Label>
            <Select id="st" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
              <option value="">— any —</option>
              <option value="open">Open</option>
              <option value="true_positive">TP</option>
              <option value="false_positive">FP</option>
              <option value="sbp">SBP</option>
              <option value="duplicate">Duplicate</option>
              <option value="fixed">Fixed</option>
            </Select>
          </div>
          <div>
            <Label htmlFor="tag">Tag</Label>
            <Select id="tag" value={tagFilter} onChange={(e) => setTagFilter(e.target.value)}>
              <option value="">— any —</option>
              <option value="sbp">SBP</option>
              <option value="ss">SS</option>
              <option value="vuln">VULN</option>
            </Select>
          </div>
        </CardBody>
      </Card>

      {q.isLoading ? (
        <div className="text-sm text-fgmuted">Loading…</div>
      ) : !q.data || q.data.length === 0 ? (
        <Card><CardBody>
          <p className="text-sm text-fgmuted">No findings in this product yet.</p>
        </CardBody></Card>
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                {sortTH("Severity", "severity", "w-20")}
                {sortTH("Title", "title")}
                {sortTH("Scan", "scan_rank", "w-20")}
                {sortTH("Dev", "status", "w-24")}
                {sortTH("AI", "ai_verdict", "w-24")}
                <TH className="w-32">Tags</TH>
                {sortTH("Triaged by", "triaged_by", "w-40")}
                {sortTH("When", "created_at", "w-24")}
              </TR>
            </THead>
            <tbody>
              {rows.map((f) => (
                <Row key={f.id} f={f} productId={project_id} />
              ))}
            </tbody>
          </Table>
        </Card>
      )}
    </>
  );
}
