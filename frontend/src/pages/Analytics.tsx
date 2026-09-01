import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { useMemo, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Button } from "@/components/ui/Button";
import { Input, Label, Textarea } from "@/components/ui/Input";
import { Badge, SeverityChip } from "@/components/ui/Badge";
import { ChatReply } from "@/components/ChatReply";
import { downloadFile } from "@/lib/download";
import {
  BarChart3, Send, Download, Sparkles, X, Check, FolderGit2,
  Table as TableIcon, ExternalLink, ArrowUpDown,
} from "lucide-react";

type Project = {
  id: string; name: string;
  i_am_owner?: boolean; i_am_member?: boolean;
};

type AnalyticsScope = {
  products: number; scans: number; findings: number; truncated: boolean;
};
type AnalyticsResponse = {
  reply: string;
  generated_report_id: string | null;
  scope: AnalyticsScope;
};

// ---------- Master spreadsheet (one row per scan, all products) ----------

type MasterRow = {
  id: string; project_id: string | null; source_report_id: string | null;
  created_at: string; state: string;
  product: string; scan_target: string; harness_used: string; scan_by: string;
  results_file: string; spreadsheet_link: string; triaged_by: string;
  findings: number; fp: number; sbp: number; tp: number; ss: number;
  duplicates: number; untriaged: number; highest_severity: string;
};

// Number columns first (after Product) so the counts are visible with no
// right-scroll. Fixed order: Findings, TP, FP, SBP, SS, Untriaged — then
// Duplicates closes the number block, then the descriptive columns.
const MASTER_COLS = [
  { k: "product",          h: "Product" },
  { k: "created_at",       h: "Scan" },
  { k: "findings",         h: "Findings",  num: true },
  { k: "tp",               h: "TP",        num: true },
  { k: "fp",               h: "FP",        num: true },
  { k: "sbp",              h: "SBP",       num: true },
  { k: "ss",               h: "SS",        num: true },
  { k: "untriaged",        h: "Untriaged", num: true },
  { k: "duplicates",       h: "Duplicates (same mem context)", num: true },
  { k: "scan_target",      h: "Scan Target" },
  { k: "harness_used",     h: "Harness used" },
  { k: "scan_by",          h: "Scan by" },
  { k: "results_file",     h: "Results file" },
  { k: "spreadsheet_link", h: "Spreadsheet link" },
  { k: "triaged_by",       h: "Triaged By" },
  { k: "highest_severity", h: "Highest Severity" },
] satisfies ReadonlyArray<{ k: keyof MasterRow; h: string; num?: boolean }>;

type SortKey = keyof MasterRow;

function csvCell(v: string | number): string {
  const s = String(v ?? "");
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function pct(n: number, d: number): number {
  return d > 0 ? Math.round((n / d) * 100) : 0;
}

// At-a-glance totals across every visible scan. Shares the ["analytics-master"]
// query with the table below, so this adds no extra network call.
function MasterStats() {
  const q = useQuery({
    queryKey: ["analytics-master"],
    queryFn: () => api<MasterRow[]>("/analytics/master"),
    refetchInterval: 30_000,
  });

  const t = useMemo(() => {
    const z = { findings: 0, tp: 0, fp: 0, sbp: 0, ss: 0, untriaged: 0, scans: 0 };
    const products = new Set<string>();
    for (const r of q.data ?? []) {
      z.findings += r.findings; z.tp += r.tp; z.fp += r.fp;
      z.sbp += r.sbp; z.ss += r.ss; z.untriaged += r.untriaged;
      z.scans += 1; products.add(r.product);
    }
    return { ...z, products: products.size };
  }, [q.data]);

  const triaged = t.findings - t.untriaged;
  const triagedPct = pct(triaged, t.findings);

  // Literal class strings (not interpolated) so Tailwind keeps them.
  const tiles = [
    { label: "Findings",  value: t.findings,  sub: `${t.scans} scans · ${t.products} products`, num: "text-primary", bar: "bg-primary" },
    { label: "TP",        value: t.tp,        sub: `${pct(t.tp, t.findings)}% of findings`,        num: "text-success", bar: "bg-success" },
    { label: "FP",        value: t.fp,        sub: `${pct(t.fp, t.findings)}% of findings`,        num: "text-danger",  bar: "bg-danger" },
    { label: "SBP",       value: t.sbp,       sub: `${pct(t.sbp, t.findings)}% of findings`,       num: "text-warning", bar: "bg-warning" },
    { label: "SS",        value: t.ss,        sub: `${pct(t.ss, t.findings)}% of findings`,        num: "text-fg",      bar: "bg-fgmuted" },
    { label: "Untriaged", value: t.untriaged, sub: `${pct(t.untriaged, t.findings)}% of findings`, num: "text-fg",     bar: "bg-fgmuted" },
  ];

  return (
    <section className="mb-4 space-y-3">
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
        {tiles.map((tile) => (
          <div key={tile.label}
               className="relative overflow-hidden bg-surface border border-border rounded-lg shadow-card">
            <div className={`absolute inset-x-0 top-0 h-1 ${tile.bar}`} />
            <div className="p-4 pt-5">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-fgmuted">{tile.label}</div>
              <div className={`mt-1 text-3xl font-bold tabular-nums ${tile.num}`}>{tile.value.toLocaleString()}</div>
              <div className="mt-0.5 text-[11px] text-fgmuted">{tile.sub}</div>
            </div>
          </div>
        ))}
      </div>

      {/* Headline: how much of the corpus has actually been triaged. */}
      <div className="relative overflow-hidden rounded-lg border border-primary/30 shadow-card bg-gradient-to-r from-primary/10 to-transparent">
        <div className="p-4 flex flex-col md:flex-row md:items-center gap-4">
          <div className="md:w-72 shrink-0">
            <div className="text-[10px] font-semibold uppercase tracking-wider text-fgmuted">Percentage of Triaged</div>
            <div className="mt-1 text-4xl font-bold tabular-nums text-fg">{triagedPct}%</div>
            <div className="mt-0.5 text-xs text-fgmuted">
              {triaged.toLocaleString()} of {t.findings.toLocaleString()} findings triaged
            </div>
          </div>
          <div className="flex-1 w-full">
            <div className="h-3 rounded-full bg-muted overflow-hidden">
              <div className="h-full bg-success transition-all" style={{ width: `${triagedPct}%` }} />
            </div>
            <div className="mt-1.5 flex justify-between text-[11px] text-fgmuted">
              <span>{triaged.toLocaleString()} triaged</span>
              <span>{t.untriaged.toLocaleString()} untriaged</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function MasterDashboard() {
  const q = useQuery({
    queryKey: ["analytics-master"],
    queryFn: () => api<MasterRow[]>("/analytics/master"),
    refetchInterval: 30_000,
  });
  const [sort, setSort] = useState<{ k: SortKey; dir: 1 | -1 }>({
    k: "product", dir: 1,
  });

  const rows = useMemo(() => {
    const r = [...(q.data ?? [])];
    const { k, dir } = sort;
    r.sort((a, b) => {
      const av = a[k], bv = b[k];
      if (typeof av === "number" && typeof bv === "number") return (av - bv) * dir;
      return String(av ?? "").localeCompare(String(bv ?? "")) * dir;
    });
    return r;
  }, [q.data, sort]);

  const totals = useMemo(() => {
    const t = { findings: 0, fp: 0, sbp: 0, tp: 0, ss: 0, duplicates: 0, untriaged: 0 };
    for (const r of rows) {
      t.findings += r.findings; t.fp += r.fp; t.sbp += r.sbp;
      t.tp += r.tp; t.ss += r.ss; t.duplicates += r.duplicates; t.untriaged += r.untriaged;
    }
    return t;
  }, [rows]);

  function exportCsv() {
    const header = MASTER_COLS.map((c) => c.h).join(",");
    const body = rows.map((r) =>
      MASTER_COLS.map((c) => csvCell(r[c.k])).join(",")
    ).join("\n");
    const blob = new Blob([header + "\n" + body], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `irs-master-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function clickSort(k: SortKey) {
    setSort((s) => s.k === k ? { k, dir: s.dir === 1 ? -1 : 1 } : { k, dir: 1 });
  }

  return (
    <Card className="mb-4">
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <TableIcon size={14} className="text-primary" /> Master spreadsheet
          <span className="text-xs text-fgmuted font-normal">
            · {rows.length} scan{rows.length === 1 ? "" : "s"} across all products
          </span>
        </CardTitle>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" onClick={() => q.refetch()} disabled={q.isFetching}>
            {q.isFetching ? "Refreshing…" : "Refresh"}
          </Button>
          <Button size="sm" variant="secondary" onClick={exportCsv} disabled={rows.length === 0}>
            <Download size={12} /> CSV
          </Button>
        </div>
      </CardHeader>
      {q.isLoading ? (
        <CardBody><p className="text-sm text-fgmuted">Loading…</p></CardBody>
      ) : rows.length === 0 ? (
        <CardBody><p className="text-sm text-fgmuted">No scans visible to you yet.</p></CardBody>
      ) : (
        <CardBody className="pt-0">
          <Table>
            <THead>
              <TR>
                {MASTER_COLS.map((c) => (
                  <TH
                    key={c.k}
                    className={
                      "cursor-pointer select-none whitespace-nowrap " +
                      (c.num ? "text-right " : "") +
                      (sort.k === c.k ? "text-fg" : "")
                    }
                    onClick={() => clickSort(c.k)}
                  >
                    <span className="inline-flex items-center gap-1">
                      {c.h}
                      <ArrowUpDown size={10} className="opacity-40" />
                    </span>
                  </TH>
                ))}
              </TR>
            </THead>
            <tbody>
              {rows.map((r) => (
                <TR key={r.id} className="hover:bg-muted/40">
                  <TD className="whitespace-nowrap">
                    {r.project_id ? (
                      <Link to={`/products/${r.project_id}`} className="text-primary hover:underline">
                        {r.product || "—"}
                      </Link>
                    ) : (r.product || "—")}
                    {r.state === "draft" ? (
                      <Badge tone="warning" className="ml-1.5">draft</Badge>
                    ) : null}
                  </TD>
                  <TD className="text-xs whitespace-nowrap">
                    <Link to={`/scans/${r.id}`} className="text-primary hover:underline">
                      {new Date(r.created_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}
                    </Link>
                  </TD>
                  <TD className="text-right tabular-nums">{r.findings}</TD>
                  <TD className="text-right tabular-nums text-success">{r.tp}</TD>
                  <TD className="text-right tabular-nums text-danger">{r.fp}</TD>
                  <TD className="text-right tabular-nums text-warning">{r.sbp}</TD>
                  <TD className="text-right tabular-nums">{r.ss}</TD>
                  <TD className="text-right tabular-nums">{r.untriaged}</TD>
                  <TD className="text-right tabular-nums">{r.duplicates}</TD>
                  <TD className="text-xs">
                    <Link to={`/scans/${r.id}`} className="hover:underline" title={r.scan_target}>
                      <span className="block max-w-[18ch] truncate">{r.scan_target || "—"}</span>
                    </Link>
                  </TD>
                  <TD className="text-xs whitespace-nowrap">{r.harness_used || "—"}</TD>
                  <TD className="text-xs whitespace-nowrap">{r.scan_by || "—"}</TD>
                  <TD className="text-xs">
                    {r.source_report_id ? (
                      <Link to={`/reports/${r.source_report_id}`}
                            className="text-primary hover:underline block max-w-[16ch] truncate"
                            title={r.results_file}>
                        {r.results_file || "report"}
                      </Link>
                    ) : (
                      <span className="block max-w-[16ch] truncate" title={r.results_file}>
                        {r.results_file || "—"}
                      </span>
                    )}
                  </TD>
                  <TD className="text-xs">
                    {r.spreadsheet_link ? (
                      <a href={r.spreadsheet_link} target="_blank" rel="noreferrer"
                         className="text-primary hover:underline inline-flex items-center gap-1">
                        <ExternalLink size={10} /> link
                      </a>
                    ) : "—"}
                  </TD>
                  <TD className="text-xs whitespace-nowrap">{r.triaged_by || "—"}</TD>
                  <TD><SeverityChip value={r.highest_severity} /></TD>
                </TR>
              ))}
            </tbody>
            <tfoot>
              <TR className="border-t-2 border-border bg-muted/30 font-medium">
                <TD className="text-xs" colSpan={2}>Totals</TD>
                <TD className="text-right tabular-nums">{totals.findings}</TD>
                <TD className="text-right tabular-nums">{totals.tp}</TD>
                <TD className="text-right tabular-nums">{totals.fp}</TD>
                <TD className="text-right tabular-nums">{totals.sbp}</TD>
                <TD className="text-right tabular-nums">{totals.ss}</TD>
                <TD className="text-right tabular-nums">{totals.untriaged}</TD>
                <TD className="text-right tabular-nums">{totals.duplicates}</TD>
                <TD colSpan={7} />
              </TR>
            </tfoot>
          </Table>
        </CardBody>
      )}
    </Card>
  );
}

const EXAMPLES = [
  {
    label: "All findings spreadsheet",
    text:
      "Give me a CSV with one row per finding across every product. " +
      "Columns: product, scan_rank, severity, title, dev_verdict, ai_verdict, " +
      "tags, cwe, cve, triaged_by.",
  },
  {
    label: "TP/FP rollup",
    text:
      "Build a markdown table summarising per-product counts of TP, FP, " +
      "open, and findings with each tag (SBP / SS / VULN). Include " +
      "a 'total' row at the bottom.",
  },
  {
    label: "Hotlist",
    text:
      "Which TEN findings most need a human's attention right now? " +
      "Prioritize untriaged criticals, AI=TP / dev=open mismatches, " +
      "and tagged VULN. For each, link to the scan + product and explain why.",
  },
];

export function Analytics() {
  const qc = useQueryClient();
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });

  const [scopeAll, setScopeAll] = useState(true);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [prompt, setPrompt] = useState("");
  const [save, setSave] = useState(false);
  const [filename, setFilename] = useState("");
  const [last, setLast] = useState<AnalyticsResponse | null>(null);
  const [lastFilenameHint, setLastFilenameHint] = useState("claude-output");

  const run = useMutation({
    mutationFn: () =>
      api<AnalyticsResponse>("/analytics/chat", {
        method: "POST",
        body: {
          prompt: prompt.trim(),
          product_ids: scopeAll ? null : Array.from(picked),
          save_as_report: save,
          save_filename: save && filename.trim() ? filename.trim() : null,
        },
      }),
    onSuccess: (r) => {
      setLast(r);
      if (save && filename.trim()) setLastFilenameHint(filename.trim());
      else setLastFilenameHint("claude-output");
      if (r.generated_report_id) {
        qc.invalidateQueries({ queryKey: ["reports"] });
      }
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (prompt.trim()) run.mutate();
  }

  function togglePick(id: string) {
    const n = new Set(picked);
    n.has(id) ? n.delete(id) : n.add(id);
    setPicked(n);
  }

  const totalScope = scopeAll
    ? (projects.data?.length ?? 0)
    : picked.size;

  const projOptions = useMemo(() => projects.data || [], [projects.data]);

  return (
    <>
      <PageHeader
        title="Analytics"
        subtitle="Ask Claude questions across one product, several, or every product you can see. Spreadsheets are downloadable."
      />

      <MasterStats />

      <MasterDashboard />

      <Card className="mb-4">
        <CardHeader className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <BarChart3 size={14} className="text-primary" /> Scope &amp; question
          </CardTitle>
          <span className="text-xs text-fgmuted">{totalScope} product{totalScope === 1 ? "" : "s"} in scope</span>
        </CardHeader>
        <CardBody>
          <form onSubmit={submit} className="space-y-3">
            <div>
              <Label>Products</Label>
              <div className="flex items-center gap-2 mb-2">
                <label className="text-sm flex items-center gap-2">
                  <input type="radio" name="scope" checked={scopeAll}
                         onChange={() => setScopeAll(true)} />
                  All products I can see
                </label>
                <label className="text-sm flex items-center gap-2 ml-4">
                  <input type="radio" name="scope" checked={!scopeAll}
                         onChange={() => setScopeAll(false)} />
                  Pick specific products
                </label>
              </div>
              {!scopeAll ? (
                <div className="border border-border rounded-md p-2 max-h-44 overflow-y-auto">
                  {projOptions.length === 0 ? (
                    <p className="text-xs text-fgmuted italic">No products visible.</p>
                  ) : (
                    projOptions.map((p) => (
                      <label key={p.id} className="flex items-center gap-2 text-xs py-0.5 cursor-pointer">
                        <input type="checkbox" checked={picked.has(p.id)}
                               onChange={() => togglePick(p.id)} />
                        <FolderGit2 size={11} className="text-fgmuted" />
                        <span>{p.name}</span>
                      </label>
                    ))
                  )}
                </div>
              ) : null}
            </div>

            <div>
              <Label htmlFor="prompt">Question</Label>
              <Textarea
                id="prompt"
                rows={3}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                placeholder="What do you want to know? Examples below."
              />
              <div className="flex items-center gap-2 mt-2 flex-wrap">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex.label}
                    type="button"
                    onClick={() => setPrompt(ex.text)}
                    className="px-2 py-0.5 rounded-full text-[11px] border border-border text-fgmuted hover:bg-muted hover:text-fg"
                  >
                    {ex.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-end gap-2 flex-wrap">
              <label className="text-sm flex items-center gap-2">
                <input type="checkbox" checked={save} onChange={(e) => setSave(e.target.checked)} />
                Save reply as a report (gives you a stable link to share)
              </label>
              {save ? (
                <div className="flex-1 min-w-[16rem]">
                  <Label htmlFor="fname">Filename</Label>
                  <Input id="fname" value={filename}
                         onChange={(e) => setFilename(e.target.value)}
                         placeholder="all-findings.csv (or .md, .json, …)" />
                </div>
              ) : null}
              <Button type="submit" disabled={run.isPending || !prompt.trim()}>
                <Send size={14} /> {run.isPending ? "Claude is thinking…" : "Run"}
              </Button>
            </div>
            {run.isError ? (
              <p className="text-xs text-danger">Couldn't run analysis.</p>
            ) : null}
          </form>
        </CardBody>
      </Card>

      {last ? (
        <Card>
          <CardHeader className="flex items-center justify-between">
            <CardTitle className="flex items-center gap-2">
              <Sparkles size={14} className="text-primary" /> Result
            </CardTitle>
            <div className="flex items-center gap-2 text-xs text-fgmuted">
              <Badge tone="muted">{last.scope.products} products</Badge>
              <Badge tone="muted">{last.scope.scans} scans</Badge>
              <Badge tone="muted">{last.scope.findings} findings{last.scope.truncated ? " (truncated)" : ""}</Badge>
              <Button variant="ghost" size="sm" onClick={() => setLast(null)}>
                <X size={12} /> clear
              </Button>
            </div>
          </CardHeader>
          <CardBody className="space-y-3">
            <ChatReply text={last.reply} defaultFilename={lastFilenameHint} />
            {last.generated_report_id ? (
              <div className="text-xs text-fgmuted border-t border-border pt-2 flex items-center justify-between gap-3 flex-wrap">
                <span>
                  <Check size={11} className="inline text-success mr-1" />
                  Saved as report <Link to={`/reports/${last.generated_report_id}`} className="text-primary hover:underline">{filename || "(generated)"}</Link>
                </span>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="secondary"
                          onClick={() => navigator.clipboard.writeText(
                            `${window.location.origin}/app/reports/${last.generated_report_id}`
                          )}>
                    Copy share link
                  </Button>
                  <Button size="sm" variant="secondary"
                          onClick={() => downloadFile(
                            `/ui/reports/${last.generated_report_id}/download`,
                            filename || "claude-output",
                          )}>
                    <Download size={12} /> Download
                  </Button>
                </div>
              </div>
            ) : null}
          </CardBody>
        </Card>
      ) : null}
    </>
  );
}
