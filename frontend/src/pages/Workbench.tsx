import {
  useEffect, useMemo, useRef, useState, useSyncExternalStore,
  type DragEvent, type FormEvent,
} from "react";
import {
  useMutation, useQuery, useQueryClient, type QueryClient,
} from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  Terminal, Plus, Send, Loader2, Wrench, CornerDownRight,
  CheckCircle2, AlertCircle, Cpu, Circle, Archive, Pencil, Save, Package,
  Paperclip, FolderUp, X, Box, UploadCloud, MessageSquareText,
} from "lucide-react";
import { api, apiUpload } from "@/lib/api";
import { cn } from "@/lib/cn";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";

type Agent = { id: string; hostname: string; last_seen: string | null; last_ip: string | null };
type Project = { id: string; name: string };
type Harness = { id: string; name: string; file_count: number; project_name: string | null };

type SessionRow = {
  id: string; agent_id: string; title: string; cwd: string | null;
  status: "idle" | "running" | "archived"; claude_session_id: string | null;
  project_id: string | null; project_name: string | null;
  harness_id: string | null; harness_name: string | null; model: string | null;
  pending_bundle: boolean;
  turn_count: number; upload_count: number; upload_bytes: number;
  created_at: string; last_activity_at: string;
};

type UploadRow = {
  id: string; relpath: string; size_bytes: number;
  content_type: string; sha256: string; created_at: string;
  source: "session" | "project"; uploaded_by_email?: string | null;
};

type Ev =
  | { type: "launch"; cwd: string; resume: boolean; model?: string | null; active_testing?: boolean }
  | { type: "system"; subtype?: string; session_id?: string; model?: string; cwd?: string }
  | { type: "assistant"; content: AssistantBlock[] }
  | { type: "tool_result"; results: { is_error: boolean; preview: string }[] }
  | { type: "result"; subtype?: string; duration_ms?: number; total_cost_usd?: number; num_turns?: number }
  | { type: "bundle"; phase: "download" | "extract" | "ready"; dest: string; files?: number }
  | { type: "error" | "text" | "truncated"; text?: string };

// Models offered in the session picker. "" = let the agent/CLI default decide.
// The chosen id is passed straight to `claude --model` on the agent host, so it
// must be a model that host's CLI/account can serve.
const MODEL_OPTIONS: { value: string; label: string }[] = [
  { value: "", label: "Agent default" },
  { value: "claude-opus-4-8", label: "Opus 4.8" },
  { value: "claude-sonnet-4-6", label: "Sonnet 4.6" },
  { value: "claude-haiku-4-5", label: "Haiku 4.5" },
];

type AssistantBlock =
  | { type: "text"; text: string }
  | { type: "thinking"; text: string }
  | { type: "tool_use"; name: string; hint: string };

type Turn = {
  request_id: string; status: "pending" | "running" | "done" | "error";
  prompt: string; cwd: string | null; output: string | null;
  events: Ev[]; error: string | null; created_at: string; completed_at: string | null;
};

type SessionDetail = SessionRow & { turns: Turn[]; uploads: UploadRow[] };

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const u = ["KB", "MB", "GB"]; let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return `${n.toFixed(n >= 10 ? 0 : 1)} ${u[i]}`;
}

const IGNORE_DIRS = new Set([
  ".git", ".hg", ".svn",
  "node_modules", "bower_components", "Pods",
  "__pycache__", ".venv", "venv", ".tox",
  ".mypy_cache", ".pytest_cache", ".ruff_cache",
  ".idea", ".vscode",
  "dist", "build", "out", ".next", ".nuxt", ".cache",
  "target", "coverage", ".gradle", ".terraform",
  "vendor", "third_party", "3rdparty",
]);
const IGNORE_FILES = new Set([".DS_Store", "Thumbs.db"]);
const UPLOAD_MAX_TOTAL = 5 * 1024 * 1024 * 1024;

type Picked = {
  keep: { file: File; rel: string }[];
  skipped: number; bytes: number; byTop: Map<string, number>;
};

function pickUploads(files: File[]): Picked {
  const keep: { file: File; rel: string }[] = [];
  const byTop = new Map<string, number>();
  let skipped = 0, bytes = 0;
  for (const f of files) {
    const wkrp = (f as any).webkitRelativePath as string | undefined;
    const rel = wkrp || f.name;
    const parts = rel.split("/");
    const base = parts[parts.length - 1];
    if (IGNORE_FILES.has(base) || base.endsWith(".pyc")
        || parts.some((p) => IGNORE_DIRS.has(p))) {
      skipped++; continue;
    }
    keep.push({ file: f, rel });
    bytes += f.size;
    // group under second segment when a folder was picked (first segment
    // is the folder's own name); otherwise the file itself
    const top = parts.length > 2 ? `${parts[1]}/`
              : parts.length === 2 ? parts[1] : parts[0];
    byTop.set(top, (byTop.get(top) ?? 0) + f.size);
  }
  return { keep, skipped, bytes, byTop };
}

function breakdown(p: Picked): string {
  const top = [...p.byTop.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6)
    .map(([k, v]) => `  ${k}  ${fmtBytes(v)}`).join("\n");
  return `${fmtBytes(p.bytes)} across ${p.keep.length} files exceeds the `
       + `${fmtBytes(UPLOAD_MAX_TOTAL)} limit.\nHeaviest entries:\n${top}\n`
       + `Try uploading just the relevant subdirectory, or zip it first.`;
}

type UploadProgress = {
  files: number; skipped: number; bytes: number; sent: number; total: number;
};

// --- module-level upload tracker ------------------------------------------
// XHR + progress live here, not in component state, so navigating between
// sessions doesn't drop an in-flight upload or lose the progress bar.

type UploadState = UploadProgress & { error?: string };

const _uploads = new Map<string, UploadState>();
const _upSubs = new Set<() => void>();
const _upSubscribe = (cb: () => void) => {
  _upSubs.add(cb);
  return () => { _upSubs.delete(cb); };
};
function _upSet(sid: string, s: UploadState | undefined) {
  if (s) _uploads.set(sid, s); else _uploads.delete(sid);
  _upSubs.forEach((f) => f());
}

function startSessionUpload(
  qc: QueryClient, sid: string, files: File[], already: number,
) {
  const picked = pickUploads(files);
  const { keep, skipped, bytes } = picked;
  const base = { files: keep.length, skipped, bytes, sent: 0, total: 0 };
  if (keep.length === 0) {
    _upSet(sid, { ...base, error: skipped
      ? `all ${skipped} files were ignored (vendored/VCS dirs)` : "no files" });
    return;
  }
  if (already + bytes > UPLOAD_MAX_TOTAL) {
    _upSet(sid, { ...base, error: breakdown(picked) });
    return;
  }
  _upSet(sid, base);
  const fd = new FormData();
  for (const { file, rel } of keep) {
    fd.append("relpaths", rel);
    fd.append("files", file, file.name);
  }
  apiUpload<SessionRow>(`/remote/sessions/${sid}/files`, fd, (sent, total) => {
    const cur = _uploads.get(sid);
    if (cur && !cur.error) _upSet(sid, { ...cur, sent, total });
  }).then(
    () => _upSet(sid, undefined),
    (e) => _upSet(sid, {
      ...(_uploads.get(sid) ?? base),
      error: String((e as any)?.detail ?? (e as Error)?.message ?? e),
    }),
  ).finally(() => {
    qc.invalidateQueries({ queryKey: ["remote-session", sid] });
    qc.invalidateQueries({ queryKey: ["remote-sessions"] });
  });
}

const _pendingSnapshot = () =>
  [..._uploads.entries()].filter(([, v]) => !v.error)
    .map(([k]) => k).sort().join(",");

function useUploadingSet(): Set<string> {
  const snap = useSyncExternalStore(_upSubscribe, _pendingSnapshot);
  return useMemo(() => new Set(snap ? snap.split(",") : []), [snap]);
}

function useSessionUpload(sid: string) {
  const qc = useQueryClient();
  const state = useSyncExternalStore(_upSubscribe, () => _uploads.get(sid));
  const isPending = !!state && state.error === undefined;
  return {
    progress: isPending ? (state as UploadProgress) : null,
    error: state?.error ?? null,
    isPending,
    start: (files: File[], already: number) =>
      startSessionUpload(qc, sid, files, already),
  };
}

// ---------------------------------------------------------------------------

export function Workbench() {
  const nav = useNavigate();
  const { sid } = useParams<{ sid?: string }>();

  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api<Agent[]>("/agents"),
  });
  const sessions = useQuery({
    queryKey: ["remote-sessions"],
    queryFn: () => api<SessionRow[]>("/remote/sessions"),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((s) => s.status === "running") ? 3000 : false,
  });

  const agentById = useMemo(
    () => Object.fromEntries((agents.data ?? []).map((a) => [a.id, a])),
    [agents.data],
  );

  return (
    <>
      <PageHeader
        title="Workbench"
        subtitle="Drive Claude on your own machine — multiple live sessions, full conversation history."
        action={<NewSessionButton agents={agents.data ?? []}
                  onCreated={(id) => nav(`/workbench/${id}`)} />}
      />

      {agents.data && agents.data.length === 0 ? (
        <Card><CardBody className="text-sm text-fgmuted">
          No agent registered yet. <Link to="/agents" className="text-primary hover:underline">
          Install one</Link> on your machine to start a session.
        </CardBody></Card>
      ) : (
        <div className="grid grid-cols-12 gap-5">
          <SessionList
            sessions={sessions.data ?? []}
            agentById={agentById}
            activeId={sid}
            onPick={(id) => nav(`/workbench/${id}`)}
          />
          <div className="col-span-12 lg:col-span-9 min-w-0">
            {sid ? (
              <SessionView key={sid} sid={sid} agentById={agentById} />
            ) : (
              <Card className="h-[60vh] flex items-center justify-center">
                <div className="text-center text-fgmuted text-sm">
                  <Terminal className="mx-auto mb-2 text-fgmuted/60" />
                  Pick a session on the left, or start a new one.
                </div>
              </Card>
            )}
          </div>
        </div>
      )}
    </>
  );
}

// ---- left rail -------------------------------------------------------------

function SessionList({ sessions, agentById, activeId, onPick }: {
  sessions: SessionRow[]; agentById: Record<string, Agent>;
  activeId?: string; onPick: (id: string) => void;
}) {
  const uploading = useUploadingSet();
  return (
    <Card className="col-span-12 lg:col-span-3 max-h-[75vh] overflow-y-auto">
      <div className="px-3 py-2.5 border-b border-border text-xs font-semibold
                      uppercase tracking-wider text-fgmuted">
        Sessions
      </div>
      {sessions.length === 0 ? (
        <div className="p-4 text-xs text-fgmuted">No sessions yet.</div>
      ) : null}
      {sessions.map((s) => {
        const a = agentById[s.agent_id];
        return (
          <button
            key={s.id}
            type="button"
            onClick={() => onPick(s.id)}
            className={cn(
              "w-full text-left px-3 py-2.5 border-b border-border/60 last:border-0",
              "hover:bg-muted transition-colors",
              activeId === s.id && "bg-primary/10",
            )}
          >
            <div className="flex items-center gap-2 min-w-0">
              <StatusDot status={s.status} />
              <div className="flex-1 truncate text-sm">
                {s.title || <span className="text-fgmuted">Untitled</span>}
              </div>
              {uploading.has(s.id) ? (
                <UploadCloud size={12}
                  className="text-primary animate-pulse" />
              ) : null}
              {s.turn_count > 0 ? (
                <span className="text-[10px] text-fgmuted">{s.turn_count}</span>
              ) : null}
            </div>
            <div className="text-[11px] text-fgmuted/80 mt-0.5 truncate">
              {a?.hostname || "unknown agent"}
              {s.cwd ? <> · <code className="text-[10px]">{s.cwd}</code></> : null}
            </div>
          </button>
        );
      })}
    </Card>
  );
}

function StatusDot({ status }: { status: SessionRow["status"] }) {
  const cls = status === "running"
    ? "text-primary animate-pulse"
    : status === "archived" ? "text-fgmuted/50" : "text-success";
  return <Circle size={8} className={cn("fill-current shrink-0", cls)} />;
}

// ---- new session -----------------------------------------------------------

function NewSessionButton({ agents, onCreated }: {
  agents: Agent[]; onCreated: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [cwd, setCwd] = useState("");
  const [projectId, setProjectId] = useState("");
  const [harnessId, setHarnessId] = useState("");
  const [model, setModel] = useState("");

  const harnesses = useQuery({
    queryKey: ["harnesses"],
    queryFn: () => api<Harness[]>("/harnesses"),
    enabled: open,
  });
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
    enabled: open,
  });

  useEffect(() => {
    if (!agentId && agents[0]) setAgentId(agents[0].id);
  }, [agents, agentId]);

  const create = useMutation({
    mutationFn: () =>
      api<SessionRow>("/remote/sessions", {
        method: "POST",
        body: {
          agent_id: agentId, cwd: cwd || null,
          project_id: projectId || null, harness_id: harnessId || null,
          model: model || null,
        },
      }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: ["remote-sessions"] });
      setOpen(false);
      onCreated(s.id);
    },
  });

  if (!open) {
    return (
      <Button onClick={() => setOpen(true)} disabled={agents.length === 0}>
        <Plus size={16} /> New session
      </Button>
    );
  }
  return (
    <form
      className="flex items-end gap-2"
      onSubmit={(e) => { e.preventDefault(); if (agentId) create.mutate(); }}
    >
      <div>
        <Label>Machine</Label>
        <Select value={agentId} onChange={(e) => setAgentId(e.target.value)}>
          {agents.map((a) => (
            <option key={a.id} value={a.id}>
              {a.hostname}
              {a.last_ip ? ` · ${a.last_ip}` : ""}
              {a.last_seen ? "" : " (never seen)"}
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label>Product <span className="opacity-60">(optional)</span></Label>
        <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
          <option value="">None</option>
          {(projects.data ?? []).map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </Select>
      </div>
      <div>
        <Label>Harness <span className="opacity-60">(optional)</span></Label>
        <Select value={harnessId} onChange={(e) => setHarnessId(e.target.value)}>
          <option value="">None</option>
          {(harnesses.data ?? []).map((h) => (
            <option key={h.id} value={h.id}>
              {h.name} ({h.file_count} files{h.project_name ? ` · ${h.project_name}` : ""})
            </option>
          ))}
        </Select>
      </div>
      <div>
        <Label>Model</Label>
        <Select value={model} onChange={(e) => setModel(e.target.value)}>
          {MODEL_OPTIONS.map((m) => (
            <option key={m.value} value={m.value}>{m.label}</option>
          ))}
        </Select>
      </div>
      <div>
        <Label>Working dir <span className="opacity-60">(optional)</span></Label>
        <Input value={cwd} onChange={(e) => setCwd(e.target.value)}
               disabled={!!harnessId}
               placeholder={harnessId ? "set by harness" : "~/src/my-project/..."} />
      </div>
      <Button type="submit" disabled={create.isPending}>
        {create.isPending ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}
        Start
      </Button>
      <Button type="button" variant="ghost" onClick={() => setOpen(false)}>Cancel</Button>
    </form>
  );
}

// ---- main chat panel -------------------------------------------------------

function SessionView({ sid, agentById }: {
  sid: string; agentById: Record<string, Agent>;
}) {
  const qc = useQueryClient();
  const detail = useQuery({
    queryKey: ["remote-session", sid],
    queryFn: () => api<SessionDetail>(`/remote/sessions/${sid}`),
    refetchInterval: (q) => {
      const d = q.state.data;
      if (!d) return false;
      const hot = d.status === "running" ||
        d.turns.some((t) => t.status === "pending" || t.status === "running");
      return hot ? 1500 : false;
    },
  });

  const send = useMutation({
    mutationFn: (prompt: string) =>
      api<{ request_id: string }>(`/remote/sessions/${sid}/prompt`, {
        method: "POST", body: { prompt },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["remote-session", sid] });
      qc.invalidateQueries({ queryKey: ["remote-sessions"] });
    },
  });

  const archive = useMutation({
    mutationFn: () =>
      api(`/remote/sessions/${sid}`, { method: "PATCH", body: { archived: true } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["remote-sessions"] }),
  });

  const up = useSessionUpload(sid);
  const doUpload = (files: File[]) =>
    up.start(files, detail.data?.upload_bytes ?? 0);

  const [showFiles, setShowFiles] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const onDrop = (e: DragEvent) => {
    e.preventDefault(); setDragOver(false);
    const fs = Array.from(e.dataTransfer.files);
    if (fs.length) { doUpload(fs); setShowFiles(true); }
  };

  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [detail.data?.turns.length,
      detail.data?.turns.at(-1)?.events.length,
      detail.data?.turns.at(-1)?.status]);

  if (detail.isLoading) {
    return <Card className="p-6 text-sm text-fgmuted">Loading…</Card>;
  }
  if (detail.isError || !detail.data) {
    return <Card className="p-6 text-sm text-danger">
      {(detail.error as Error)?.message || "Session not found."}
    </Card>;
  }

  const s = detail.data;
  const agent = agentById[s.agent_id];
  const busy = s.status === "running";
  const lastTurn = s.turns.at(-1);

  return (
    <Card className="flex flex-col h-[75vh]">
      <div className="px-4 py-3 border-b border-border flex items-center gap-3">
        <StatusDot status={s.status} />
        <div className="min-w-0 flex-1">
          <TitleEdit s={s} />
          <div className="text-[11px] text-fgmuted/80 truncate">
            {agent?.hostname || "unknown"}
            {agent?.last_ip ? ` · ${agent.last_ip}` : null}
            {s.cwd ? <> · <code>{s.cwd}</code></> : null}
            {s.claude_session_id ? " · resumable" : null}
          </div>
        </div>
        {s.project_id ? (
          <Link to={`/products/${s.project_id}`}
                className="hover:opacity-80" title="Open product">
            <Badge tone="muted" className="gap-1">
              <Box size={11} /> {s.project_name || "product"}
            </Badge>
          </Link>
        ) : null}
        {s.harness_id ? (
          <Link to={`/harnesses/${s.harness_id}`}
                className="hover:opacity-80" title="Open harness">
            <Badge tone="primary" className="gap-1">
              <Package size={11} /> {s.harness_name || "harness"}
              {s.pending_bundle && !s.upload_count
                ? <Loader2 size={10} className="animate-spin ml-0.5" /> : null}
            </Badge>
          </Link>
        ) : null}
        {s.model ? (
          <Badge tone="muted" className="gap-1" title={`Model: ${s.model}`}>
            <Cpu size={11} />
            {MODEL_OPTIONS.find((m) => m.value === s.model)?.label ?? s.model}
          </Badge>
        ) : null}
        <button type="button" onClick={() => setShowFiles((v) => !v)}
                title="Session files">
          <Badge tone={s.upload_count ? "primary" : "muted"} className="gap-1">
            <Paperclip size={11} />
            {s.upload_count
              ? <>{s.upload_count} file{s.upload_count === 1 ? "" : "s"} · {fmtBytes(s.upload_bytes)}</>
              : "Files"}
            {s.pending_bundle && s.upload_count
              ? <Loader2 size={10} className="animate-spin ml-0.5" /> : null}
          </Badge>
        </button>
        <Badge tone={busy ? "primary" : "muted"}>
          {busy ? "running" : `${s.turn_count} turn${s.turn_count === 1 ? "" : "s"}`}
        </Badge>
        <Button variant="ghost" size="sm" onClick={() => archive.mutate()}
                title="Archive session">
          <Archive size={14} />
        </Button>
      </div>

      {showFiles || up.progress ? (
        <SessionFiles s={s} onUpload={doUpload} isPending={up.isPending}
                      progress={up.progress} onClose={() => setShowFiles(false)} />
      ) : null}

      <div
        className={cn("flex-1 overflow-y-auto px-4 py-4 space-y-5 relative",
                      dragOver && "ring-2 ring-primary ring-inset")}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={onDrop}
      >
        {dragOver ? (
          <div className="absolute inset-0 bg-primary/5 flex items-center
                          justify-center text-sm pointer-events-none gap-2">
            <UploadCloud size={16} /> Drop to add files to this session
          </div>
        ) : null}
        {s.turns.length === 0 ? (
          <div className="text-center text-fgmuted text-sm pt-12">
            Send a prompt to start — or drop source files / a zip here first.
            {agent && !agent.last_seen ? (
              <div className="text-warning mt-2">
                Heads up: <b>{agent.hostname}</b> hasn't connected yet.
                The prompt will queue until the agent comes online.
              </div>
            ) : null}
          </div>
        ) : null}
        {s.turns.map((t) => <TurnView key={t.request_id} t={t} />)}
        {busy && lastTurn ? (
          <SaveAsReport agentId={s.agent_id} rp={lastTurn}
                        defaultProjectId={s.project_id} hidden />
        ) : lastTurn && lastTurn.output ? (
          <SaveAsReport agentId={s.agent_id} rp={lastTurn}
                        defaultProjectId={s.project_id} />
        ) : null}
        <div ref={bottomRef} />
      </div>

      <PromptBox
        busy={busy}
        phase={busy ? phaseOf(lastTurn) : null}
        productName={s.project_name}
        onSend={(p) => send.mutate(p)}
        error={
          send.isError ? (send.error as Error).message
          : up.error ? `Upload failed: ${up.error}`
          : null
        }
      />
    </Card>
  );
}

function SessionFiles({ s, onUpload, isPending, progress, onClose }: {
  s: SessionDetail; onUpload: (files: File[]) => void; isPending: boolean;
  progress: UploadProgress | null; onClose: () => void;
}) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);

  const del = useMutation({
    mutationFn: (id: string) =>
      api(`/remote/sessions/${s.id}/files/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["remote-session", s.id] }),
  });
  const clearAll = useMutation({
    mutationFn: () =>
      api(`/remote/sessions/${s.id}/files`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["remote-session", s.id] }),
  });

  const pick = (list: FileList | null) => {
    const fs = Array.from(list ?? []);
    if (fs.length) onUpload(fs);
  };

  return (
    <div className="border-b border-border bg-surface/40 px-4 py-3">
      <div className="flex items-center gap-2 mb-2">
        <Paperclip size={13} />
        <div className="text-xs font-medium">
          {s.project_id
            ? <>Files <span className="text-fgmuted font-normal">
                · shared with <b>{s.project_name}</b></span></>
            : "Session files"}
          {s.upload_count
            ? <span className="text-fgmuted font-normal">
                {" "}— {s.upload_count} · {fmtBytes(s.upload_bytes)}
                {s.pending_bundle ? " · will sync on next prompt" : null}
              </span>
            : null}
        </div>
        <div className="flex-1" />
        <Button type="button" variant="secondary" size="sm"
                onClick={() => fileRef.current?.click()} disabled={isPending}>
          {isPending ? <Loader2 size={13} className="animate-spin" /> : <Plus size={13} />}
          Add files
        </Button>
        <Button type="button" variant="secondary" size="sm"
                onClick={() => dirRef.current?.click()} disabled={isPending}>
          <FolderUp size={13} /> Add folder
        </Button>
        {s.uploads.some((u) => u.source === "session") ? (
          <Button type="button" variant="ghost" size="sm"
                  onClick={() => clearAll.mutate()}
                  disabled={clearAll.isPending}>
            Clear all
          </Button>
        ) : null}
        <Button type="button" variant="ghost" size="sm" onClick={onClose}>
          <X size={13} />
        </Button>
      </div>
      <input ref={fileRef} type="file" multiple className="hidden"
             onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
      <input ref={dirRef} type="file" multiple className="hidden"
             // @ts-expect-error non-standard but supported
             webkitdirectory="" directory=""
             onChange={(e) => { pick(e.target.files); e.target.value = ""; }} />
      {progress ? (() => {
        const pct = progress.total
          ? Math.min(100, Math.round((progress.sent / progress.total) * 100)) : 0;
        return (
          <div className="mb-2">
            <div className="flex items-center justify-between text-[11px] mb-1">
              <span className="flex items-center gap-1.5">
                <Loader2 size={11} className="animate-spin text-primary" />
                Uploading {progress.files} file{progress.files === 1 ? "" : "s"}
                {" · "}{fmtBytes(progress.sent)} / {fmtBytes(progress.total || progress.bytes)}
                {progress.skipped
                  ? <span className="text-fgmuted">({progress.skipped} ignored)</span>
                  : null}
              </span>
              <span className="tabular-nums">{pct}%</span>
            </div>
            <div className="h-1.5 rounded bg-border overflow-hidden">
              <div className="h-full bg-primary transition-[width]"
                   style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })() : null}
      {s.uploads.length === 0 ? (
        progress ? null : (
          <div className="text-[11px] text-fgmuted">
            Drag files or a .zip / .tar.gz onto the conversation, or use the
            buttons above. Archives are unpacked server-side; .git,
            node_modules and similar are skipped automatically.
            {s.project_id
              ? <> Files you add here become part of <b>{s.project_name}</b> and
                  are visible to anyone who starts a session on that product.</>
              : null}
          </div>
        )
      ) : (
        <div className="max-h-40 overflow-y-auto rounded border border-border">
          {s.uploads.map((u) => (
            <div key={u.id}
                 className="flex items-center gap-2 px-2 py-1 text-[11px]
                            border-b border-border last:border-0 hover:bg-surface">
              <code className="flex-1 truncate" title={u.relpath}>{u.relpath}</code>
              {s.project_id ? (
                u.source === "project" ? (
                  <span className="text-fgmuted truncate max-w-32"
                        title={u.uploaded_by_email ?? undefined}>
                    {u.uploaded_by_email?.split("@")[0] ?? "shared"}
                  </span>
                ) : (
                  <span className="text-primary">this session</span>
                )
              ) : null}
              <span className="text-fgmuted tabular-nums">{fmtBytes(u.size_bytes)}</span>
              <button type="button" onClick={() => del.mutate(u.id)}
                      disabled={del.isPending}
                      className="text-fgmuted hover:text-danger">
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function TitleEdit({ s }: { s: SessionDetail }) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [val, setVal] = useState(s.title);
  const save = useMutation({
    mutationFn: () => api(`/remote/sessions/${s.id}`,
      { method: "PATCH", body: { title: val } }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["remote-sessions"] });
      qc.invalidateQueries({ queryKey: ["remote-session", s.id] });
      setEditing(false);
    },
  });
  if (!editing) {
    return (
      <div className="flex items-center gap-1.5 group">
        <div className="font-semibold text-sm truncate">
          {s.title || <span className="text-fgmuted">Untitled session</span>}
        </div>
        <button type="button" onClick={() => { setVal(s.title); setEditing(true); }}
                className="opacity-0 group-hover:opacity-100 text-fgmuted hover:text-fg">
          <Pencil size={12} />
        </button>
      </div>
    );
  }
  return (
    <form className="flex items-center gap-1.5"
          onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <Input value={val} onChange={(e) => setVal(e.target.value)}
             className="h-7 py-0 text-sm" autoFocus
             onKeyDown={(e) => e.key === "Escape" && setEditing(false)} />
      <Button type="submit" size="sm" variant="ghost"><CheckCircle2 size={14} /></Button>
    </form>
  );
}

// ---- one turn --------------------------------------------------------------

function TurnView({ t }: { t: Turn }) {
  const live = t.status === "pending" || t.status === "running";
  // The final assistant text is streamed as an event AND returned as `output`.
  // Only fall back to the output bubble when the stream had no text blocks.
  const hasStreamedText = t.events.some(
    (ev) => ev.type === "assistant" && ev.content.some((c) => c.type === "text"),
  );
  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <div className="max-w-[80%] bg-primary/10 border border-primary/20 rounded-lg
                        rounded-br-sm px-3 py-2 text-sm whitespace-pre-wrap">
          {t.prompt}
        </div>
      </div>

      <div className="max-w-[92%] space-y-1.5">
        {t.status === "pending" && t.events.length === 0 ? (
          <PhaseLine spinning>queued — waiting for agent to pick up…</PhaseLine>
        ) : null}
        {t.events.map((ev, i) => <EventLine key={i} ev={ev} />)}
        {live && t.events.length > 0 ? (
          <PhaseLine spinning>working…</PhaseLine>
        ) : null}
        {!live && !hasStreamedText && t.output ? (
          <div className="bg-surface border border-border rounded-lg rounded-bl-sm
                          px-3 py-2 text-sm whitespace-pre-wrap">
            {t.output}
          </div>
        ) : null}
        {t.status === "error" ? (
          <div className="flex items-start gap-2 text-danger text-xs
                          bg-danger/5 border border-danger/30 rounded px-2 py-1.5">
            <AlertCircle size={14} className="mt-px shrink-0" />
            <span className="whitespace-pre-wrap">{t.error}</span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function EventLine({ ev }: { ev: Ev }) {
  switch (ev.type) {
    case "bundle":
      return <PhaseLine icon={<Package size={13} className="text-primary" />}>
        {ev.phase === "download" ? "Preparing workspace — downloading bundle…"
          : ev.phase === "extract" ? "Preparing workspace — extracting…"
          : <>Workspace ready{ev.files != null ? ` (${ev.files} files)` : ""} ·{" "}
              <code className="text-[11px]">{ev.dest}</code></>}
      </PhaseLine>;
    case "launch":
      return <PhaseLine icon={<Terminal size={13} />}>
        Launching claude in <code className="text-[11px]">{ev.cwd}</code>
        {ev.resume ? " (resuming)" : ""}
        {ev.active_testing ? <> · <span className="text-warning">active-testing authorized</span></> : null}
      </PhaseLine>;
    case "system":
      if (ev.subtype !== "init") return null;
      return <PhaseLine icon={<Cpu size={13} />}>
        Session started{ev.model ? <> · <span className="text-fg">{ev.model}</span></> : null}
      </PhaseLine>;
    case "assistant":
      return <>{ev.content.map((c, i) => <AssistantPart key={i} c={c} />)}</>;
    case "tool_result":
      return (
        <details className="ml-5 text-[11px] text-fgmuted/80">
          <summary className="cursor-pointer flex items-center gap-1.5 hover:text-fgmuted">
            <CornerDownRight size={11} />
            tool result{ev.results.length > 1 ? ` ×${ev.results.length}` : ""}
            {ev.results.some((r) => r.is_error) ? (
              <span className="text-danger ml-1">(error)</span>
            ) : null}
          </summary>
          {ev.results.map((r, i) => (
            <pre key={i} className={cn(
              "mt-1 ml-4 p-1.5 rounded bg-muted/50 whitespace-pre-wrap break-all max-h-32 overflow-auto",
              r.is_error && "text-danger",
            )}>{r.preview}</pre>
          ))}
        </details>
      );
    case "result": {
      const ok = ev.subtype === "success";
      const bits: string[] = [];
      if (ev.duration_ms != null) bits.push(`${(ev.duration_ms / 1000).toFixed(1)}s`);
      if (ev.total_cost_usd != null) bits.push(`$${ev.total_cost_usd.toFixed(4)}`);
      return <PhaseLine icon={ok
          ? <CheckCircle2 size={13} className="text-success" />
          : <AlertCircle size={13} className="text-danger" />}>
        {ok ? "Done" : `Finished (${ev.subtype})`}
        {bits.length ? ` · ${bits.join(" · ")}` : null}
      </PhaseLine>;
    }
    case "error":
      return <PhaseLine icon={<AlertCircle size={13} className="text-danger" />}>
        <span className="text-danger">{ev.text}</span>
      </PhaseLine>;
    case "truncated":
      return <PhaseLine>…output truncated</PhaseLine>;
    default:
      return ev.text ? (
        <div className="text-xs text-fgmuted ml-5 whitespace-pre-wrap">{ev.text}</div>
      ) : null;
  }
}

function AssistantPart({ c }: { c: AssistantBlock }) {
  if (c.type === "tool_use") {
    return <PhaseLine icon={<Wrench size={13} className="text-primary" />}>
      <span className="text-fg font-medium">{c.name}</span>
      {c.hint ? <code className="ml-2 text-[11px] text-fgmuted truncate">{c.hint}</code> : null}
    </PhaseLine>;
  }
  if (c.type === "thinking") {
    if (!c.text?.trim()) {
      return <PhaseLine spinning={false}>
        <span className="italic">thinking</span>
      </PhaseLine>;
    }
    return (
      <details className="text-xs text-fgmuted/70 ml-5 italic">
        <summary className="cursor-pointer hover:text-fgmuted select-none">thinking…</summary>
        <div className="whitespace-pre-wrap not-italic mt-1 pl-2 border-l border-border">
          {c.text}
        </div>
      </details>
    );
  }
  return (
    <div className="bg-surface border border-border rounded-lg rounded-bl-sm
                    px-3 py-2 text-sm whitespace-pre-wrap">
      {c.text}
    </div>
  );
}

function PhaseLine({ icon, spinning, children }: {
  icon?: React.ReactNode; spinning?: boolean; children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-2 text-xs text-fgmuted">
      <span className="w-4 flex justify-center">
        {spinning ? <Loader2 size={13} className="animate-spin text-primary" />
                  : icon ?? <Circle size={6} className="fill-current" />}
      </span>
      <span className="min-w-0 truncate">{children}</span>
    </div>
  );
}

// ---- prompt box ------------------------------------------------------------

function phaseOf(t: Turn | undefined): string {
  if (!t) return "starting…";
  if (t.status === "pending") return "queued — waiting for agent…";
  for (let i = t.events.length - 1; i >= 0; i--) {
    const ev = t.events[i];
    if (ev.type === "assistant") {
      const last = ev.content[ev.content.length - 1];
      if (last?.type === "tool_use") return `using ${last.name}…`;
      if (last?.type === "thinking") return "thinking…";
      if (last?.type === "text") return "writing…";
    }
    if (ev.type === "tool_result") return "reading tool output…";
    if (ev.type === "system") return "session started…";
    if (ev.type === "launch") return "launching claude…";
    if (ev.type === "bundle") return "preparing workspace…";
  }
  return "working…";
}

type WbPrompt = { id: string; title: string; category: string; description: string; body: string };

function promptVars(body: string): string[] {
  const set = new Set<string>();
  for (const m of body.matchAll(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g)) set.add(m[1]);
  return [...set];
}
function applyVars(body: string, vals: Record<string, string>): string {
  return body.replace(/\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g, (_m, n) => (vals[n] || `{{${n}}}`));
}

function PromptPicker({ productName, onInsert }: {
  productName: string | null; onInsert: (text: string) => void;
}) {
  const q = useQuery({ queryKey: ["prompts"], queryFn: () => api<WbPrompt[]>("/prompts") });
  const [open, setOpen] = useState(false);
  const [sel, setSel] = useState<WbPrompt | null>(null);
  const [vals, setVals] = useState<Record<string, string>>({});
  const prompts = q.data ?? [];
  if (prompts.length === 0) return null;

  function choose(p: WbPrompt) {
    setSel(p);
    const init: Record<string, string> = {};
    for (const v of promptVars(p.body)) init[v] = v === "product" ? (productName ?? "") : "";
    setVals(init);
  }
  function insert() {
    if (!sel) return;
    onInsert(applyVars(sel.body, vals));
    setOpen(false); setSel(null);
  }

  return (
    <div className="mb-2">
      <Button type="button" size="sm" variant="secondary" onClick={() => setOpen((o) => !o)}>
        <MessageSquareText size={13} /> Use a prompt
      </Button>
      {open ? (
        <div className="mt-2 border border-border rounded-md p-2 bg-surface">
          {!sel ? (
            <div className="max-h-52 overflow-auto space-y-0.5">
              {prompts.map((p) => (
                <button key={p.id} type="button" onClick={() => choose(p)}
                        className="block w-full text-left px-2 py-1 rounded hover:bg-muted text-xs">
                  <span className="font-medium">{p.title}</span>
                  {p.category ? <span className="text-fgmuted"> · {p.category}</span> : null}
                  {p.description ? <div className="text-[11px] text-fgmuted">{p.description}</div> : null}
                </button>
              ))}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="text-xs font-medium flex items-center justify-between">
                <span>{sel.title}</span>
                <button type="button" className="text-fgmuted hover:text-fg" onClick={() => setSel(null)}>← back</button>
              </div>
              {promptVars(sel.body).length > 0 ? (
                <div className="grid grid-cols-2 gap-2">
                  {promptVars(sel.body).map((v) => (
                    <div key={v}>
                      <Label className="text-[10px] uppercase tracking-wider">{v}</Label>
                      <Input value={vals[v] ?? ""} onChange={(e) => setVals({ ...vals, [v]: e.target.value })}
                             placeholder={`{{${v}}}`} />
                    </div>
                  ))}
                </div>
              ) : <p className="text-[11px] text-fgmuted">No variables — inserts as-is.</p>}
              <Button type="button" size="sm" onClick={insert}>Insert into prompt</Button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}

function PromptBox({ busy, phase, productName, onSend, error }: {
  busy: boolean; phase: string | null; productName: string | null;
  onSend: (p: string) => void; error: string | null;
}) {
  const [val, setVal] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const p = val.trim();
    if (!p || busy) return;
    onSend(p);
    setVal("");
  };
  return (
    <form onSubmit={submit} className="border-t border-border p-3">
      <PromptPicker productName={productName} onInsert={(t) => setVal(t)} />
      {busy ? (
        <div className="flex items-center gap-2 text-xs text-fgmuted mb-2">
          <Loader2 size={13} className="animate-spin text-primary" />
          Claude is {phase}
        </div>
      ) : null}
      {error ? (
        <div className="text-xs text-danger mb-2 whitespace-pre-wrap font-mono">
          {error}
        </div>
      ) : null}
      <div className="flex items-end gap-2">
        <Textarea
          value={val}
          onChange={(e) => setVal(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e);
          }}
          rows={2}
          placeholder={busy ? "Waiting for the current turn to finish…"
                            : "Ask Claude something… (⌘/Ctrl+Enter to send)"}
          disabled={busy}
          className="resize-none"
        />
        <Button type="submit" disabled={busy || !val.trim()}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
          Send
        </Button>
      </div>
    </form>
  );
}

// ---- save as report (per-turn, reuses the legacy endpoint) -----------------

function SaveAsReport({ agentId, rp, defaultProjectId, hidden }: {
  agentId: string; rp: Turn; defaultProjectId: string | null; hidden?: boolean;
}) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(() => rp.prompt.slice(0, 60));
  const [projectId, setProjectId] = useState(defaultProjectId ?? "");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
    enabled: open,
  });
  const save = useMutation({
    mutationFn: () =>
      api<{ scan_id: string }>(`/agents/${agentId}/remote/${rp.request_id}/save`, {
        method: "POST", body: { title, project_id: projectId || null },
      }),
    onSuccess: (r) => nav(`/scans/${r.scan_id}`),
  });

  if (hidden) return null;
  if (!open) {
    return (
      <div>
        <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
          <Save size={14} /> Save last answer as report
        </Button>
      </div>
    );
  }
  return (
    <form
      className="border border-border rounded-md p-3 space-y-3 bg-surface"
      onSubmit={(e) => { e.preventDefault(); if (title.trim()) save.mutate(); }}
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <Label>Scan title</Label>
          <Input value={title} onChange={(e) => setTitle(e.target.value)} required autoFocus />
        </div>
        <div>
          <Label>Product</Label>
          <Select value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            <option value="">No product</option>
            {(projects.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </Select>
        </div>
      </div>
      <div className="flex items-center gap-2">
        <Button type="submit" disabled={save.isPending || !title.trim()}>
          <Save size={14} /> {save.isPending ? "Saving…" : "Save"}
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={() => setOpen(false)}>
          Cancel
        </Button>
        {save.isError ? (
          <span className="text-xs text-danger">{(save.error as Error).message}</span>
        ) : null}
      </div>
    </form>
  );
}
