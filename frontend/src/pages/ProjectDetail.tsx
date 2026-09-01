import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, getToken } from "@/lib/api";
import { useRef } from "react";
import { useAuth } from "@/lib/auth";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge, SeverityChip } from "@/components/ui/Badge";
import { useMemo, useState, type FormEvent, type ReactNode } from "react";
import { ArrowLeft, UserPlus, X, Check, Copy, Link2, Trash2, GitMerge, Settings, ChevronRight, FolderUp, Folder, FileText, Terminal, FileArchive } from "lucide-react";

/**
 * Thin disclosure wrapper for product-page sections users open occasionally
 * (Members, Invite links). Renders as a single compact bar when closed so
 * the page leads with the things that actually drive day-to-day work
 * (findings summary, scans, reports). Uses native <details> so there's no
 * state to manage and keyboard support comes for free.
 */
function Collapsible({
  title, subtitle, children, defaultOpen = false,
}: { title: ReactNode; subtitle?: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  return (
    <details className="group rounded-md border border-border bg-surface" open={defaultOpen || undefined}>
      <summary className="cursor-pointer select-none list-none flex items-center justify-between gap-3 px-3 py-2 hover:bg-muted/30 rounded-md">
        <div className="flex items-center gap-2 min-w-0">
          <ChevronRight size={14} className="text-fgmuted transition-transform group-open:rotate-90 shrink-0" />
          <span className="text-sm font-medium truncate">{title}</span>
          {subtitle ? <span className="text-xs text-fgmuted truncate">{subtitle}</span> : null}
        </div>
      </summary>
      <div className="px-3 pb-3 pt-1 border-t border-border">
        {children}
      </div>
    </details>
  );
}

type Member = { id: string; email: string };
type ProjectDetail = {
  id: string; name: string; description: string;
  created_by: string; created_at: string; updated_at: string;
  members: Member[];
  i_am_member: boolean;
  i_am_owner: boolean;
  can_edit: boolean;
  file_count: number;
  file_bytes: number;
};

export function ProjectDetail() {
  const { project_id = "" } = useParams();
  const qc = useQueryClient();

  const proj = useQuery({
    queryKey: ["project", project_id],
    queryFn: () => api<ProjectDetail>(`/projects/${project_id}`),
  });

  const [email, setEmail] = useState("");
  const [addError, setAddError] = useState<string | null>(null);

  const addMember = useMutation({
    mutationFn: () => api(`/projects/${project_id}/members`, { method: "POST", body: { email } }),
    onSuccess: () => { setEmail(""); setAddError(null); qc.invalidateQueries({ queryKey: ["project", project_id] }); },
    onError: (e: any) => setAddError(e.detail ?? e.message ?? "Failed"),
  });

  function onAdd(e: FormEvent) {
    e.preventDefault();
    if (email.trim()) addMember.mutate();
  }

  if (proj.isLoading) return <div className="text-sm text-fgmuted">Loading…</div>;
  if (proj.isError || !proj.data) return <div className="text-sm text-danger">Product not found or not visible.</div>;
  const p = proj.data;

  return (
    <div className="space-y-5">
      <div>
        <Link to="/products" className="text-xs text-fgmuted hover:text-fg inline-flex items-center gap-1">
          <ArrowLeft size={12} /> Back to products
        </Link>
        <h1 className="text-2xl font-semibold mt-2">{p.name}</h1>
        {p.description ? <p className="text-sm text-fgmuted mt-1">{p.description}</p> : null}
      </div>

      {!p.i_am_member && !p.can_edit ? (
        <Card>
          <CardBody>
            <p className="text-sm">
              <strong>You're not a member of this product.</strong>
            </p>
            <p className="text-xs text-fgmuted mt-1">
              Ask the product's owner to add you to see its members, runs, scans, and reports.
            </p>
          </CardBody>
        </Card>
      ) : (
        <Collapsible title={`Members (${p.members.length})`}>
          <div className="space-y-3">
            <Table>
              <THead>
                <TR><TH>Email</TH><TH className="w-24"></TH></TR>
              </THead>
              <tbody>
                {p.members.map((m) => (
                  <TR key={m.id}>
                    <TD>
                      {m.email}
                      {m.id === p.created_by ? <Badge tone="muted" className="ml-2">creator</Badge> : null}
                    </TD>
                    <TD className="text-right">
                      {p.can_edit && m.id !== p.created_by ? (
                        <RemoveMember projectId={p.id} userId={m.id} email={m.email} />
                      ) : null}
                    </TD>
                  </TR>
                ))}
              </tbody>
            </Table>

            {p.can_edit ? (
              <form onSubmit={onAdd} className="flex items-end gap-2">
                <div className="flex-1">
                  <Label htmlFor="email">Add member by email</Label>
                  <Input id="email" type="email" placeholder="user@example.com"
                         value={email} onChange={(e) => setEmail(e.target.value)} />
                </div>
                <Button type="submit" disabled={addMember.isPending}>
                  <UserPlus size={14} /> Add
                </Button>
              </form>
            ) : (
              <p className="text-xs text-fgmuted">
                Only the product owner (or an admin) can add or remove members.
              </p>
            )}
            {addError ? <p className="text-xs text-danger">{addError}</p> : null}
          </div>
        </Collapsible>
      )}

      {p.can_edit ? <InviteLinksCard projectId={p.id} /> : null}
      {p.can_edit ? <PendingAccessRequests projectId={p.id} /> : null}

      {(p.i_am_member || p.can_edit) ? (
        <>
          <ProductFiles project={p} />
          <UploadToProductCard projectId={p.id} productName={p.name} />
          <ComponentsCard projectId={p.id} />
          <FindingsSummaryCard projectId={p.id} />
          <ProjectScans projectId={p.id} />
          <ProjectReportsWithoutScan projectId={p.id} />
        </>
      ) : null}

      {p.can_edit ? <ProductSettings project={p} /> : null}
      <AdminMergeCard project={p} />

      <p className="text-xs text-fgmuted">
        Tip: attach runs / scans / reports to this product from their detail pages.
      </p>
    </div>
  );
}

// ---------- Shared source files (Workbench library) ----------

type ProjectFile = {
  id: string; relpath: string; size_bytes: number;
  uploaded_by_email: string | null; created_at: string;
};
type ProjectFiles = { count: number; total_bytes: number; files: ProjectFile[] };

type DirNode = {
  dirs: Map<string, DirNode>;
  files: ProjectFile[];
  count: number;
  bytes: number;
};

function buildTree(files: ProjectFile[]): DirNode {
  const root: DirNode = { dirs: new Map(), files: [], count: 0, bytes: 0 };
  for (const f of files) {
    const parts = f.relpath.split("/");
    const name = parts.pop()!;
    let node = root;
    for (const seg of parts) {
      let next = node.dirs.get(seg);
      if (!next) {
        next = { dirs: new Map(), files: [], count: 0, bytes: 0 };
        node.dirs.set(seg, next);
      }
      node = next;
    }
    node.files.push({ ...f, relpath: name });
  }
  const tally = (n: DirNode): [number, number] => {
    let c = n.files.length, b = n.files.reduce((s, f) => s + f.size_bytes, 0);
    for (const d of n.dirs.values()) {
      const [dc, db] = tally(d); c += dc; b += db;
    }
    n.count = c; n.bytes = b;
    return [c, b];
  };
  tally(root);
  return root;
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n >= 10 ? 0 : 1)} ${u[i]}`;
}

function ProductFiles({ project }: { project: ProjectDetail }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["project-files", project.id],
    queryFn: () => api<ProjectFiles>(`/projects/${project.id}/files`),
    enabled: open,
  });
  const tree = useMemo(
    () => (q.data ? buildTree(q.data.files) : null),
    [q.data],
  );

  const del = useMutation({
    mutationFn: (id: string) =>
      api(`/projects/${project.id}/files/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-files", project.id] });
      qc.invalidateQueries({ queryKey: ["project", project.id] });
    },
  });
  const clear = useMutation({
    mutationFn: () => api(`/projects/${project.id}/files`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-files", project.id] });
      qc.invalidateQueries({ queryKey: ["project", project.id] });
    },
  });

  return (
    <details
      className="group rounded-md border border-border bg-surface"
      open={open}
      onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="cursor-pointer select-none list-none flex items-center justify-between gap-3 px-3 py-2 hover:bg-muted/30 rounded-md">
        <div className="flex items-center gap-2 min-w-0">
          <ChevronRight size={14} className="text-fgmuted transition-transform group-open:rotate-90 shrink-0" />
          <span className="text-sm font-medium">Source files</span>
          <span className="text-xs text-fgmuted">
            {project.file_count > 0
              ? `· ${project.file_count.toLocaleString()} files, ${fmtSize(project.file_bytes)}`
              : "· none yet"}
          </span>
        </div>
        <Link
          to="/workbench"
          onClick={(e) => e.stopPropagation()}
          className="text-xs text-primary hover:underline inline-flex items-center gap-1"
        >
          <Terminal size={12} /> Analyze in Workbench
        </Link>
      </summary>
      <div className="px-3 pb-3 pt-2 border-t border-border space-y-2">
        <p className="text-xs text-fgmuted">
          Files uploaded by any member from a Workbench session linked to{" "}
          <strong>{project.name}</strong>. Every new session on this product
          gets these automatically — no re-upload needed.
        </p>
        {q.isLoading ? (
          <div className="text-xs text-fgmuted">Loading file list…</div>
        ) : !tree || tree.count === 0 ? (
          <div className="text-xs text-fgmuted italic">
            No source files yet. In the Workbench, create a session with this
            product selected and use “Upload folder” — files land here for the
            whole team.
          </div>
        ) : (
          <>
            <div className="max-h-96 overflow-y-auto rounded border border-border font-mono text-[11px]">
              <DirView node={tree} depth={0} onDelete={(id) => del.mutate(id)} deleting={del.isPending} />
            </div>
            {project.can_edit ? (
              <div className="flex justify-end">
                <Button
                  variant="ghost" size="sm"
                  className="text-danger hover:bg-danger/10"
                  disabled={clear.isPending}
                  onClick={() => {
                    if (confirm(
                      `Delete all ${tree.count.toLocaleString()} source files from ` +
                      `${project.name}? Every member's Workbench sessions will lose them.`
                    )) clear.mutate();
                  }}
                >
                  <Trash2 size={12} /> Clear all
                </Button>
              </div>
            ) : null}
          </>
        )}
      </div>
    </details>
  );
}

function DirView({
  node, depth, onDelete, deleting,
}: { node: DirNode; depth: number; onDelete: (id: string) => void; deleting: boolean }) {
  const indent = { paddingLeft: `${depth * 14 + 6}px` };
  const dirs = [...node.dirs.entries()].sort(([a], [b]) => a.localeCompare(b));
  return (
    <>
      {dirs.map(([name, d]) => (
        <details key={name} className="group/dir" open={depth === 0 && dirs.length === 1}>
          <summary
            style={indent}
            className="cursor-pointer select-none list-none flex items-center gap-1.5 py-0.5 pr-2 hover:bg-muted/40"
          >
            <ChevronRight size={10} className="text-fgmuted transition-transform group-open/dir:rotate-90 shrink-0" />
            <Folder size={11} className="text-primary shrink-0" />
            <span className="truncate">{name}</span>
            <span className="ml-auto text-fgmuted tabular-nums">
              {d.count.toLocaleString()} · {fmtSize(d.bytes)}
            </span>
          </summary>
          <DirView node={d} depth={depth + 1} onDelete={onDelete} deleting={deleting} />
        </details>
      ))}
      {node.files.map((f) => (
        <div
          key={f.id}
          style={indent}
          className="flex items-center gap-1.5 py-0.5 pr-1 hover:bg-muted/40"
        >
          <span className="w-[10px] shrink-0" />
          <FileText size={11} className="text-fgmuted shrink-0" />
          <span className="truncate" title={f.relpath}>{f.relpath}</span>
          <span className="ml-auto text-fgmuted truncate max-w-28 font-sans">
            {f.uploaded_by_email?.split("@")[0] ?? ""}
          </span>
          <span className="text-fgmuted tabular-nums w-16 text-right">{fmtSize(f.size_bytes)}</span>
          <button
            type="button"
            onClick={() => onDelete(f.id)}
            disabled={deleting}
            className="text-fgmuted hover:text-danger px-1"
            title="Delete from product"
          >
            <X size={11} />
          </button>
        </div>
      ))}
    </>
  );
}

function UploadToProductCard({ projectId, productName }: { projectId: string; productName: string }) {
  const nav = useNavigate();
  const qc = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const zipRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sourceMode, setSourceMode] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  function pick(list: FileList | null) {
    if (!list) return;
    setFiles(Array.from(list));
  }

  // Zips are additive: pick several at once, or click again to add more.
  function addZips(list: FileList | null) {
    if (!list || list.length === 0) return;
    const incoming = Array.from(list);
    setFiles((cur) => {
      const seen = new Set(cur.map((f) => `${f.name}:${f.size}`));
      return [...cur, ...incoming.filter((f) => !seen.has(`${f.name}:${f.size}`))];
    });
  }

  async function upload() {
    if (files.length === 0) return;
    setUploading(true);
    setError(null);
    setDone(null);
    try {
      const tok = getToken();
      if (sourceMode) {
        // Source-code mode: each .zip becomes an AI-identified component.
        const zips = files.filter((f) => f.name.toLowerCase().endsWith(".zip"));
        if (zips.length === 0) throw new Error("Source-code mode needs .zip archives — use “Choose .zip…”.");
        const fd = new FormData();
        for (const f of zips) fd.append("files", f, f.name);
        const resp = await fetch(`/projects/${projectId}/components`, {
          method: "POST",
          headers: tok ? { Authorization: `Bearer ${tok}` } : {},
          body: fd,
        });
        if (!resp.ok) throw new Error(`upload failed (${resp.status}): ${await resp.text()}`);
        const out = await resp.json();
        setFiles([]);
        setUploading(false);
        setDone(`Identified ${out.length} component${out.length === 1 ? "" : "s"}.`);
        qc.invalidateQueries({ queryKey: ["product-components", projectId] });
        qc.invalidateQueries({ queryKey: ["project-files", projectId] });
        return;
      }
      const fd = new FormData();
      fd.append("label", `Upload to ${productName}`);
      fd.append("project_id", projectId);
      for (const f of files) {
        const wkrp = (f as any).webkitRelativePath as string | undefined;
        const rel = wkrp && wkrp.includes("/")
          ? wkrp.split("/").slice(1).join("/")
          : (wkrp || f.name);
        fd.append("relpaths", rel || f.name);
        fd.append("files", f, f.name);
      }
      const resp = await fetch("/imports", {
        method: "POST",
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
        body: fd,
      });
      if (!resp.ok) {
        const text = await resp.text();
        throw new Error(`upload failed (${resp.status}): ${text}`);
      }
      const out = await resp.json();
      nav(`/imports/${out.id}`);
    } catch (e: any) {
      setError(e.message || String(e));
      setUploading(false);
    }
  }

  const total = files.reduce((n, f) => n + f.size, 0);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderUp size={14} className="text-primary" /> Upload files to this product
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={sourceMode}
                 onChange={(e) => { setSourceMode(e.target.checked); setDone(null); }} />
          This is source code
          <span className="text-xs text-fgmuted">— identify each zip as a component with AI</span>
        </label>

        {sourceMode ? (
          <p className="text-xs text-fgmuted">
            Upload one or more <strong>.zip</strong> archives — each becomes a
            component of <strong>{productName}</strong>. The source is stored on
            the product (so scans can use it) and AI identifies what each
            component is in the context of the whole product.
          </p>
        ) : (
          <p className="text-xs text-fgmuted">
            Pick a folder — or a <strong>.zip</strong> of source code — of reports,
            POCs, or notes. A zip is unpacked on the server, keeping its directory
            structure. The configured model will look through everything and propose how to
            organize it — already targeting <strong>{productName}</strong>. You
            review and confirm before anything is saved.
          </p>
        )}

        <input
          ref={inputRef}
          type="file"
          multiple
          // @ts-expect-error — folder picker attrs
          webkitdirectory="" directory=""
          className="hidden"
          onChange={(e) => pick(e.target.files)}
        />
        <input
          ref={zipRef}
          type="file"
          multiple
          accept=".zip,application/zip"
          className="hidden"
          onClick={(e) => { (e.target as HTMLInputElement).value = ""; }}
          onChange={(e) => addZips(e.target.files)}
        />
        <div className="flex items-center gap-2 flex-wrap">
          {!sourceMode ? (
            <Button type="button" variant="secondary" onClick={() => inputRef.current?.click()}>
              <FolderUp size={14} /> Choose folder…
            </Button>
          ) : null}
          <Button type="button" variant="secondary" onClick={() => zipRef.current?.click()}>
            <FileArchive size={14} /> Choose .zip…
          </Button>
          {files.length > 0 ? (
            <span className="text-xs text-fgmuted">
              {files.length} file{files.length === 1 ? "" : "s"}, {(total / 1024).toFixed(1)} KB
            </span>
          ) : (
            <span className="text-xs text-fgmuted">Nothing picked yet.</span>
          )}
          {files.length > 0 ? (
            <Button onClick={upload} disabled={uploading}>
              {uploading
                ? (sourceMode ? "Identifying…" : "Uploading…")
                : (sourceMode ? "Upload & identify components" : "Upload & analyze")}
            </Button>
          ) : null}
        </div>

        {sourceMode && uploading ? (
          <p className="text-xs text-fgmuted">Storing source and asking AI to identify each component — this can take a moment per zip.</p>
        ) : null}
        {done ? <p className="text-sm text-success">{done}</p> : null}
        {error ? <p className="text-sm text-danger">{error}</p> : null}
      </CardBody>
    </Card>
  );
}

type ProductComponent = {
  id: string; name: string; description: string; role: string;
  source_name: string; file_count: number; total_bytes: number;
  ai_rationale: string; created_at: string;
};

function ComponentsCard({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["product-components", projectId],
    queryFn: () => api<ProductComponent[]>(`/projects/${projectId}/components`),
  });
  const del = useMutation({
    mutationFn: (id: string) =>
      api(`/projects/${projectId}/components/${id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["product-components", projectId] });
      qc.invalidateQueries({ queryKey: ["project-files", projectId] });
    },
  });

  const comps = q.data ?? [];
  if (comps.length === 0) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Folder size={14} className="text-primary" /> Components
          <span className="text-xs text-fgmuted font-normal">
            · {comps.length} AI-identified from uploaded source
          </span>
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-2">
        {comps.map((c) => (
          <div key={c.id} className="border border-border rounded-md p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium">{c.name}</div>
                {c.description ? (
                  <div className="text-xs text-fg mt-0.5">{c.description}</div>
                ) : null}
                {c.role ? (
                  <div className="text-xs text-fgmuted mt-1"><span className="uppercase tracking-wider text-[10px]">Role:</span> {c.role}</div>
                ) : null}
                <div className="text-[11px] text-fgmuted mt-1">
                  {c.source_name} · {c.file_count} files · {(c.total_bytes / 1024).toFixed(0)} KB
                </div>
              </div>
              <Button size="sm" variant="ghost" disabled={del.isPending}
                      onClick={() => { if (confirm(`Delete component “${c.name}” and its source files?`)) del.mutate(c.id); }}>
                <Trash2 size={13} />
              </Button>
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

type FindingsSummary = {
  total: number;
  scan_count: number;
  by_status: Record<string, number>;
  by_ai_verdict: Record<string, number>;
  by_tag: Record<string, number>;
  by_severity: Record<string, number>;
};

function FindingsSummaryCard({ projectId }: { projectId: string }) {
  const q = useQuery({
    queryKey: ["product-findings-summary", projectId],
    queryFn: () => api<FindingsSummary>(`/projects/${projectId}/findings/summary`),
  });
  if (q.isLoading || !q.data) return null;
  const s = q.data;
  if (s.total === 0) return null;

  const pct = (a: number, b: number) => (b ? Math.round((a / b) * 100) : 0);
  const findings = s.total;
  const tp = s.by_status.true_positive || 0;
  const fp = s.by_status.false_positive || 0;
  const sbp = s.by_status.sbp || 0;
  const ss = s.by_tag.ss || 0;
  const untriaged = s.by_status.open || 0;
  const triaged = findings - untriaged;
  const triagedPct = pct(triaged, findings);

  // Literal class strings (not interpolated) so Tailwind keeps them.
  const tiles = [
    { label: "Findings",  value: findings,  sub: `${s.scan_count} scan${s.scan_count === 1 ? "" : "s"}`, num: "text-primary", bar: "bg-primary" },
    { label: "TP",        value: tp,        sub: `${pct(tp, findings)}% of findings`,        num: "text-success", bar: "bg-success" },
    { label: "FP",        value: fp,        sub: `${pct(fp, findings)}% of findings`,        num: "text-danger",  bar: "bg-danger" },
    { label: "SBP",       value: sbp,       sub: `${pct(sbp, findings)}% of findings`,       num: "text-warning", bar: "bg-warning" },
    { label: "SS",        value: ss,        sub: `${pct(ss, findings)}% of findings`,        num: "text-fg",      bar: "bg-fgmuted" },
    { label: "Untriaged", value: untriaged, sub: `${pct(untriaged, findings)}% of findings`, num: "text-fg",      bar: "bg-fgmuted" },
  ];

  return (
    <Card>
      <CardHeader className="flex items-center justify-between py-2.5">
        <CardTitle>Findings summary ({s.total} across {s.scan_count} scan{s.scan_count === 1 ? "" : "s"})</CardTitle>
        <Link to={`/products/${projectId}/findings`}>
          <Button size="sm" variant="secondary">View all findings</Button>
        </Link>
      </CardHeader>
      <CardBody className="space-y-3">
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

        {/* Headline: how much of this product's findings have been triaged. */}
        <div className="relative overflow-hidden rounded-lg border border-primary/30 shadow-card bg-gradient-to-r from-primary/10 to-transparent">
          <div className="p-4 flex flex-col md:flex-row md:items-center gap-4">
            <div className="md:w-72 shrink-0">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-fgmuted">Percentage of Triaged</div>
              <div className="mt-1 text-4xl font-bold tabular-nums text-fg">{triagedPct}%</div>
              <div className="mt-0.5 text-xs text-fgmuted">
                {triaged.toLocaleString()} of {findings.toLocaleString()} findings triaged
              </div>
            </div>
            <div className="flex-1 w-full">
              <div className="h-3 rounded-full bg-muted overflow-hidden">
                <div className="h-full bg-success transition-all" style={{ width: `${triagedPct}%` }} />
              </div>
              <div className="mt-1.5 flex justify-between text-[11px] text-fgmuted">
                <span>{triaged.toLocaleString()} triaged</span>
                <span>{untriaged.toLocaleString()} untriaged</span>
              </div>
            </div>
          </div>
        </div>
      </CardBody>
    </Card>
  );
}

type ScanRow = {
  id: string; product: string; scan_target: string;
  findings: number; tp: number; fp: number; sbp: number; untriaged: number;
  highest_severity: string; state: string; created_at: string;
};

function ProjectScans({ projectId }: { projectId: string }) {
  const q = useQuery({
    queryKey: ["project-scans", projectId],
    queryFn: () => api<ScanRow[]>(`/scans?project_id=${encodeURIComponent(projectId)}`),
  });
  // Stable "Scan #N" label per scan: rank by created_at ASC so the oldest
  // scan is always #1, even when we display newest-first. The server
  // returns scans in some order (currently descending) — we don't depend
  // on that for ranking.
  const ranks = useMemo(() => {
    const rows = q.data || [];
    const ordered = [...rows].sort(
      (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
    );
    const m = new Map<string, number>();
    ordered.forEach((s, i) => m.set(s.id, i + 1));
    return m;
  }, [q.data]);

  return (
    <Card>
      <CardHeader><CardTitle>Scans ({q.data?.length ?? 0})</CardTitle></CardHeader>
      {!q.data || q.data.length === 0 ? (
        <CardBody><p className="text-sm text-fgmuted">No scans in this product yet.</p></CardBody>
      ) : (
        <Table>
          <THead>
            <TR>
              <TH>Scan</TH><TH>Target</TH><TH>Findings</TH>
              <TH>TP / FP / SBP</TH><TH>Untriaged</TH><TH>Severity</TH><TH>State</TH>
            </TR>
          </THead>
          <tbody>
            {q.data.map((s) => (
              <TR key={s.id} className="hover:bg-muted/40">
                <TD>
                  <Link to={`/scans/${s.id}`} className="text-primary hover:underline font-medium">
                    Scan #{ranks.get(s.id) ?? "?"}
                  </Link>
                  {s.product ? (
                    <div className="text-[11px] text-fgmuted mt-0.5 truncate" title={s.product}>
                      {s.product}
                    </div>
                  ) : null}
                </TD>
                <TD className="text-fgmuted text-xs">{s.scan_target}</TD>
                <TD className="tabular-nums">{s.findings}</TD>
                <TD className="text-xs tabular-nums">
                  <span className="text-success">{s.tp}</span> /
                  {" "}<span className="text-danger">{s.fp}</span> /
                  {" "}<span className="text-warning">{s.sbp}</span>
                </TD>
                <TD className="tabular-nums">{s.untriaged}</TD>
                <TD><SeverityChip value={s.highest_severity} /></TD>
                <TD><Badge tone={s.state === "draft" ? "warning" : "success"}>{s.state}</Badge></TD>
              </TR>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

type ReportRow = {
  id: string; filename: string; title: string;
  source_tool: string;
  created_at: string; summary: string | null;
  agent_hostname: string | null; owner_email: string | null;
  derived_scan_id: string | null;
};

function ProjectReportsWithoutScan({ projectId }: { projectId: string }) {
  // Show only the reports that didn't end up tied to a scan — anything
  // with a derived_scan_id will already appear under that scan.
  const q = useQuery({
    queryKey: ["project-reports", projectId],
    queryFn: () => api<ReportRow[]>(`/reports?project_id=${encodeURIComponent(projectId)}&limit=200`),
  });
  const stray = (q.data || []).filter((r) => !r.derived_scan_id);
  if (q.isLoading) return null;
  if (stray.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Reports without a linked scan ({stray.length})</CardTitle>
      </CardHeader>
      <CardBody className="pb-1">
        <p className="text-xs text-fgmuted -mt-1 mb-2">
          These are uploaded reports that aren't yet tied to a scan. Open one
          and use the "Linked scan" picker to attach it.
        </p>
      </CardBody>
      <Table>
        <THead>
          <TR>
            <TH>Title</TH><TH>Tool</TH><TH>By</TH><TH>When</TH><TH className="w-1/3">Summary</TH>
          </TR>
        </THead>
        <tbody>
          {stray.map((r) => (
            <TR key={r.id} className="hover:bg-muted/40">
              <TD>
                <Link to={`/reports/${r.id}`} className="text-primary hover:underline font-medium">
                  {r.title || <span className="text-fgmuted italic">(untitled)</span>}
                </Link>
                <div className="text-[11px] text-fgmuted font-mono truncate max-w-[30ch]" title={r.filename}>
                  {r.filename}
                </div>
              </TD>
              <TD><Badge tone="muted">{r.source_tool}</Badge></TD>
              <TD className="text-fgmuted text-xs">{r.owner_email || "—"}</TD>
              <TD className="text-fgmuted text-xs whitespace-nowrap">
                {new Date(r.created_at).toLocaleString(undefined,{dateStyle:"medium",timeStyle:"short"})}
              </TD>
              <TD className="text-xs text-fgmuted">
                <div className="max-w-[50ch] truncate" title={r.summary || ""}>{r.summary || "—"}</div>
              </TD>
            </TR>
          ))}
        </tbody>
      </Table>
    </Card>
  );
}

type Invite = {
  id: string;
  project_id: string;
  token: string;
  expires_at: string | null;
  max_uses: number | null;
  uses_count: number;
  revoked_at: string | null;
  note: string;
  created_at: string;
};

function InviteLinksCard({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["project-invites", projectId],
    queryFn: () => api<Invite[]>(`/projects/${projectId}/invites`),
  });

  const [expiresDays, setExpiresDays] = useState<string>("30");
  const [maxUses, setMaxUses] = useState<string>("");
  const [note, setNote] = useState<string>("");

  const create = useMutation({
    mutationFn: () =>
      api<Invite>(`/projects/${projectId}/invites`, {
        method: "POST",
        body: {
          expires_in_days: expiresDays.trim() === "" ? null : Number(expiresDays),
          max_uses: maxUses.trim() === "" ? null : Number(maxUses),
          note: note.trim(),
        },
      }),
    onSuccess: () => {
      setNote("");
      qc.invalidateQueries({ queryKey: ["project-invites", projectId] });
    },
  });

  const revoke = useMutation({
    mutationFn: (id: string) =>
      api(`/projects/${projectId}/invites/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project-invites", projectId] }),
  });

  const active = (q.data || []).filter((i) => !i.revoked_at);

  return (
    <Collapsible
      title={<span className="inline-flex items-center gap-1.5"><Link2 size={12} /> Invite links</span>}
      subtitle={`· ${active.length} active`}
    >
      <div className="space-y-3 pt-1">
        <p className="text-xs text-fgmuted">
          Anyone with the link can join this product (after signing up if needed). Defaults to 30-day expiry and unlimited uses; tune per-link as you mint.
        </p>

        <div className="grid grid-cols-1 md:grid-cols-[8rem_8rem_1fr_auto] gap-2 items-end">
          <div>
            <Label htmlFor="exp">Expiry (days)</Label>
            <Input id="exp" inputMode="numeric" value={expiresDays}
                   onChange={(e) => setExpiresDays(e.target.value)} placeholder="30 (blank = never)" />
          </div>
          <div>
            <Label htmlFor="maxu">Max uses</Label>
            <Input id="maxu" inputMode="numeric" value={maxUses}
                   onChange={(e) => setMaxUses(e.target.value)} placeholder="blank = ∞" />
          </div>
          <div>
            <Label htmlFor="note">Note (optional)</Label>
            <Input id="note" value={note} onChange={(e) => setNote(e.target.value)}
                   placeholder="e.g. 'Acme Gateway auditor cohort'" />
          </div>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>
            <Link2 size={14} /> {create.isPending ? "Generating…" : "Generate"}
          </Button>
        </div>

        {active.length === 0 ? (
          <p className="text-xs text-fgmuted italic">No active invite links yet.</p>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>URL</TH><TH>Uses</TH><TH>Expires</TH><TH>Note</TH><TH className="w-12"></TH>
              </TR>
            </THead>
            <tbody>
              {active.map((inv) => {
                const url = `${window.location.origin}/app/join/${inv.token}`;
                const usesLabel = inv.max_uses == null
                  ? `${inv.uses_count} / ∞`
                  : `${inv.uses_count} / ${inv.max_uses}`;
                const expiresLabel = inv.expires_at
                  ? new Date(inv.expires_at).toLocaleDateString()
                  : "never";
                return (
                  <TR key={inv.id} className="hover:bg-muted/40">
                    <TD>
                      <div className="flex items-center gap-2">
                        <Input readOnly value={url}
                               className="font-mono text-[11px]"
                               onClick={(e) => (e.target as HTMLInputElement).select()} />
                        <CopyInline text={url} />
                      </div>
                    </TD>
                    <TD className="tabular-nums text-xs">{usesLabel}</TD>
                    <TD className="text-xs text-fgmuted">{expiresLabel}</TD>
                    <TD className="text-xs text-fgmuted">{inv.note || "—"}</TD>
                    <TD className="text-right">
                      <Button variant="ghost" size="sm"
                              onClick={() => { if (confirm("Revoke this invite?")) revoke.mutate(inv.id); }}
                              disabled={revoke.isPending}>
                        <Trash2 size={12} />
                      </Button>
                    </TD>
                  </TR>
                );
              })}
            </tbody>
          </Table>
        )}
        {create.isError ? <p className="text-xs text-danger">Couldn't mint invite.</p> : null}
      </div>
    </Collapsible>
  );
}

function CopyInline({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <Button variant="secondary" size="sm" onClick={() => {
      navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    }}>
      <Copy size={12} /> {done ? "Copied" : "Copy"}
    </Button>
  );
}

type AccessRequest = {
  id: string;
  project_id: string;
  user_id: string;
  user_email: string;
  status: "pending" | "approved" | "denied" | "cancelled";
  reason: string;
  import_id: string | null;
  import_file_count: number;
  import_status: string | null;
  created_at: string;
};

function PendingAccessRequests({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["project-access-requests", projectId],
    queryFn: () =>
      api<AccessRequest[]>(
        `/project_requests?project_id=${encodeURIComponent(projectId)}&status_filter=pending`
      ),
  });

  const approve = useMutation({
    mutationFn: (id: string) =>
      api(`/project_requests/${id}/approve`, { method: "POST", body: { reason: "" } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-access-requests", projectId] });
      qc.invalidateQueries({ queryKey: ["project", projectId] });
    },
  });
  const deny = useMutation({
    mutationFn: (id: string) =>
      api(`/project_requests/${id}/deny`, { method: "POST", body: { reason: "" } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project-access-requests", projectId] });
    },
  });

  const rows = q.data || [];
  if (q.isLoading || rows.length === 0) return null;

  return (
    <Card className="border-primary/40">
      <CardHeader>
        <CardTitle>Pending access requests ({rows.length})</CardTitle>
      </CardHeader>
      <CardBody className="space-y-2">
        {rows.map((r) => (
          <div key={r.id} className="border border-border rounded-md p-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium">{r.user_email}</div>
                {r.reason ? (
                  <div className="text-xs text-fgmuted mt-1 whitespace-pre-wrap">"{r.reason}"</div>
                ) : (
                  <div className="text-xs text-fgmuted italic mt-1">(no reason provided)</div>
                )}
                {r.import_id ? (
                  <div className="text-xs mt-2 px-2 py-1 rounded bg-primary/5 border border-primary/30">
                    <Link to={`/imports/${r.import_id}`} className="text-primary hover:underline">
                      📂 Attached import — {r.import_file_count} file{r.import_file_count === 1 ? "" : "s"}
                      {r.import_status ? ` (${r.import_status})` : ""}
                    </Link>
                    <div className="text-[11px] text-fgmuted mt-0.5">
                      Approving will also import these files into this project automatically.
                    </div>
                  </div>
                ) : null}
                <div className="text-[11px] text-fgmuted/70 mt-1">
                  asked {new Date(r.created_at).toLocaleString()}
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <Button size="sm" onClick={() => approve.mutate(r.id)} disabled={approve.isPending}>
                  <Check size={12} /> {r.import_id ? "Approve & import" : "Approve"}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => deny.mutate(r.id)} disabled={deny.isPending}>
                  <X size={12} /> Deny
                </Button>
              </div>
            </div>
          </div>
        ))}
      </CardBody>
    </Card>
  );
}

function ProductSettings({ project }: { project: ProjectDetail }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description);
  const [savedAt, setSavedAt] = useState(0);

  const save = useMutation({
    mutationFn: () =>
      api(`/projects/${project.id}`, {
        method: "PATCH",
        body: { name, description },
      }),
    onSuccess: () => {
      setSavedAt(Date.now());
      setTimeout(() => setSavedAt(0), 2400);
      qc.invalidateQueries({ queryKey: ["project", project.id] });
      qc.invalidateQueries({ queryKey: ["projects"] });
    },
  });

  const destroy = useMutation({
    mutationFn: () => api(`/projects/${project.id}`, { method: "DELETE" }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      nav("/products");
    },
  });

  const dirty = name !== project.name || description !== project.description;
  const justSaved = Date.now() - savedAt < 2400;

  return (
    <Card>
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <Settings size={14} /> Product settings
        </CardTitle>
        <div className="flex items-center gap-2">
          {justSaved ? (
            <span className="text-xs text-success inline-flex items-center gap-1">
              <Check size={12} /> Saved!
            </span>
          ) : dirty ? (
            <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save"}
            </Button>
          ) : null}
        </div>
      </CardHeader>
      <CardBody className="space-y-3">
        <div>
          <Label htmlFor="ps-name">Name</Label>
          <Input id="ps-name" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="ps-desc">Description</Label>
          <Textarea id="ps-desc" rows={2} value={description}
                    onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="pt-2 border-t border-border flex items-center justify-between gap-3">
          <p className="text-xs text-fgmuted">
            Deleting clears the product but leaves orphaned scans/reports under "No product".
          </p>
          <Button
            variant="ghost"
            size="sm"
            className="text-danger hover:bg-danger/10"
            onClick={() => {
              if (confirm(`Delete product '${project.name}'? This cannot be undone.`)) {
                destroy.mutate();
              }
            }}
            disabled={destroy.isPending}
          >
            <Trash2 size={12} /> Delete product
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

function AdminMergeCard({ project }: { project: ProjectDetail }) {
  const { me } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const others = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Array<{ id: string; name: string }>>("/projects"),
    enabled: me?.role === "admin",
  });
  const [intoId, setIntoId] = useState("");

  const merge = useMutation({
    mutationFn: () =>
      api(`/projects/${project.id}/merge`, {
        method: "POST",
        body: { into_id: intoId },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      // Source is gone — jump to the target.
      nav(`/products/${intoId}`);
    },
  });

  if (me?.role !== "admin") return null;
  const candidates = (others.data || []).filter((p) => p.id !== project.id);
  const targetName = candidates.find((p) => p.id === intoId)?.name;

  return (
    <Card className="border-warning/40">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <GitMerge size={14} className="text-warning" /> Admin: merge product
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-xs text-fgmuted">
          Moves every report, scan, run, harness, invite link, and pending
          access request from <strong>{project.name}</strong> into the target
          product, unions their members, then deletes the source. Members of
          both products get a notification.
        </p>
        <div className="flex items-end gap-2">
          <div className="flex-1">
            <Label htmlFor="merge-target">Merge into…</Label>
            <Select id="merge-target" value={intoId} onChange={(e) => setIntoId(e.target.value)}>
              <option value="">— pick target —</option>
              {candidates.map((p) => (
                <option key={p.id} value={p.id}>{p.name}</option>
              ))}
            </Select>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="text-warning hover:bg-warning/10"
            disabled={!intoId || merge.isPending}
            onClick={() => {
              if (!targetName) return;
              if (confirm(
                `Merge '${project.name}' into '${targetName}'?\n\n` +
                "This deletes the source product and reassigns all its data. " +
                "It cannot be undone."
              )) merge.mutate();
            }}
          >
            <GitMerge size={12} /> {merge.isPending ? "Merging…" : "Merge"}
          </Button>
        </div>
        {merge.isError ? (
          <p className="text-xs text-danger">Merge failed.</p>
        ) : null}
      </CardBody>
    </Card>
  );
}

function RemoveMember({ projectId, userId, email }: { projectId: string; userId: string; email: string }) {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: () => api(`/projects/${projectId}/members/${userId}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["project", projectId] }),
  });
  return (
    <Button
      variant="ghost" size="sm"
      onClick={() => {
        if (confirm(`Remove ${email} from this project?`)) m.mutate();
      }}
      disabled={m.isPending}
    >
      <X size={12} /> remove
    </Button>
  );
}
