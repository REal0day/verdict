import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "react-router-dom";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, getToken } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Label, Textarea } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { Empty } from "@/components/ui/Empty";
import {
  Sparkles, FolderGit2, Send, Check, ArrowRight, ArrowLeft,
  Server, Copy, Download, FolderUp, FileText,
} from "lucide-react";

type Project = {
  id: string; name: string; description: string;
  i_am_owner?: boolean; i_am_member?: boolean;
};
type AccessRequest = {
  id: string; project_id: string; project_name: string;
  status: "pending" | "approved" | "denied" | "cancelled";
};
type Agent = { id: string; hostname: string; api_key?: string; last_seen?: string | null };

const STEPS = ["Welcome", "Request access", "Add data", "Done"] as const;
type StepIdx = 0 | 1 | 2 | 3;

export function Welcome() {
  const { me, refresh } = useAuth();
  const nav = useNavigate();
  const [step, setStep] = useState<StepIdx>(0);

  const finish = useMutation({
    mutationFn: () => api("/auth/finish_onboarding", { method: "POST" }),
    onSuccess: async () => { await refresh(); nav("/"); },
  });

  function skip() {
    if (confirm("Skip onboarding? You can re-run it later from your Profile.")) {
      finish.mutate();
    }
  }

  return (
    <div className="max-w-3xl mx-auto py-10">
      <div className="mb-6">
        <h1 className="text-3xl font-semibold flex items-center gap-2">
          <Sparkles size={20} className="text-primary" /> Welcome to Verdict
        </h1>
        <p className="text-sm text-fgmuted mt-1">
          Hi {me?.email}. Let's get you set up.
        </p>
      </div>

      <StepBar step={step} />

      <div className="mt-6">
        {step === 0 ? <Step0 onNext={() => setStep(1)} onSkip={skip} /> : null}
        {step === 1 ? <Step1 onNext={() => setStep(2)} onBack={() => setStep(0)} /> : null}
        {step === 2 ? <Step2 onNext={() => setStep(3)} onBack={() => setStep(1)} /> : null}
        {step === 3 ? (
          <Step3
            onBack={() => setStep(2)}
            onFinish={() => finish.mutate()}
            finishing={finish.isPending}
          />
        ) : null}
      </div>
    </div>
  );
}

function StepBar({ step }: { step: StepIdx }) {
  return (
    <ol className="flex items-center gap-2">
      {STEPS.map((label, i) => {
        const active = i === step;
        const done = i < step;
        return (
          <li key={label} className="flex-1 flex items-center gap-2">
            <div className={
              "flex items-center justify-center rounded-full w-7 h-7 text-xs font-medium " +
              (active ? "bg-primary text-white" :
                done ? "bg-success/20 text-success" :
                "bg-muted text-fgmuted")
            }>
              {done ? <Check size={14} /> : i + 1}
            </div>
            <span className={"text-xs " + (active ? "text-fg font-medium" : "text-fgmuted")}>
              {label}
            </span>
            {i < STEPS.length - 1 ? <span className="flex-1 h-px bg-border" /> : null}
          </li>
        );
      })}
    </ol>
  );
}

function Step0({ onNext, onSkip }: { onNext: () => void; onSkip: () => void }) {
  return (
    <Card>
      <CardHeader><CardTitle>What you can do here</CardTitle></CardHeader>
      <CardBody className="space-y-4">
        <ul className="space-y-2 text-sm">
          <li className="flex gap-2"><FolderGit2 size={14} className="text-primary mt-0.5" />
            <span><strong>Join products</strong> — request access to any product your team has already created.</span></li>
          <li className="flex gap-2"><Server size={14} className="text-primary mt-0.5" />
            <span><strong>Install the agent</strong> on the machines that run Claude Code so your reports flow in automatically.</span></li>
          <li className="flex gap-2"><FolderUp size={14} className="text-primary mt-0.5" />
            <span><strong>Upload existing files</strong> — point us at a folder of past reports/POCs and Claude will organize them.</span></li>
        </ul>
        <div className="flex items-center justify-end gap-2 pt-2 border-t border-border">
          <Button variant="ghost" onClick={onSkip}>Skip for now</Button>
          <Button onClick={onNext}>
            Get started <ArrowRight size={14} />
          </Button>
        </div>
      </CardBody>
    </Card>
  );
}

function Step1({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const qc = useQueryClient();
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: () => api<Project[]>("/projects"),
  });
  const mine = useQuery({
    queryKey: ["my-requests"],
    queryFn: () => api<AccessRequest[]>("/project_requests?mine=1"),
  });

  // Things I can actually request: projects I'm not already in.
  const eligible = useMemo(() => {
    return (projects.data || []).filter((p) => !p.i_am_member && !p.i_am_owner);
  }, [projects.data]);

  // Map project_id -> latest request status so we don't show "Request" on
  // pending/approved ones.
  const myReqByProj = useMemo(() => {
    const m = new Map<string, AccessRequest>();
    for (const r of mine.data || []) {
      const prev = m.get(r.project_id);
      if (!prev) m.set(r.project_id, r);
    }
    return m;
  }, [mine.data]);

  const [reasons, setReasons] = useState<Record<string, string>>({});

  const send = useMutation({
    mutationFn: (project_id: string) =>
      api(`/project_requests`, {
        method: "POST",
        body: { project_id, reason: reasons[project_id] || "" },
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["my-requests"] });
    },
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderGit2 size={14} /> Request access to products
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm text-fgmuted">
          Pick any products you'd like to join. The product owner will be notified
          and can approve or deny. You can always come back here later.
        </p>

        {projects.isLoading ? (
          <p className="text-sm text-fgmuted">Loading products…</p>
        ) : eligible.length === 0 ? (
          <Empty
            icon={<FolderGit2 size={28} />}
            title="No products to request"
            hint="There aren't any products yet, or you're already a member of all of them. Move on to the next step."
          />
        ) : (
          <ul className="space-y-2">
            {eligible.map((p) => {
              const req = myReqByProj.get(p.id);
              const pending = req?.status === "pending";
              const approved = req?.status === "approved";
              const denied = req?.status === "denied";
              return (
                <li key={p.id} className="border border-border rounded-md p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium truncate">{p.name}</div>
                      {p.description ? (
                        <div className="text-xs text-fgmuted mt-0.5 line-clamp-2">{p.description}</div>
                      ) : null}
                    </div>
                    <div className="shrink-0">
                      {approved ? (
                        <Badge tone="success">already approved</Badge>
                      ) : pending ? (
                        <Badge tone="warning">requested</Badge>
                      ) : denied ? (
                        <Badge tone="danger">denied</Badge>
                      ) : (
                        <Button
                          size="sm"
                          onClick={() => send.mutate(p.id)}
                          disabled={send.isPending}
                        >
                          <Send size={12} /> Request
                        </Button>
                      )}
                    </div>
                  </div>
                  {!pending && !approved && !denied ? (
                    <Textarea
                      rows={2}
                      className="mt-2 text-xs"
                      placeholder="Optional: tell the owner why you need access."
                      value={reasons[p.id] || ""}
                      onChange={(e) =>
                        setReasons((r) => ({ ...r, [p.id]: e.target.value }))
                      }
                    />
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}

        <div className="flex items-center justify-between pt-3 border-t border-border">
          <Button variant="ghost" onClick={onBack}><ArrowLeft size={14} /> Back</Button>
          <Button onClick={onNext}>Next <ArrowRight size={14} /></Button>
        </div>
      </CardBody>
    </Card>
  );
}

function Step2({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  return (
    <div className="space-y-4">
      <AgentInstallCard />
      <UploadFilesCard />
      <Card>
        <CardBody className="flex items-center justify-between">
          <Button variant="ghost" onClick={onBack}><ArrowLeft size={14} /> Back</Button>
          <Button onClick={onNext}>Next <ArrowRight size={14} /></Button>
        </CardBody>
      </Card>
    </div>
  );
}

function AgentInstallCard() {
  const { me } = useAuth();
  // Cache the minted agent (incl. api_key — which the server only shows once)
  // in sessionStorage so navigating between wizard steps doesn't burn through
  // a new agent each time. Scoped to the user so other accounts on the same
  // machine don't pick it up.
  const cacheKey = `irs_onboarding_agent_${me?.id || "anon"}`;
  const [agent, setAgent] = useState<Agent | null>(() => {
    try {
      const raw = sessionStorage.getItem(cacheKey);
      return raw ? (JSON.parse(raw) as Agent) : null;
    } catch { return null; }
  });
  const minted = useRef(false);

  const defaultHostname = useMemo(() => {
    const prefix = (me?.email || "agent").split("@")[0].replace(/[^a-zA-Z0-9_-]/g, "-");
    return `${prefix || "agent"}-1`;
  }, [me?.email]);

  const create = useMutation({
    mutationFn: (hostname: string) =>
      api<Agent>("/agents", { method: "POST", body: { hostname } }),
    onSuccess: (a) => {
      setAgent(a);
      try { sessionStorage.setItem(cacheKey, JSON.stringify(a)); } catch { /* quota */ }
    },
  });

  // Auto-mint exactly once on first mount if we don't already have a cached one.
  useEffect(() => {
    if (agent || minted.current) return;
    minted.current = true;
    create.mutate(defaultHostname);
  }, [agent, defaultHostname]);

  if (!agent || !agent.api_key) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Server size={14} className="text-primary" /> Install the agent on your machine
          </CardTitle>
        </CardHeader>
        <CardBody>
          {create.isError ? (
            <p className="text-sm text-danger">Couldn't generate an installer. Try again.</p>
          ) : (
            <p className="text-sm text-fgmuted">Generating your installer…</p>
          )}
        </CardBody>
      </Card>
    );
  }

  const dlUrl = `/ui/agent/install.sh?agent_id=${agent.id}&api_key=${encodeURIComponent(agent.api_key)}`;
  const oneLiner = `curl -fsSL "${window.location.origin}${dlUrl}" | bash`;

  return (
    <Card className="border-success/40">
      <CardHeader>
        <CardTitle className="text-success flex items-center gap-2">
          <Server size={14} /> Install the agent — copy &amp; paste on the target machine
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm text-fgmuted">
          Reports Claude produces on that machine — plus any POC files alongside
          them — will stream up to this server automatically. The API key is
          baked into this link and shown <strong>once</strong>; copy the
          one-liner now.
        </p>
        <div className="flex items-stretch gap-2">
          <Input readOnly value={oneLiner} className="font-mono text-xs"
                 onClick={(e) => (e.target as HTMLInputElement).select()} />
          <CopyButton text={oneLiner} />
        </div>
        <p className="text-xs text-fgmuted">
          Agent name: <code className="text-fg">{agent.hostname}</code>. Rename
          it (or generate more) from the{" "}
          <Link to="/agents" className="text-primary hover:underline">Agents</Link> page.
        </p>
        <details>
          <summary className="text-xs text-fgmuted cursor-pointer">Other install options</summary>
          <div className="mt-2 space-y-2">
            <a href={dlUrl}>
              <Button variant="secondary" size="sm"><Download size={12} /> Download install.sh</Button>
            </a>
            <RegenerateAgent
              defaultHostname={defaultHostname}
              onCreated={(a) => {
                setAgent(a);
                try { sessionStorage.setItem(cacheKey, JSON.stringify(a)); } catch { /* quota */ }
              }}
            />
          </div>
        </details>
      </CardBody>
    </Card>
  );
}

function RegenerateAgent({
  defaultHostname, onCreated,
}: { defaultHostname: string; onCreated: (a: Agent) => void }) {
  const [hostname, setHostname] = useState(defaultHostname);
  const create = useMutation({
    mutationFn: () => api<Agent>("/agents", { method: "POST", body: { hostname } }),
    onSuccess: onCreated,
  });
  return (
    <div className="flex items-end gap-2 pt-2">
      <div className="flex-1">
        <Label htmlFor="rehost">Generate another with a different hostname</Label>
        <Input id="rehost" value={hostname} onChange={(e) => setHostname(e.target.value)} />
      </div>
      <Button variant="secondary" size="sm"
              onClick={() => hostname.trim() && create.mutate()}
              disabled={create.isPending || !hostname.trim()}>
        {create.isPending ? "Generating…" : "Generate"}
      </Button>
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button variant="secondary" onClick={() => {
      navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }}>
      <Copy size={14} /> {copied ? "Copied" : "Copy"}
    </Button>
  );
}

function UploadFilesCard() {
  const nav = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [label, setLabel] = useState("");
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function pick(list: FileList | null) {
    if (!list) return;
    setFiles(Array.from(list));
  }

  const total = files.reduce((n, f) => n + f.size, 0);

  async function upload() {
    if (files.length === 0) return;
    setUploading(true); setError(null);
    try {
      const fd = new FormData();
      fd.append("label", label);
      for (const f of files) {
        const wkrp = (f as any).webkitRelativePath as string | undefined;
        const rel = wkrp && wkrp.includes("/") ? wkrp.split("/").slice(1).join("/") : (wkrp || f.name);
        fd.append("relpaths", rel || f.name);
        fd.append("files", f, f.name);
      }
      const tok = getToken();
      const resp = await fetch("/imports", {
        method: "POST",
        headers: tok ? { Authorization: `Bearer ${tok}` } : {},
        body: fd,
      });
      if (!resp.ok) throw new Error(`upload failed: ${resp.status}`);
      const j = await resp.json();
      nav(`/imports/${j.id}`);
    } catch (e: any) {
      setError(e.message || String(e));
      setUploading(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FolderUp size={14} className="text-primary" /> Upload existing files (optional)
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-3">
        <p className="text-sm text-fgmuted">
          Got a folder of past reports, POCs, scan notes, or screenshots?
          Drop the whole directory in — Claude will look through it and propose
          how to organize everything (product, scans, reports, attachments).
          You review and confirm before anything is saved.
        </p>

        <div>
          <Label htmlFor="ul-label">Label (optional)</Label>
          <Input id="ul-label" value={label}
                 onChange={(e) => setLabel(e.target.value)}
                 placeholder="e.g. 'libfoo audit — June'" />
        </div>

        <input
          ref={inputRef}
          type="file"
          multiple
          // @ts-expect-error — folder picker attrs
          webkitdirectory="" directory=""
          className="hidden"
          onChange={(e) => pick(e.target.files)}
        />

        <div className="flex items-center gap-2">
          <Button type="button" variant="secondary" onClick={() => inputRef.current?.click()}>
            <FolderUp size={14} /> Choose folder…
          </Button>
          {files.length > 0 ? (
            <span className="text-xs text-fgmuted">
              {files.length} file{files.length === 1 ? "" : "s"}, {(total / 1024).toFixed(1)} KB
            </span>
          ) : (
            <span className="text-xs text-fgmuted">No folder picked yet.</span>
          )}
        </div>

        {error ? <p className="text-sm text-danger">{error}</p> : null}

        {files.length > 0 ? (
          <div className="flex justify-end">
            <Button onClick={upload} disabled={uploading}>
              {uploading ? "Uploading…" : "Upload & analyze"}
            </Button>
          </div>
        ) : null}
      </CardBody>
    </Card>
  );
}

function Step3({
  onBack, onFinish, finishing,
}: { onBack: () => void; onFinish: () => void; finishing: boolean }) {
  // Poll until the first report lands (or until the user clicks Finish).
  // Lets the user *see* their agent connect and upload before they bail.
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api<Agent[]>("/agents"),
    refetchInterval: 5_000,
  });
  type ReportLite = { id: string; title: string; filename: string; created_at: string };
  const reports = useQuery({
    queryKey: ["reports-tail"],
    queryFn: () => api<ReportLite[]>("/reports?limit=5"),
    refetchInterval: 5_000,
  });

  const myAgents = agents.data || [];
  const myReports = reports.data || [];
  const everSeen = myAgents.some((a) => !!a.last_seen);
  const newestSeen = myAgents
    .map((a) => a.last_seen)
    .filter((s): s is string => !!s)
    .sort()
    .pop();

  return (
    <div className="space-y-4">
      <Card className={everSeen ? "border-success/40" : ""}>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            {everSeen ? <Check size={14} className="text-success" /> : <Server size={14} className="text-fgmuted" />}
            {everSeen ? "Your agent is connected" : "Waiting for your agent to phone home…"}
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-2 text-sm">
          {myAgents.length === 0 ? (
            <p className="text-fgmuted">
              No agents yet. Go back a step and generate one — the install script
              has to run on the target machine before we see anything here.
            </p>
          ) : (
            <ul className="space-y-1">
              {myAgents.map((a) => (
                <li key={a.id} className="flex items-center justify-between gap-3 text-xs">
                  <code className="text-fg">{a.hostname}</code>
                  <span className="text-fgmuted">
                    {a.last_seen
                      ? <>last seen {timeAgo(a.last_seen)}</>
                      : <span className="italic">not seen yet</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
          {everSeen ? (
            <p className="text-xs text-success">
              Heartbeat received {newestSeen ? timeAgo(newestSeen) : "just now"}. The agent will upload any .md report it
              writes (and POC files alongside it) automatically.
            </p>
          ) : (
            <p className="text-xs text-fgmuted">
              On the target machine, run the one-liner from the previous step.
              Once Claude Code writes its first .md report, you'll see it land
              here in real time.
            </p>
          )}
        </CardBody>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FileText size={14} className="text-primary" />
            Where uploads land
          </CardTitle>
        </CardHeader>
        <CardBody className="space-y-2 text-sm">
          <p className="text-fgmuted">
            Reports your agent uploads show up on the{" "}
            <Link to="/" className="text-primary hover:underline">Reports</Link>{" "}
            page. POC files attached to those reports show up under the matching{" "}
            <Link to="/scans" className="text-primary hover:underline">Scan</Link>.
            You'll get a notification on every upload — check the bell at the top right.
          </p>
          {myReports.length > 0 ? (
            <div className="border border-success/40 rounded-md p-2 bg-success/5">
              <div className="text-xs text-success font-medium mb-1">
                {myReports.length} report{myReports.length === 1 ? "" : "s"} received so far:
              </div>
              <ul className="space-y-0.5 text-xs">
                {myReports.slice(0, 3).map((r) => (
                  <li key={r.id}>
                    <Link to={`/reports/${r.id}`} className="text-primary hover:underline">
                      {r.title || r.filename}
                    </Link>{" "}
                    <span className="text-fgmuted">· {timeAgo(r.created_at)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-xs text-fgmuted italic">
              No uploads yet. We'll keep checking — you can also finish onboarding
              now and watch them arrive from the Reports page.
            </p>
          )}
          <div className="flex flex-wrap gap-2 pt-2">
            <Link to="/"><Button variant="secondary" size="sm">Go to Reports</Button></Link>
            <Link to="/agents"><Button variant="secondary" size="sm">Manage Agents</Button></Link>
            <Link to="/products"><Button variant="secondary" size="sm">Products</Button></Link>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardBody className="flex items-center justify-between">
          <Button variant="ghost" onClick={onBack}><ArrowLeft size={14} /> Back</Button>
          <Button onClick={onFinish} disabled={finishing}>
            {finishing ? "Saving…" : "Finish onboarding"}
          </Button>
        </CardBody>
      </Card>
    </div>
  );
}

function timeAgo(iso: string): string {
  const then = new Date(iso).getTime();
  const sec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.round(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.round(sec / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}
