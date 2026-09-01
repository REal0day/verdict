import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { PageHeader } from "@/components/Layout";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Table, THead, TR, TH, TD } from "@/components/ui/Table";
import { Input, Label, Select, Textarea } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import { Server, Plus, Copy, Download, Check, Trash2, Terminal, Send, Loader2, X, Save, RefreshCw, KeyRound } from "lucide-react";

type Project = { id: string; name: string };

type Agent = {
  id: string;
  hostname: string;
  last_seen: string | null;
  last_ip: string | null;
  version: string | null;
  pending_upgrade: boolean;
  update_available: boolean;
  anthropic_key_last4: string | null;
  anthropic_key_expires_at: string | null;
  anthropic_key_pushed_at: string | null;
  pending_key_push: boolean;
  api_key?: string; // present only on creation
};

export function Agents() {
  const qc = useQueryClient();

  const list = useQuery({
    queryKey: ["agents"],
    queryFn: () => api<Agent[]>("/agents"),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((a) => a.pending_upgrade || a.pending_key_push)
        ? 3000 : false,
  });
  const latest = useQuery({
    queryKey: ["agent-latest-version"],
    queryFn: () => api<{ version: string | null }>("/agents/latest-version"),
  });

  const upgrade = useMutation({
    mutationFn: (id: string) => api(`/agents/${id}/upgrade`, { method: "POST" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });

  const [hostname, setHostname] = useState("");
  const [createdAgent, setCreatedAgent] = useState<Agent | null>(null);
  const [keyFor, setKeyFor] = useState<Agent | null>(null);

  const del = useMutation({
    mutationFn: (id: string) => api(`/agents/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["agents"] }),
  });

  const create = useMutation({
    mutationFn: () => api<Agent>("/agents", { method: "POST", body: { hostname } }),
    onSuccess: (a) => {
      setCreatedAgent(a);   // shown in modal-card with the one-time key
      setHostname("");
      qc.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (hostname.trim()) create.mutate();
  }

  return (
    <>
      <PageHeader
        title="Agents"
        subtitle="Each machine that runs Claude installs one. Reports + POC files flow through them to the server."
      />

      {createdAgent ? (
        <CreatedAgentCard agent={createdAgent} onClose={() => setCreatedAgent(null)} />
      ) : null}

      <Card className="mb-5">
        <CardHeader><CardTitle>Generate installer for a new machine</CardTitle></CardHeader>
        <CardBody>
          <form onSubmit={submit} className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
            <div>
              <Label htmlFor="hostname">Hostname / label</Label>
              <Input id="hostname" value={hostname}
                     onChange={(e) => setHostname(e.target.value)}
                     placeholder="agent-1, prod-runner-01, ..." required />
            </div>
            <Button type="submit" disabled={create.isPending}>
              <Plus size={14} /> {create.isPending ? "Generating…" : "Generate"}
            </Button>
          </form>
          <p className="text-xs text-fgmuted mt-3">
            Generates a fresh API key for that machine. The key is shown to you
            <strong className="text-fg"> exactly once</strong>; the server only stores its hash.
          </p>
        </CardBody>
      </Card>

      <Card>
        <CardHeader><CardTitle>Your agents</CardTitle></CardHeader>
        {list.isLoading ? (
          <CardBody><p className="text-sm text-fgmuted">Loading…</p></CardBody>
        ) : !list.data || list.data.length === 0 ? (
          <CardBody>
            <Empty icon={<Server size={28} />}
                   title="No agents yet" hint="Generate one above to install on your first machine." />
          </CardBody>
        ) : (
          <Table>
            <THead>
              <TR>
                <TH>Hostname</TH>
                <TH>IP</TH>
                <TH>Last seen</TH>
                <TH>Version</TH>
                <TH>Claude key</TH>
                <TH>Agent id</TH>
                <TH className="w-0"></TH>
              </TR>
            </THead>
            <tbody>
              {list.data.map((a) => (
                <TR key={a.id}>
                  <TD className="font-medium">{a.hostname}</TD>
                  <TD className="text-fgmuted text-xs font-mono">{a.last_ip || "—"}</TD>
                  <TD className="text-fgmuted text-xs">{a.last_seen ? fmt(a.last_seen) : "never"}</TD>
                  <TD className="text-xs">
                    {a.version ? (
                      <span className="font-mono">{a.version}</span>
                    ) : (
                      <span className="text-fgmuted">—</span>
                    )}
                    {a.pending_upgrade ? (
                      <Badge tone="primary" className="ml-2 gap-1">
                        <Loader2 size={10} className="animate-spin" /> updating
                      </Badge>
                    ) : a.update_available ? (
                      <Badge tone="warning" className="ml-2">
                        update → {latest.data?.version}
                      </Badge>
                    ) : a.version ? (
                      <Badge tone="success" className="ml-2">current</Badge>
                    ) : null}
                  </TD>
                  <TD className="text-xs"><KeyCell a={a} /></TD>
                  <TD className="text-xs font-mono text-fgmuted">{a.id}</TD>
                  <TD className="whitespace-nowrap">
                    <Button variant="secondary" size="sm" className="mr-1"
                            onClick={() => setKeyFor(a)}
                            title="Push an Anthropic API key to this agent">
                      <KeyRound size={12} /> Key
                    </Button>
                    <Button variant="secondary" size="sm"
                            disabled={!a.last_seen || a.pending_upgrade ||
                                      (upgrade.isPending && upgrade.variables === a.id)}
                            onClick={() => upgrade.mutate(a.id)}
                            title={a.last_seen ? "Push update to this agent"
                                               : "Agent has never connected"}>
                      <RefreshCw size={12}
                                 className={a.pending_upgrade ? "animate-spin" : ""} />
                      Update
                    </Button>
                    <Button variant="ghost" size="sm" className="ml-1"
                            disabled={del.isPending && del.variables === a.id}
                            onClick={() => {
                              if (confirm(`Delete agent "${a.hostname}"? Its API key will stop working immediately. Uploaded reports are kept.`))
                                del.mutate(a.id);
                            }}>
                      <Trash2 size={12} />
                    </Button>
                  </TD>
                </TR>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {keyFor ? (
        <SetKeyCard agent={list.data?.find((a) => a.id === keyFor.id) ?? keyFor}
                    onClose={() => setKeyFor(null)} />
      ) : null}

      {list.data && list.data.length > 0 ? <RemotePrompt agents={list.data} /> : null}
    </>
  );
}

const DAY_MS = 24 * 60 * 60 * 1000;

function daysLeft(iso: string | null): number | null {
  if (!iso) return null;
  return Math.floor((new Date(iso).getTime() - Date.now()) / DAY_MS);
}

function KeyCell({ a }: { a: Agent }) {
  if (!a.anthropic_key_last4) {
    return <span className="text-fgmuted">not set</span>;
  }
  const d = daysLeft(a.anthropic_key_expires_at);
  return (
    <div className="flex items-center gap-2">
      <span className="font-mono" title={a.anthropic_key_pushed_at
        ? `Pushed to agent ${fmt(a.anthropic_key_pushed_at)}`
        : "Not yet delivered to agent"}>
        ••••{a.anthropic_key_last4}
      </span>
      {a.pending_key_push ? (
        <Badge tone="primary" className="gap-1">
          <Loader2 size={10} className="animate-spin" /> pushing
        </Badge>
      ) : d === null ? null : d < 0 ? (
        <Badge tone="danger">expired</Badge>
      ) : d <= 2 ? (
        <Badge tone="danger">{d}d left</Badge>
      ) : (
        <Badge tone={d <= 5 ? "warning" : "success"}>{d}d left</Badge>
      )}
    </div>
  );
}

function SetKeyCard({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [expires, setExpires] = useState(() => {
    const d = new Date(Date.now() + 7 * DAY_MS);
    return d.toISOString().slice(0, 10);
  });

  const push = useMutation({
    mutationFn: () =>
      api(`/agents/${agent.id}/anthropic-key`, {
        method: "PUT",
        body: {
          key,
          expires_at: expires ? new Date(expires + "T23:59:59Z").toISOString() : null,
        },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agents"] });
      setKey("");
      onClose();
    },
  });
  const clear = useMutation({
    mutationFn: () => api(`/agents/${agent.id}/anthropic-key`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["agents"] }); onClose(); },
  });

  return (
    <Card className="mt-5 border-primary/40">
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="flex items-center gap-2">
          <KeyRound size={16} /> Anthropic key — {agent.hostname}
        </CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}><X size={14} /></Button>
      </CardHeader>
      <CardBody>
        {agent.anthropic_key_last4 ? (
          <p className="text-xs text-fgmuted mb-3">
            Current: <code>••••{agent.anthropic_key_last4}</code>
            {agent.anthropic_key_expires_at
              ? <> · expires {new Date(agent.anthropic_key_expires_at).toLocaleDateString()}</>
              : null}
            {agent.anthropic_key_pushed_at
              ? <> · delivered {fmt(agent.anthropic_key_pushed_at)}</>
              : <> · <span className="text-warning">not yet delivered</span></>}
          </p>
        ) : null}
        <form onSubmit={(e) => { e.preventDefault(); if (key.trim()) push.mutate(); }}
              className="grid grid-cols-1 md:grid-cols-[1fr_auto] gap-3 items-end">
          <div>
            <Label>API key</Label>
            <Input type="password" value={key} onChange={(e) => setKey(e.target.value)}
                   placeholder="sk-ant-api03-…" required autoFocus
                   className="font-mono text-xs" autoComplete="off" />
          </div>
          <div>
            <Label>Expires</Label>
            <Input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
          </div>
          <div className="md:col-span-2 flex items-center gap-2">
            <Button type="submit" disabled={push.isPending || !key.trim()}>
              <Send size={14} /> {push.isPending ? "Pushing…" : "Push to agent"}
            </Button>
            {agent.anthropic_key_last4 ? (
              <Button type="button" variant="ghost" size="sm"
                      onClick={() => { if (confirm("Clear the key on this agent?")) clear.mutate(); }}>
                Clear key
              </Button>
            ) : null}
            {push.isError ? (
              <span className="text-xs text-danger">{(push.error as Error).message}</span>
            ) : null}
          </div>
        </form>
        <p className="text-xs text-fgmuted mt-3">
          Stored encrypted on the server and handed to the agent on its next poll.
          Anthropic keys don't carry their expiry, so enter the date the Console
          showed you when you created it — the badge in the table counts down from that.
        </p>
      </CardBody>
    </Card>
  );
}

type RemoteResult = {
  request_id: string;
  status: "pending" | "running" | "done" | "error";
  prompt: string;
  cwd: string | null;
  output: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
};

const statusTone = {
  pending: "warning", running: "warning", done: "success", error: "danger",
} as const;

function RemotePrompt({ agents }: { agents: Agent[] }) {
  const qc = useQueryClient();
  const [agentId, setAgentId] = useState(agents[0].id);
  const [prompt, setPrompt] = useState("");
  const [cwd, setCwd] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    if (!agents.some((a) => a.id === agentId)) setAgentId(agents[0].id);
  }, [agents, agentId]);

  const history = useQuery({
    queryKey: ["remote-history", agentId],
    queryFn: () => api<RemoteResult[]>(`/agents/${agentId}/remote`),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((r) => r.status === "pending" || r.status === "running")
        ? 2000 : false,
  });

  // Default the expanded row to the most recent prompt whenever the agent or
  // its history changes — this is what restores your view after a reload.
  useEffect(() => {
    const rows = history.data ?? [];
    if (rows.length === 0) { setOpenId(null); return; }
    if (!openId || !rows.some((r) => r.request_id === openId)) {
      setOpenId(rows[0].request_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, history.data]);

  const send = useMutation({
    mutationFn: () =>
      api<{ request_id: string }>(`/agents/${agentId}/remote`, {
        method: "POST",
        body: { prompt, cwd: cwd.trim() || null },
      }),
    onSuccess: (r) => {
      setOpenId(r.request_id);
      setPrompt("");
      qc.invalidateQueries({ queryKey: ["remote-history", agentId] });
    },
  });

  const del = useMutation({
    mutationFn: (rid: string) =>
      api(`/agents/${agentId}/remote/${rid}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["remote-history", agentId] }),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (prompt.trim()) send.mutate();
  }

  const rows = history.data ?? [];
  const open = rows.find((r) => r.request_id === openId) ?? null;
  const active = rows.some((r) => r.status === "pending" || r.status === "running");
  const sel = agents.find((a) => a.id === agentId);

  return (
    <Card className="mt-5">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Terminal size={16} /> Run Claude on your machine
          {active ? <Loader2 size={14} className="animate-spin text-warning" /> : null}
        </CardTitle>
      </CardHeader>
      <CardBody>
        <p className="text-xs text-fgmuted mb-3">
          Sends a one-shot <code>claude -p</code> to the selected agent. Prompts and
          replies are saved server-side, so you can close this page and come back.
          Only you can do this — the agent only accepts prompts from the account that
          installed it.
        </p>
        <form onSubmit={submit} className="space-y-3">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <Label>Agent</Label>
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
              <Label>Working directory (optional)</Label>
              <Input value={cwd} onChange={(e) => setCwd(e.target.value)}
                     placeholder="~/code/myrepo  (defaults to home)" />
            </div>
          </div>
          <div>
            <Label>Prompt</Label>
            <Textarea rows={4} value={prompt} onChange={(e) => setPrompt(e.target.value)}
                      placeholder="e.g. List the open TODOs in this repo" required />
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={send.isPending || !prompt.trim()}>
              <Send size={14} /> {send.isPending ? "Sending…" : "Send"}
            </Button>
            {sel && !sel.last_seen ? (
              <span className="text-xs text-warning">
                This agent has never connected — start <code>irs-agent run</code> on {sel.hostname} first.
              </span>
            ) : null}
            {send.isError ? (
              <span className="text-xs text-danger">{(send.error as Error).message}</span>
            ) : null}
          </div>
        </form>

        {rows.length > 0 ? (
          <div className="mt-5">
            <div className="text-xs font-medium text-fgmuted mb-2">
              Recent prompts on {sel?.hostname}
            </div>
            <div className="border border-border rounded-md divide-y divide-border max-h-48 overflow-auto mb-3">
              {rows.map((r) => (
                <div key={r.request_id}
                     onClick={() => setOpenId(r.request_id)}
                     className={
                       "flex items-center gap-2 px-3 py-2 text-xs cursor-pointer hover:bg-muted/40 " +
                       (r.request_id === openId ? "bg-muted/60" : "")
                     }>
                  <Badge tone={statusTone[r.status]} className="gap-1">
                    {(r.status === "pending" || r.status === "running")
                      ? <Loader2 size={10} className="animate-spin" /> : null}
                    {r.status}
                  </Badge>
                  <span className="flex-1 truncate" title={r.prompt}>{r.prompt}</span>
                  <span className="text-fgmuted whitespace-nowrap">{fmt(r.created_at)}</span>
                  <button type="button" className="text-fgmuted hover:text-danger p-1"
                          title="Delete from history"
                          onClick={(e) => { e.stopPropagation(); del.mutate(r.request_id); }}>
                    <X size={12} />
                  </button>
                </div>
              ))}
            </div>

            {open ? (
              <>
                <div className="text-xs text-fgmuted mb-1">
                  <code>{open.request_id.slice(0, 8)}</code>
                  {open.cwd ? <> · cwd <code>{open.cwd}</code></> : null}
                  {" · "}<span className={`text-${statusTone[open.status]}`}>{open.status}</span>
                  {open.completed_at ? <> · finished {fmt(open.completed_at)}</> : null}
                </div>
                {open.error ? (
                  <pre className="text-xs whitespace-pre-wrap bg-danger/10 border border-danger/30 rounded-md p-3 mb-2">
                    {open.error}
                  </pre>
                ) : null}
                <pre className="text-xs whitespace-pre-wrap bg-muted/40 border border-border rounded-md p-3 max-h-[28rem] overflow-auto">
                  {open.output
                    ? open.output
                    : open.status === "pending" ? "waiting for agent to pick this up…"
                    : open.status === "running" ? "claude is running on the agent…"
                    : "(no output)"}
                  {(open.status === "pending" || open.status === "running") && open.output
                    ? <span className="text-warning">▌ still running…</span> : null}
                </pre>
                {(open.status === "done" || open.status === "error") && open.output ? (
                  <SaveAsReport agentId={agentId} rp={open} />
                ) : null}
              </>
            ) : null}
          </div>
        ) : history.isLoading ? null : (
          <p className="mt-4 text-xs text-fgmuted">No prompts sent to this agent yet.</p>
        )}
      </CardBody>
    </Card>
  );
}

function CreatedAgentCard({ agent, onClose }: { agent: Agent; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  function copy() {
    if (!agent.api_key) return;
    navigator.clipboard.writeText(agent.api_key);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }
  const dlUrl = `/ui/agent/install.sh?agent_id=${agent.id}&api_key=${encodeURIComponent(agent.api_key || "")}`;
  return (
    <Card className="mb-5 border-success/40">
      <CardHeader className="flex items-center justify-between">
        <CardTitle className="text-success flex items-center gap-2">
          <Check size={16} /> Agent created — save this key
        </CardTitle>
        <Button variant="ghost" size="sm" onClick={onClose}>dismiss</Button>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm">
          This is the <strong>only time</strong> the API key is shown. If you lose it,
          generate a new agent.
        </p>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div>
            <Label>Hostname</Label>
            <Input readOnly value={agent.hostname} />
          </div>
          <div>
            <Label>Agent id</Label>
            <Input readOnly value={agent.id} className="font-mono text-xs" />
          </div>
        </div>
        <div>
          <Label>API key (one-time)</Label>
          <div className="flex items-stretch gap-2">
            <Input readOnly value={agent.api_key || ""} className="font-mono text-xs"
                   onClick={(e) => (e.target as HTMLInputElement).select()} />
            <Button variant="secondary" onClick={copy}><Copy size={14} /> {copied ? "Copied" : "Copy"}</Button>
          </div>
        </div>
        <div className="pt-2">
          <a href={dlUrl}>
            <Button><Download size={14} /> Download install.sh (Linux)</Button>
          </a>
        </div>
        <div className="mt-2">
          <div className="text-xs text-fgmuted">Or run this one line on the target machine — it installs, starts, and registers the agent for boot:</div>
          <pre className="mt-1 text-xs">{`curl -fsSL "${window.location.origin}${dlUrl}" | bash`}</pre>
        </div>
      </CardBody>
    </Card>
  );
}

function SaveAsReport({ agentId, rp }: { agentId: string; rp: RemoteResult }) {
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState(() => rp.prompt.slice(0, 60));
  const [projectId, setProjectId] = useState("");

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
    enabled: open,
  });

  const save = useMutation({
    mutationFn: () =>
      api<{ report_id: string; scan_id: string }>(
        `/agents/${agentId}/remote/${rp.request_id}/save`,
        { method: "POST", body: { title, project_id: projectId || null } }
      ),
    onSuccess: (r) => nav(`/scans/${r.scan_id}`),
  });

  if (!open) {
    return (
      <div className="mt-3">
        <Button variant="secondary" size="sm" onClick={() => setOpen(true)}>
          <Save size={14} /> Save as report
        </Button>
      </div>
    );
  }

  return (
    <form
      className="mt-3 border border-border rounded-md p-3 space-y-3 bg-surface"
      onSubmit={(e) => { e.preventDefault(); if (title.trim()) save.mutate(); }}
    >
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <Label>Scan title</Label>
          <Input value={title} onChange={(e) => setTitle(e.target.value)}
                 placeholder="e.g. Acme Gateway source-code scan" required autoFocus />
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
      <p className="text-xs text-fgmuted">
        Creates a Report from this output and a draft Scan titled above
        {rp.cwd ? <> (target: <code>{rp.cwd}</code>)</> : null}. You'll land on
        the scan page where you can fill in the rest.
      </p>
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

function fmt(iso: string) {
  return new Date(iso).toLocaleString(undefined,
    { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
