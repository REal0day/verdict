/**
 * Onboarding step: connect the model that powers summaries, extraction and chat.
 *
 * Admin-only in substance — AI credentials are server-wide, so a non-admin sees
 * a read-only summary of what their administrator connected rather than a form
 * they can't submit.
 *
 * On "log in with Anthropic": there is no third-party OAuth flow to build. The
 * Claude API supports API keys, Workload Identity Federation, and App Attest —
 * none of which lets a self-hosted app ask a user to delegate their Anthropic
 * account to it. `claude auth` style login is first-party CLI only. So the
 * closest honest thing is a one-click hop to the provider's key page with the
 * paste field waiting here.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { Card, CardBody, CardHeader, CardTitle } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input, Label } from "@/components/ui/Input";
import { Badge } from "@/components/ui/Badge";
import { aiErrorText } from "@/components/AIStatus";
import {
  Check, AlertCircle, AlertTriangle, ArrowRight, ArrowLeft, ExternalLink,
  Loader2, Search, KeyRound, Server, Sparkles,
} from "lucide-react";

type Provider = {
  name: string;
  display_name: string;
  configured: boolean;
  source: "db" | "env" | "none";
  hint: string | null;
  model: string;
  base_url: string | null;
  requires_key: boolean;
  supports_tools: boolean;
  self_hosted: boolean;
  is_active: boolean;
};
type AISettings = { active_provider: string; providers: Provider[] };
type TestResult = { ok: boolean; provider: string; model?: string; error?: string };
type LocalCandidate = {
  label: string; base_url: string; reachable: boolean;
  models: string[]; error: string | null;
};

/** Where each hosted provider mints API keys. */
const KEY_PAGES: Record<string, string> = {
  anthropic: "https://platform.claude.com/settings/keys",
  openai: "https://platform.openai.com/api-keys",
  gemini: "https://aistudio.google.com/apikey",
  grok: "https://console.x.ai/",
};

const BLURB: Record<string, string> = {
  anthropic: "Claude. Strongest at the structured extraction the findings pipeline depends on.",
  openai: "GPT models, or anything behind an OpenAI-compatible gateway.",
  gemini: "Google Gemini. No tool-calling here yet, so the folder-import planner won't run on it.",
  grok: "xAI Grok, via its OpenAI-compatible endpoint.",
  local: "A model you run yourself — on this machine or another box. Nothing leaves your network.",
};

export function ConnectAIStep({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const { me } = useAuth();
  const isAdmin = me?.role === "admin";
  return isAdmin
    ? <AdminConnect onNext={onNext} onBack={onBack} />
    : <MemberView onNext={onNext} onBack={onBack} />;
}

/* ------------------------------------------------------------------ member */

function MemberView({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const q = useQuery({
    queryKey: ["ai-status"],
    queryFn: () => api<{ configured: boolean; display_name: string; model: string }>(
      "/settings/ai/status"),
    retry: false,
  });
  const s = q.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles size={14} className="text-primary" /> AI assistant
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        {s?.configured ? (
          <p className="text-sm">
            Your administrator connected <strong>{s.display_name}</strong>
            {s.model ? <> (<code className="text-xs">{s.model}</code>)</> : null}. Summaries,
            finding extraction and chat all run through it — nothing for you to set up.
          </p>
        ) : (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm">
            <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warning" />
            <span>
              No AI provider is connected yet, so summaries and chat won't work.
              Ask an administrator to set one up under Settings → AI.
            </span>
          </div>
        )}
        <Nav onBack={onBack} onNext={onNext} nextLabel="Next" />
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------- admin */

function AdminConnect({ onNext, onBack }: { onNext: () => void; onBack: () => void }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["ai-settings"], queryFn: () => api<AISettings>("/settings/ai") });
  const [picked, setPicked] = useState<string | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [result, setResult] = useState<TestResult | null>(null);

  const providers = q.data?.providers ?? [];
  const current = providers.find((p) => p.name === picked) ?? null;
  const alreadyWorking = providers.find((p) => p.is_active && p.configured) ?? null;

  function choose(p: Provider) {
    setPicked(p.name);
    setApiKey(""); setResult(null);
    setModel(p.model || "");
    setBaseUrl(p.base_url ?? "");
  }

  // Save the provider's config, make it active, then probe it — so "Connect"
  // means "it actually answered", not "the form submitted".
  const connect = useMutation({
    mutationFn: async () => {
      if (!current) throw new Error("No provider selected");
      await api<AISettings>("/settings/ai", {
        method: "PUT",
        body: {
          provider: current.name,
          ...(apiKey ? { api_key: apiKey } : {}),
          ...(model ? { model } : {}),
          ...(current.self_hosted && baseUrl ? { base_url: baseUrl } : {}),
        },
      });
      await api("/settings/ai/active", { method: "PUT", body: { provider: current.name } });
      return api<TestResult>("/settings/ai/test", {
        method: "POST", body: { provider: current.name },
      });
    },
    onSuccess: (r) => {
      setResult(r);
      setApiKey("");
      qc.invalidateQueries({ queryKey: ["ai-settings"] });
      qc.invalidateQueries({ queryKey: ["ai-status"] });
    },
  });

  const discover = useMutation({
    mutationFn: () => api<LocalCandidate[]>("/settings/ai/local/discover"),
  });
  const found = (discover.data ?? []).filter((c) => c.reachable);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles size={14} className="text-primary" /> Connect an AI model
        </CardTitle>
      </CardHeader>
      <CardBody className="space-y-4">
        <p className="text-sm text-fgmuted">
          This powers report summaries, finding extraction, and chat. You can change
          it later under Settings → AI, and pin a different model per product or team.
        </p>

        {alreadyWorking && !result ? (
          <div className="flex items-center gap-2 text-sm rounded-md border border-success/40 bg-success/10 px-3 py-2">
            <Check size={15} className="text-success shrink-0" />
            <span>
              <strong>{alreadyWorking.display_name}</strong> is already connected. Pick
              another below to change it, or continue.
            </span>
          </div>
        ) : null}

        <div className="grid gap-2 sm:grid-cols-2">
          {providers.map((p) => (
            <button
              key={p.name}
              type="button"
              onClick={() => choose(p)}
              className={
                "text-left rounded-md border px-3 py-2 transition " +
                (picked === p.name
                  ? "border-primary bg-primary/5"
                  : "border-border hover:bg-muted/50")
              }
            >
              <div className="flex items-center gap-2 text-sm font-medium">
                {p.self_hosted ? <Server size={13} /> : <KeyRound size={13} />}
                {p.display_name}
                {p.configured ? <Badge tone="success">ready</Badge> : null}
                {p.is_active ? <Badge tone="muted">active</Badge> : null}
              </div>
              <p className="text-[11px] text-fgmuted mt-1">{BLURB[p.name] ?? ""}</p>
            </button>
          ))}
        </div>

        {current ? (
          <div className="space-y-3 border-t border-border pt-4">
            {current.self_hosted ? (
              <LocalFields
                baseUrl={baseUrl} setBaseUrl={setBaseUrl}
                model={model} setModel={setModel}
                found={found}
                scanning={discover.isPending}
                scanned={discover.isSuccess}
                onScan={() => discover.mutate()}
                onPick={(url, m) => { setBaseUrl(url); setModel(m); setResult(null); }}
              />
            ) : (
              <HostedFields
                provider={current}
                apiKey={apiKey} setApiKey={(v) => { setApiKey(v); setResult(null); }}
                model={model} setModel={setModel}
              />
            )}

            <div className="flex items-center gap-2 flex-wrap">
              <Button type="button" onClick={() => connect.mutate()} disabled={connect.isPending}>
                {connect.isPending
                  ? <><Loader2 size={14} className="animate-spin" /> Connecting…</>
                  : <>Connect {current.display_name}</>}
              </Button>
              {result?.ok ? (
                <span className="text-xs text-success inline-flex items-center gap-1">
                  <Check size={13} /> Connected — answered using {result.model}
                </span>
              ) : null}
            </div>

            {connect.isError ? (
              <p className="text-sm text-danger">{aiErrorText(connect.error, "Couldn't save.")}</p>
            ) : null}
            {result && !result.ok ? (
              <div className="flex items-start gap-2 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">
                <AlertCircle size={14} className="mt-0.5 shrink-0" />
                <span>
                  Saved, but the test call failed: {result.error}
                  <br />Fix it here or later under Settings → AI.
                </span>
              </div>
            ) : null}
          </div>
        ) : null}

        {!alreadyWorking && !result?.ok ? (
          <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
            <AlertTriangle size={14} className="mt-0.5 shrink-0 text-warning" />
            <span>
              You can skip this, but summaries, finding extraction and chat won't
              work until a provider is connected.
            </span>
          </div>
        ) : null}

        <Nav onBack={onBack} onNext={onNext}
             nextLabel={result?.ok || alreadyWorking ? "Next" : "Skip for now"} />
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ fields */

function HostedFields({
  provider, apiKey, setApiKey, model, setModel,
}: {
  provider: Provider;
  apiKey: string; setApiKey: (v: string) => void;
  model: string; setModel: (v: string) => void;
}) {
  const keyPage = KEY_PAGES[provider.name];
  return (
    <>
      <div>
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <Label htmlFor="apikey">API key</Label>
          {keyPage ? (
            <a href={keyPage} target="_blank" rel="noreferrer"
               className="text-xs text-primary hover:underline inline-flex items-center gap-1">
              Get a key from {provider.display_name} <ExternalLink size={11} />
            </a>
          ) : null}
        </div>
        <Input
          id="apikey" type="password" autoComplete="off" value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={provider.configured ? "leave blank to keep the current key" : "paste the key"}
        />
        <p className="text-[11px] text-fgmuted mt-1">
          Stored encrypted on this server. {provider.configured
            ? `A key is already set (${provider.hint ?? "…"}).`
            : "Opens in a new tab — create the key, then paste it here."}
        </p>
      </div>
      <div>
        <Label htmlFor="model">Model</Label>
        <Input id="model" value={model} onChange={(e) => setModel(e.target.value)}
               placeholder={provider.model || "model id"} />
      </div>
    </>
  );
}

function LocalFields({
  baseUrl, setBaseUrl, model, setModel, found, scanning, scanned, onScan, onPick,
}: {
  baseUrl: string; setBaseUrl: (v: string) => void;
  model: string; setModel: (v: string) => void;
  found: LocalCandidate[];
  scanning: boolean; scanned: boolean;
  onScan: () => void;
  onPick: (url: string, model: string) => void;
}) {
  return (
    <>
      <div className="rounded-md border border-border p-3 space-y-2">
        <div className="flex items-center gap-2 flex-wrap">
          <Button type="button" variant="secondary" onClick={onScan} disabled={scanning}>
            {scanning ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            {scanning ? "Scanning…" : "Scan this machine"}
          </Button>
          <span className="text-[11px] text-fgmuted">
            Checks the default ports for Ollama, LM Studio, vLLM and friends.
          </span>
        </div>
        {found.map((c) => (
          <div key={c.base_url} className="text-xs space-y-1">
            <div className="flex items-center gap-2">
              <Check size={12} className="text-success" />
              <strong>{c.label}</strong>
              <code className="text-fgmuted">{c.base_url}</code>
            </div>
            <div className="flex flex-wrap gap-1">
              {c.models.map((m) => (
                <Button key={m} type="button" size="sm" variant="ghost"
                        onClick={() => onPick(c.base_url, m)}>
                  use {m}
                </Button>
              ))}
            </div>
          </div>
        ))}
        {scanned && found.length === 0 ? (
          <p className="text-[11px] text-fgmuted">
            Nothing responded. Start your model server (e.g. <code>ollama serve</code>)
            and scan again, or enter the address manually below.
          </p>
        ) : null}
      </div>

      <div>
        <Label htmlFor="baseurl">Address</Label>
        <Input id="baseurl" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)}
               placeholder="http://localhost:11434  or  http://gpu-box.lan:11434" />
        <p className="text-[11px] text-fgmuted mt-1">
          On this machine, use <code>localhost</code> — the server rewrites it so it
          can reach your host from inside Docker. On another box, use its hostname
          or IP. <code>/v1</code> is added if you leave it off.
        </p>
      </div>
      <div>
        <Label htmlFor="localmodel">Model</Label>
        <Input id="localmodel" value={model} onChange={(e) => setModel(e.target.value)}
               placeholder="llama3.1:8b" />
        <p className="text-[11px] text-fgmuted mt-1">
          Most local servers need no API key. Smaller models are noticeably weaker at
          the structured extraction the findings pipeline relies on.
        </p>
      </div>
    </>
  );
}

function Nav({ onBack, onNext, nextLabel }: {
  onBack: () => void; onNext: () => void; nextLabel: string;
}) {
  return (
    <div className="flex items-center justify-between pt-2">
      <Button variant="ghost" onClick={onBack}><ArrowLeft size={14} /> Back</Button>
      <Button variant="secondary" onClick={onNext}>{nextLabel} <ArrowRight size={14} /></Button>
    </div>
  );
}
